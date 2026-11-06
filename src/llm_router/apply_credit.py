"""Transcript-only llm_apply credit and the three-message Claude-finished exclusion.

The JSON state is resumable across status-line transcript scans. Each transcript
has its own state: a sibling agent's edits cannot erase this call's credit.
"""
from __future__ import annotations

import json

TOOL_NAME = "mcp__llm_router__llm_apply"
PREFIX = "[llm_apply credit] "
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


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
    if obj.get("type") == "assistant":
        mid = message.get("id")
        edits = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
                 and b.get("name") in EDIT_TOOLS]
        for call in state.setdefault("calls", {}).values():
            if "credit" not in call or not isinstance(mid, str) or not mid:
                continue
            turns = call["turns"]
            if mid not in turns and len(turns) < 3:
                turns.append(mid)
            if mid in turns:
                for edit in edits:
                    args = edit.get("input")
                    if isinstance(args, dict) and (args.get("file_path") or args.get("notebook_path")) in call["credit"]["files"]:
                        call["redo"] = True
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
                call.update(credit=credit, turns=[], redo=False)


def counts(call: dict) -> tuple[int, int]:
    credit = call.get("credit", {}) if not call.get("redo") else {}
    return credit.get("program", 0), credit.get("local", 0)


def transcript(records: list[dict]) -> dict:
    state: dict = {}
    for obj in records:
        observe(state, obj)
    return state.get("calls", {})
