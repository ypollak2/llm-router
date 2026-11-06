"""Transcript-only llm_apply credit and the three-message Claude-finished exclusion.

The JSON state is resumable across status-line transcript scans. Each transcript
has its own state: a sibling agent's edits cannot erase this call's credit.
"""
from __future__ import annotations

import json
import os

TOOL_NAME = "mcp__llm_router__llm_apply"
PREFIX = "[llm_apply credit] "
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


def _norm(path: str) -> str:
    """One spelling per file: ``/repo/./a.py``, ``/repo//a.py`` and symlinked prefixes compare equal."""
    return os.path.realpath(path)


def _credit(text: str) -> dict | None:
    for line in text.splitlines():
        if not line.startswith(PREFIX):
            continue
        try:
            value = json.loads(line[len(PREFIX):])
        except ValueError:
            continue
        if (isinstance(value, dict)
                and all(type(value.get(k)) is int and value[k] >= 0 for k in ("program", "local"))
                and isinstance(value.get("files"), list)
                and all(isinstance(p, str) for p in value["files"])):
            return value
    return None


def observe(state: dict, obj: dict) -> None:
    """Consume one record, retaining only apply calls, results and up to three ids."""
    message = obj.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        content = []
    if "open" not in state and state.get("calls"):   # a scan cached before the open list existed
        state["open"] = [cid for cid, c in state["calls"].items() if "credit" in c and not c.get("redo")]
    if obj.get("type") == "assistant":
        mid = message.get("id")
        edits = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
                 and b.get("name") in EDIT_TOOLS]
        calls = state.setdefault("calls", {})
        still_open = []
        for cid in state.get("open", []):           # only calls whose 3-message window is still open
            call = calls[cid]
            turns = call["turns"]
            if isinstance(mid, str) and mid:
                if mid not in turns and len(turns) < 3:
                    turns.append(mid)
                if mid in turns:
                    for edit in edits:
                        args = edit.get("input")
                        target = args.get("file_path") or args.get("notebook_path") if isinstance(args, dict) else None
                        if isinstance(target, str) and _norm(target) in call["credit"]["files"]:
                            call["redo"] = True
                elif len(turns) >= 3:
                    continue                          # a 4th message: the window is closed for good
            if not call.get("redo"):
                still_open.append(cid)
        state["open"] = still_open
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == TOOL_NAME and b.get("id"):
                state["calls"].setdefault(b["id"], {})
    elif obj.get("type") == "user":
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                continue
            call = state.setdefault("calls", {}).get(b.get("tool_use_id"))
            if call is None or "credit" in call:
                continue
            raw = b.get("content")
            text = raw if isinstance(raw, str) else "\n".join(
                p["text"] for p in (raw if isinstance(raw, list) else [])
                if isinstance(p, dict) and isinstance(p.get("text"), str))
            credit = _credit(text)
            if credit is not None:
                credit["files"] = sorted({_norm(p) for p in credit["files"]})
                call.update(credit=credit, turns=[], redo=False)
                state.setdefault("open", []).append(b.get("tool_use_id"))


def merge(into: dict, calls: dict) -> None:
    """Fold one transcript's calls into ``into`` by tool_use id. A call replayed in several
    transcripts (resume, sub-agent copy) counts once, and a redo seen in ANY copy zeroes it."""
    for cid, call in calls.items():
        have = into.get(cid)
        if have is None:
            into[cid] = dict(call)
            continue
        if "credit" in call:
            if "credit" not in have:
                have.update(credit=call["credit"], turns=list(call.get("turns", [])))
            elif have["credit"] != call["credit"]:   # copies disagree: the smaller credit, whatever the read order
                a, b = have["credit"], call["credit"]
                have["credit"] = {"program": min(a["program"], b["program"]), "local": min(a["local"], b["local"]),
                                  "files": sorted(set(a["files"]) | set(b["files"]))}
        if call.get("redo"):
            have["redo"] = True


def counts(call: dict) -> tuple[int, int]:
    credit = call.get("credit", {}) if not call.get("redo") else {}
    return credit.get("program", 0), credit.get("local", 0)


def transcript(records: list[dict]) -> dict:
    state: dict = {}
    for obj in records:
        observe(state, obj)
    return state.get("calls", {})
