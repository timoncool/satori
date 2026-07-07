#!/usr/bin/env python3
"""satori — self-learning loop MCP for Claude Code (Windows-native).

4-stage loop: capture -> decide -> distill -> curate.
The MCP does deterministic mechanics (parsing, counters, storage, validation);
the calling model in-session does the thinking. No background `claude -p` calls.

Storage: ~/.claude/satori/{state.db, staging/, backups/}
"""

import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

from fastmcp import FastMCP

HOME = Path.home()
ROOT = HOME / ".claude" / "satori"
STAGING = ROOT / "staging"
BACKUPS = ROOT / "backups"
DB_PATH = ROOT / "state.db"
PROJECTS = HOME / ".claude" / "projects"
USER_SKILLS = HOME / ".claude" / "skills"

# Полный автомат: драфт активируется сразу (0 = ручной staging-гейт для консерваторов)
AUTO_APPROVE = os.environ.get("SATORI_AUTO_APPROVE", "1") == "1"
# Пороги (env-переопределяемые)
SEGMENT_TOOL_CALLS = int(os.environ.get("SN_SEGMENT_TOOL_CALLS", "12"))
SEGMENT_FILE_EDITS = int(os.environ.get("SN_SEGMENT_FILE_EDITS", "2"))
PROMOTE_AT = int(os.environ.get("SN_PROMOTE_AT", "2"))
# Коррекция юзера = самый ценный сигнал, не ждёт повторов (механика Devin)
CORRECTION_PROMOTE_AT = int(os.environ.get("SN_CORRECTION_PROMOTE_AT", "1"))
STALE_DAYS = int(os.environ.get("SN_STALE_DAYS", "30"))
ARCHIVE_DAYS = int(os.environ.get("SN_ARCHIVE_DAYS", "90"))
MAX_DRAFT_BYTES = 64 * 1024
FTS_MAX_FILE_MB = 40

CORRECTION_RX = re.compile(
    r"(?:я же говорил|я просил|опять|снова ты|не так|неправильно|не то\b|хватит|"
    r"зачем ты|кто тебя просил|откати|верни как было|ты дурак|долбо|заеб|бесит|"
    r"нахуя|нихуя|хуйн|блять|блядь|сука|пиздец|мудак|идиот|stop doing|i asked you|"
    r"you keep|wrong again|not what i)", re.IGNORECASE)
FAILURE_RX = re.compile(
    r"(?:is_error\"?\s*:\s*true|exit code [1-9]|command not found|Traceback \(most|"
    r"FAILED|error TS\d+|SyntaxError|ModuleNotFoundError|ENOENT|EACCES|"
    r"fatal:|npm ERR!|Build failed|тесты? упал)", re.IGNORECASE)
SECRET_RX = re.compile(
    r"(?i)((?:api[_-]?key|token|password|passwd|secret|access[_-]?key)"
    r"['\"]?\s*[:=]\s*['\"]?)([^\s'\",;]{8,})|"
    r"((?:bearer|authorization)['\"]?\s*[:=]?\s+)([^\s'\",;]{12,})|"
    r"\b(glpat-[\w-]{15,}|sk-[\w-]{20,}|hf_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|npm_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{30,}|"
    r"xox[bp]-[\w-]{20,}|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
    r"[A-Za-z0-9_-]{10,})\b|"
    r"([a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@)|"  # user:pass@ в URL
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)")
INJECTION_RX = re.compile(
    r"(?i)(?:(?:ignore|disregard|forget)\s+(?:all\s+|everything\s+)?"
    r"(?:previous|prior|above|the\s+above|your)\s*[\w\s]*"
    r"(?:instructions?|rules?|prompts?)?|"
    r"(?:from\s+now\s+on|starting\s+now)[\w\s,]*(?:you\s+(?:are|will|must)|"
    r"your\s+new)|you\s+are\s+now\s+(?:a|an|in)\b|new\s+(?:system\s+)?"
    r"(?:prompt|instructions?|rules?)|<\s*system\s*>|BEGIN\s+SYSTEM|"
    r"(?:do\s+not|don't|never)\s+(?:tell|inform|mention|reveal|disclose)"
    r"[\w\s]*(?:the\s+)?(?:user|this)|"
    r"(?:игнорир|забудь|отмени|проигнорир)[а-я]*\s+(?:все\s+)?"
    r"(?:предыдущ|прошл|вышеуказан|эти)[а-я]*\s*(?:инструкц|правил|указан)?|"
    r"отныне[\w\s,а-я]*(?:ты|вы)\s+(?:работа|действу|будешь)|"
    r"(?:не\s+)?(?:говори|сообщай|рассказыв)[а-я]*\s+(?:об\s+этом\s+)?"
    r"(?:юзеру|пользовател)|\boverride\s+(?:safety|security|all)\b)")
# Ключ урока = нормализованная «сущность» сигнала: путь файла / имя команды / первые слова
KEY_STOP = re.compile(r"[^a-z0-9а-яё_./-]+", re.IGNORECASE)

mcp = FastMCP(
    "satori",
    instructions=(
        "FULLY AUTOMATIC self-learning loop (capture->decide->distill->curate). "
        "CALL `reflect` SEVERAL TIMES PER SESSION without asking the user: after any "
        "substantial task (~10+ tool calls), after the user corrects you, before the "
        "session ends. reflect returns lesson candidates — YOU judge each AND ACT, "
        "never ask permission: noise -> `skip_lesson`; worth keeping -> `submit_draft` "
        "(prefer patching the similar existing skill reflect points to). In auto mode "
        "(default) the draft ACTIVATES IMMEDIATELY — validated, backed up, reversible "
        "via `retire_skill`. The user's control is VISIBILITY, not pre-approval: after "
        "every loop event report in 1-2 plain chat lines starting with ⛩ — what fired, "
        "why, what was learned/activated/skipped, and that `retire_skill` undoes it. "
        "Use `session_search` to recall how past sessions solved something; "
        "`loop_status` for telemetry. Never put secrets in drafts. Draft descriptions "
        "must be PUSHY with concrete trigger phrases: 'Use when X, Y, or the user "
        "mentions Z, even if they don't explicitly ask.'"))


def db() -> sqlite3.Connection:
    ROOT.mkdir(parents=True, exist_ok=True)
    STAGING.mkdir(exist_ok=True)
    BACKUPS.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS lessons(
      key TEXT PRIMARY KEY, kind TEXT, excerpt TEXT, seen_count INTEGER DEFAULT 1,
      status TEXT DEFAULT 'candidate', first_ts REAL, last_ts REAL, note TEXT);
    CREATE TABLE IF NOT EXISTS skill_usage(
      name TEXT PRIMARY KEY, use_count INTEGER DEFAULT 0, last_used REAL, staged_ts REAL);
    CREATE VIRTUAL TABLE IF NOT EXISTS transcripts USING fts5(
      content, session UNINDEXED, ts UNINDEXED);
    CREATE TABLE IF NOT EXISTS indexed_files(path TEXT PRIMARY KEY, offset INTEGER);
    """)
    try:
        conn.execute("ALTER TABLE skill_usage ADD COLUMN pinned_project TEXT")
    except sqlite3.OperationalError:
        pass
    return conn


def redact(text: str) -> str:
    def sub(m: re.Match) -> str:
        # группы 1 и 3 — префиксы key=/bearer, оставляем их, режем значение
        for g in (1, 3):
            if m.group(g):
                return m.group(g) + "[REDACTED]"
        return "[REDACTED]"
    return SECRET_RX.sub(sub, text)


def defang(text: str) -> str:
    """Обезвредить injection-маркеры в тексте, который вернётся модели как данные."""
    return INJECTION_RX.sub("[injection-marker removed]", text)


def lesson_key(kind: str, material: str) -> str:
    material = redact(material)  # секрет не должен попасть в первичный ключ
    material = KEY_STOP.sub("-", material.strip().lower())[:80].strip("-")
    return f"{kind}:{material}" if material else ""


def find_transcript(transcript_path: str) -> Path | None:
    if transcript_path:
        p = Path(transcript_path)
        return p if p.is_file() else None
    fresh, best = None, 0.0
    for f in PROJECTS.glob("*/*.jsonl"):
        m = f.stat().st_mtime
        if m > best:
            fresh, best = f, m
    if fresh and time.time() - best < 3600:
        return fresh
    return None


def parse_new_lines(conn: sqlite3.Connection, path: Path) -> tuple[list, dict]:
    """Читает транскрипт с сохранённого offset'а. Возвращает (сигналы, статистика сегмента)."""
    off_key = f"offset:{path}"
    row = conn.execute("SELECT value FROM meta WHERE key=?", (off_key,)).fetchone()
    offset = int(row[0]) if row else 0
    size = path.stat().st_size
    if size < offset:  # файл пересоздан
        offset = 0
    signals, stats = [], {"tool_calls": 0, "file_edits": 0, "user_msgs": 0, "skills_seen": set()}
    prev_failed = None
    with path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            blocks = content if isinstance(content, list) else []
            if rec.get("type") == "user" and isinstance(content, str):
                stats["user_msgs"] += 1
                if CORRECTION_RX.search(content):
                    signals.append(("correction", content[:300]))
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    stats["tool_calls"] += 1
                    name = b.get("name", "")
                    inp = b.get("input") or {}
                    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                        stats["file_edits"] += 1
                    if name == "Skill":
                        stats["skills_seen"].add(str(inp.get("skill", "")))
                    tgt = str(inp.get("file_path") or inp.get("command", "")[:60])
                    # починка после падения: следующий tool_use трогает тот же locus
                    if prev_failed and prev_failed != "generic" and prev_failed in tgt:
                        signals.append(("fix_after_fail", f"{name} {tgt}"[:300]))
                    prev_failed = None
                if b.get("type") == "tool_result":
                    txt = json.dumps(b.get("content", ""))[:2000]
                    if b.get("is_error") or FAILURE_RX.search(txt):
                        m = re.search(r"[\w./\\-]+\.(?:py|ts|tsx|js|php|md|json|scss)", txt)
                        locus = m.group(0) if m else "generic"
                        signals.append(("failure", f"{locus}: {txt[:220]}"))
                        prev_failed = locus
        new_offset = f.tell()
    conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (off_key, str(new_offset)))
    return signals, stats


def skill_bases(pinned: str | None = None) -> list[Path]:
    """Все места, где живут скиллы — для поиска дублей и ретайра."""
    bases = [USER_SKILLS, STAGING]
    if pinned:
        bases.insert(0, Path(pinned) / ".claude" / "skills")
    for extra in (Path("D:/Projects/claude-skills"),):  # локальная библиотека юзера
        if extra.is_dir():
            bases.append(extra)
    return [b for b in dict.fromkeys(bases)]


def similar_skills(query: str, limit: int = 3) -> list[dict]:
    """Ищет похожие скиллы по имени/описанию во всех местах, где они живут."""
    out, q = [], set(re.findall(r"[a-zа-яё0-9]{4,}", query.lower()))
    for base in skill_bases():
        if not base.is_dir():
            continue
        for sk in base.glob("*/SKILL.md"):
            head = sk.read_text(encoding="utf-8", errors="replace")[:600].lower()
            score = sum(1 for w in q if w in head)
            if score:
                out.append({"name": sk.parent.name, "path": str(sk), "score": score,
                            "staged": base == STAGING})
    return sorted(out, key=lambda x: -x["score"])[:limit]


def _archive_skill(name: str, pinned: str | None, tag: str) -> list[str]:
    """Перенести живой скилл + staging-копию в архив лупа (обратимо). Возвращает что убрал."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    (ROOT / "archive").mkdir(exist_ok=True)
    moved = []
    seen: set[str] = set()
    for base in ([Path(pinned) / ".claude" / "skills"] if pinned else []) + [USER_SKILLS, STAGING]:
        d = base / name
        if str(d) in seen or not d.is_dir():
            continue
        seen.add(str(d))
        dst = ROOT / "archive" / f"{name}-{tag}-{ts}"
        while dst.exists():
            dst = ROOT / "archive" / f"{name}-{tag}-{ts}-{len(moved)}"
        shutil.move(str(d), str(dst))
        moved.append(str(d))
    return moved


@mcp.tool()
def reflect(transcript_path: str = "") -> dict:
    """Stage 1+2+4 of the loop: capture new signals from the session transcript,
    aggregate them into lesson candidates (patch-not-append), tick the curator.
    Call several times per session. Returns candidates for YOU to judge:
    skip_lesson() the worthless, submit_draft() the worthy."""
    conn = db()
    try:
        path = find_transcript(transcript_path)
        if not path:
            return {"error": "no fresh transcript found; pass transcript_path explicitly"}
        # эксклюзивная транзакция: параллельный reflect (in-session + SessionEnd-хук)
        # не прочитает тот же offset и не задвоит seen_count
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return {"error": "satori busy (another reflect running) — retry in a moment"}
        signals, stats = parse_new_lines(conn, path)
        now = time.time()
        for kind, excerpt in signals:
            key = lesson_key(kind, excerpt)
            if not key:
                continue
            conn.execute("""INSERT INTO lessons(key,kind,excerpt,first_ts,last_ts)
                VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                seen_count=seen_count+1, last_ts=?, excerpt=excerpt""",
                (key, kind, redact(excerpt), now, now, now))
        if (stats["tool_calls"] >= SEGMENT_TOOL_CALLS
                and stats["file_edits"] >= SEGMENT_FILE_EDITS):
            key = f"segment:{path.stem[:12]}:{int(now // 3600)}"
            conn.execute("""INSERT OR IGNORE INTO lessons(key,kind,excerpt,first_ts,last_ts,
                seen_count) VALUES(?,?,?,?,?,?)""",
                (key, "complex_segment",
                 f"{stats['tool_calls']} tool calls, {stats['file_edits']} edits",
                 now, now, PROMOTE_AT))
        for name in stats["skills_seen"]:
            if name:
                conn.execute("""INSERT INTO skill_usage(name,use_count,last_used)
                    VALUES(?,1,?) ON CONFLICT(name) DO UPDATE SET
                    use_count=use_count+1, last_used=?""", (name, now, now))
        # Куратор: stale в 30д, архив в 90д; проверенные (use_count>=3) стареют вдвое медленнее.
        # Ретайрит ЖИВОЙ скилл (не только staging-копию) — иначе автоактивированные не стареют.
        stale, archived = [], []
        for nm, uc, lu, sts, pin in conn.execute(
                """SELECT name,use_count,last_used,staged_ts,pinned_project FROM skill_usage
                   WHERE staged_ts IS NOT NULL"""):
            live = any((b / nm / "SKILL.md").is_file() for b in skill_bases(pin))
            if not live:
                continue
            mult = 2 if (uc or 0) >= 3 else 1
            idle_d = (now - max(lu or 0, sts or 0)) / 86400
            if idle_d > ARCHIVE_DAYS * mult:
                _archive_skill(nm, pin, "stale")
                conn.execute("UPDATE skill_usage SET staged_ts=NULL WHERE name=?", (nm,))
                archived.append(nm)
            elif idle_d > STALE_DAYS * mult:
                stale.append(nm)
        candidates = []
        for key, kind, excerpt, cnt in conn.execute(
                """SELECT key,kind,excerpt,seen_count FROM lessons
                   WHERE status='candidate' AND (seen_count>=?
                     OR (kind='correction' AND seen_count>=?))
                   ORDER BY (kind='correction') DESC, last_ts DESC LIMIT 8""",
                (PROMOTE_AT, CORRECTION_PROMOTE_AT)):
            # excerpt возвращается модели как ДАННЫЕ — обезвредить injection-маркеры
            candidates.append({"key": key, "kind": kind, "seen_count": cnt,
                               "excerpt": defang(excerpt),
                               "similar_skills": similar_skills(excerpt)})
        conn.commit()
        return {
            "new_signals": len(signals),
            "segment": {k: (sorted(v) if isinstance(v, set) else v) for k, v in stats.items()},
            "lesson_candidates": candidates,
            "stale_staged_skills": stale,
            "archived_staged_skills": archived,
            "next": ("Judge each candidate: recurring & generalizable & not already in a "
                     "skill/recipe/memory -> submit_draft (patch similar_skills[0] if any); "
                     "one-off noise -> skip_lesson. User corrections surface after a SINGLE "
                     "occurrence — highest-value signal. Draft description MUST start with "
                     "'Use when ...' (recall trigger). Age alone is not evidence.")
            if candidates else "nothing to distill yet",
        }
    finally:
        conn.close()


@mcp.tool()
def skip_lesson(key: str, reason: str) -> dict:
    """SKIP gate: mark a lesson candidate as not skill-worthy. Remembered forever —
    the same pattern will not be suggested again."""
    conn = db()
    try:
        n = conn.execute("UPDATE lessons SET status='skipped', note=? WHERE key=?",
                         (reason[:300], key)).rowcount
        conn.commit()
        return {"skipped": bool(n), "key": key}
    finally:
        conn.close()


@mcp.tool()
def submit_draft(name: str, markdown: str, lesson_key: str = "", patches: str = "",
                 pinned_project: str = "") -> dict:
    """Stage 3: store a distilled SKILL.md draft into staging (NOT active skills).
    Validates frontmatter ('description: Use when ...' — the recall trigger is sacred),
    size, secrets, prompt-injection markers; backs up overwrites, stamps provenance.
    `patches` = existing skill this improves. `pinned_project` = absolute project path
    if the lesson is project-specific (approve routes it to <project>/.claude/skills)."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,60}", name):
        return {"error": "name must be kebab-case: [a-z0-9-], 3-61 chars"}
    if len(markdown.encode("utf-8")) > MAX_DRAFT_BYTES:
        return {"error": f"draft exceeds {MAX_DRAFT_BYTES} bytes"}
    if not re.match(r"^---\s*\nname:", markdown):
        return {"error": "draft must start with YAML frontmatter: ---\\nname: ...\\ndescription: ..."}
    fm = markdown.split("---", 2)[1]
    if not re.search(r"description:\s*[\"']?Use when ", fm):
        return {"error": "description must start with 'Use when ...' — it is the recall "
                         "trigger; state WHEN to recall this (not what it does) and be "
                         "pushy: list concrete phrases/situations, skills undertrigger"}
    fm_name = re.search(r"^name:\s*[\"']?([^\"'\n]+)", fm, re.MULTILINE)
    if not fm_name or fm_name.group(1).strip() != name:
        return {"error": f"frontmatter name: must equal the name arg '{name}' — "
                         "Claude Code resolves skills by frontmatter, a mismatch = silent no-trigger"}
    if SECRET_RX.search(markdown):
        return {"error": "draft contains something that looks like a secret — redact it"}
    if INJECTION_RX.search(markdown):
        return {"error": "draft contains prompt-injection markers — a skill is instructions "
                         "to an agent; rephrase without override/conceal language"}
    if pinned_project and not Path(pinned_project).is_dir():
        return {"error": f"pinned_project is not an existing directory: {pinned_project}"}
    conn = db()
    try:
        dest = STAGING / name / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.copy2(dest, BACKUPS / f"{name}.{int(time.time())}.md")
        stamp = (f"\n<!-- satori: staged {time.strftime('%Y-%m-%d')}"
                 + (f", patches '{patches}'" if patches else "")
                 + (f", lesson '{lesson_key}'" if lesson_key else "")
                 + (f", pinned '{pinned_project}'" if pinned_project else "") + " -->\n")
        dest.write_text(markdown.rstrip() + stamp, encoding="utf-8")
        now = time.time()
        conn.execute("""INSERT INTO skill_usage(name,staged_ts,pinned_project) VALUES(?,?,?)
            ON CONFLICT(name) DO UPDATE SET staged_ts=?, pinned_project=?""",
            (name, now, pinned_project or None, now, pinned_project or None))
        if lesson_key:
            conn.execute("UPDATE lessons SET status='staged' WHERE key=?", (lesson_key,))
        conn.commit()
        if not AUTO_APPROVE:
            return {"staged": str(dest), "pinned_project": pinned_project or "global",
                    "note": "Manual mode: inactive until approve_draft."}
    finally:
        conn.close()
    # Полный автомат: активируем сразу (обратимо через retire_skill)
    base = Path(pinned_project) / ".claude" / "skills" if pinned_project else USER_SKILLS
    live = base / name / "SKILL.md"
    # Provenance-guard: не перезаписывать чужой рукописный скилл (без satori-штампа) авто-магией
    if live.exists() and "<!-- satori:" not in live.read_text(encoding="utf-8", errors="replace"):
        return {"staged": str(dest), "pinned_project": pinned_project or "global",
                "note": (f"A hand-written skill '{name}' already exists and was NOT overwritten. "
                         "Draft kept in staging — approve_draft to override deliberately, or "
                         "rename the draft.")}
    live.parent.mkdir(parents=True, exist_ok=True)
    if live.exists():
        shutil.copy2(live, BACKUPS / f"{name}.pre-activate.{int(time.time())}.md")
    shutil.copy2(dest, live)
    conn = db()
    try:
        if lesson_key:
            conn.execute("UPDATE lessons SET status='promoted' WHERE key=?", (lesson_key,))
        conn.commit()
    finally:
        conn.close()
    return {"activated": str(live), "staged_copy": str(dest),
            "note": ("AUTO-ACTIVATED — loads next session/restart. Announce it to the "
                     "user with a ⛩ line; `retire_skill` reverts in one call.")}


@mcp.tool()
def approve_draft(name: str, dest_dir: str = "") -> dict:
    """Gate: promote a staged draft into active skills (default ~/.claude/skills/<name>/).
    Meant to be called on explicit user approval or by the wake skill."""
    src = STAGING / name / "SKILL.md"
    if not src.is_file():
        return {"error": f"no staged draft '{name}'"}
    conn0 = db()
    pinned = conn0.execute("SELECT pinned_project FROM skill_usage WHERE name=?",
                           (name,)).fetchone()
    conn0.close()
    if dest_dir:
        if not Path(dest_dir).is_dir():
            return {"error": f"dest_dir is not an existing directory: {dest_dir}"}
        base = Path(dest_dir)
    elif pinned and pinned[0]:
        base = Path(pinned[0]) / ".claude" / "skills"
    else:
        base = USER_SKILLS
    dest = base / name / "SKILL.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.copy2(dest, BACKUPS / f"{name}.pre-approve.{int(time.time())}.md")
    shutil.copy2(src, dest)
    conn = db()
    try:
        conn.execute("UPDATE lessons SET status='promoted' WHERE status='staged' AND note IS NULL AND key LIKE ?",
                     (f"%{name}%",))
        conn.commit()
    finally:
        conn.close()
    return {"promoted": str(dest), "backup_dir": str(BACKUPS),
            "note": "Restart Claude Code (or start a new session) to load the skill."}


@mcp.tool()
def retire_skill(name: str) -> dict:
    """Instant undo for an auto-activated skill: moves the live skill dir and its
    staging copy into satori's archive (recoverable), restores a pre-activation
    backup if one existed."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,60}", name):
        return {"error": "invalid skill name"}
    conn = db()
    try:
        row = conn.execute("SELECT pinned_project FROM skill_usage WHERE name=?",
                           (name,)).fetchone()
    finally:
        conn.close()
    pinned = row[0] if row and row[0] else None
    # убрать ЖИВОЙ скилл (все базы) + staging-копию в архив
    moved = _archive_skill(name, pinned, "retired")
    # восстановить чужой рукописный скилл, если satori перезаписал его при активации
    restored = None
    backups = sorted(BACKUPS.glob(f"{name}.pre-activate.*.md"))
    if backups and "<!-- satori:" not in backups[-1].read_text(encoding="utf-8", errors="replace"):
        base = (Path(pinned) / ".claude" / "skills") if pinned else USER_SKILLS
        d = base / name / "SKILL.md"
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backups[-1], d)
        restored = str(d)
    conn = db()
    try:
        conn.execute("UPDATE skill_usage SET staged_ts=NULL WHERE name=?", (name,))
        # точный матч связки урок↔скилл (LIKE '%name%' задевал чужие уроки)
        conn.execute("UPDATE lessons SET status='skipped', note='retired by user' "
                     "WHERE status IN ('staged','promoted') AND key IN "
                     "(SELECT key FROM lessons WHERE excerpt LIKE ?)", (f"%{name}%",))
        conn.commit()
    finally:
        conn.close()
    if not moved:
        return {"error": f"nothing found to retire for '{name}'"}
    return {"retired": moved, "restored_handwritten": restored,
            "archive": str(ROOT / "archive"),
            "note": "Recoverable from archive; lesson marked skipped, won't resurface."}


@mcp.tool()
def session_search(query: str, limit: int = 8) -> dict:
    """FTS5 keyword search over past session transcripts ('when did I solve exactly
    this error'). Lazily indexes new/grown .jsonl files on each call."""
    conn = db()
    try:
        indexed = 0
        for f in PROJECTS.glob("*/*.jsonl"):
            if f.stat().st_size > FTS_MAX_FILE_MB * 1024 * 1024:
                continue
            row = conn.execute("SELECT offset FROM indexed_files WHERE path=?",
                               (str(f),)).fetchone()
            offset = row[0] if row else 0
            size = f.stat().st_size
            if size <= offset:
                continue
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset if offset <= size else 0)
                chunk, ts = [], f.stat().st_mtime
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    c = (rec.get("message") or {}).get("content")
                    if isinstance(c, str) and len(c) > 40:
                        chunk.append(c[:1500])
                    elif isinstance(c, list):  # ответы ассистента: текст + результаты тулзов
                        for b in c:
                            if not isinstance(b, dict):
                                continue
                            t = b.get("text") if b.get("type") == "text" else None
                            if b.get("type") == "tool_result":
                                t = json.dumps(b.get("content", ""))[:1500]
                            if t and len(t) > 40:
                                chunk.append(t[:1500])
                if chunk:
                    conn.execute("INSERT INTO transcripts(content,session,ts) VALUES(?,?,?)",
                                 (redact("\n".join(chunk))[:200000], f.stem, ts))
                    indexed += 1
            conn.execute("INSERT OR REPLACE INTO indexed_files VALUES(?,?)", (str(f), size))
        conn.commit()
        # каждый токен в кавычки — FTS5 трактует его как литерал, иначе . / - = синтаксис/операторы
        toks = re.findall(r"[\w./-]{2,}", query)
        safe_q = " ".join('"' + t.replace('"', '') + '"' for t in toks) or '"' + query.replace('"', '') + '"'
        try:
            rows = conn.execute(
                """SELECT session, ts, snippet(transcripts, 0, '>>', '<<', ' … ', 24)
                   FROM transcripts WHERE transcripts MATCH ? ORDER BY rank LIMIT ?""",
                (safe_q, limit)).fetchall()
        except sqlite3.OperationalError as e:
            return {"indexed_now": indexed, "error": f"FTS query failed: {e}", "hits": []}
        return {"indexed_now": indexed,
                "hits": [{"session": s, "date": time.strftime("%Y-%m-%d", time.localtime(t)),
                          "snippet": sn} for s, t, sn in rows]}
    finally:
        conn.close()


@mcp.tool()
def loop_status() -> dict:
    """Telemetry: lesson counts by status, top candidates, staged drafts, stale skills."""
    conn = db()
    try:
        by_status = dict(conn.execute(
            "SELECT status, COUNT(*) FROM lessons GROUP BY status").fetchall())
        top = [{"key": k, "seen": c, "kind": kd} for k, c, kd in conn.execute(
            """SELECT key,seen_count,kind FROM lessons WHERE status='candidate'
               ORDER BY seen_count DESC, last_ts DESC LIMIT 10""")]
        staged = sorted(p.parent.name for p in STAGING.glob("*/SKILL.md"))
        usage = [{"name": n, "uses": u} for n, u in conn.execute(
            "SELECT name,use_count FROM skill_usage ORDER BY use_count DESC LIMIT 10")]
        return {"lessons_by_status": by_status, "top_candidates": top,
                "staged_drafts": staged, "skill_usage_top": usage,
                "thresholds": {"promote_at": PROMOTE_AT,
                               "correction_promote_at": CORRECTION_PROMOTE_AT,
                               "segment_tool_calls": SEGMENT_TOOL_CALLS,
                               "stale_days": STALE_DAYS, "archive_days": ARCHIVE_DAYS}}
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
