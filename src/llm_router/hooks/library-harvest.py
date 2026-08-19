#!/usr/bin/env python3
"""Library harvest — PostToolUse manuscript writer (zero model tokens).

Appends one JSONL line per tool event to the open Book's raw/events.jsonl,
updates working-memory/entities.json, and mechanically regenerates
working-memory/delta.md. The expensive model never journals; this is a
court stenographer, not an author.

Also handles SessionStart (open a Book) when invoked with --session-start.
Never blocks: all failures exit 0 silently (the Library is an optimization,
not a dependency).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from llm_router.library.store import LibraryStore, now_utc, scrub_secrets  # noqa: E402

MAX_DESC = 200
DELTA_EVENTS = 60  # last N events shown in delta.md


def _head_sha(cwd: str) -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=cwd, capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _branch(cwd: str) -> str:
    try:
        r = subprocess.run(["git", "branch", "--show-current"],
                           cwd=cwd, capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _files_touched(tool: str, tool_input: dict) -> list[str]:
    files: list[str] = []
    for key in ("file_path", "notebook_path", "path"):
        v = tool_input.get(key)
        if isinstance(v, str):
            files.append(v)
    if tool == "Bash":
        # cheap heuristic: absolute/relative paths with an extension
        import re
        cmd = str(tool_input.get("command", ""))
        files.extend(re.findall(r"[\w./~-]+\.(?:py|md|toml|json|yaml|yml|txt|sh|jsonl)\b", cmd)[:8])
    return sorted(set(files))[:16]


def _outcome(hook_input: dict) -> tuple[str, int]:
    resp = hook_input.get("tool_response")
    if isinstance(resp, dict):
        if resp.get("interrupted"):
            return "interrupted", -1
        code = resp.get("exit_code", resp.get("returnCode"))
        if isinstance(code, int):
            return ("ok" if code == 0 else "error"), code
        if resp.get("is_error") or resp.get("error"):
            return "error", 1
    return "ok", 0


def _current_book(store: LibraryStore) -> str | None:
    marker = store.root / "working-memory" / "current_book"
    if marker.exists():
        name = marker.read_text().strip()
        if (store.root / "books" / name).exists():
            return name
    return None


def open_book(store: LibraryStore, session_id: str) -> str:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = f"{date}--{(session_id or 'session')[:8]}"
    (store.root / "books" / name / "chapters").mkdir(parents=True, exist_ok=True)
    (store.root / "books" / name / "raw").mkdir(parents=True, exist_ok=True)
    (store.root / "working-memory").mkdir(parents=True, exist_ok=True)
    (store.root / "working-memory" / "current_book").write_text(name)
    return name


def _regen_delta(store: LibraryStore, book: str) -> None:
    raw = store.root / "books" / book / "raw" / "events.jsonl"
    events: list[dict] = []
    if raw.exists():
        for line in raw.read_text(encoding="utf-8").splitlines()[-DELTA_EVENTS:]:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    seal_marker = store.root / "working-memory" / "last_seal.json"
    covers_since = ""
    if seal_marker.exists():
        try:
            covers_since = json.loads(seal_marker.read_text()).get("sealed_at", "")
        except Exception:
            covers_since = ""
    if covers_since:
        events = [e for e in events if e.get("ts", "") > covers_since]

    by_file: dict[str, list[dict]] = {}
    other: list[dict] = []
    for e in events:
        touched = e.get("files") or []
        if touched:
            for f in touched:
                by_file.setdefault(f, []).append(e)
        else:
            other.append(e)

    lines: list[str] = []
    for f in sorted(by_file):
        evs = by_file[f]
        last = evs[-1]
        lines.append(f"- `{f}` — {len(evs)} events, last: {last.get('tool')} "
                     f"{last.get('outcome')} ({last.get('ts')}, evt-{last.get('id')})")
    if other:
        lines.append(f"- (no files) {len(other)} events, last: "
                     f"{other[-1].get('tool')} {other[-1].get('outcome')}")

    entities = sorted(by_file)
    (store.root / "working-memory" / "entities.json").write_text(
        json.dumps({"files": entities, "updated_at": now_utc()}, indent=1))

    store.write_doc(
        "working-memory/delta.md",
        {"type": "working-memory", "book": book,
         "covers_since": covers_since or "book-open",
         "event_count": len(events), "written_at": now_utc()},
        "\n".join(lines) or "(no events since last seal)")


def harvest(hook_input: dict) -> None:
    cwd = hook_input.get("cwd") or os.getcwd()
    store = LibraryStore.for_repo(Path(cwd))
    if store is None:
        return
    store.ensure_layout()
    book = _current_book(store) or open_book(store, str(hook_input.get("session_id", "")))

    raw = store.root / "books" / book / "raw" / "events.jsonl"
    prev = 0
    if raw.exists():
        prev = sum(1 for _ in raw.open())
    tool = str(hook_input.get("tool_name", ""))
    tool_input = hook_input.get("tool_input") or {}
    outcome, code = _outcome(hook_input)
    desc = str(tool_input.get("description") or tool_input.get("command")
               or tool_input.get("file_path") or "")[:MAX_DESC]
    event = {
        "id": prev + 1,
        "ts": now_utc(),
        "tool": tool,
        "desc": scrub_secrets(desc),
        "cmd": scrub_secrets(str(tool_input.get("command", ""))[:MAX_DESC]) if tool == "Bash" else "",
        "outcome": outcome,
        "exit": code,
        "files": _files_touched(tool, tool_input),
        "sha": _head_sha(cwd),
        "branch": _branch(cwd),
    }
    with raw.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    try:
        from llm_router.library.sealer import maybe_seal_on_event
        maybe_seal_on_event(store, book, event, cwd=Path(cwd))
    except Exception:
        pass  # sealing is best-effort; the manuscript is already safe

    _regen_delta(store, book)


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        if "--session-start" in sys.argv:
            cwd = hook_input.get("cwd") or os.getcwd()
            store = LibraryStore.for_repo(Path(cwd))
            if store is not None:
                store.ensure_layout()
                open_book(store, str(hook_input.get("session_id", "")))
                store.regen_indexes()
        elif "--pack" in sys.argv:
            cwd = hook_input.get("cwd") or os.getcwd()
            store = LibraryStore.for_repo(Path(cwd))
            if store is not None:
                from llm_router.library.pack import pack_for
                pack = pack_for(store, _current_book(store))
                if pack:
                    print(json.dumps({
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": pack,
                        }
                    }))
        elif "--session-end" in sys.argv:
            cwd = hook_input.get("cwd") or os.getcwd()
            store = LibraryStore.for_repo(Path(cwd))
            if store is not None:
                book = _current_book(store)
                if book:
                    from llm_router.library.book_closer import close_book
                    close_book(store, book, cwd=Path(cwd))
        else:
            harvest(hook_input)
    except Exception:
        pass  # the Library never breaks the session
    return 0


if __name__ == "__main__":
    sys.exit(main())
