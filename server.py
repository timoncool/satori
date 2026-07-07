#!/usr/bin/env python3
"""second-nature — self-learning loop MCP for Claude Code (Windows-native).

4-stage loop: capture -> decide -> distill -> curate.
The MCP does deterministic mechanics (parsing, counters, storage, validation);
the calling model in-session does the thinking. No background `claude -p` calls.

Storage: ~/.claude/second-nature/{state.db, staging/, backups/}
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
ROOT = HOME / ".claude" / "second-nature"
STAGING = ROOT / "staging"
BACKUPS = ROOT / "backups"
DB_PATH = ROOT / "state.db"
PROJECTS = HOME / ".claude" / "projects"
USER_SKILLS = HOME / ".claude" / "skills"

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
    r"(?i)((?:api[_-]?key|token|password|passwd|secret|bearer|authorization)"
    r"['\"]?\s*[:=]\s*['\"]?)([^\s'\",;]{8,})|"
    r"\b(glpat-[\w-]{15,}|sk-[\w-]{20,}|hf_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|"
    r"xox[bp]-[\w-]{20,}|AKIA[A-Z0-9]{16})\b")
INJECTION_RX = re.compile(
    r"(?i)(?:ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions|"
    r"disregard\s+(?:your|the|all)[\w\s]*(?:instructions|rules)|"
    r"you\s+are\s+now\s+(?:a|an|in)\b|new\s+system\s+prompt|<\s*system\s*>|"
    r"do\s+not\s+(?:tell|inform|mention\s+to)\s+the\s+user|"
    r"(?:игнорируй|забудь)\s+(?:все\s+)?(?:предыдущие|прошлые)\s+(?:инструкции|правила)|"
    r"не\s+говори\s+(?:об\s+этом\s+)?(?:юзеру|пользователю)|"
    r"BEGIN\s+SYSTEM|\boverride\s+(?:safety|security|all)\b)")
# Ключ урока = нормализованная «сущность» сигнала: путь файла / имя команды / первые слова
KEY_STOP = re.compile(r"[^a-z0-9а-яё_./-]+", re.IGNORECASE)

mcp = FastMCP(
    "second-nature",
    instructions=(
        "Self-learning loop (4 stages: capture->decide->distill->curate). "
        "CALL `reflect` SEVERAL TIMES PER SESSION: after finishing any substantial task "
        "(~10+ tool calls), after the user corrects you, and before the session ends. "
        "reflect returns lesson candidates — YOU judge each: worthless -> `skip_lesson`; "
        "worth keeping -> write a SKILL.md draft and `submit_draft` (prefer patching the "
        "similar existing skill reflect points to, over creating new). Drafts land in "
        "staging, NEVER auto-activate: the user (or dream/wake) calls `approve_draft`. "
        "Use `session_search` to recall how past sessions solved something. "
        "`loop_status` shows telemetry. Never put secrets in drafts. "
        "Draft descriptions must be PUSHY with concrete trigger phrases (Claude "
        "undertriggers skills — official skill-creator guidance): 'Use when X, Y, "
        "or the user mentions Z, even if they don't explicitly ask.'"))


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
        if m.group(1):
            return m.group(1) + "[REDACTED]"
        return "[REDACTED]"
    return SECRET_RX.sub(sub, text)


def lesson_key(kind: str, material: str) -> str:
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
                    tgt = inp.get("file_path") or inp.get("command", "")[:60]
                    if prev_failed and prev_failed == (name, str(tgt)):
                        signals.append(("fix_after_fail", f"{name} {tgt}"[:300]))
                    prev_failed = None
                if b.get("type") == "tool_result":
                    txt = json.dumps(b.get("content", ""))[:2000]
                    if b.get("is_error") or FAILURE_RX.search(txt):
                        m = re.search(r"[\w./\\-]+\.(?:py|ts|tsx|js|php|md|json|scss)", txt)
                        locus = m.group(0) if m else "generic"
                        signals.append(("failure", f"{locus}: {txt[:220]}"))
                        prev_failed = ("_last", locus)
        new_offset = f.tell()
    conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (off_key, str(new_offset)))
    return signals, stats


def similar_skills(query: str, limit: int = 3) -> list[dict]:
    """Ищет похожие скиллы по имени/описанию в staging + пользовательских скиллах."""
    out, q = [], set(re.findall(r"[a-zа-яё0-9]{4,}", query.lower()))
    for base in (STAGING, USER_SKILLS):
        if not base.is_dir():
            continue
        for sk in base.glob("*/SKILL.md"):
            head = sk.read_text(encoding="utf-8", errors="replace")[:600].lower()
            score = sum(1 for w in q if w in head)
            if score:
                out.append({"name": sk.parent.name, "path": str(sk), "score": score,
                            "staged": base == STAGING})
    return sorted(out, key=lambda x: -x["score"])[:limit]


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
        # Куратор: пометить протухшие staged-драфты
        stale_cut = now - STALE_DAYS * 86400
        stale = [r[0] for r in conn.execute(
            """SELECT name FROM skill_usage WHERE staged_ts IS NOT NULL
               AND (last_used IS NULL OR last_used < ?) AND staged_ts < ?""",
            (stale_cut, stale_cut))]
        candidates = []
        for key, kind, excerpt, cnt in conn.execute(
                """SELECT key,kind,excerpt,seen_count FROM lessons
                   WHERE status='candidate' AND (seen_count>=?
                     OR (kind='correction' AND seen_count>=?))
                   ORDER BY (kind='correction') DESC, last_ts DESC LIMIT 8""",
                (PROMOTE_AT, CORRECTION_PROMOTE_AT)):
            candidates.append({"key": key, "kind": kind, "seen_count": cnt,
                               "excerpt": excerpt,
                               "similar_skills": similar_skills(excerpt)})
        conn.commit()
        return {
            "new_signals": len(signals),
            "segment": {k: (sorted(v) if isinstance(v, set) else v) for k, v in stats.items()},
            "lesson_candidates": candidates,
            "stale_staged_skills": stale,
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
        stamp = (f"\n<!-- second-nature: staged {time.strftime('%Y-%m-%d')}"
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
        return {"staged": str(dest), "pinned_project": pinned_project or "global",
                "note": "Draft is INACTIVE until approve_draft is called (by user or wake)."}
    finally:
        conn.close()


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
                if chunk:
                    conn.execute("INSERT INTO transcripts(content,session,ts) VALUES(?,?,?)",
                                 (redact("\n".join(chunk))[:200000], f.stem, ts))
                    indexed += 1
            conn.execute("INSERT OR REPLACE INTO indexed_files VALUES(?,?)", (str(f), size))
        conn.commit()
        safe_q = " ".join(re.findall(r"[\w./-]{2,}", query))
        rows = conn.execute(
            """SELECT session, ts, snippet(transcripts, 0, '>>', '<<', ' … ', 24)
               FROM transcripts WHERE transcripts MATCH ? ORDER BY rank LIMIT ?""",
            (safe_q or query, limit)).fetchall()
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
