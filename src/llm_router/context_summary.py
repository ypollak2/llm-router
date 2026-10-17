"""Rolling session summary (PLAN v16 P1.2, R-CTX-3).

A detached worker keeps one summary per session in ``sessions.db`` so the context
pack (P1.1) can read it instead of re-reading the whole transcript.

OPT-IN, DEFAULT OFF. Nothing here runs unless ``LLM_ROUTER_ROLLING_SUMMARY=1``.
The Stop hook reads that one variable and imports this module only when it is set
(D-47: a P1 core may land before G0 only if it is live-path-free).

Shape:

* :func:`spawn` (hook side) forks a detached ``python -m llm_router.context_summary``
  (``statusline_tick._spawn_detached``: fork + execv, nothing waits on it).
* :func:`maybe_update` (worker side) parses the transcript, decides whether a summary
  is due, and writes a row to ``rolling_summaries``.
* :func:`read_summary_text` is what ``context_pack.build_pack`` calls (pack mode, the
  hook / agent / mcp doors, only while this feature is on): read-only, short busy
  timeout. This module does not import ``context_pack``.

Cadence: due when more than ``RECENT_WINDOW`` (7) messages are unsummarised, or
``LLM_ROUTER_SUMMARY_EVERY`` (default 4) human turns have passed. The summary covers
everything except the last 7 messages, which the pack carries verbatim.

Modes: a local Ollama model when one is installed and memory is free (>= 20%),
otherwise ``extractive`` (user requests + tool calls, no model). A model answer that
lacks any of the five sections is discarded for the extractive one.

Compaction: a transcript record with ``isCompactSummary: true`` is the host's own
summary. It is stored with ``source="host_compact"`` and summarising continues from it.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

RECENT_WINDOW = 7
DEFAULT_EVERY = 4
MIN_FREE_PCT = 20.0
SECTIONS = ("Goal", "Decisions", "Files touched", "Current state", "Open questions")
SPAWN_COOLDOWN_S = 10.0
_TOOL_RESULT_CAP = 600
_MAX_PROMPT_CHARS_PER_TOKEN = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rolling_summaries (
    session_id   TEXT PRIMARY KEY,
    upto_msg_idx INTEGER NOT NULL,
    text         TEXT NOT NULL,
    model        TEXT NOT NULL,
    source       TEXT NOT NULL,
    updated_at   REAL NOT NULL
);
"""

# Family preference among the map's connected local generate models.
_PREFERRED = ("qwen3.5", "qwen3.6", "qwen3", "llama3", "gemma", "mistral", "phi")
_EXCLUDED = ("llmr-", "embed", "classifier", "systemone", "coder:30b")


def enabled() -> bool:
    return os.environ.get("LLM_ROUTER_ROLLING_SUMMARY", "").strip().lower() in ("1", "true", "on", "yes")


def summary_every() -> int:
    try:
        return max(1, int(os.environ.get("LLM_ROUTER_SUMMARY_EVERY", DEFAULT_EVERY)))
    except ValueError:
        return DEFAULT_EVERY


def db_path() -> Path:
    from llm_router import paths

    return Path(os.environ.get("LLM_ROUTER_SESSIONS_PATH") or paths.state_path("sessions.db"))


def _connect(path: Path | None = None) -> sqlite3.Connection:
    from llm_router.sqlite_wal import enable_wal

    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    enable_wal(conn, busy_timeout_ms=5000, label="rolling_summaries")
    conn.executescript(_SCHEMA)
    return conn


# --- storage ---------------------------------------------------------------

def load_summary(session_id: str, path: Path | None = None) -> dict[str, Any] | None:
    """The stored summary row as a dict, or None. Never raises (read-only caller)."""
    try:
        conn = _connect(path)
        try:
            row = conn.execute(
                "SELECT session_id, upto_msg_idx, text, model, source, updated_at "
                "FROM rolling_summaries WHERE session_id=?", (session_id,)).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    keys = ("session_id", "upto_msg_idx", "text", "model", "source", "updated_at")
    return dict(zip(keys, row))


def read_summary_text(session_id: str, path: Path | None = None,
                      timeout_s: float = 0.25) -> str | None:
    """The stored summary text, read-only, or None. Never raises, never writes.

    For the context pack's hot path: a ``mode=ro`` connection with a short busy
    timeout, so a locked database or a missing table costs at most
    ``timeout_s`` and never creates the file, the table or a WAL.
    """
    try:
        p = path or db_path()
        if not p.is_file():
            return None
        conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout_s)
        try:
            row = conn.execute(
                "SELECT text FROM rolling_summaries WHERE session_id=?", (session_id,)).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None
    return str(row[0]) if row and row[0] else None


def _store(session_id: str, upto: int, text: str, model: str, source: str,
           path: Path | None = None) -> None:
    conn = _connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO rolling_summaries VALUES (?,?,?,?,?,?)",
            (session_id, upto, text, model, source, time.time()))
        conn.commit()
    finally:
        conn.close()


# --- transcript ------------------------------------------------------------

def _render(content: Any) -> tuple[str, bool]:
    """(text, is_human) for one message's ``content``. Tool blocks become text."""
    if isinstance(content, str):
        return content, True
    parts: list[str] = []
    human = False
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(str(b.get("text", "")))
            human = True
        elif t == "tool_use":
            inp = json.dumps(b.get("input", {}), ensure_ascii=False)[:200]
            parts.append(f"[tool_use {b.get('name', '?')} {inp}]")
        elif t == "tool_result":
            c = b.get("content", "")
            if isinstance(c, list):
                c = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
            parts.append(f"[tool_result {str(c)[:_TOOL_RESULT_CAP]}]")
    return "\n".join(parts), human


def read_messages(transcript_path: str | Path) -> list[dict[str, Any]]:
    """Conversation messages in order: ``{role, text, human, compact, tools, files}``.

    Sidechain and meta records are skipped. ``human`` is True for a user message
    typed by a person (not a tool_result carrier). ``compact`` marks the host's
    own compaction summary.
    """
    out: list[dict[str, Any]] = []
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # torn trailing line
            if not isinstance(rec, dict) or rec.get("type") not in ("user", "assistant"):
                continue
            if rec.get("isSidechain") or (rec.get("isMeta") and not rec.get("isCompactSummary")):
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            text, human = _render(msg.get("content"))
            tools: list[str] = []
            files: list[str] = []
            for b in msg.get("content") if isinstance(msg.get("content"), list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tools.append(str(b.get("name", "?")))
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    for k in ("file_path", "path", "notebook_path"):
                        if isinstance(inp.get(k), str):
                            files.append(inp[k])
            out.append({"role": rec["type"], "text": text,
                        "human": human and rec["type"] == "user",
                        "compact": bool(rec.get("isCompactSummary")),
                        "tools": tools, "files": files})
    return out


# --- decision --------------------------------------------------------------

def plan(messages: list[dict[str, Any]], prev_upto: int, every: int | None = None) -> int | None:
    """New ``upto_msg_idx`` when a summary is due, else None.

    Due when > RECENT_WINDOW messages are unsummarised or ``every`` human turns
    passed since ``prev_upto``. The summary covers every message so far; the pack
    also carries the last 7 verbatim, and the overlap is harmless.
    """
    every = every or summary_every()
    total = len(messages)
    unsummarised = total - prev_upto
    human_turns = sum(1 for m in messages[prev_upto:] if m["human"])
    if unsummarised > 0 and (unsummarised > RECENT_WINDOW or human_turns >= every):
        return total
    return None


def last_compact_idx(messages: list[dict[str, Any]]) -> int | None:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["compact"]:
            return i
    return None


# --- summarising -----------------------------------------------------------

def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def extractive(messages: list[dict[str, Any]], base: str = "") -> str:
    """Deterministic summary: user requests and tool calls only, no model."""
    reqs = [_clip(m["text"], 200) for m in messages if m["human"] and not m["compact"]]
    files: list[str] = []
    for m in messages:
        for p in m["files"]:
            if p not in files:
                files.append(p)
    tool_counts: dict[str, int] = {}
    for m in messages:
        for t in m["tools"]:
            tool_counts[t] = tool_counts.get(t, 0) + 1
    tools = ", ".join(f"{k} x{v}" for k, v in sorted(tool_counts.items())) or "none"
    body = {
        "Goal": reqs[0] if reqs else (_clip(base, 200) or "unknown"),
        "Decisions": "not extracted (extractive mode)",
        "Files touched": ", ".join(files[-40:]) or "none",
        "Current state": f"{len(messages)} messages folded; tool calls: {tools}. "
                         f"Latest requests: " + (" | ".join(reqs[-5:]) or "none"),
        "Open questions": "not extracted (extractive mode)",
    }
    return "\n\n".join(f"## {k}\n{body[k]}" for k in SECTIONS)


def has_all_sections(text: str) -> bool:
    heads = {m.group(1).strip().lower() for m in re.finditer(r"^#{1,3}\s*(.+?)\s*$", text, re.M)}
    return all(s.lower() in heads for s in SECTIONS)


def free_memory_pct() -> float | None:
    """Percent of RAM available, or None when it cannot be read (treated as ok)."""
    try:
        if sys.platform == "darwin":
            import subprocess

            # fixed argv, explicit minimal env (R4 subprocess env allowlist ratchet)
            out = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=3,
                                 env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}).stdout
            page = int(re.search(r"page size of (\d+)", out).group(1))  # type: ignore[union-attr]
            pg = {k: int(v) for k, v in re.findall(r"Pages ([a-z ]+):\s+(\d+)\.", out)}
            free = (pg.get("free", 0) + pg.get("inactive", 0) + pg.get("speculative", 0)) * page
            total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
            return 100.0 * free / total
        info = {k: int(v) for k, v in re.findall(r"(\w+):\s+(\d+) kB", Path("/proc/meminfo").read_text())}
        return 100.0 * info["MemAvailable"] / info["MemTotal"]
    except Exception:  # noqa: BLE001
        return None


def _ollama_url() -> str:
    return os.environ.get("LLM_ROUTER_OLLAMA_URL", "").strip() or "http://localhost:11434"


def installed_models() -> list[str]:
    import urllib.request

    try:
        with urllib.request.urlopen(_ollama_url() + "/api/tags", timeout=2) as r:  # noqa: S310
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:  # noqa: BLE001
        return []


def map_candidates(data: dict | None) -> list[str] | None:
    """Local generate models the P1.8 resource map lists as connected (None: no map)."""
    if not data:
        return None
    out = []
    for r in data.get("resources") or []:
        if r.get("kind") == "local" and r.get("status") == "connected":
            caps = r.get("capabilities") or []
            res = str(r.get("resource", ""))
            if "generate" in caps and "embedding" not in caps and res.startswith("ollama/"):
                out.append(res.split("/", 1)[1])
    return out


def pick_model(installed: list[str], map_data: dict | None = None) -> str | None:
    """Best local summary model: the map's connected generate models when a map
    exists (file read only, no network), else the ``/api/tags`` list; then the
    family preference order. ``LLM_ROUTER_SUMMARY_MODEL`` overrides."""
    override = os.environ.get("LLM_ROUTER_SUMMARY_MODEL", "").strip()
    if override:
        return override
    mapped = map_candidates(map_data)
    pool = mapped if mapped is not None else installed
    ok = [m for m in pool if not any(x in m.lower() for x in _EXCLUDED)]
    for fam in _PREFERRED:
        for m in ok:
            if m.lower().startswith(fam):
                return m
    return None


def _prompt(prev: str, chunk: str) -> str:
    heads = "\n".join(f"## {s}" for s in SECTIONS)
    return ("Update the running summary of a coding session. Fold the NEW MESSAGES into "
            "the PREVIOUS SUMMARY. Reply with ONLY these five markdown sections, in order, "
            "terse, no preamble:\n" + heads + "\n\n"
            f"PREVIOUS SUMMARY:\n{prev or '(none)'}\n\nNEW MESSAGES:\n{chunk}\n")


def _generate(model: str, prompt: str) -> str:
    import urllib.request

    from llm_router import local_models

    body = json.dumps({"model": model, "stream": False,
                       "messages": [{"role": "user", "content": prompt}],
                       "options": {"num_ctx": local_models.num_ctx(model), "temperature": 0}}).encode()
    req = urllib.request.Request(_ollama_url() + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310
        return str(json.load(r).get("message", {}).get("content", "")).strip()


def summarise(messages: list[dict[str, Any]], prev_upto: int, new_upto: int, prev_text: str,
              *, model: str | None, generate=_generate) -> tuple[str, str, str]:
    """(text, model, source) for messages[:new_upto] given the stored prefix summary."""
    if model:
        from llm_router import local_models

        budget = local_models.num_ctx(model) * _MAX_PROMPT_CHARS_PER_TOKEN // 2
        chunk = "\n".join(f"{m['role']}: {m['text']}" for m in messages[prev_upto:new_upto])
        chunk = chunk[-budget:]
        try:
            text = generate(model, _prompt(prev_text, chunk))
        except Exception:  # noqa: BLE001
            text = ""
        if has_all_sections(text):
            return text, model, "model"
    return extractive(messages[:new_upto], prev_text), "none", "extractive"


# --- entry points ----------------------------------------------------------

def maybe_update(session_id: str, transcript_path: str | Path, *, model: str | None = None,
                 db: Path | None = None, generate=_generate,
                 free_pct: float | None = None) -> str:
    """Update the stored summary if due. Returns what happened: ``skipped``,
    ``host_compact``, ``model``, or ``extractive``. Raises only on storage errors."""
    messages = read_messages(transcript_path)
    row = load_summary(session_id, db)
    prev_upto = row["upto_msg_idx"] if row else 0
    prev_text = row["text"] if row else ""
    if prev_upto > len(messages):  # transcript was rewritten (compaction): start over
        prev_upto, prev_text = 0, ""

    ci = last_compact_idx(messages)
    if ci is not None and ci + 1 > prev_upto:
        _store(session_id, ci + 1, messages[ci]["text"], "host", "host_compact", db)
        return "host_compact"

    new_upto = plan(messages, prev_upto)
    if new_upto is None:
        return "skipped"
    if free_pct is None:
        free_pct = free_memory_pct()
    use_model = model if (free_pct is None or free_pct >= MIN_FREE_PCT) else None
    text, used, source = summarise(messages, prev_upto, new_upto, prev_text,
                                   model=use_model, generate=generate)
    _store(session_id, new_upto, text, used, source, db)
    return source


def _safe(sid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sid)[:100]


def spawn(session_id: str, transcript_path: str, *, argv: list[str] | None = None) -> bool:
    """Hook side: start ONE detached worker, at most once per SPAWN_COOLDOWN_S per
    session. Never waits, never raises. True when a worker was started."""
    try:
        if not session_id or not transcript_path:
            return False
        from llm_router import paths

        claim = paths.state_path(f"rolling_summary.{_safe(session_id)}.claim")
        now = time.time()
        try:
            if 0 <= now - claim.stat().st_mtime < SPAWN_COOLDOWN_S:
                return False
        except OSError:
            pass
        claim.parent.mkdir(parents=True, exist_ok=True)
        claim.write_text(str(now))
        from llm_router.statusline_tick import _spawn_detached

        _spawn_detached(argv or [sys.executable, "-m", "llm_router.context_summary",
                                 "--session", session_id, "--transcript", transcript_path])
        return True
    except Exception:  # noqa: BLE001
        return False


def coverage(sessions: dict[str, int], db: Path | None = None, min_messages: int = 12) -> dict[str, Any]:
    """P1.2-a live coverage: of sessions with >= ``min_messages`` messages, the share
    that have a stored summary. ``sessions`` maps session_id -> message count.
    Reports ``n``; the gate needs n >= 20 sessions and >= 95%."""
    eligible = [s for s, n in sessions.items() if n >= min_messages]
    covered = [s for s in eligible if load_summary(s, db) is not None]
    pct = 100.0 * len(covered) / len(eligible) if eligible else None
    return {"n_sessions": len(eligible), "covered": len(covered), "pct": pct,
            "informative": len(eligible) >= 20}


def _worker(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="llm_router.context_summary")
    ap.add_argument("--session", required=True)
    ap.add_argument("--transcript", required=True)
    a = ap.parse_args(argv)
    try:
        import fcntl
    except ImportError:  # Windows: no flock; the claim cooldown is the only guard
        fcntl = None  # type: ignore[assignment]
    lock = None
    if fcntl is not None:
        from llm_router import paths

        p = paths.state_path(f"rolling_summary.{_safe(a.session)}.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        lock = open(p, "w")  # noqa: SIM115 -- held until process exit
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # another worker owns this session
    from llm_router import resource_map

    data = resource_map.load_map()
    model = pick_model([] if data else installed_models(), data)
    maybe_update(a.session, a.transcript, model=model)
    return 0


def _main(argv: list[str]) -> int:
    if "--coverage-dir" in argv:
        d = Path(argv[argv.index("--coverage-dir") + 1])
        sess = {p.stem: len(read_messages(p)) for p in d.glob("*.jsonl")}
        print(json.dumps(coverage(sess)))
        return 0
    return _worker(argv)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
