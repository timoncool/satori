#!/usr/bin/env python3
"""satori nudge hook. Events: prompt-submit | stop | session-end.

Молчит по умолчанию. Голос: коррекция юзера в текущем сообщении (мгновенно)
или накопленная работа с последнего reflect (>=SN_NUDGE_MIN_CALLS tool calls).
Отказ значит отказ: после nudge молчим, пока не накопится следующий полный порог.
session-end: тихий capture без модели (напрямую в server.py).
"""

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path.home() / ".claude" / "satori"
STATE_PATH = ROOT / "nudge_state.json"
DB_PATH = ROOT / "state.db"

MIN_CALLS = int(os.environ.get("SN_NUDGE_MIN_CALLS", "25"))
COOLDOWN_S = int(os.environ.get("SN_NUDGE_COOLDOWN_MIN", "10")) * 60
CORR_COOLDOWN_S = int(os.environ.get("SN_CORR_COOLDOWN_MIN", "3")) * 60
STOP_NUDGE = os.environ.get("SN_STOP_NUDGE", "1") == "1"
TAIL_CAP = 4 * 1024 * 1024

CORRECTION_RX = re.compile(
    r"(?:я же говорил|я просил|опять|снова ты|не так|неправильно|не то\b|хватит|"
    r"зачем ты|кто тебя просил|откати|верни как было|ты дурак|долбо|заеб|бесит|"
    r"нахуя|нихуя|хуйн|блять|блядь|сука|пиздец|мудак|идиот|stop doing|i asked you|"
    r"you keep|wrong again|not what i)", re.IGNORECASE)


def emit(obj: dict) -> None:
    # stdout на Windows по умолчанию cp1251 — хук-протокол ждёт UTF-8
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()


def norm_path(p: str) -> str:
    # Git-Bash mount /c/Users/... -> C:\Users\...
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    return f"{m.group(1).upper()}:\\{m.group(2).replace('/', chr(92))}" if m else p


def state_load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def state_save(st: dict) -> None:
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(st), encoding="utf-8")
    except Exception:
        pass


def reflect_offset(tp: str) -> int:
    if not DB_PATH.is_file():
        return 0
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (f"offset:{tp}",)).fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def pending_calls(tp: str, offset: int) -> int:
    p = Path(tp)
    if not p.is_file():
        return 0
    size = p.stat().st_size
    if size <= offset:
        return 0
    with p.open("rb") as f:
        f.seek(max(offset, size - TAIL_CAP) if size - offset > TAIL_CAP else offset)
        tail = f.read().decode("utf-8", errors="replace")
    return tail.count('"type":"tool_use"') + tail.count('"type": "tool_use"')


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else "prompt-submit"
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except Exception:
        data = {}
    tp = norm_path(data.get("transcript_path", "") or "")

    if event == "session-end":
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import server  # noqa: PLC0415
            server.reflect(tp)
        except Exception:
            pass
        return

    st = state_load()
    now = time.time()
    calls = pending_calls(tp, reflect_offset(tp))

    # Мгновенный nudge на коррекцию (порог 1, с дедупом серии)
    if event == "prompt-submit":
        prompt = data.get("prompt", "") or ""
        if CORRECTION_RX.search(prompt) and now - st.get("corr_ts", 0) > CORR_COOLDOWN_S:
            st["corr_ts"] = now
            state_save(st)
            emit({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    "⛩ satori-хук: в сообщении юзера коррекция. ПЕРВОЙ строкой видимого "
                    "ответа покажи маркер «⛩ satori: поймал коррекцию → после фикса "
                    "зарефлексирую». Почини по существу, затем вызови mcp__satori__reflect "
                    "и ЯВНО 1-2 строками скажи юзеру, что записалось в кандидаты / что "
                    "ушло в драфт / что скипнуто и почему.")}})
            return

    # Nudge по наработке: отказ значит отказ ( re-nudge только после нового полного порога)
    nudged_at_calls = st.get("nudged_at_calls", 0)
    workload = (calls >= MIN_CALLS and calls >= nudged_at_calls + MIN_CALLS
                and now - st.get("nudge_ts", 0) > COOLDOWN_S)
    if not workload:
        return
    msg = (f"⛩ satori-хук: накопилось ~{calls} tool calls. ПЕРВОЙ строкой видимого "
           f"ответа покажи юзеру маркер «⛩ satori: {calls} calls → reflect», вызови "
           f"mcp__satori__reflect и ЯВНО скажи, что записалось/скипнуто и почему — "
           f"вызови mcp__satori__reflect (или проигнорируй, напомню после "
           f"следующих {MIN_CALLS}).")
    st.update(nudge_ts=now, nudged_at_calls=calls)
    state_save(st)
    if event == "prompt-submit":
        emit({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": msg}})
    elif event == "stop" and STOP_NUDGE and not data.get("stop_hook_active"):
        emit({"decision": "block", "reason": msg + " Потом завершай ход."})


if __name__ == "__main__":
    main()
