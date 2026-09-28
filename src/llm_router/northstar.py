"""NS1 — the North Star metric: routed-and-used share, per Claude Code session.

Set by the user 2026-09-27 (``~/Projects/rsi-engine/docs/NORTH_STAR.md``):

    In every Claude Code session, at least 50-70% of (all user prompts + all
    LLM calls in the session, including sub-agent/sidechain and tool-driven
    model calls) are ROUTED SUCCESSFULLY.

    Success = a non-Claude model produced the answer/work AND it was used
    as-is (not discarded, not redone by Claude).

This module counts UNITS, judges which were routed and USED, and reports the
per-session share plus the distribution across sessions. It never reports a
mean alone (CLAUDE.md "Measuring anything": a rate without n is not a
measurement; below ~50 units, say "too few to tell").

``report()`` is computed FROM ``units()`` (see bottom of this docstring) —
there is exactly one pass that classifies a call, and both the per-unit view
and the aggregate view read it, so they cannot disagree with each other.
``tests/test_northstar.py::test_report_matches_units`` pins this.

────────────────────────────────────────────────────────────────────────────
COUNTING RULES — read this before changing a number here
────────────────────────────────────────────────────────────────────────────

A **unit** is one of eight kinds. All eight sum to the denominator.

  user_prompt     One human-authored prompt in a Claude Code transcript
                  (``~/.claude/projects/<proj>/<session>.jsonl``, ``type ==
                  "user"``). Uses ``scripts/groundtruth/sources.py`` verbatim
                  for what counts as "human-authored": ``classify_drop``
                  excludes tool-result echoes, harness/system-reminder
                  injections, bare attachments, pasted tool output, and
                  degenerate (<3 word) turns. Sessions whose id is synthetic
                  (``is_synthetic_session``) or whose workspace is a
                  benchmark sandbox (``^-(private-)?(tmp|var-folders)-``) are
                  dropped entirely, same as the ground-truth corpus.

  claude_main_call
                  One per ``type == "assistant"`` record in a session's own
                  top-level transcript file — every turn Claude itself
                  produced, whether text, a tool call, or both. This is an
                  "LLM call" even when Claude routed nothing, because the
                  North Star's denominator is every call, not every routed
                  call.

  sidechain_call  One per assistant turn belonging to a SUB-AGENT that has
                  been folded into its parent session (see JOIN below). These
                  are what the North Star calls "sub-agent ... model calls".

  routed_mcp      One per ``tool_use`` block anywhere in an assistant turn
                  (main or folded sidechain) whose tool name starts with
                  ``mcp__llm_router__``. This is a call Claude explicitly
                  routed to a non-Claude backend through the MCP surface.

  draft           One per ``DIRECT SUCCESS`` invocation in
                  ``~/.llm-router/auto-route-debug.log`` for the session: the
                  UserPromptSubmit hook produced a local/free-tier answer
                  BEFORE Claude's turn (a "draft" Claude may relay or
                  discard). Matched to a session by the debug log's 8-hex
                  ``session_id=`` prefix (the log only ever carries the
                  prefix; ``session_id[:8]`` is the join key back to the
                  full transcript UUID).

  direct          One per ``DIRECT SUCCESS`` invocation that ran with
                  zero-Claude replacement active (``LLM_ROUTER_ZERO_CLAUDE``)
                  — the routed answer stood in for Claude's turn entirely,
                  detected from the debug log carrying a ``ZERO_CLAUDE *``
                  annotation in the same invocation. Rare: zero-Claude is off
                  by default and the 2026-09 workload study found it firing on
                  well under 1% of days. Expect this bucket to be 0 on most
                  machines, honestly.

  routed_edit     Lever ``llm_edit`` (#181). One unit per ROW in
                  ``~/.llm-router/edit_outcomes.jsonl``
                  (``llm_router.edit_ledger.record_edit_outcome``) — i.e. one
                  per FILE an ``llm_edit`` call touched, not one per call: the
                  ledger is written per file (a 3-file call writes 3 rows) and
                  survival is judged per file, so that is the natural grain.
                  See OUTCOME below and the DEDUP section for how this avoids
                  double-counting the transcript's own ``routed_mcp`` unit for
                  the same call.

  agent_route_codex
                  Lever ``agent_route_codex`` (#184). One unit per ROW in
                  ``~/.llm-router/north_star_units.jsonl``
                  (``hooks/agent-route.py``'s ``_record_north_star_unit``)
                  whose recorded ``outcome`` reflects an actual Codex
                  invocation: ``delegated`` (Codex ran and its output stood in
                  for the sub-agent's result) or ``codex_failed`` (Codex was
                  invoked but produced nothing usable). Rows recording a
                  decision NOT to invoke Codex at all — ``unsuitable``,
                  ``budget_exhausted``, ``codex_unavailable`` — are not
                  attempts and produce no unit, the same way "the router chose
                  not to draft" produces no ``draft`` unit above.

``routed_mcp``, ``draft``, ``direct``, ``routed_edit`` and ``agent_route_codex``
are **attempted routing** whether or not they were used.
``claude_main_call``/``sidechain_call``/``user_prompt`` are never "attempted"
by definition, and their per-unit ``outcome`` is always ``not_routed`` — they
are the rest of the denominator the North Star insists on counting.

────────────────────────────────────────────────────────────────────────────
DEDUP: routed_edit vs. the transcript's own routed_mcp unit
────────────────────────────────────────────────────────────────────────────

An ``llm_edit`` call is BOTH a ``mcp__llm_router__llm_edit`` tool_use in the
transcript (which ``routed_mcp`` construction, above, would otherwise count
generically like any other MCP tool call) AND one-or-more rows in
``edit_outcomes.jsonl`` (richer: ``applied``/``survived`` ground truth instead
of the shingle-overlap heuristic). Counting both would double-count the same
call.

JOIN KEY: a ledger row is attributed to the LATEST ``mcp__llm_router__llm_edit``
tool_use in the same (raw, pre-fold) session whose transcript timestamp is
``<= row["ts"]`` and within ``EDIT_LEDGER_JOIN_WINDOW_S`` (300s) of it.
``edit_ledger.record_edit_outcome`` writes ``ts = time.time()`` from inside
the MCP call handler, strictly AFTER the tool_use block was already appended
to the transcript (the assistant turn is written when Claude emits the call,
before the call executes) — so the matching call's ts is always ``<=`` the
row's ts, and "latest such call" is "the call this row's file result belongs
to." When a match is found, the generic ``routed_mcp`` unit for that
tool_use is dropped from the session and replaced by the ledger-backed
``routed_edit`` row(s) — counted ONCE, as the richer unit, per the task's
"same call" rule. A ledger row with no matching call in the window is kept as
a standalone ``routed_edit`` unit (nothing to dedup against).

────────────────────────────────────────────────────────────────────────────
JOIN: folding sub-agent sessions into their parent
────────────────────────────────────────────────────────────────────────────

Measured on this machine (2026-09-27, 30-day baseline, n=300 session files):
``isSidechain`` is ``false`` on every one of ~46,000 sampled assistant
records. Sub-agent turns do **not** appear inline in the parent's transcript;
each sub-agent (Agent tool spawn) writes its own top-level session file — one
dispatch prompt followed by dozens of assistant turns — and the transcript
schema carries no ``parentSessionId`` field.

The cross-session record ``_load_agent_calls()`` builds the candidate pool
from is the UNION of two files ``hooks/agent-route.py`` writes from the same
entry at spawn time: ``~/.llm-router/agent_calls.json`` (rolling, capped at
the last 50 calls — the #180 join's only source, and on a real machine that
window turns over within hours of active use, ~24 rows measured 2026-09-27)
and ``~/.llm-router/agent_calls_ledger.jsonl`` (append-only, pruned by AGE not
count — every spawn for the last 30 days). Deduplicated on
``(session_id, timestamp, prompt)`` since both files are written from the
same dict and carry no other id. Each entry: ``{timestamp, subagent_type,
prompt, decision, session_id}`` where ``session_id`` is the PARENT's. It
carries no child id either, so the join is a heuristic, best-effort match
(``find_parent_session``):

  a candidate child session C is the child of a logged spawn S iff
    * C's first user-turn text, normalised (whitespace-collapsed), starts
      with S["prompt"] normalised (the logged prompt is truncated, so this
      is a prefix match, not equality), AND
    * C's first user-turn timestamp is within ``_JOIN_WINDOW_S`` (15 min)
      after ``S["timestamp"]``, AND
    * S["session_id"] != C (a session cannot spawn itself).

When a match is found, C's assistant turns count as ``sidechain_call`` (not
``claude_main_call``) under the PARENT's session, and C's own dispatch
message is excluded from ``user_prompt`` (it is an orchestration instruction,
not something a human typed) — but any FURTHER human turns in C still count.

**When no match is found, the session is reported as its own bucket** — its
dispatch prompt counts as a ``user_prompt`` and its turns as
``claude_main_call``, same as an ordinary top-level session. This is stated
here explicitly per instruction: it is NOT silently double-counted, but it is
also not confidently folded. There is no reliable signal to flag an unjoined
session as "probably a sub-agent anyway" without the join succeeding.

────────────────────────────────────────────────────────────────────────────
OUTCOME signals — each named, each with a stated confidence
────────────────────────────────────────────────────────────────────────────

Every unit gets exactly one ``outcome``:
``used`` | ``redo`` | ``discarded`` | ``unknown`` | ``not_routed``.
Only ``used`` counts toward the North Star's numerator. ``redo`` and
``discarded`` are both confirmed failures (kept distinct for finer-grained
consumers — see ``units()`` — but folded together into the ``report()``
schema's single ``redo`` count, since that schema has no separate
``discarded`` field). ``unknown`` is counted separately and NEVER folded into
either side: "unknown renders as the favourable answer" is a defect this
repo's CLAUDE.md has already caught five times.

(a) draft_verdict (confidence: HIGH — the hook's own recorded verdict).
    ``hooks/draft_usage.py`` already judges every draft at the NEXT
    invocation and writes ``DRAFT USED`` / ``DRAFT UNUSED`` into
    auto-route-debug.log (``routing_report._ANNOTATIONS``). Read directly:
    USED -> ``used``, UNUSED -> ``discarded`` (the hook is confident it was
    not relayed; that is not the same claim as "Claude visibly redid it",
    hence ``discarded`` rather than ``redo``).

(b) attribution_reconstructed (confidence: MEDIUM — used only when (a) has
    no verdict for this invocation, e.g. the debug log rotated past it).
    Reimplements ``draft_usage.draft_was_relayed``: the assistant turn
    immediately following the draft opens (first non-blank line) with
    ``"🎯 LLM Router routed"`` or ``"🎯 llm_router →"``. Same used/discarded
    mapping as (a).

(c) tool_result_reused (confidence: MEDIUM — a lexical-overlap heuristic,
    not a semantic one). For a ``routed_mcp`` call: take the 8-gram word
    shingles of the MCP tool_result text and of Claude's next substantive
    assistant text; call it USED when at least ``TOOL_REUSE_THRESHOLD``
    (0.60) of the tool result's shingles also appear in that next text. 0.60
    was picked, not measured, to demand a clear majority of the routed
    content survive verbatim while tolerating light rewording/formatting —
    lower would credit "used" to answers Claude substantially rewrote
    (closer to a redo), higher would fail on cosmetic edits alone. Below the
    threshold: ``redo`` if the following turn shows Claude doing further
    substantive work (its own text, or an Edit/Write/NotebookEdit call —
    evidence it kept working rather than just moving on), else ``unknown``.

(d) zero_claude_stood (confidence: MEDIUM). A ``direct`` unit is ``used``
    unless the very next user prompt in the transcript is prefixed
    ``claude:``/``native:``/``opus:`` (``_EXPLICIT_CLAUDE_PREFIX_RE`` in
    ``hooks/auto-route.py``) — the user's own escape hatch for "that answer
    was not good enough, ask Claude directly instead". That prefix on the
    following prompt is ``redo``, not ``unknown``: the user has told us, in
    the only channel available under zero-Claude, that the routed answer
    did not stand.

────────────────────────────────────────────────────────────────────────────
Share
────────────────────────────────────────────────────────────────────────────

    share            = used / units                      (the headline)
    attempted_share  = attempted / units                 (routed, used or not)
    unknown          = count, reported separately, never folded into share

Below ``MIN_UNITS`` (50) units in a session, the session is still listed but
its share renders as ``None`` / "too few to tell" per CLAUDE.md.

────────────────────────────────────────────────────────────────────────────
Public surface
────────────────────────────────────────────────────────────────────────────

``units(days=N, session_id=None, root=None) -> Iterator[dict]``
    One dict per unit: ``{"session_id", "ts" (iso8601 or None), "kind",
    "lever", "task_type", "model", "outcome", "signal"}``. ``lever`` is
    "drafts" | "mcp_llm" | "direct" | "agent_route" | "llm_edit" |
    "agent_route_codex" | "none" (the mechanism that produced the unit, for
    NS3/NS4 to slice by — NS4's quality breaker keys on ``(lever,
    task_type)``, so every unit of every lever, including these two, carries
    both fields).

``report(days=N, session_id=None, root=None) -> dict``
    Computed by aggregating ``units()`` — see the schema in its own
    docstring below.
"""

from __future__ import annotations

import glob
import json
import os
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from llm_router import paths

MIN_UNITS = 50
TOOL_REUSE_THRESHOLD = 0.60
_JOIN_WINDOW_S = 900.0  # 15 minutes: spawn -> child's first turn
EDIT_LEDGER_JOIN_WINDOW_S = 300.0  # 5 minutes: llm_edit tool_use -> its ledger row(s)

UNIT_USER_PROMPT = "user_prompt"
UNIT_CLAUDE_MAIN = "claude_main_call"
UNIT_SIDECHAIN = "sidechain_call"
UNIT_ROUTED_MCP = "routed_mcp"
UNIT_DRAFT = "draft"
UNIT_DIRECT = "direct"
UNIT_ROUTED_EDIT = "routed_edit"
UNIT_AGENT_ROUTE_CODEX = "agent_route_codex"
ALL_KINDS = (
    UNIT_USER_PROMPT, UNIT_CLAUDE_MAIN, UNIT_SIDECHAIN,
    UNIT_ROUTED_MCP, UNIT_DRAFT, UNIT_DIRECT,
    UNIT_ROUTED_EDIT, UNIT_AGENT_ROUTE_CODEX,
)
ATTEMPTED_KINDS = frozenset({
    UNIT_ROUTED_MCP, UNIT_DRAFT, UNIT_DIRECT, UNIT_ROUTED_EDIT, UNIT_AGENT_ROUTE_CODEX,
})

_LEVER_OF_KIND = {
    UNIT_USER_PROMPT: "none",
    UNIT_CLAUDE_MAIN: "none",
    UNIT_SIDECHAIN: "agent_route",
    UNIT_ROUTED_MCP: "mcp_llm",
    UNIT_DRAFT: "drafts",
    UNIT_DIRECT: "direct",
    UNIT_ROUTED_EDIT: "llm_edit",
    UNIT_AGENT_ROUTE_CODEX: "agent_route_codex",
}

# The MCP tool name llm_edit is registered under (server.py / tool_surface.py) —
# the one routed_mcp tool_use name that the edit ledger can dedup against.
_LLM_EDIT_TOOL_NAME = "mcp__llm_router__llm_edit"

OUTCOME_USED = "used"
OUTCOME_REDO = "redo"
OUTCOME_DISCARDED = "discarded"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_NOT_ROUTED = "not_routed"
# report()'s per-session/per-kind "redo" count folds both confirmed-failure
# outcomes together (that schema has no separate "discarded" field).
_REDO_LIKE_OUTCOMES = frozenset({OUTCOME_REDO, OUTCOME_DISCARDED})

RELAY_MARKER_A = "🎯 LLM Router routed"
RELAY_MARKER_B = "🎯 llm_router →"
_EXPLICIT_CLAUDE_PREFIX_RE = re.compile(r"^\s*(?:claude|native|opus)\s*:\s*", re.IGNORECASE)
_TASK_RE = re.compile(r"\btask=([A-Za-z_]+)")
_MODEL_RE = re.compile(r"\bmodel=(\S+)")


def claude_projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECTS_DIR", "").strip()
                or Path.home() / ".claude" / "projects")


# ── ground-truth exclusion rules, reused rather than re-implemented ─────────
# scripts/groundtruth/sources.py is the repo's own owner of these rules
# (CLAUDE.md: "Do not re-implement them"). It lives outside src/ so it is
# imported by path, matching how scripts/routing_rate.py imports its sibling
# package module.
def _load_sources_module():
    import importlib.util
    import sys as _sys
    here = Path(__file__).resolve().parents[2]  # repo root from src/llm_router/
    cand = here / "scripts" / "groundtruth" / "sources.py"
    if not cand.exists():
        return None
    name = "_ns_groundtruth_sources"
    if name in _sys.modules:
        return _sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, cand)
    mod = importlib.util.module_from_spec(spec)
    _sys.modules[name] = mod  # dataclasses' postponed-annotation resolution needs this
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — never fail import over an optional reuse
        _sys.modules.pop(name, None)
        return None
    return mod


_SOURCES = _load_sources_module()


# scripts/northstar/edit_survival.py is the repo's own owner of the
# applied-row -> survived/redone/unknown git-history judgement (see its
# module docstring's METHOD section). Imported by path for the same reason
# as _load_sources_module above: it lives outside src/ and this module must
# not re-derive its own copy of "did a later commit touch this file."
def _load_edit_survival_module():
    import importlib.util
    import sys as _sys
    here = Path(__file__).resolve().parents[2]  # repo root from src/llm_router/
    cand = here / "scripts" / "northstar" / "edit_survival.py"
    if not cand.exists():
        return None
    name = "_ns_edit_survival"
    if name in _sys.modules:
        return _sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, cand)
    mod = importlib.util.module_from_spec(spec)
    _sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — never fail import over an optional reuse
        _sys.modules.pop(name, None)
        return None
    return mod


_EDIT_SURVIVAL = _load_edit_survival_module()


@dataclass
class Unit:
    kind: str
    session_id: str
    ts: float | None = None
    outcome: str = OUTCOME_NOT_ROUTED
    signal: str | None = None
    confidence: str | None = None
    task_type: str | None = None
    model: str | None = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "ts": (datetime.fromtimestamp(self.ts, tz=timezone.utc).isoformat()
                   if self.ts is not None else None),
            "kind": self.kind,
            "lever": _LEVER_OF_KIND.get(self.kind, "none"),
            "task_type": self.task_type,
            "model": self.model,
            "outcome": self.outcome,
            "signal": self.signal,
        }


@dataclass
class SessionUnits:
    session_id: str
    units: list = field(default_factory=list)
    folded_children: list = field(default_factory=list)  # child session ids folded in


# ── transcript reading ──────────────────────────────────────────────────────

def _iter_jsonl(path: Path) -> Iterator[dict]:
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue


def _assistant_text(obj: dict) -> str:
    message = obj.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts)
    return ""


def _assistant_tool_uses(obj: dict) -> list[dict]:
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _user_text_and_tool_results(obj: dict) -> tuple[str | None, list[dict]]:
    """(human text or None, list of tool_result blocks) for a type=='user' record."""
    message = obj.get("message")
    if not isinstance(message, dict):
        return None, []
    content = message.get("content")
    if isinstance(content, str):
        return content, []
    if isinstance(content, list):
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(parts) if parts else None
        return text, results
    return None, []


def _ts_of(obj: dict) -> float | None:
    ts = obj.get("timestamp")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


_WORD_RE = re.compile(r"[a-z0-9]+")


def _shingles(text: str, n: int = 8) -> set[tuple[str, ...]]:
    words = _WORD_RE.findall((text or "").lower())
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def _reuse_fraction(source_text: str, candidate_text: str) -> float:
    src = _shingles(source_text)
    if not src:
        return 0.0
    cand = _shingles(candidate_text)
    return len(src & cand) / len(src)


def _draft_was_relayed(assistant_text: str) -> bool:
    for line in (assistant_text or "").splitlines():
        if not line.strip():
            continue
        return RELAY_MARKER_A in line or RELAY_MARKER_B in line
    return False


# ── auto-route-debug.log: draft / direct units + their verdicts ────────────

_LINE_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d) [\d:]+\] \[INVOCATION ([\d.]+)\] (.*)$")
_SESSION_RE = re.compile(r"session_id=(\S*)")


def _debug_log_path() -> Path:
    return paths.state_path("auto-route-debug.log")


def _parse_debug_log(path: Path | None = None) -> dict[str, dict]:
    """One record per invocation id (which is itself a unix timestamp)."""
    path = path if path is not None else _debug_log_path()
    records: dict[str, dict] = {}
    if not path.exists():
        return records
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return records
    for raw in lines:
        m = _LINE_RE.match(raw.rstrip("\n"))
        if not m:
            continue
        _day, iid, rest = m.groups()
        rec = records.setdefault(iid, {"session8": None, "msgs": []})
        rec["msgs"].append(rest)
        s = _SESSION_RE.search(rest)
        if s and s.group(1):
            rec["session8"] = s.group(1)
    return records


def _debug_units_for_session(session_id: str, debug_records: dict[str, dict]) -> list[Unit]:
    prefix = session_id[:8]
    items = sorted(
        ((float(iid), rec) for iid, rec in debug_records.items() if rec.get("session8") == prefix),
        key=lambda t: t[0],
    )
    units: list[Unit] = []
    for i, (iid, rec) in enumerate(items):
        msgs = rec["msgs"]
        has_success = any("DIRECT SUCCESS" in m for m in msgs)
        if not has_success:
            continue
        zero_claude = any("ZERO_CLAUDE" in m for m in msgs)
        kind = UNIT_DIRECT if zero_claude else UNIT_DRAFT
        joined = "\n".join(msgs)
        tm = _TASK_RE.search(joined)
        mm = _MODEL_RE.search(joined)
        unit = Unit(kind=kind, session_id=session_id, ts=iid,
                     task_type=tm.group(1) if tm else None,
                     model=mm.group(1) if mm else None)

        if kind == UNIT_DRAFT:
            # Signal (a): the hook's own recorded verdict, on the NEXT
            # invocation for this session (draft_usage.audit runs one
            # invocation late).
            if i + 1 < len(items):
                _next_iid, next_rec = items[i + 1]
                next_msgs = next_rec["msgs"]
                if any("DRAFT USED" in m for m in next_msgs):
                    unit.outcome, unit.signal, unit.confidence = OUTCOME_USED, "draft_verdict", "high"
                elif any("DRAFT UNUSED" in m for m in next_msgs):
                    unit.outcome, unit.signal, unit.confidence = OUTCOME_DISCARDED, "draft_verdict", "high"
        units.append(unit)
    return units


def _fill_unresolved_drafts_from_transcript(units: list[Unit], assistant_turns: list[dict]) -> None:
    """Signal (b): reconstruct from the transcript when (a) gave no verdict."""
    if not assistant_turns:
        return
    turns_by_ts = sorted(
        ((_ts_of(o), o) for o in assistant_turns if _ts_of(o) is not None),
        key=lambda t: t[0],
    )
    for u in units:
        if u.kind != UNIT_DRAFT or u.outcome != OUTCOME_NOT_ROUTED:
            continue
        u.outcome = OUTCOME_UNKNOWN  # attempted; default until resolved below
        if u.ts is None:
            continue
        following = next((o for ts, o in turns_by_ts if ts is not None and ts >= u.ts), None)
        if following is None:
            continue
        text = _assistant_text(following)
        if not text:
            continue
        if _draft_was_relayed(text):
            u.outcome, u.signal, u.confidence = OUTCOME_USED, "attribution_reconstructed", "medium"
        else:
            u.outcome, u.signal, u.confidence = OUTCOME_DISCARDED, "attribution_reconstructed", "medium"


def _judge_direct_units(units: list[Unit], records: list[dict]) -> None:
    """Signal (d): zero-Claude replacement stands unless the user re-asks Claude."""
    user_turns = sorted(
        ((_ts_of(o), o) for o in records if o.get("type") == "user" and _ts_of(o) is not None),
        key=lambda t: t[0],
    )
    for u in units:
        if u.kind != UNIT_DIRECT:
            continue
        following_text = None
        for ts, o in user_turns:
            # Strictly AFTER this invocation's own prompt (which shares its ts):
            # the invocation's triggering prompt must not count as its own re-ask.
            if ts is None or u.ts is None or ts <= u.ts:
                continue
            text, _results = _user_text_and_tool_results(o)
            if text is not None:
                following_text = text
                break
        if following_text and _EXPLICIT_CLAUDE_PREFIX_RE.match(following_text):
            u.outcome, u.signal, u.confidence = OUTCOME_REDO, "zero_claude_stood", "medium"
        else:
            u.outcome, u.signal, u.confidence = OUTCOME_USED, "zero_claude_stood", "medium"


# ── llm_edit ledger (#181): richer routed_edit units ────────────────────────

def _edit_outcomes_path() -> Path:
    # Literal filename, same pattern as _debug_log_path above: LEDGER_FILENAME
    # in edit_ledger.py is this string's own source of truth ("edit_outcomes.jsonl").
    return paths.state_path("edit_outcomes.jsonl")


def _load_edit_outcomes() -> list[dict]:
    """All rows of ``edit_outcomes.jsonl`` (session/day filtering happens by
    the caller, same pattern as ``_parse_debug_log``)."""
    path = _edit_outcomes_path()
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _judge_edit_row(row: dict) -> tuple[str, str]:
    """(outcome, signal) for one edit_outcomes.jsonl row, using the SAME
    applied/survived logic as ``scripts/northstar/edit_survival.py`` — not a
    second copy of it (see ``_load_edit_survival_module``)."""
    if not row.get("applied"):
        return OUTCOME_DISCARDED, "edit_ledger_not_applied"
    if _EDIT_SURVIVAL is None:
        return OUTCOME_UNKNOWN, "edit_ledger_survival_unavailable"
    verdict = _EDIT_SURVIVAL.judge_row(row)
    if verdict.verdict == "survived":
        return OUTCOME_USED, "edit_ledger_survived"
    if verdict.verdict == "redone":
        return OUTCOME_REDO, "edit_ledger_redone"
    return OUTCOME_UNKNOWN, "edit_ledger_unresolved"  # verdict.verdict == "unknown"


def _fold_edit_ledger(su: SessionUnits, edit_call_units: list[Unit], rows: list[dict]) -> None:
    """Turn this session's edit_outcomes.jsonl rows into routed_edit units,
    and drop the generic routed_mcp unit for any llm_edit tool_use a row
    matched — see the module docstring's DEDUP section for the join key.

    ``edit_call_units`` are the UNIT_ROUTED_MCP Unit objects already appended
    to ``su.units`` for ``mcp__llm_router__llm_edit`` tool_use blocks in this
    (raw, pre-fold) session's own transcript file.
    """
    matched_call_ids: set[int] = set()
    for row in rows:
        row_ts = row.get("ts")
        if not isinstance(row_ts, (int, float)):
            row_ts = None
        best: Unit | None = None
        if row_ts is not None:
            for call in edit_call_units:
                if call.ts is None or call.ts > row_ts:
                    continue
                if row_ts - call.ts > EDIT_LEDGER_JOIN_WINDOW_S:
                    continue
                if best is None or call.ts > best.ts:  # type: ignore[operator]
                    best = call
        outcome, signal = _judge_edit_row(row)
        su.units.append(Unit(
            kind=UNIT_ROUTED_EDIT, session_id=su.session_id, ts=row_ts,
            outcome=outcome, signal=signal,
            confidence="high" if outcome != OUTCOME_UNKNOWN else "medium",
            task_type="code", model=row.get("model"),
        ))
        if best is not None:
            matched_call_ids.add(id(best))
    if matched_call_ids:
        su.units[:] = [u for u in su.units if id(u) not in matched_call_ids]


# ── agent-route Codex delegation ledger (#184): agent_route_codex units ────

def _north_star_units_path() -> Path:
    return paths.state_path("north_star_units.jsonl")


def _load_north_star_ledger() -> list[dict]:
    """All rows of ``north_star_units.jsonl`` (``hooks/agent-route.py``'s
    ``_record_north_star_unit``). Same read pattern as ``_load_edit_outcomes``."""
    path = _north_star_units_path()
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


_CODEX_ATTEMPT_OUTCOMES = {
    "delegated": (OUTCOME_USED, "agent_route_codex_delegated"),
    "codex_failed": (OUTCOME_DISCARDED, "agent_route_codex_failed"),
}


def _agent_route_codex_units_for_session(sid: str, rows: list[dict]) -> list[Unit]:
    """One unit per north_star_units.jsonl row for THIS session whose
    recorded outcome reflects an actual Codex invocation (see the module
    docstring's kind description for why decision-only rows — unsuitable,
    budget_exhausted, codex_unavailable — are skipped, not counted)."""
    units: list[Unit] = []
    for row in rows:
        if row.get("session_id") != sid or row.get("lever") != "agent_route_codex":
            continue
        mapped = _CODEX_ATTEMPT_OUTCOMES.get(row.get("outcome"))
        if mapped is None:
            continue
        outcome, signal = mapped
        units.append(Unit(
            kind=UNIT_AGENT_ROUTE_CODEX, session_id=sid, ts=row.get("ts"),
            outcome=outcome, signal=signal, confidence="high",
            task_type=row.get("task_type"), model=row.get("model") or None,
        ))
    return units


# ── sub-agent join ──────────────────────────────────────────────────────────

def _load_agent_calls() -> list[dict]:
    """Union of ``agent_calls.json`` (50-cap) and ``agent_calls_ledger.jsonl``
    (30-day append-only) — see the module docstring's JOIN section for why
    the ledger is unioned in. Deduplicated on ``(session_id, timestamp,
    prompt)``: both files are appended from the identical entry dict at spawn
    time (``hooks/agent-route.py``'s ``_log_agent_call``), and neither carries
    any other id.
    """
    calls: list[dict] = []
    seen: set[tuple] = set()

    def _add_all(entries) -> None:
        for c in entries:
            if not isinstance(c, dict):
                continue
            key = (c.get("session_id"), c.get("timestamp"), c.get("prompt"))
            if key in seen:
                continue
            seen.add(key)
            calls.append(c)

    p = paths.state_path("agent_calls.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        _add_all(data.get("calls") if isinstance(data, dict) else [])
    except (OSError, ValueError):
        pass

    lp = paths.state_path("agent_calls_ledger.jsonl")
    try:
        lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        _add_all([row])

    return calls


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def find_parent_session(child_first_text: str, child_first_ts: float | None,
                         child_session_id: str, agent_calls: list[dict]) -> str | None:
    """Best-effort join: child -> the parent that spawned it, or None.

    See the module docstring's JOIN section for the exact rule and its known
    limitation (no child id is ever logged, so this is a heuristic).
    """
    if child_first_ts is None or not child_first_text:
        return None
    child_norm = _norm(child_first_text)
    best: tuple[float, str] | None = None
    for call in agent_calls:
        sid = call.get("session_id")
        prompt = call.get("prompt")
        ts = call.get("timestamp")
        if not sid or sid == child_session_id or not isinstance(prompt, str):
            continue
        if not isinstance(ts, (int, float)):
            continue
        delta = child_first_ts - ts
        if not (0 <= delta <= _JOIN_WINDOW_S):
            continue
        prompt_norm = _norm(prompt)
        if not prompt_norm or not child_norm.startswith(prompt_norm[:min(len(prompt_norm), 120)]):
            continue
        if best is None or delta < best[0]:
            best = (delta, sid)
    return best[1] if best else None


# ── per-session assembly ─────────────────────────────────────────────────────

def _session_files(root: Path | None = None) -> list[Path]:
    root = root if root is not None else claude_projects_dir()
    return sorted(Path(p) for p in glob.glob(str(root / "*" / "*.jsonl")))


def _first_user_turn(records: list[dict]) -> tuple[str | None, float | None]:
    for obj in records:
        if obj.get("type") != "user":
            continue
        text, _results = _user_text_and_tool_results(obj)
        if text is None:
            continue
        return text, _ts_of(obj)
    return None, None


def build_sessions(days: int | None, root: Path | None = None,
                    session_id: str | None = None) -> dict[str, SessionUnits]:
    """Assemble every real, non-synthetic session in the last `days` days.

    Sub-agent sessions that join to a parent are folded in (their turns
    become sidechain_call units under the parent, their dispatch prompt is
    NOT counted as a user_prompt). Unjoined sessions are reported standalone.
    """
    root = root if root is not None else claude_projects_dir()
    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400

    files = _session_files(root)
    agent_calls = _load_agent_calls()
    debug_records = _parse_debug_log()
    edit_outcome_rows = _load_edit_outcomes()
    codex_ledger_rows = _load_north_star_ledger()

    per_session: dict[str, SessionUnits] = {}
    parent_of: dict[str, str] = {}  # child session_id -> parent session_id

    loaded: dict[str, list[dict]] = {}
    sandbox_of: dict[str, bool] = {}
    for path in files:
        sid = path.stem
        sandbox = bool(_SOURCES and _SOURCES._SANDBOX_PROJECT.match(path.parent.name))
        sandbox_of[sid] = sandbox
        if _SOURCES and _SOURCES.is_synthetic_session(sid):
            continue
        if sandbox:
            continue
        mtime = path.stat().st_mtime if path.exists() else None
        if cutoff is not None and mtime is not None and mtime < cutoff:
            continue
        loaded[sid] = list(_iter_jsonl(path))

    for sid, records in loaded.items():
        first_text, first_ts = _first_user_turn(records)
        parent = find_parent_session(first_text or "", first_ts, sid, agent_calls)
        if parent and parent in loaded:
            parent_of[sid] = parent

    def _root_of(s: str) -> str:
        seen = set()
        while s in parent_of and s not in seen:
            seen.add(s)
            s = parent_of[s]
        return s

    for sid in loaded:
        per_session.setdefault(_root_of(sid), SessionUnits(session_id=_root_of(sid)))

    for sid, records in loaded.items():
        target_sid = _root_of(sid)
        is_child = sid != target_sid
        su = per_session[target_sid]
        if is_child:
            su.folded_children.append(sid)

        assistant_turns = [o for o in records if o.get("type") == "assistant"]
        skip_first_user = is_child  # dispatch prompt is not a human user_prompt

        first_user_seen = False
        edit_call_units: list[Unit] = []
        for obj in records:
            if obj.get("type") == "user":
                text, _results = _user_text_and_tool_results(obj)
                if text is None:
                    continue
                is_dispatch = skip_first_user and not first_user_seen
                first_user_seen = True
                if is_dispatch:
                    continue
                drop_reason = None
                if _SOURCES:
                    drop_reason = _SOURCES.classify_drop(text, sid, sandbox_of.get(sid, False))
                if drop_reason:
                    continue
                su.units.append(Unit(kind=UNIT_USER_PROMPT, session_id=target_sid, ts=_ts_of(obj)))
            elif obj.get("type") == "assistant":
                kind = UNIT_SIDECHAIN if is_child else UNIT_CLAUDE_MAIN
                su.units.append(Unit(kind=kind, session_id=target_sid, ts=_ts_of(obj)))
                for tu in _assistant_tool_uses(obj):
                    name = tu.get("name") or ""
                    if not name.startswith("mcp__llm_router__"):
                        continue
                    tool_input = tu.get("input") if isinstance(tu.get("input"), dict) else {}
                    mcp_unit = Unit(
                        kind=UNIT_ROUTED_MCP, session_id=target_sid, ts=_ts_of(obj),
                        outcome=OUTCOME_UNKNOWN,
                        task_type=tool_input.get("task") if isinstance(tool_input, dict) else None,
                    )
                    su.units.append(mcp_unit)
                    if name == _LLM_EDIT_TOOL_NAME:
                        edit_call_units.append(mcp_unit)

        _judge_routed_mcp(su, records)

        draft_units = _debug_units_for_session(sid, debug_records)
        _fill_unresolved_drafts_from_transcript(draft_units, assistant_turns)
        _judge_direct_units(draft_units, records)
        su.units.extend(draft_units)

        su.units.extend(_agent_route_codex_units_for_session(sid, codex_ledger_rows))

        edit_rows_for_sid = [r for r in edit_outcome_rows if r.get("session_id") == sid]
        if edit_rows_for_sid:
            _fold_edit_ledger(su, edit_call_units, edit_rows_for_sid)

    if session_id:
        per_session = {k: v for k, v in per_session.items() if k == session_id}
    return per_session


def _judge_routed_mcp(su: SessionUnits, records: list[dict]) -> None:
    """Signal (c): shingle-overlap reuse for each routed_mcp unit in this session."""
    tool_use_ids = set()
    for obj in records:
        if obj.get("type") != "assistant":
            continue
        for tu in _assistant_tool_uses(obj):
            if (tu.get("name") or "").startswith("mcp__llm_router__") and tu.get("id"):
                tool_use_ids.add(tu["id"])

    result_text_by_use_id: dict[str, str] = {}
    for obj in records:
        if obj.get("type") != "user":
            continue
        _text, results = _user_text_and_tool_results(obj)
        for r in results:
            use_id = r.get("tool_use_id")
            if use_id in tool_use_ids:
                result_text_by_use_id[use_id] = _tool_result_text(r)

    assistant_by_ts = sorted(
        ((_ts_of(o), o) for o in records if o.get("type") == "assistant" and _ts_of(o) is not None),
        key=lambda t: t[0],
    )

    routed_units = [u for u in su.units if u.kind == UNIT_ROUTED_MCP]
    idx = 0
    for obj in records:
        if obj.get("type") != "assistant":
            continue
        for tu in _assistant_tool_uses(obj):
            name = tu.get("name") or ""
            if not name.startswith("mcp__llm_router__"):
                continue
            if idx >= len(routed_units):
                break
            unit = routed_units[idx]
            idx += 1
            use_id = tu.get("id")
            result_text = result_text_by_use_id.get(use_id, "")
            call_ts = _ts_of(obj)
            following = next(
                (o for ts, o in assistant_by_ts if ts is not None and call_ts is not None and ts > call_ts),
                None,
            )
            if not result_text or following is None:
                continue  # leave as unknown: no evidence either way
            next_text = _assistant_text(following)
            has_further_work = bool(next_text.strip()) or any(
                (b.get("name") or "") in ("Edit", "Write", "NotebookEdit")
                for b in _assistant_tool_uses(following)
            )
            frac = _reuse_fraction(result_text, next_text)
            if frac >= TOOL_REUSE_THRESHOLD:
                unit.outcome, unit.signal, unit.confidence = OUTCOME_USED, "tool_result_reused", "medium"
            elif has_further_work:
                unit.outcome, unit.signal, unit.confidence = OUTCOME_REDO, "tool_result_reused", "medium"
            # else: leave OUTCOME_UNKNOWN


# ── public surface ───────────────────────────────────────────────────────────

def units(days: int | None = 30, session_id: str | None = None,
          root: Path | None = None) -> Iterator[dict]:
    """One dict per classified unit. See the module docstring for the shape."""
    sessions = build_sessions(days=days, root=root, session_id=session_id)
    for sid in sorted(sessions):
        su = sessions[sid]
        ordered = sorted(su.units, key=lambda u: (u.ts is None, u.ts if u.ts is not None else 0.0))
        for u in ordered:
            yield u.to_dict()


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    k = (len(vals) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def report(days: int | None = 30, session_id: str | None = None, root: Path | None = None) -> dict:
    """The northstar report, computed from ``units()`` so the two cannot disagree.

    {"window_days": N, "generated_at": iso8601,
     "aggregate": {"n_sessions": int, "median": float|null, "p25": float|null,
                   "max": float|null, "too_few": bool},
     "sessions": [{"session_id", "units", "used", "attempted", "unknown",
                   "redo", "share"}, ...],
     "by_kind": {"<kind>": {"units", "attempted", "used", "redo", "unknown"}}}

    ``redo`` here folds both ``redo`` and ``discarded`` unit outcomes
    together (this schema carries no separate "discarded" field; ``units()``
    exposes the finer distinction for consumers that want it).
    """
    per_session_counts: dict[str, dict[str, int]] = {}
    by_kind: dict[str, dict[str, int]] = {
        k: {"units": 0, "attempted": 0, "used": 0, "redo": 0, "unknown": 0} for k in ALL_KINDS
    }

    for u in units(days=days, session_id=session_id, root=root):
        sid = u["session_id"]
        c = per_session_counts.setdefault(
            sid, {"units": 0, "used": 0, "attempted": 0, "unknown": 0, "redo": 0},
        )
        c["units"] += 1
        kind = u["kind"]
        bk = by_kind[kind]
        bk["units"] += 1
        if kind in ATTEMPTED_KINDS:
            c["attempted"] += 1
            bk["attempted"] += 1
            outcome = u["outcome"]
            if outcome == OUTCOME_USED:
                c["used"] += 1
                bk["used"] += 1
            elif outcome in _REDO_LIKE_OUTCOMES:
                c["redo"] += 1
                bk["redo"] += 1
            elif outcome == OUTCOME_UNKNOWN:
                c["unknown"] += 1
                bk["unknown"] += 1

    session_rows = []
    shares = []
    for sid, c in sorted(per_session_counts.items()):
        share = (c["used"] / c["units"]) if c["units"] >= MIN_UNITS and c["units"] else None
        session_rows.append({
            "session_id": sid,
            "units": c["units"],
            "used": c["used"],
            "attempted": c["attempted"],
            "unknown": c["unknown"],
            "redo": c["redo"],
            "share": share,
        })
        if share is not None:
            shares.append(share)

    aggregate = {
        "n_sessions": len(session_rows),
        "median": statistics.median(shares) if shares else None,
        "p25": _percentile(shares, 25) if shares else None,
        "max": max(shares) if shares else None,
        "too_few": len(shares) < 1,
    }
    return {
        "window_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "aggregate": aggregate,
        "sessions": session_rows,
        "by_kind": by_kind,
    }


def current_session_line(session_id: str, root: Path | None = None) -> str:
    """The Stop-line's compact item: 'north star 12% (n=87)' or 'too few to tell'.

    Bounded to a 2-day window: this runs on every Stop event (every turn), and
    an unbounded ``days=None`` scan walks the ENTIRE ~/.claude/projects history
    (2.76s over 30 days / 310 sessions on this machine, measured 2026-09-27,
    and only grows). The current session's own file was just written to, so
    it always clears a 2-day cutoff; the same window also bounds the sub-agent
    join search to recent activity, which is the only activity a live Stop
    hook could plausibly need to fold in.
    """
    try:
        data = report(days=2, session_id=session_id, root=root)
        rows = data["sessions"]
        row = rows[0] if rows else None
        if row is None:
            return "north star: no data"
        n = row["units"]
        if n < MIN_UNITS:
            return f"north star: too few to tell (n={n})"
        pct = row["share"] * 100 if row["share"] is not None else 0.0
        return f"north star {pct:.0f}% (n={n})"
    except Exception:  # noqa: BLE001 — never break a caller that renders a line
        return "north star: unavailable"
