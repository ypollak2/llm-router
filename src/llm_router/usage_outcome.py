"""Was a routed output USED AS-IS? One used / redone / unknown verdict per routed event.

This is the signal the North Star (NS) and the redo rate (D3) need and did not
have: ``edit_outcomes.jsonl`` writes ``survived: null`` forever, and
``northstar.py`` judges "the next assistant message" rather than a window of
human turns, and persists no per-event verdict.

DEFINITION (the contract; the tests pin it)
-------------------------------------------
A routed event is a Claude tool call that handed work to a non-Claude model:

* ``edit``   -- ``llm_edit`` (a cheap model returns ``{file, old_string,
  new_string}`` pairs that Claude applies with its own Edit tool).
* ``answer`` -- ``llm`` / ``llm_local_task`` / ``llm_route`` / ``llm_act`` /
  ``llm_router_agent_route`` (a routed answer or a delegation to Codex/local).

The event happens in human turn ``t`` (the count of human prompts so far). Its
WINDOW is the rest of turn ``t`` plus the next 3 human turns (``t+1..t+3``). A
human turn is a user record carrying typed text: not a tool result, not a
``<system-reminder>`` / ``<command-*>`` / ``<task-notification>`` wrapper, not a
record flagged ``isMeta``.

Verdict:

* ``redone``   -- positive evidence, inside the window, that Claude did the
  work again. Evidence is never undone by later turns, so it holds even while
  the window is still open.
    - edit:   the routed ``new_string`` was never applied but Claude edited the
      same file differently (``edited_differently``); or after applying it Claude
      edited text inside it (``re_edited``), restored the old text
      (``reverted``), rewrote the file with ``Write`` (``rewritten``), or ran a
      ``git checkout/restore/revert/reset`` that covers the file (``reverted``).
    - answer: a later tool call in the window re-asks something near-identical
      (``re_asked``: word-8-gram containment >= 0.6, or an exact match for short
      prompts), or a later human turn starts with ``claude:`` / ``native:`` /
      ``opus:`` (``overridden``).
* ``used``     -- no redo evidence AND the window has closed (3 human turns
  have followed the event). ``reason`` grades the evidence: ``applied_verbatim``
  (an edit's every pair was applied as returned) and ``result_reused`` (>= 0.6 of
  an answer's 8-grams appear in Claude's next message) are strong; ``not_redone``
  (silence) is the weak form.
* ``unknown``  -- everything else, never rounded to either side:
  ``window_open`` (fewer than 3 human turns have followed and nothing was
  redone), ``no_result`` (no tool result, an error, or empty), ``no_pairs`` (the
  edit result held no parseable pairs: nothing was applied), ``not_applied_seen``
  (the pair was never applied and the file was never touched; it may have been
  applied by some means the transcript does not show), ``partly_applied``.

``so_far`` is the reason the evidence gives now, before ``window_open`` hides it: an
open-window edit reads ``applied_verbatim`` or ``not_applied_seen`` there. O3's local
answers close their window after 2 turns and must not take an unapplied edit as used.

Out of scope, stated rather than guessed: edits that a hook applies itself
(ZERO_CLAUDE_EDIT) leave no tool call in the transcript, so they produce no
event here. Whether a *human* later hand-edits the same lines is not visible.

``judge_transcript`` is pure over parsed records. ``record_outcomes`` appends to
``usage_outcomes.jsonl`` (idempotent per ``event_id``). ``sweep`` walks recent
transcripts. ``python -m llm_router.usage_outcome [--days N] [--dry-run]``
runs a sweep.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm_router import paths, session_kind

LEDGER_FILENAME = "usage_outcomes.jsonl"
WINDOW_TURNS = 3

OUTCOME_USED = "used"
OUTCOME_REDONE = "redone"
OUTCOME_UNKNOWN = "unknown"

KIND_EDIT = "edit"
KIND_ANSWER = "answer"

_EDIT_TOOLS = frozenset({"llm_edit"})
_ANSWER_TOOLS = frozenset({"llm", "llm_local_task", "llm_route", "llm_act", "llm_router_agent_route"})
_CLAUDE_EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
_REASK_TOOLS = frozenset({"Agent", "Task"})

_NON_HUMAN_PREFIXES = ("<system-reminder", "<command-", "<local-command", "<task-notification",
                       "<user-prompt-submit-hook", "Caveat:")
_OVERRIDE_RE = re.compile(r"^\s*(?:claude|native|opus)\s*:", re.IGNORECASE)
# The receipt band's `r` prompt starts with `claude:` (so the proxy escalates) and ends with
# this sentinel (hooks/logic.mjs REDO_MARK). The press is already one ``user_signal`` row that
# D3 folds in (commands/kpi.py), so the override detector must not count the same redo again.
BAND_REDO_MARK = "[receipt-band:redo]"
_GIT_UNDO_RE = re.compile(r"\bgit\s+(?:checkout|restore|revert|reset|stash)\b")
_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
_WORD_RE = re.compile(r"[a-z0-9]+")
_SHINGLE_N = 8
_SIMILAR_AT = 0.6
_REUSE_AT = 0.6
_MIN_FRAGMENT = 4  # a later edit's old_string shorter than this proves nothing


# ── transcript parsing ──────────────────────────────────────────────────────

def _content_blocks(rec: dict) -> list:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return content if isinstance(content, list) else []


def _human_text(rec: dict) -> str | None:
    """The typed text of a human turn, or None when this record is not one."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return None
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    text = text.strip()
    if not text or text.startswith(_NON_HUMAN_PREFIXES):
        return None
    return text


def _assistant_text(rec: dict) -> str:
    if rec.get("type") != "assistant":
        return ""
    return "\n".join(b.get("text", "") for b in _content_blocks(rec)
                     if isinstance(b, dict) and b.get("type") == "text")


def _tool_uses(rec: dict) -> list[dict]:
    if rec.get("type") != "assistant":
        return []
    return [b for b in _content_blocks(rec) if isinstance(b, dict) and b.get("type") == "tool_use"]


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _unwrap(text: str) -> str:
    """The MCP server wraps a tool's text as ``{"result": "<text>"}``; real
    transcripts carry that envelope, so peel it before reading the content."""
    if text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return text
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            return data["result"]
    return text


def _base_tool(name: str) -> str | None:
    """``llm_edit`` for ``mcp__llm_router__llm_edit`` (any router MCP server), else None."""
    parts = (name or "").split("__")
    if len(parts) >= 3 and parts[0] == "mcp" and "router" in parts[1]:
        return parts[-1]
    return None


def _input_text(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    return "\n".join(v for v in tool_input.values() if isinstance(v, str))[:4000]


@dataclass
class _Rec:
    i: int
    turn: int  # human turns seen up to and including this record
    rec: dict
    human: str | None = None


@dataclass
class Event:
    event_id: str
    tool: str
    kind: str
    rec_i: int
    turn: int
    ts: float | None
    tool_input: dict = field(default_factory=dict)
    result: str | None = None
    result_error: bool = False


def _ts(rec: dict) -> float | None:
    raw = rec.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _annotate(records: list[dict]) -> list[_Rec]:
    out, turn = [], 0
    for i, rec in enumerate(records):
        human = _human_text(rec)
        if human is not None:
            turn += 1
        out.append(_Rec(i, turn, rec, human))
    return out


def _events(recs: list[_Rec]) -> list[Event]:
    # A sub-agent's own tool calls are not the main thread's routed decision: counting
    # them here would judge a routed call (or a redo) against work the human never saw
    # as "the" turn. Same exclusion as _human_text, applied consistently (CHZ verifier
    # finding, 2026-10-03): this function and _claude_actions previously did not filter it.
    recs = [r for r in recs if not r.rec.get("isSidechain")]
    results: dict[str, tuple[str, bool]] = {}
    for r in recs:
        if r.rec.get("type") != "user":
            continue
        for b in _content_blocks(r.rec):
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                results[b["tool_use_id"]] = (_unwrap(_result_text(b)), bool(b.get("is_error")))
    events = []
    for r in recs:
        for tu in _tool_uses(r.rec):
            base = _base_tool(tu.get("name") or "")
            if base in _EDIT_TOOLS:
                kind = KIND_EDIT
            elif base in _ANSWER_TOOLS:
                kind = KIND_ANSWER
            else:
                continue
            tid = tu.get("id") or f"rec{r.i}"
            text, err = results.get(tid, (None, False))
            events.append(Event(tid, base, kind, r.i, r.turn, _ts(r.rec),
                                tu.get("input") if isinstance(tu.get("input"), dict) else {},
                                text, err))
    return events


# ── similarity ──────────────────────────────────────────────────────────────

def _words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _grams(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + _SHINGLE_N]) for i in range(len(words) - _SHINGLE_N + 1)}


def _similar(a: str, b: str) -> bool:
    """Near-identical prompts: exact for short ones, 8-gram containment otherwise."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    if len(wa) < _SHINGLE_N or len(wb) < _SHINGLE_N:
        return wa == wb
    ga, gb = _grams(wa), _grams(wb)
    return len(ga & gb) / min(len(ga), len(gb)) >= _SIMILAR_AT


def _reuse(source: str, candidate: str) -> float:
    src = _grams(_words(source))
    if not src:
        return 0.0
    return len(src & _grams(_words(candidate))) / len(src)


# ── edit judging ────────────────────────────────────────────────────────────

def _pairs(result: str) -> list[dict]:
    """The ``{file, old_string, new_string}`` pairs from an ``llm_edit`` result."""
    for m in reversed(_JSON_BLOCK_RE.findall(result or "")):
        try:
            data = json.loads(m)
        except ValueError:
            continue
        if isinstance(data, list):
            return [p for p in data if isinstance(p, dict) and p.get("file")
                    and isinstance(p.get("new_string"), str)]
    return []


def _same_file(a: str, b: str) -> bool:
    a, b = (a or "").replace("\\", "/").strip(), (b or "").replace("\\", "/").strip()
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b.lstrip("./")) or b.endswith("/" + a.lstrip("./"))


def _claude_actions(window: list[_Rec]) -> list[dict]:
    acts = []
    for r in window:
        if r.rec.get("isSidechain"):
            continue
        for tu in _tool_uses(r.rec):
            name, inp = tu.get("name") or "", tu.get("input") or {}
            if name in ("Edit", "NotebookEdit"):
                acts.append({"op": "edit", "file": inp.get("file_path") or inp.get("notebook_path") or "",
                             "edits": [(inp.get("old_string") or "", inp.get("new_string") or "")]})
            elif name == "MultiEdit":
                acts.append({"op": "edit", "file": inp.get("file_path") or "",
                             "edits": [(e.get("old_string") or "", e.get("new_string") or "")
                                       for e in inp.get("edits") or [] if isinstance(e, dict)]})
            elif name == "Write":
                acts.append({"op": "write", "file": inp.get("file_path") or "",
                             "content": inp.get("content") or ""})
            elif name == "Bash" and _GIT_UNDO_RE.search(inp.get("command") or ""):
                acts.append({"op": "git_undo", "file": "", "command": inp.get("command") or ""})
    return acts


def _applies(act: dict, new: str) -> bool:
    if act["op"] == "edit":
        return any(n == new for _o, n in act["edits"])
    return act["op"] == "write" and new in act["content"]


def _redoes(act: dict, pair: dict) -> str | None:
    """Reason this later action redoes ``pair`` after it was applied, else None."""
    new, old = pair["new_string"], pair.get("old_string") or ""
    if act["op"] == "write":
        return "rewritten"
    if act["op"] == "git_undo":
        cmd, base = act["command"], Path(pair["file"]).name
        broad = re.search(r"\bgit\s+(?:checkout|restore)\s+(?:--\s+)?\.(?:\s|$)|\bgit\s+(?:revert|reset)\b", cmd)
        return "reverted" if (base and base in cmd) or broad else None
    for o, n in act["edits"]:
        if len(old.strip()) >= _MIN_FRAGMENT and old in n and n != new:
            return "reverted"
        if len(o.strip()) >= _MIN_FRAGMENT and (o in new or new in o):
            return "re_edited"
    return None


def _judge_edit(ev: Event, window: list[_Rec]) -> tuple[str, str]:
    pairs = _pairs(ev.result or "")
    if not pairs:
        return OUTCOME_UNKNOWN, "no_pairs"
    acts = _claude_actions(window)
    n_applied = 0
    for pair in pairs:
        file_acts = [(k, a) for k, a in enumerate(acts)
                     if a["op"] == "git_undo" or _same_file(a["file"], pair["file"])]
        applied_at = next((k for k, a in file_acts if a["op"] != "git_undo"
                           and _applies(a, pair["new_string"])), None)
        if applied_at is None:
            touched = any(a["op"] != "git_undo" for _k, a in file_acts)
            if touched:
                return OUTCOME_REDONE, "edited_differently"
            continue
        n_applied += 1
        for k, a in file_acts:
            if k > applied_at:
                why = _redoes(a, pair)
                if why:
                    return OUTCOME_REDONE, why
    if n_applied < len(pairs):
        return OUTCOME_UNKNOWN, "partly_applied" if n_applied else "not_applied_seen"
    return OUTCOME_USED, "applied_verbatim"


# ── answer judging ──────────────────────────────────────────────────────────

def _judge_answer(ev: Event, window: list[_Rec], recs: list[_Rec]) -> tuple[str, str]:
    if not (ev.result or "").strip() or ev.result_error:
        return OUTCOME_UNKNOWN, "no_result"
    mine = _input_text(ev.tool_input)
    for r in window:
        if r.human and _OVERRIDE_RE.match(r.human) and not r.human.rstrip().endswith(BAND_REDO_MARK):
            return OUTCOME_REDONE, "overridden"
        for tu in _tool_uses(r.rec):
            if tu.get("id") == ev.event_id:
                continue
            name = tu.get("name") or ""
            if (_base_tool(name) in _ANSWER_TOOLS or name in _REASK_TOOLS) and \
                    _similar(mine, _input_text(tu.get("input"))):
                return OUTCOME_REDONE, "re_asked"
    nxt = next((_assistant_text(r.rec) for r in recs
                if r.i > ev.rec_i and _assistant_text(r.rec).strip()), "")
    if _reuse(ev.result or "", nxt) >= _REUSE_AT:
        return OUTCOME_USED, "result_reused"
    return OUTCOME_USED, "not_redone"


# ── public: judge ───────────────────────────────────────────────────────────

def judge_transcript(records: list[dict], *, session_id: str | None = None,
                     kind_lookup=None) -> list[dict]:
    """One verdict row per routed event in ``records`` (parsed transcript lines,
    in file order). Pure: no I/O. See the module docstring for the definition."""
    recs = _annotate(records)
    max_turn = recs[-1].turn if recs else 0
    rows = []
    for ev in _events(recs):
        window = [r for r in recs if r.i > ev.rec_i and r.turn <= ev.turn + WINDOW_TURNS]
        closed = max_turn - ev.turn >= WINDOW_TURNS
        if ev.kind == KIND_EDIT:
            outcome, reason = _judge_edit(ev, window)
        else:
            outcome, reason = _judge_answer(ev, window, recs)
        so_far = reason
        if not closed and outcome == OUTCOME_USED:
            outcome, reason = OUTCOME_UNKNOWN, "window_open"  # a redo could still land
        elif not closed and reason in ("not_applied_seen", "partly_applied"):
            reason = "window_open"
        rows.append({
            "event_id": ev.event_id, "session_id": session_id,
            "session_kind": kind_lookup(session_id) if kind_lookup else None,
            "ts": ev.ts, "tool": ev.tool, "kind": ev.kind,
            "outcome": outcome, "reason": reason, "so_far": so_far,
            "turns_after": min(max_turn - ev.turn, WINDOW_TURNS), "window_closed": closed,
        })
    return rows


# ── ledger ──────────────────────────────────────────────────────────────────

def ledger_path() -> Path:
    return paths.state_path(LEDGER_FILENAME)


def load_ledger(path: Path | None = None) -> dict[str, dict]:
    """Latest row per ``event_id`` (a re-judged event appends; the last one wins)."""
    latest: dict[str, dict] = {}
    try:
        fh = (path or ledger_path()).open("r", encoding="utf-8")
    except OSError:
        return latest
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("event_id"):
                latest[row["event_id"]] = row
    return latest


def record_outcomes(rows: list[dict], path: Path | None = None) -> int:
    """Append verdicts not already recorded. A final verdict (used/redone) is
    never rewritten; an ``unknown`` is re-recorded only when its outcome or
    reason changed. Returns the number of rows written. Never raises."""
    target = path or ledger_path()
    known = load_ledger(target)
    fresh = []
    for row in rows:
        old = known.get(row["event_id"])
        if old is not None and (old.get("outcome") != OUTCOME_UNKNOWN
                                or (old.get("outcome"), old.get("reason")) == (row["outcome"], row["reason"])):
            continue
        fresh.append({**row, "judged_at": time.time()})
    if not fresh:
        return 0
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8", opener=paths.private_opener) as fh:
            for row in fresh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - telemetry must never raise into a caller
        return 0
    return len(fresh)


# ── sweep ───────────────────────────────────────────────────────────────────

def _read_records(path: Path) -> list[dict]:
    out = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


def judge_recent(days: int = 7, root: Path | None = None) -> list[dict]:
    """Verdicts for every routed event in transcripts modified in the last ``days``.
    Read-only. A transcript with no router tool call is skipped without parsing."""
    from llm_router.northstar import claude_projects_dir

    base = root if root is not None else claude_projects_dir()
    cutoff = time.time() - days * 86400
    rows: list[dict] = []
    for path in sorted(base.glob("*/*.jsonl")):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            if "router__" not in path.read_text(encoding="utf-8", errors="replace"):
                continue
        except OSError:
            continue
        rows.extend(judge_transcript(_read_records(path), session_id=path.stem,
                                     kind_lookup=session_kind.kind_of))
    return rows


def sweep(days: int = 7, root: Path | None = None, *, persist: bool = True) -> dict:
    rows = judge_recent(days, root)
    written = record_outcomes(rows) if persist else 0
    counts = {o: sum(1 for r in rows if r["outcome"] == o)
              for o in (OUTCOME_USED, OUTCOME_REDONE, OUTCOME_UNKNOWN)}
    return {"events": len(rows), "written": written, "days": days, **counts}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m llm_router.usage_outcome",
                                 description="Judge routed outputs used/redone/unknown.")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true", help="judge and print; write nothing")
    args = ap.parse_args(argv)
    print(json.dumps(sweep(args.days, persist=not args.dry_run)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
