"""Which proxy calls belong to a sub-agent: a join of the proxy ledger's ``msg_id`` to the
Claude Code transcripts (M0.3b).

A proxy row's ``msg_id`` is the API message id of its response. The same id is
``message.id`` on the transcript's assistant entry. Claude Code writes a sub-agent's work in
``<projects>/<project>/<session>/subagents/**/agent-*.jsonl`` (every entry ``isSidechain: true``;
workflow agents sit one level deeper, under ``subagents/workflows/wf_*``);
older layouts mark ``isSidechain`` inside the main file. Either way the call is a sub-agent
call, and its first call is not a human turn (O3: the unit is a human turn).

Reads message ids only: no prompt text leaves a transcript through this module.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from llm_router import northstar as _ns


def _scan(path: Path, *, force_sidechain: bool, out: dict[str, bool]) -> None:
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            # Cheap pre-filter: only assistant entries carry the message id we join on.
            if '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("type") != "assistant":
                continue
            msg = d.get("message")
            mid = msg.get("id") if isinstance(msg, dict) else None
            if not (isinstance(mid, str) and mid):
                continue
            side = force_sidechain or d.get("isSidechain") is True
            out[mid] = out.get(mid, False) or side


def sidechain_index(session_ids: Iterable[str], *, projects_dir: Path | None = None) -> dict[str, bool]:
    """``{msg_id: is_sidechain}`` for every assistant message of the given sessions.

    A message id absent from the result is *unjoined* (no transcript holds it): the caller keeps
    that row in O3 and counts it."""
    root = projects_dir if projects_dir is not None else _ns.claude_projects_dir()
    out: dict[str, bool] = {}
    for sid in session_ids:
        if not isinstance(sid, str) or not sid or "/" in sid or sid.startswith("."):
            continue
        try:
            for main in root.glob(f"*/{sid}.jsonl"):
                _scan(main, force_sidechain=False, out=out)
            # `**` also reaches workflow agents: <sid>/subagents/workflows/wf_*/agent-*.jsonl
            for sub in root.glob(f"*/{sid}/subagents/**/*.jsonl"):
                _scan(sub, force_sidechain=True, out=out)
        except OSError:
            continue
    return out
