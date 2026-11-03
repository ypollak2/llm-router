#!/usr/bin/env python3
"""Release-time outcome audit: was routed work actually USED? (Phase 0.1)

This is a RELEASE tool, not product code. It runs from
``pre-release-verify.sh``, reads the owner's transcripts and ledgers
read-only, and writes a labelled artifact that ``outcome_gate.py`` judges.
Nothing under ``src/llm_router/`` may import it, and it imports nothing from
``src/`` (``tests/test_outcome_audit_import_boundary.py`` enforces both). The
reason for the second direction: ``northstar.py``'s readers carry the defects
this audit exists to catch (``agent_route_codex`` is ``used`` at dispatch;
``--days`` filters by file mtime; any ``ZERO_CLAUDE`` substring makes a draft
``direct``). Reusing them would audit the product with its own bugs. The
minimal transcript and debug-log parsing is copied here instead, and the two
``scripts/`` modules that are stdlib-only and owned outside ``src/`` are
reused by path: ``scripts/groundtruth/sources.py`` (what counts as a human
prompt, sandbox and synthetic-session rules) and
``scripts/northstar/edit_survival.py`` (git survival of an edited file).

LABELS
------
Every ATTEMPTED routed unit gets exactly one of::

    used       the routed content is in the terminal event, as-is
    corrected  the routed content is in the terminal event, partly rewritten
    redone     the host (or the user) visibly did the same work again
    unused     the routed content was produced and never incorporated
    unknown    no terminal event, or one too ambiguous to call

A unit that was never meant to carry content gets ``not_applicable`` and is
OUTSIDE the denominator (``attempted``): a fixed-reply probe ("Reply OK", the
call a hold's escape-valve text invites) and a router status/admin view. The
allowlist is ``NOT_APPLICABLE_SIGNALS``; ``outcome_gate.py`` rejects any other
signal on such a row, because shrinking the denominator raises the used rate.
The summary carries the rate with the not-applicable units included, so the
exclusion can always be undone by a reader.

AUDIT_VERSION
-------------
The scoring rules below are versioned (``AUDIT_VERSION``). A number from one
version is NOT comparable with a number from another: v2 (2026-11-03) pairs
Codex units by tool_use_id, reads sub-agent transcripts, honours
``session_kind_overrides.json``, scores MCP answers on their body (not the JSON
envelope), counts tool inputs and ``llm_local_task`` file effects as use,
labels probes ``not_applicable``, dedupes replayed rows and follows background
stubs. 15.4.0's recorded 0.229% (2/873) is a v1 number and its transcripts are
purged, so it cannot be re-scored with v2. ``outcome_gate.py`` records the
version and refuses a cross-version comparison unless told how to treat it.

THE RULE: no ``used`` or ``corrected`` without a CONTENT-BEARING terminal
event: the host's reply, the host's own Edit call, or a served proxy turn's
executed tool call, compared against the routed content itself. A dispatch
flag, a PreToolUse door-call (``execution_events.adoption_method =
'door_call'``), a validation flag, git silence on a file, or a flat per-turn
savings credit never counts. ``CONTENT_BEARING_SIGNALS`` is the allowlist the
gate checks every ``used``/``corrected`` row against.

PER KIND (the kinds are northstar's, so the populations line up)
----------------------------------------------------------------
draft / direct  DIRECT SUCCESS invocations in ``auto-route-debug.log``.
    northstar calls one ``direct`` when its log lines contain any
    ``ZERO_CLAUDE`` substring; ``ZERO_CLAUDE_EDIT: not edit-class`` is the
    zero-Claude edit path DECLINING, so most ``direct`` units are ordinary
    drafts. The kind name is kept; the label comes from evidence. The draft
    body is read from the ``hook_additional_context`` attachment the hook
    wrote into the transcript, and compared with the host's reply up to the
    next human prompt (its text plus anything it wrote with Edit/Write):
    relay marker with >= 60% of the draft's 8-word shingles, or >= 60%
    without a marker -> used; relay marker with less -> corrected; 20-60%
    without a marker -> unknown (ambiguous: in the hand check it was shared
    context, not a relay); a relayed draft followed by a ``claude:`` re-ask
    -> redone; a reply that does not carry the draft -> ``unused``
    (a draft the host never relayed is NOT used; 8 of 9 northstar ``used``
    direct units on 2026-09-29 were exactly this).
routed_mcp  every ``mcp__llm_router__*`` tool_use, once per tool_use id (a
    resumed or replayed session repeats history). A fixed-reply probe or a
    status/admin view -> not_applicable. Tool result error -> unused. The
    answer BODY (JSON envelope, header and footer removed) is compared with
    the host's following output, which is its text plus the content it wrote
    plus the inputs of every non-router, non-read tool (a commit message
    relayed into ``git commit`` is use): body shingles >= 60% -> used, 5-60% ->
    unknown (ambiguous; judge-eligible: in the hand check one such overlap was
    the host quoting the result to critique it, another the host adopting part
    of it), < 5% -> unused; a ``claude:`` re-ask -> redone. Shingle size follows
    the body: 8 words from 20 words up, 4 from 8, the whole body for 3-7, and
    under 3 words nothing can be attributed (unknown). ``llm_local_task`` writes
    files itself, so its text is not compared: a tool that wrote files and a
    later host ``git commit|add`` naming its workdir -> used; no effect ->
    unused. A call moved to the background (``moved to the background as task
    X``) is followed to its ``<task-notification>``; with none in the
    transcript the unit is unknown. An ``llm_edit`` call with no ledger row is
    judged like routed_edit from the files named in its result.
routed_edit  one per ``edit_outcomes.jsonl`` row (per file, northstar's
    grain). The ``**With:**`` blocks of the ``llm_edit`` tool result are
    compared with the host's own later Edit/Write calls on that file: exact
    -> used, >= 50% of 4-word shingles -> corrected, a different edit ->
    redone, no edit -> unused. Git survival (edit_survival.py) only sets the
    confidence; it never makes or unmakes a label.
agent_route_codex  ``north_star_units.jsonl`` rows. ``codex_failed`` and
    ``codex_blocked`` -> unused. ``delegated`` is only a dispatch record: the
    Agent tool_result decides. The row is paired with its Agent call by
    ``tool_use_id`` when the ledger has one, otherwise by the PreToolUse:Agent
    hook run that followed the row (317 of 317 on the 16.0.0 window; the old
    "latest unclaimed Agent call within 900 s" mis-paired 31%), and the
    call is looked up in the session AND its ``subagents/*.jsonl`` transcripts
    (a nested spawn lives there). If a Claude sub-agent was launched for that
    id (an "Async agent launched" result, a toolUseResult agentId, or a
    ``subagents/*.meta.json`` naming the id) -> redone; if the result carries
    the Codex text, no sub-agent ran and the parent's output carries >= 60% of
    it with no re-spawn -> used; a re-spawn -> redone; otherwise unknown. BOUND
    (the binding metric, reported apart from ``used``) = the result carries
    the Codex text AND no Claude sub-agent was launched for that id.
proxy  served ``proxy_calls.jsonl`` turns in organic transcripts: every tool
    call got a non-error result -> used; error/rejected/re-ask -> redone.
delegate / bounded_operational  ``routing_quality.jsonl`` rows. They carry
    no session id, so they cannot be scoped to an organic session; they are
    counted under ``unscoped`` and never enter the headline.

SCOPE: organic sessions only: entrypoint ``cli``, workspace not a benchmark
sandbox (``/tmp``, ``/var/folders``), not a worktree, not ``~/.rsi``, not a
synthetic session id, and not named non-organic by the owner's
``session_kind_overrides.json`` (``{sid: {"kind": ..., "reason": ...}}``; the
override wins, as it does for every other reader). A unit is in the window by
ITS OWN timestamp.

OUTPUT: ``outcome_audit_<release>.jsonl``, one row per unit, exactly
``{unit_id, kind, label, signal, confidence, human_reviewed}``, and
``outcome_audit_<release>.summary.json`` (rates with Wilson 95% CIs, n per
kind, "too few to tell" below n=50). No prompt or response text is written.
The default directory is ``<repo>/release-artifacts/outcome-audit/``
(git-ignored), never ``~/.llm-router``.

OPTIONAL JUDGE (off by default): ``--judge ollama/<model>`` asks a local model
to label only the ambiguous remainder. Its labels count only when its
calibration against the 45 hand labels
(``--calibrate-judge PATH``) reaches ``JUDGE_ACTIVE_AGREEMENT`` on n >= 40
(PROPOSED -> ACTIVE); otherwise they are reported in shadow and change
nothing.

    python3 scripts/release/outcome_audit.py --days 30
    python3 scripts/release/outcome_audit.py --days 30 --human-labels hand.jsonl
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: The scoring rules' version. A used rate is only comparable within one version
#: (see the module docstring). Bump it with any change that can move a label.
AUDIT_VERSION = 2
AUDIT_CHANGES = (
    "codex units paired by tool_use_id (ledger) or the PreToolUse:Agent hook run; sub-agent transcripts read",
    "session_kind_overrides.json honoured",
    "routed_mcp scored on the answer body, with tool inputs counted as host output",
    "llm_local_task judged by file effects; probes and status views are not_applicable",
    "replayed tool_use ids deduped; background stubs followed to their task notification",
)
LABELS = ("used", "corrected", "redone", "unused", "unknown")
NOT_APPLICABLE = "not_applicable"
ALL_LABELS = LABELS + (NOT_APPLICABLE,)
KINDS = ("draft", "direct", "routed_mcp", "routed_edit", "agent_route_codex",
         "proxy", "delegate", "bounded_operational")
ROW_KEYS = ("unit_id", "kind", "label", "signal", "confidence", "human_reviewed")
MIN_N = 50  # CLAUDE.md: below ~50, say "too few to tell"

# The ONLY signals allowed to put a unit in the used/corrected numerator. Each
# names a comparison between the routed content and a terminal event.
CONTENT_BEARING_SIGNALS = frozenset({
    "relay_marker",                 # host reply opens with the relay line
    "draft_shingle_reuse",          # host reply carries >=60% of the draft
    "draft_partial_reuse",          # relayed (marker) with <60% of the draft kept
    "tool_result_reused",           # host reply carries >=60% of the MCP result
    "edit_applied_verbatim",        # host's Edit new_string == llm_edit's With block
    "edit_applied_modified",        # host's Edit shares >=50% of the With block
    "codex_output_incorporated",    # parent reply carries the Codex output
    "local_task_effect_kept",       # llm_local_task wrote files; the host then committed its workdir
    "local_task_effect_amended",    # ... after editing one of the files the tool wrote
    "proxy_tool_result_ok",         # served turn's tool calls all executed OK
    "proxy_text_not_reasked",       # served text turn, next human turn not a re-ask
    "human_review",                 # a person read the transcript
})
# Judge signals ("judge:<model>") are content-bearing only while the judge is
# ACTIVE; outcome_gate.py checks that against the summary.
JUDGE_SIGNAL_PREFIX = "judge:"

# Sources the 2026-09-29 audit found inflating "used". Each maps to the signal
# names it would carry if it ever leaked into a row.
INFLATED_SOURCES = {
    "door_call_verified_used": frozenset({"door_call", "route_acknowledged", "verified_used"}),
    "codex_dispatch_flags": frozenset({"agent_route_codex_delegated", "dispatch"}),
    "agentic_flat_credits": frozenset({"agentic_flat_credit", "llm_router-agentic-router"}),
}
FORBIDDEN_USED_SIGNALS = frozenset().union(*INFLATED_SOURCES.values())

AMBIGUOUS_SIGNALS = frozenset({
    "tool_result_ambiguous_overlap", "codex_output_not_visibly_incorporated",
    "draft_partial_overlap_ambiguous",
})

# The ONLY signals allowed on a not_applicable row (outcome_gate.py checks). A
# unit leaves the denominator only for one of these reasons, each a statement
# about the CALL, never about its outcome:
NOT_APPLICABLE_SIGNALS = frozenset({
    "probe_fixed_reply",    # the prompt asks for a fixed reply ("Reply OK"): nothing to relay
    "hold_release_probe",   # the same, issued in the turn after a "Routing directive BLOCKED" hold
    "status_view",          # llm_router_status / _admin / _session: a view, not an answer
})

USED_THRESHOLD = 0.60
PARTIAL_THRESHOLD = 0.20
AMBIGUOUS_FLOOR = 0.05
EDIT_PARTIAL_THRESHOLD = 0.50
AGENT_MATCH_WINDOW_S = 330.0  # last-resort pairing only; the PreToolUse hook's own timeout is 320 s
HOOK_JOIN_BEFORE_S = 2.0      # the hook writes its ledger row BEFORE it emits, so the run's
HOOK_JOIN_AFTER_S = 20.0      # attachment follows the row (16.0.0 window: median 0.39 s, max 5.5 s)
LOCAL_TASK_KEEP_WINDOW_S = 6 * 3600.0
RESPAWN_WINDOW_S = 1800.0
EDIT_JOIN_WINDOW_S = 300.0
DRAFT_ATTACH_WINDOW_S = 600.0
JUDGE_ACTIVE_AGREEMENT = 0.80
JUDGE_MIN_N = 40

RELAY_MARKERS = ("🎯 LLM Router routed", "🎯 llm_router →")
_REASK_RE = re.compile(r"^\s*(?:claude|native|opus)\s*:\s*", re.IGNORECASE)
_CODEX_MARKER = "delegated to Codex"
#: Phrases that mark a delivered Codex result (case-insensitive). The 16.0.0 hook says
#: "Subagent task was delegated to Codex CLI"; the BIND W1 wrapper says "This sub-agent task
#: was run by Codex CLI (...) instead of a Claude sub-agent". A wrapper that keeps none of
#: these cannot be called bound (the hook's own reason text is also compared when the
#: transcript recorded it, which it does not for a deny the harness honoured).
CODEX_RESULT_MARKERS = ("delegated to codex", "run by codex", "result from codex", "codex cli", "codex output",
                        "codex result")
ROUTER_TOOL_PREFIX = "mcp__llm_router__"
#: router tools that return a view of the router, not an answer to relay
NON_CONTENT_TOOLS = frozenset({"llm_router_status", "llm_router_admin", "llm_router_session",
                               "llm_router_agent_start_session"})
#: tools whose inputs are not output the host sends onward
_PASSIVE_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "ToolSearch", "TaskOutput", "TaskStop",
                            "WebSearch", "WebFetch"})
HOLD_MARKER = "Routing directive BLOCKED"
MIN_ATTRIBUTABLE_WORDS = 3
_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_AGENT_TOOLS = ("Agent", "Task")
_SANDBOX_CWD = re.compile(r"^/(private/)?(tmp|var/folders)/")


# ── reused rule modules (stdlib-only leaves, loaded by path) ─────────────────
# Both moved from scripts/ into src/llm_router/ in Phase 0.2d (the wheel ships
# no scripts/, so northstar lost them when installed). They are still loaded by
# PATH, never imported as llm_router: that keeps llm_router/__init__ and every
# product reader out of this process. tests/test_outcome_audit_import_boundary.py
# allows exactly these two paths and pins that they import nothing from llm_router.

def _load_script(rel: str, name: str):
    path = ROOT / rel
    if not path.exists():
        return None
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve annotations through sys.modules
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        sys.modules.pop(name, None)
        return None
    return mod


_SOURCES = _load_script("src/llm_router/groundtruth_sources.py", "_oa_groundtruth_sources")
_EDIT_SURVIVAL = _load_script("src/llm_router/edit_survival.py", "_oa_edit_survival")
if _SOURCES is None:  # the prompt rules are not optional for a release number
    raise ImportError("the outcome audit requires the groundtruth_sources rule module")


# ── small pure helpers ───────────────────────────────────────────────────────

def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    """Wilson 95% interval for k/n; None when n == 0 (no interval, not zero)."""
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


_WORD_RE = re.compile(r"[a-z0-9]+")


def _shingles(text: str, n: int) -> set[tuple[str, ...]]:
    words = _WORD_RE.findall((text or "").lower())
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def reuse_fraction(source: str | None, candidate: str | None, n: int = 8) -> float:
    """Share of `source`'s n-word shingles that also appear in `candidate`."""
    src = _shingles(source or "", n)
    if not src:
        return 0.0
    return len(src & _shingles(candidate or "", n)) / len(src)


def _relayed(host_text: str | None) -> bool:
    for line in (host_text or "").splitlines():
        if line.strip():
            return any(m in line for m in RELAY_MARKERS)
    return False


def _is_reask(text: str | None) -> bool:
    return bool(text) and bool(_REASK_RE.match(text or ""))


def extract_draft(attachment_text: str) -> str:
    """The draft body between the hook's DRAFT separator lines."""
    lines = attachment_text.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("─") and "DRAFT" in s and "END" not in s and start is None:
            start = i + 1
        elif s.startswith("─") and "END" in s and start is not None:
            end = i
            break
    if start is None:
        return attachment_text
    return "\n".join(lines[start:end])


# ── MCP result helpers (pure) ────────────────────────────────────────────────

def _unwrap(result_text: str) -> str:
    try:  # the MCP result is often a JSON envelope {"result": "..."} (or a bare JSON string)
        obj = json.loads(result_text)
        if isinstance(obj, dict) and isinstance(obj.get("result"), str):
            return obj["result"]
        if isinstance(obj, str):
            return obj
    except ValueError:
        pass
    return result_text


# The router wraps an answer: "> 🤖 **model** · N tokens · $x · Nms", sometimes a
# route line or a "_validation: ..._" line, then the answer, then a footer after
# "─────" (llm) or "---\n🔀" (llm_route). Scoring the wrapper scores the router's
# own words; 9 of 113 verbatim relays reached "used" before this was removed.
_HDR_LINE = re.compile(r"^(?:> 🤖 \*\*|> \*\*Total cost|_validation: |\[[A-Z]\] \w+ \(\d+%\))")
_FOOTER_SEPS = ("\n─────\n", "\n---\n🔀")


def answer_body(result_text: str | None) -> str:
    """The answer the router returned, without its JSON envelope, header or footer."""
    lines = _unwrap(result_text or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) and (not lines[i].strip() or _HDR_LINE.match(lines[i])):
        i += 1
    body = "\n".join(lines[i:])
    cut = len(body)
    for sep in _FOOTER_SEPS:
        k = body.rfind(sep)
        if k != -1:
            cut = min(cut, k)
    return body[:cut].strip()


_PROBE_START = re.compile(r"^\s*reply\b", re.IGNORECASE)
_PROBE_END = re.compile(r"\breply\s+(?:with\s+)?[\"']?ok[\"']?[.!]?\s*$", re.IGNORECASE)


def is_probe_prompt(prompt: str | None) -> bool:
    """A prompt that asks for a fixed reply ("Reply OK", "Reply with the single word:
    ready"): there is nothing to relay. Hold-release calls are of this shape (the
    hold's text says "call ANY llm_* tool"). A question mark makes it a real
    question asked in reply-with-one-word form, and a prompt of several lines
    carries context, which a probe never does (0 of the 82 has a newline; median
    11 characters, longest 129). Reproduces R2's 82 ack/ping rows on the 16.0.0
    window (BIND-R2 section 2) and none of its 76 non-probe ones."""
    p = (prompt or "").strip()
    if not p or "?" in p or "\n" in p:
        return False
    if _PROBE_START.match(p):
        return len(p) < 150
    return bool(_PROBE_END.search(p)) and len(p) < 200


def shingle_size(n_words: int) -> int:
    """8 words from 20 words up, 4 from 8, and the whole body below that."""
    return n_words if n_words < 8 else (4 if n_words < 20 else 8)


def body_reuse(body: str, host: str | None) -> tuple[float | None, int]:
    """(share of the body's shingles found in `host`, words in the body). None when
    the body is too short to attribute to anything (under 3 words)."""
    words = len(_WORD_RE.findall((body or "").lower()))
    if words < MIN_ATTRIBUTABLE_WORDS:
        return None, words
    return reuse_fraction(body, host, shingle_size(words)), words


def _walk_strings(x, out: list[str]) -> None:
    if isinstance(x, str):
        out.append(x)
    elif isinstance(x, dict):
        for v in x.values():
            _walk_strings(v, out)
    elif isinstance(x, list):
        for v in x:
            _walk_strings(v, out)


def input_strings(tool_use: dict) -> list[str]:
    """Strings the host sent onward in a tool call: a Bash command, a sub-agent
    prompt, a message. The router's own tools and read-only tools are left out."""
    name = tool_use.get("name") or ""
    if name.startswith(ROUTER_TOOL_PREFIX) or name in _PASSIVE_TOOLS:
        return []
    out: list[str] = []
    _walk_strings(tool_use.get("input"), out)
    return out


_STUB_RE = re.compile(r"moved to the background as task (\w+)")
_NOTIF_ID = re.compile(r"<task-id>\s*(\w+)\s*</task-id>")
_NOTIF_STATUS = re.compile(r"<status>\s*(\w+)\s*</status>")
_NOTIF_RESULT = re.compile(r"<result>\s*(.*?)\s*</result>", re.S)


def parse_notification(text: str) -> tuple[str, str, str] | None:
    """(task id, status, result) of a ``<task-notification>`` user message."""
    if "<task-notification>" not in text:
        return None
    tid, st, res = _NOTIF_ID.search(text), _NOTIF_STATUS.search(text), _NOTIF_RESULT.search(text)
    if not tid:
        return None
    return tid.group(1), (st.group(1) if st else ""), (res.group(1) if res else "")


def parse_local_task(result_text: str | None) -> dict | None:
    try:
        obj = json.loads(_unwrap(result_text or ""))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


_GIT_KEEP = re.compile(r"\bgit\b[^\n;&|]*\b(?:commit|add)\b")


def local_task_follow_up(actions: list[tuple[str, str]], workdir: str, changed: list[str]) -> str | None:
    """What the host did with the workdir an ``llm_local_task`` wrote into. `actions` are the
    host's later Bash commands ("bash", cmd) and file edits ("edit", path), in order.
    "kept": a ``git commit|add`` naming the workdir came first. "amended": the host edited one
    of the files the tool changed before that commit. None: no commit seen. (An undo is not
    looked for: ``git stash`` / ``git checkout`` also appear in ordinary baseline checks, and
    the diff itself is not in the transcript.)"""
    wd = (workdir or "").rstrip("/")
    if not wd:
        return None
    wanted = {c.lstrip("./") for c in changed}
    amended = False
    for kind, val in actions:
        if kind == "edit":
            amended = amended or (val.startswith(wd + "/") and val[len(wd) + 1:] in wanted)
        elif wd in val and _GIT_KEEP.search(val):
            return "amended" if amended else "kept"
    return None


_FENCE = re.compile(r"-----\s*BEGIN UNTRUSTED[^\n]*-----\n(.*?)\n-----\s*END UNTRUSTED", re.S)


def codex_body(result_text: str | None) -> str:
    """The Codex text in a delivered result, without the router's wrapper: the fenced block
    of the BIND W1 wrapper, else the 16.0.0 hook's header paragraph and the harness prefix."""
    t = (result_text or "").strip()
    fenced = _FENCE.search(t)
    if fenced:
        return fenced.group(1).strip()
    if t.startswith("PreToolUse:"):
        t = t.split(":", 2)[2].strip() if t.count(":") >= 2 else t
    if t.lower().startswith("[llm_router]") and "\n\n" in t:
        head, rest = t.split("\n\n", 1)
        if any(m in head.lower() for m in CODEX_RESULT_MARKERS):
            t = rest
    return t


def carries_codex_text(result_text: str | None, hook_reason: str | None = None) -> bool:
    """The Agent tool_result holds the Codex answer: a delivered-result marker, or the
    hook's own reason text (when the transcript recorded it) found in the result."""
    t = result_text or ""
    low = t.lower()
    if any(m in low for m in CODEX_RESULT_MARKERS):
        return True
    return bool(hook_reason) and reuse_fraction(hook_reason, t) >= USED_THRESHOLD


def looks_launched(result_text: str | None) -> bool:
    return (result_text or "").lstrip().startswith("Async agent launched")


_AGENT_ID_IN_TEXT = re.compile(r"agentId:\s*([A-Za-z0-9]+)")


def agent_id_in_text(result_text: str | None) -> str | None:
    """The launched sub-agent's id as the harness prints it ("agentId: a1a8...")."""
    m = _AGENT_ID_IN_TEXT.search(result_text or "")
    return m.group(1) if m else None


_BLOCK_ENV = re.compile(r"read[- ]only|sandbox", re.IGNORECASE)
_BLOCK_REFUSAL = re.compile(r"\bblocked\b|\bcannot\b|\bcan'?t\b|\bunable to\b|not performed|\bcould not\b|\bcouldn'?t\b",
                            re.IGNORECASE)


def is_blocked_answer(text: str | None) -> bool:
    """A SCREEN, not a label: the text names a read-only/sandbox limit AND says it
    could not do the work. On the 317 void 16.0.0 replies 287 (90.5%) match; BIND-R1's
    own keyword screen gave 269 (85%) and its hand read found 8 of 9 blocked. D-358
    gates the share on a hand check of 20 bound units; this only picks candidates."""
    t = text or ""
    return bool(_BLOCK_ENV.search(t)) and bool(_BLOCK_REFUSAL.search(t))


def is_hold_result(text: str | None) -> bool:
    """A PreToolUse deny from the router's enforcement hold. Anchored on the
    harness prefix so a file that merely quotes the hook's wording does not count."""
    t = (text or "").lstrip()
    return t.startswith("PreToolUse:") and HOLD_MARKER in t[:400]


# ── labelling rules (pure; one per kind) ─────────────────────────────────────

Label = tuple[str, str, str]  # (label, signal, confidence)


def label_draft(draft_text: str | None, host_text: str | None,
                next_prompt: str | None, hook_verdict: str | None) -> Label:
    if not draft_text:
        if hook_verdict == "unused":
            return "unused", "draft_hook_verdict_unused", "medium"
        return "unknown", "draft_text_not_found", "low"
    if not host_text or not host_text.strip():
        if hook_verdict == "unused":
            return "unused", "draft_hook_verdict_unused", "medium"
        return "unknown", "no_host_reply", "low"
    frac = reuse_fraction(draft_text, host_text)
    relayed = _relayed(host_text)
    if (relayed or frac >= USED_THRESHOLD) and _is_reask(next_prompt):
        return "redone", "relayed_then_reasked", "high"
    if relayed:
        # The host declared a relay; how much of the draft survived decides
        # whether it went out as-is or rewritten.
        if frac >= USED_THRESHOLD:
            return "used", "relay_marker", "high"
        return "corrected", "draft_partial_reuse", "medium"
    if frac >= USED_THRESHOLD:
        return "used", "draft_shingle_reuse", "medium"
    if frac >= PARTIAL_THRESHOLD:
        # Hand check 2026-09-29: the only unmarked 20-60% draft was the host
        # restating facts both had seen (URLs), not a relay. Ambiguous.
        return "unknown", "draft_partial_overlap_ambiguous", "low"
    return "unused", "draft_not_relayed", "high" if frac < AMBIGUOUS_FLOOR else "medium"


def label_routed_mcp(result_text: str | None, is_error: bool, host_text: str | None,
                     host_did_work: bool, next_prompt: str | None) -> Label:
    if result_text is None:
        return "unknown", "tool_result_missing", "low"
    if is_error or not result_text.strip():
        return "unused", "tool_result_error", "high"
    if _is_reask(next_prompt):
        return "redone", "explicit_reask", "high"
    if not host_did_work and not (host_text or "").strip():
        return "unknown", "no_host_reply", "low"
    body = answer_body(result_text)
    frac, words = body_reuse(body, host_text)
    if words == 0:
        return "unused", "tool_result_empty", "medium"
    if frac is None:
        # Under 3 words ("Hash."): any host text would "contain" it. Not attributable.
        return "unknown", "body_too_short_to_attribute", "low"
    if frac >= USED_THRESHOLD:
        return "used", "tool_result_reused", "medium" if words >= 8 else "low"
    if frac >= AMBIGUOUS_FLOOR:
        # Hand check 2026-09-29: a 25% overlap was the host QUOTING the result
        # to critique it; a 5% one was the host adopting two of five review
        # points. Lexical overlap cannot tell those apart, so it is ambiguous.
        return "unknown", "tool_result_ambiguous_overlap", "low"
    return "unused", "tool_result_not_reused", "medium"


def label_local_task(result: dict | None, follow_up: str | None) -> Label:
    """``llm_local_task`` writes its edit itself (``apply_writes``) and returns a JSON
    status, so there is no text to compare: the evidence is the file effect and what
    the host then did with the workdir (`follow_up`, from ``local_task_follow_up``)."""
    if result is None:
        return "unknown", "tool_result_unreadable", "low"
    status = result.get("status")
    changed = result.get("changed_files") or []
    if status in ("blocked", "failed") or result.get("error"):
        return "unused", "local_task_failed", "high"
    if not result.get("writes_applied"):
        return "unused", "local_task_not_applied", "high"
    if status == "incomplete" and not changed:
        return "unused", "local_task_no_effect", "high"
    if follow_up == "kept":
        return "used", "local_task_effect_kept", "medium"
    if follow_up == "amended":
        return "corrected", "local_task_effect_amended", "medium"
    return "unknown", "local_task_effect_unconfirmed", "low"


def label_codex(outcome: str | None, tool_result_text: str | None,
                parent_text: str | None, respawned: bool,
                launched: bool | None = None, carries: bool | None = None) -> Label:
    if outcome == "codex_failed":
        return "unused", "codex_failed", "high"
    if outcome == "codex_blocked":
        # Codex ran and its answer said it could not do the work; the hook did not bind it.
        return "unused", "codex_blocked", "high"
    if outcome != "delegated":
        return "unknown", "codex_outcome_unrecognised", "low"
    if tool_result_text is None:
        return "unknown", "no_agent_call_found", "low"
    if carries is None:
        carries = carries_codex_text(tool_result_text)
    if launched is None:  # text-only callers: no Codex text means Claude's own agent ran
        launched = looks_launched(tool_result_text) or not carries
    if launched:
        # The PreToolUse block was not honoured: Claude's own agent ran.
        return "redone", "claude_subagent_ran", "high"
    if not carries:
        return "unknown", "codex_text_not_in_result", "low"
    if respawned:
        return "redone", "respawned_after_codex", "medium"
    if reuse_fraction(codex_body(tool_result_text), parent_text) >= USED_THRESHOLD:
        return "used", "codex_output_incorporated", "medium"
    return "unknown", "codex_output_not_visibly_incorporated", "low"


def label_edit(applied: bool, with_blocks: list[str], host_edits: list[str],
               file_exists: bool, result_found: bool) -> Label:
    if not applied:
        return "unused", "edit_not_applied", "high"
    if not result_found or not with_blocks:
        return "unknown", "edit_result_unreadable", "low"
    if not host_edits:
        return "unused", "edit_never_applied_by_host", "high"
    wanted = {w.strip() for w in with_blocks}
    if any(h.strip() in wanted for h in host_edits):
        return "used", "edit_applied_verbatim", "high"
    best = max(reuse_fraction(w, h, n=4) for w in with_blocks for h in host_edits)
    if best >= EDIT_PARTIAL_THRESHOLD:
        return "corrected", "edit_applied_modified", "medium"
    return "redone", "edit_host_rewrote", "medium" if file_exists else "low"


def label_proxy(results: list[tuple[str, bool]] | None, n_tool_uses: int,
                next_prompt: str | None) -> Label:
    if _is_reask(next_prompt):
        return "redone", "proxy_explicit_reask", "high"
    if n_tool_uses == 0:
        if next_prompt is None:
            return "unknown", "proxy_no_terminal_event", "low"
        return "used", "proxy_text_not_reasked", "low"
    if results is None or len(results) < n_tool_uses:
        return "unknown", "proxy_tool_result_missing", "low"
    for text, is_error in results:
        if is_error or "doesn't want to proceed" in text or "[Request interrupted" in text:
            return "redone", "proxy_tool_rejected", "high"
    return "used", "proxy_tool_result_ok", "high"


def label_delegate(row: dict) -> Label:
    if row.get("route_outcome") == "failed" or row.get("route_succeeded") is False:
        return "unused", "delegate_failed", "high"
    return "unknown", "delegate_no_host_evidence", "low"


# ── organic scope ────────────────────────────────────────────────────────────

SESSION_KINDS = ("organic", "research", "harness", "headless")  # src/llm_router/session_kind.py
OVERRIDES_FILENAME = "session_kind_overrides.json"


def read_session_overrides(path: Path) -> dict[str, str]:
    """{session_id: kind} from the owner's override file. Like the product's reader, a
    missing, unreadable or malformed file or entry is "no override", and only a valid
    kind counts. The audit never writes it."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for sid, entry in data.items():
            kind = entry.get("kind") if isinstance(entry, dict) else None
            if isinstance(sid, str) and kind in SESSION_KINDS:
                out[sid] = kind
    return out


def is_organic(meta: dict, override_kind: str | None = None) -> bool:
    """Organic scope. The owner's override comes first (precedence "override, then
    the rules", as for every other reader): a non-organic kind excludes the
    session; "organic" lifts the classification rules (cwd, worktree, sandbox) but
    not the structural ones (a human-less sdk entrypoint, a synthetic test id)."""
    sid = meta.get("session_id") or ""
    if override_kind is not None and override_kind != "organic":
        return False
    if meta.get("entrypoint") != "cli":
        return False
    if _SOURCES.is_synthetic_session(sid):
        return False
    if override_kind == "organic":
        return True
    proj = meta.get("project_dir") or ""
    cwd = meta.get("cwd") or ""
    if _SOURCES._SANDBOX_PROJECT.match(proj) or _SANDBOX_CWD.match(cwd):
        return False
    low = (proj + " " + cwd).lower()
    # "<repo>-wt-<name>" is this machine's git-worktree naming convention
    # (e.g. ~/Projects/llm-router-wt-p01); ".claude/worktrees" is Claude Code's.
    wt = "worktree" in low or re.search(r"-wt-[^/]*(/|$)", cwd.lower() + "/") or "-wt-" in proj.lower()
    return not (wt or "/.rsi/" in cwd or "--rsi-" in proj)


# ── transcript model ─────────────────────────────────────────────────────────

def _ts(obj: dict) -> float | None:
    v = obj.get("timestamp")
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _content(obj: dict):
    m = obj.get("message")
    return m.get("content") if isinstance(m, dict) else None


def _text_blocks(obj: dict) -> str:
    c = _content(obj)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tool_uses(obj: dict) -> list[dict]:
    c = _content(obj)
    return [b for b in c if isinstance(b, dict) and b.get("type") == "tool_use"] if isinstance(c, list) else []


def _tool_results(obj: dict) -> list[dict]:
    c = _content(obj)
    return [b for b in c if isinstance(b, dict) and b.get("type") == "tool_result"] if isinstance(c, list) else []


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return ""


def _written_content(tool_use: dict) -> list[str]:
    inp = tool_use.get("input") or {}
    out = [inp[k] for k in ("new_string", "content", "new_source") if isinstance(inp.get(k), str)]
    out += [e["new_string"] for e in inp.get("edits") or []
            if isinstance(e, dict) and isinstance(e.get("new_string"), str)]
    return out


_NOT_HUMAN = {_SOURCES.DROP_SYSTEM_NOISE, _SOURCES.DROP_HARNESS_ARTEFACT, _SOURCES.DROP_BARE_ATTACHMENT,
              _SOURCES.DROP_PASTED_TOOL_OUTPUT, _SOURCES.DROP_TOOL_ECHO, _SOURCES.DROP_EMPTY}


@dataclass
class Session:
    sid: str
    records: list[dict]
    meta: dict
    human_idx: list[int] = field(default_factory=list)   # any human turn (re-ask detection)
    prompt_idx: list[int] = field(default_factory=list)  # counted organic prompts
    hold_idx: list[tuple[int, str]] = field(default_factory=list)  # (record, tool_use_id) of router holds
    first_ts: float = 0.0

    def human_text(self, i: int) -> str | None:
        rec = self.records[i]
        if rec.get("type") != "user" or rec.get("isMeta") or _tool_results(rec):
            return None
        text = _text_blocks(rec)
        if not text.strip():
            return None
        if _SOURCES.classify_drop(text, self.sid) in _NOT_HUMAN:
            return None
        return text

    def next_human(self, i: int) -> int | None:
        for j in self.human_idx:
            if j > i:
                return j
        return None

    def reply_after(self, i: int, inputs: bool = False) -> tuple[str, list[dict], int]:
        """Host output after record i, up to the next human turn: its text
        plus the content it wrote with Edit/Write (routed content written into
        a file is used content too), and its tool_uses. With `inputs`, also the
        strings it sent onward in other tools' inputs (a Bash command, a
        sub-agent prompt, a message): a routed commit message relayed into
        ``git commit`` is used content."""
        stop = self.next_human(i)
        stop = len(self.records) if stop is None else stop
        texts, uses = [], []
        for rec in self.records[i + 1:stop]:
            if rec.get("type") == "assistant":
                t = _text_blocks(rec)
                if t.strip():
                    texts.append(t)
                for tu in _tool_uses(rec):
                    uses.append(tu)
                    if tu.get("name") in _EDIT_TOOLS:
                        texts.extend(_written_content(tu))
                    if inputs:
                        texts.extend(input_strings(tu))
        return "\n".join(texts), uses, stop

    def hold_in_turn(self, i: int) -> bool:
        """A router hold (a deny with the BLOCKED directive) landed in the human turn
        that contains record i, before it: a call made now is made to clear the hold."""
        prev = max((j for j in self.human_idx if j < i), default=0)
        return any(prev <= k < i for k, _tid in self.hold_idx)

    def next_human_text(self, i: int) -> str | None:
        j = self.next_human(i)
        return self.human_text(j) if j is not None else None

    def index_at_or_after(self, ts: float, slack: float = 2.0) -> int | None:
        for i, rec in enumerate(self.records):
            t = _ts(rec)
            if t is not None and t >= ts - slack:
                return i
        return None


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


def index_session(s: Session) -> Session:
    """Fill the turn indexes (human turns, counted prompts, router holds) and the start time."""
    s.human_idx.clear()
    s.prompt_idx.clear()
    s.hold_idx.clear()
    held: set[str] = set()
    for i, rec in enumerate(s.records):
        if not s.first_ts:
            s.first_ts = _ts(rec) or 0.0
        text = s.human_text(i)
        if text is not None:
            s.human_idx.append(i)
            if _SOURCES.classify_drop(text, s.sid) is None:
                s.prompt_idx.append(i)
        for b in _tool_results(rec):
            tid = b.get("tool_use_id")
            # the same deny can be recorded twice; count the tool_use once
            if tid and tid not in held and is_hold_result(_result_text(b)):
                held.add(tid)
                s.hold_idx.append((i, tid))
    return s


def load_sessions(projects_dir: Path, cutoff: float, overrides: dict[str, str] | None = None,
                  skipped: list[str] | None = None) -> tuple[dict[str, Session], int]:
    """Organic sessions with any activity after `cutoff`, and how many files
    were read. A file last modified before the cutoff cannot hold a unit in
    the window, so it is skipped; units are then windowed by their own ts.
    `overrides` is the owner's {session_id: kind}: a session it names as
    non-organic is not read, and its id is appended to `skipped`."""
    sessions: dict[str, Session] = {}
    n_files = 0
    for p in sorted(glob.glob(str(projects_dir / "*" / "*.jsonl"))):
        path = Path(p)
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        kind = (overrides or {}).get(path.stem)
        if kind is not None and kind != "organic":
            if skipped is not None:
                skipped.append(path.stem)  # the override wins; the file is not even read
            continue
        n_files += 1
        records = _read_jsonl(path)
        meta = {"session_id": path.stem, "project_dir": path.parent.name, "path": str(path),
                "entrypoint": next((r["entrypoint"] for r in records if r.get("entrypoint")), None),
                "cwd": next((r["cwd"] for r in records if r.get("cwd")), "")}
        if not is_organic(meta, kind):
            continue
        sessions[path.stem] = index_session(Session(path.stem, records, meta))
    return sessions, n_files


# ── ledgers ──────────────────────────────────────────────────────────────────

_LOG_LINE = re.compile(r"^\[[\d\- :]+\] \[INVOCATION ([\d.]+)\] (.*)$")
_LOG_SESSION = re.compile(r"session_id=(\S*)")
_LOG_VERDICT = re.compile(r"DRAFT (USED|UNUSED)\b.*?invocation ([\d.]+)")


def parse_debug_log(path: Path) -> tuple[dict[str, dict], dict[float, str]]:
    """({invocation_id: {session8, msgs}}, {draft invocation ts: 'used'|'unused'})."""
    recs: dict[str, dict] = {}
    verdicts: dict[float, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return recs, verdicts
    for raw in lines:
        m = _LOG_LINE.match(raw)
        if not m:
            continue
        iid, rest = m.groups()
        rec = recs.setdefault(iid, {"session8": None, "msgs": []})
        rec["msgs"].append(rest)
        s = _LOG_SESSION.search(rest)
        if s and s.group(1):
            rec["session8"] = s.group(1)
        v = _LOG_VERDICT.search(rest)
        if v:
            verdicts[round(float(v.group(2)), 2)] = v.group(1).lower()
    return recs, verdicts


def _sqlite_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


# ── unit assembly ────────────────────────────────────────────────────────────

@dataclass
class Unit:
    kind: str
    session_id: str
    ts: float
    label: str
    signal: str
    confidence: str
    evidence: dict = field(default_factory=dict)  # in-memory only, never written
    human_reviewed: bool = False
    unit_id: str = ""
    extra: dict = field(default_factory=dict)  # in-memory facts for the summary/report (ids, binding); never text

    def row(self) -> dict:
        return {"unit_id": self.unit_id, "kind": self.kind, "label": self.label,
                "signal": self.signal, "confidence": self.confidence,
                "human_reviewed": self.human_reviewed}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _draft_units(sessions, recs, verdicts, lo, hi) -> list[Unit]:
    by_prefix: dict[str, list[Session]] = {}
    for s in sessions.values():
        by_prefix.setdefault(s.sid[:8], []).append(s)
    units = []
    for iid, rec in recs.items():
        msgs = rec["msgs"]
        if not any("DIRECT SUCCESS" in m for m in msgs):
            continue
        ts = float(iid)
        if not (lo <= ts <= hi):
            continue
        cands = by_prefix.get(rec.get("session8") or "", [])
        if not cands:
            continue
        s = cands[0]
        kind = "direct" if any("ZERO_CLAUDE" in m for m in msgs) else "draft"
        verdict = verdicts.get(round(ts, 2))
        draft_text, at = None, None
        for i, r in enumerate(s.records):
            t = _ts(r)
            if t is None or t < ts - 2 or t > ts + DRAFT_ATTACH_WINDOW_S:
                continue
            att = r.get("attachment") or {}
            if att.get("type") != "hook_additional_context":
                continue
            body = "\n".join(c for c in (att.get("content") or []) if isinstance(c, str))
            if "DRAFT" in body and "─" in body:
                draft_text, at = extract_draft(body), i
                break
        if at is None:
            at = s.index_at_or_after(ts)
        host, _uses, stop = s.reply_after(at) if at is not None else ("", [], None)
        nxt = s.human_text(stop) if stop is not None and stop < len(s.records) else None
        label = label_draft(draft_text, host, nxt, verdict)
        units.append(Unit(kind, s.sid, ts, *label,
                          evidence={"routed": draft_text or "", "host": host}))
    return units


def _find_notification(s: Session, start: int, task_id: str) -> tuple[int, str, bool] | None:
    """The ``<task-notification>`` that delivered a backgrounded MCP call's real result:
    (record index, result text, failed)."""
    for j in range(start, len(s.records)):
        rec = s.records[j]
        if rec.get("type") != "user":
            continue
        c = _content(rec)
        text = c if isinstance(c, str) else _text_blocks(rec)
        got = parse_notification(text) if text else None
        if got and got[0] == task_id:
            return j, got[2], got[1] != "completed"
    return None


def _host_actions_after(s: Session, start: int, deadline: float) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for rec in s.records[start + 1:]:
        if rec.get("type") != "assistant":
            continue
        t = _ts(rec)
        if t is not None and t > deadline:
            break
        for tu in _tool_uses(rec):
            if tu.get("name") == "Bash":
                out.append(("bash", (tu.get("input") or {}).get("command") or ""))
            elif tu.get("name") in _EDIT_TOOLS:
                out.append(("edit", (tu.get("input") or {}).get("file_path") or ""))
    return out


def _mcp_call_unit(s: Session, i: int, ts: float, tu: dict, ri: int | None, rb: dict | None) -> Unit:
    """One router tool call that is not llm_edit -> one unit."""
    name = (tu.get("name") or "")[len(ROUTER_TOOL_PREFIX):]
    inp = tu.get("input") or {}
    extra = {"tool": name, "tool_use_id": tu.get("id")}

    def unit(label: str, signal: str, conf: str, **evidence) -> Unit:
        return Unit("routed_mcp", s.sid, ts, label, signal, conf, evidence=evidence, extra=extra)

    if name in NON_CONTENT_TOOLS:
        return unit(NOT_APPLICABLE, "status_view", "high")
    prompt = inp.get("prompt") if isinstance(inp.get("prompt"), str) else ""
    if is_probe_prompt(prompt):
        extra["probe"] = True
        return unit(NOT_APPLICABLE, "hold_release_probe" if s.hold_in_turn(i) else "probe_fixed_reply", "high")
    if rb is None or ri is None:
        return unit(*label_routed_mcp(None, False, None, False, None))
    text, is_err, at = _result_text(rb), bool(rb.get("is_error")), ri
    stub = None if is_err else _STUB_RE.search(text)
    if stub:  # moved to the background after 120 s: the real result arrives as a task notification
        extra["background"] = True
        note = _find_notification(s, ri, stub.group(1))
        if note is None:
            return unit("unknown", "background_result_missing", "low")
        at, text, is_err = note
    if name == "llm_local_task":
        if is_err:
            return unit("unused", "tool_result_error", "high")
        res = parse_local_task(text)
        follow = local_task_follow_up(_host_actions_after(s, at, ts + LOCAL_TASK_KEEP_WINDOW_S),
                                      inp.get("workdir") or "", (res or {}).get("changed_files") or [])
        return unit(*label_local_task(res, follow))
    host, uses, _stop = s.reply_after(at, inputs=True)
    did_work = bool(host.strip()) or any(u.get("name") in _EDIT_TOOLS for u in uses)
    label = label_routed_mcp(text, is_err, host, did_work, s.next_human_text(at))
    return unit(*label, routed=answer_body(text), host=host)


def _mcp_and_edit_units(sessions, edit_rows, lo, hi, stats: dict | None = None) -> list[Unit]:
    units: list[Unit] = []
    stats = stats if stats is not None else {}
    seen: set[str] = set()  # tool_use ids already turned into a unit
    edit_rows_by_sid: dict[str, list[dict]] = {}
    orphans = []
    for r in edit_rows:
        if not isinstance(r.get("ts"), (int, float)) or not (lo <= r["ts"] <= hi):
            continue
        if r.get("session_id"):
            edit_rows_by_sid.setdefault(r["session_id"], []).append(r)
        else:
            orphans.append(r)
    # Oldest session first: a resumed or forked session replays its parent's history
    # (same tool_use ids, same timestamps); the unit belongs to the session it happened in.
    for s in sorted(sessions.values(), key=lambda x: (x.first_ts, x.sid)):
        results: dict[str, tuple[int, dict]] = {}
        for i, rec in enumerate(s.records):
            for b in _tool_results(rec):
                if b.get("tool_use_id"):
                    results[b["tool_use_id"]] = (i, b)
        edit_calls: list[tuple[float, dict, int | None, dict | None]] = []
        for i, rec in enumerate(s.records):
            if rec.get("type") != "assistant":
                continue
            ts = _ts(rec)
            for tu in _tool_uses(rec):
                name = tu.get("name") or ""
                if not name.startswith(ROUTER_TOOL_PREFIX) or ts is None or not (lo <= ts <= hi):
                    continue
                if tu.get("id") in seen:
                    stats["replayed_tool_uses"] = stats.get("replayed_tool_uses", 0) + 1
                    continue
                if tu.get("id"):
                    seen.add(tu["id"])
                ri, rb = results.get(tu.get("id"), (None, None))
                if name == "mcp__llm_router__llm_edit":
                    edit_calls.append((ts, tu, ri, rb))
                    continue
                units.append(_mcp_call_unit(s, i, ts, tu, ri, rb))
        rows = list(edit_rows_by_sid.get(s.sid, []))
        for r in orphans:  # FALLBACK JOIN: session-less rows matched by ts + file
            if any(c_ts <= r["ts"] <= c_ts + EDIT_JOIN_WINDOW_S
                   and r.get("file") in ((tu.get("input") or {}).get("files") or [])
                   for c_ts, tu, _ri, _rb in edit_calls):
                rows.append(r)
        claimed: set[int] = set()
        for r in rows:
            call = None
            for c in edit_calls:
                if c[0] <= r["ts"] <= c[0] + EDIT_JOIN_WINDOW_S and (call is None or c[0] > call[0]):
                    call = c
            if call is not None:
                claimed.add(id(call))
            units.append(_edit_unit(s, r, call))
        for c in edit_calls:  # an llm_edit call with no ledger row stays routed_mcp
            if id(c) in claimed:
                continue
            c_ts, _tu, ri, rb = c
            host, uses, _ = s.reply_after(ri) if ri is not None else ("", [], None)
            files = _edit_files_in(_result_text(rb)) if rb is not None and not rb.get("is_error") else []
            if files:
                # The ledger row is missing (edit_outcomes.jsonl did not record
                # this call), but the result still says which edits to apply:
                # judge it the same way as a ledger-backed routed_edit.
                withs = [w for f in files for w in _with_blocks_for(_result_text(rb), f)]
                edits = [e for f in files for e in _host_edits_on(uses, f)]
                label = label_edit(True, withs, edits, all(Path(f).exists() for f in files), True)
            else:
                did_work = bool(host.strip()) or any(u.get("name") in _EDIT_TOOLS for u in uses)
                label = label_routed_mcp(_result_text(rb) if rb else None, bool(rb and rb.get("is_error")),
                                         host, did_work, s.next_human_text(ri) if ri is not None else None)
            units.append(Unit("routed_mcp", s.sid, c_ts, *label))
    return units


_WITH_BLOCK = re.compile(r"\*\*With:\*\*\s*\n```[^\n]*\n(.*?)\n```", re.S)
_EDIT_HEADER = re.compile(r"^### Edit \d+: (.+)$", re.M)


def _edit_files_in(result_text: str) -> list[str]:
    return sorted({h.group(1).strip() for h in _EDIT_HEADER.finditer(_unwrap(result_text))})


def _with_blocks_for(result_text: str, file: str) -> list[str]:
    text = _unwrap(result_text)
    heads = list(_EDIT_HEADER.finditer(text))
    out = []
    for k, h in enumerate(heads):
        if h.group(1).strip() != file:
            continue
        seg = text[h.end(): heads[k + 1].start() if k + 1 < len(heads) else len(text)]
        out += _WITH_BLOCK.findall(seg)
    return out


def _host_edits_on(uses: list[dict], file: str) -> list[str]:
    out = []
    for u in uses:
        if u.get("name") not in _EDIT_TOOLS:
            continue
        inp = u.get("input") or {}
        if inp.get("file_path") != file:
            continue
        out += _written_content(u)
    return out


def _edit_unit(s: Session, row: dict, call) -> Unit:
    file = row.get("file") or ""
    with_blocks, host_edits, result_found = [], [], False
    if call is not None and call[3] is not None:
        result_found = True
        with_blocks = _with_blocks_for(_result_text(call[3]), file)
        _host, uses, _ = s.reply_after(call[2])
        host_edits = _host_edits_on(uses, file)
    label, signal, conf = label_edit(bool(row.get("applied")), with_blocks, host_edits,
                                     Path(file).exists() if file else False, result_found)
    if label in ("used", "corrected"):
        # git survival never makes or unmakes a label; it only says how sure.
        verdict = _EDIT_SURVIVAL.judge_row(row).verdict if _EDIT_SURVIVAL is not None else "unknown"
        conf = {"survived": "high", "redone": "low"}.get(verdict, "medium")
    return Unit("routed_edit", s.sid, float(row["ts"]), label, signal, conf)


@dataclass
class _AgentCall:
    """One Agent/Task tool_use, in the session transcript or a sub-agent's."""
    tid: str
    ts: float
    inp: dict
    src_path: str | None              # None: the session's own transcript
    result_idx: int | None = None
    result_text: str | None = None
    result_is_error: bool = False
    tur: object = None                # the result record's toolUseResult


@dataclass
class _SessionAgents:
    calls: dict[str, _AgentCall] = field(default_factory=dict)
    hooks: list[tuple[float, str, str]] = field(default_factory=list)  # (ts, tool_use_id, stdout)
    launched: dict[str, str] = field(default_factory=dict)             # tool_use_id -> agent id (meta.json)


_AGENT_NEEDLES = ("PreToolUse:Agent", '"Agent"', '"Task"', "tool_result")


def _scan_agent_records(records, src_path: str | None, agents: _SessionAgents) -> None:
    for i, rec in enumerate(records):
        t = _ts(rec) or 0.0
        att = rec.get("attachment")
        if isinstance(att, dict) and att.get("hookName") == "PreToolUse:Agent" and att.get("toolUseID"):
            agents.hooks.append((t, att["toolUseID"], att.get("stdout") or ""))
        if rec.get("type") == "assistant":
            for tu in _tool_uses(rec):
                if tu.get("name") in _AGENT_TOOLS and tu.get("id"):
                    agents.calls[tu["id"]] = _AgentCall(tu["id"], t, tu.get("input") or {}, src_path)
        elif rec.get("type") == "user":
            for b in _tool_results(rec):
                call = agents.calls.get(b.get("tool_use_id"))
                if call is not None and call.src_path == src_path:
                    call.result_idx, call.result_text = i, _result_text(b)
                    call.result_is_error, call.tur = bool(b.get("is_error")), rec.get("toolUseResult")


def _read_jsonl_matching(path: Path, needles: tuple[str, ...]):
    """Records whose line mentions a needle: a sub-agent transcript is mostly tool
    output nobody here needs, and the six sessions that hold Codex units carry 1.4 GB of it."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not any(n in line for n in needles):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def subagent_dir(s: Session) -> Path | None:
    p = s.meta.get("path")
    return Path(p).with_suffix("") / "subagents" if p else None


def _index_agents(s: Session) -> _SessionAgents:
    """Every Agent call of the session AND of its sub-agents (a nested spawn lives only in
    a ``subagents/agent-*.jsonl``), the PreToolUse:Agent hook runs, and which tool_use ids
    a sub-agent's ``.meta.json`` says it was launched for."""
    agents = _SessionAgents()
    _scan_agent_records(s.records, None, agents)
    d = subagent_dir(s)
    if d is not None and d.is_dir():
        for f in sorted(d.glob("agent-*.jsonl")):
            _scan_agent_records(_read_jsonl_matching(f, _AGENT_NEEDLES), str(f), agents)
        for f in sorted(d.glob("agent-*.meta.json")):
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict) and meta.get("toolUseId"):
                agents.launched[meta["toolUseId"]] = f.name[len("agent-"):-len(".meta.json")]
    return agents


def hook_reason(stdout: str | None) -> str | None:
    """The reason text a PreToolUse hook emitted (the legacy ``reason`` or the
    ``permissionDecisionReason``); stdout may open with a stray log line."""
    out = stdout or ""
    k = out.find("{")
    while k != -1:
        try:
            obj = json.loads(out[k:])
            break
        except ValueError:
            k = out.find("{", k + 1)
    else:
        return None
    if not isinstance(obj, dict):
        return None
    r = obj.get("reason")
    if not isinstance(r, str):
        r = (obj.get("hookSpecificOutput") or {}).get("permissionDecisionReason")
    return r if isinstance(r, str) else None


def _pair_codex_row(row: dict, agents: _SessionAgents, claimed: set[str]) -> tuple[_AgentCall | None, str]:
    """The Agent call a ledger row belongs to, and how it was found. By tool_use_id when the
    ledger has one. Otherwise by the PreToolUse:Agent hook run that FOLLOWED the row (the hook
    writes the row, then the harness records the run: median 0.39 s, max 5.5 s over 317 rows;
    all 317 pairs agree with the ledger's subagent_type and with the sub-agent's
    ``.meta.json``). Last resort: the latest unclaimed call before the row."""
    tid = row.get("tool_use_id")
    if isinstance(tid, str) and tid:
        claimed.add(tid)
        return agents.calls.get(tid), "tool_use_id"
    ts = float(row["ts"])
    want = row.get("subagent_type")
    best: tuple[tuple[bool, float], _AgentCall] | None = None
    for h_ts, h_tid, _out in agents.hooks:
        call = agents.calls.get(h_tid)
        if call is None or h_tid in claimed or not (ts - HOOK_JOIN_BEFORE_S <= h_ts <= ts + HOOK_JOIN_AFTER_S):
            continue
        key = (bool(want) and (call.inp.get("subagent_type") or "general-purpose") != want, abs(h_ts - ts))
        if best is None or key < best[0]:
            best = (key, call)
    if best is not None:
        claimed.add(best[1].tid)
        return best[1], "hook_run"
    match = None
    for call in agents.calls.values():
        if call.tid in claimed or call.ts > ts + 2 or ts - call.ts > AGENT_MATCH_WINDOW_S:
            continue
        if match is None or call.ts > match.ts:
            match = call
    if match is not None:
        claimed.add(match.tid)
        return match, "call_time_window"
    return None, "none"


def claude_subagent_launched(call: _AgentCall, agents: _SessionAgents) -> bool:
    """A Claude sub-agent ran for this Agent call: the harness said so in the result, in
    toolUseResult, or in a sub-agent ``.meta.json`` that names the call."""
    if looks_launched(call.result_text) or call.tid in agents.launched:
        return True
    tur = call.tur
    return isinstance(tur, dict) and bool(tur.get("agentId") or tur.get("status") in ("async_launched", "completed"))


def _agent_reply_text(call: _AgentCall, s: Session) -> str:
    """What the caller of the Agent tool did next (text, edits and tool inputs)."""
    if call.result_idx is None:
        return ""
    if call.src_path is None:
        return s.reply_after(call.result_idx, inputs=True)[0]
    sub = index_session(Session(s.sid, _read_jsonl(Path(call.src_path)), {}))
    i = next((k for k, rec in enumerate(sub.records)
              if any(b.get("tool_use_id") == call.tid for b in _tool_results(rec))), None)
    return sub.reply_after(i, inputs=True)[0] if i is not None else ""


def _codex_units(sessions, ns_rows, lo, hi) -> list[Unit]:
    units = []
    claimed: set[str] = set()
    cache: dict[str, _SessionAgents] = {}
    for r in sorted(ns_rows, key=lambda x: x.get("ts") or 0):
        outcome = r.get("outcome")
        if r.get("lever") != "agent_route_codex" or outcome not in ("delegated", "codex_failed", "codex_blocked"):
            continue
        ts = r.get("ts")
        s = sessions.get(r.get("session_id") or "")
        if s is None or not isinstance(ts, (int, float)) or not (lo <= ts <= hi):
            continue
        extra: dict = {"outcome": outcome, "tool_use_id": r.get("tool_use_id"),
                       "subagent_type": r.get("subagent_type")}
        if outcome != "delegated":
            units.append(Unit("agent_route_codex", s.sid, float(ts), *label_codex(outcome, None, None, False),
                              extra=extra))
            continue
        agents = cache.get(s.sid)
        if agents is None:
            agents = cache[s.sid] = _index_agents(s)
        call, how = _pair_codex_row(r, agents, claimed)
        extra.update(pairing=how, call_found=call is not None, tool_use_id=call.tid if call else extra["tool_use_id"],
                     nested=bool(call and call.src_path), launched=False, bound=False, respawned=False)
        if call is None:
            units.append(Unit("agent_route_codex", s.sid, float(ts), *label_codex("delegated", None, None, False),
                              extra=extra))
            continue
        launched = claude_subagent_launched(call, agents)
        reason = next((hook_reason(out) for _t, tid, out in agents.hooks if tid == call.tid), None)
        carries = call.result_text is not None and carries_codex_text(call.result_text, reason)
        respawned = (not launched) and any(
            o.src_path == call.src_path and call.ts < o.ts <= call.ts + RESPAWN_WINDOW_S
            and reuse_fraction(call.inp.get("prompt"), o.inp.get("prompt"), n=4) >= 0.5
            for o in agents.calls.values())
        parent = _agent_reply_text(call, s) if (carries and not launched and not respawned) else ""
        if call.result_text is None:
            label = (("redone", "claude_subagent_ran", "high") if launched
                     else ("unknown", "agent_result_missing", "low"))
        else:
            label = label_codex("delegated", call.result_text, parent, respawned, launched, carries)
        tur = call.tur if isinstance(call.tur, dict) else {}
        extra.update(launched=launched, bound=bool(carries and not launched), respawned=respawned,
                     call_subagent_type=call.inp.get("subagent_type") or "general-purpose",
                     agent_id=agents.launched.get(call.tid) or tur.get("agentId") or agent_id_in_text(call.result_text),
                     resolved_model=tur.get("resolvedModel"),
                     blocked_screen=is_blocked_answer(codex_body(call.result_text)) if carries and not launched else None)
        units.append(Unit("agent_route_codex", s.sid, float(ts), *label, extra=extra,
                          evidence={"routed": call.result_text or "", "host": parent}))
    return units


def _proxy_units(sessions, proxy_rows, lo, hi) -> list[Unit]:
    served = {r["msg_id"]: r for r in proxy_rows
              if r.get("decision") == "served" and isinstance(r.get("msg_id"), str)}
    units = []
    if not served:
        return units
    for s in sessions.values():
        seen: set[str] = set()
        for i, rec in enumerate(s.records):
            if rec.get("type") != "assistant":
                continue
            mid = (rec.get("message") or {}).get("id")
            ts = _ts(rec)
            if mid not in served or mid in seen or ts is None or not (lo <= ts <= hi):
                continue
            seen.add(mid)
            use_ids = {tu.get("id") for r2 in s.records if r2.get("type") == "assistant"
                       and (r2.get("message") or {}).get("id") == mid for tu in _tool_uses(r2)}
            use_ids.discard(None)
            results = []
            for r2 in s.records[i:]:
                for b in _tool_results(r2):
                    if b.get("tool_use_id") in use_ids:
                        results.append((_result_text(b), bool(b.get("is_error"))))
            label = label_proxy(results if use_ids else None, len(use_ids), s.next_human_text(i))
            units.append(Unit("proxy", s.sid, ts, *label))
    return units


# ── the audit ────────────────────────────────────────────────────────────────

def default_state_dir() -> Path:
    home = os.environ.get("LLM_ROUTER_HOME", "").strip()
    return Path(home).expanduser() if home else Path.home() / ".llm-router"


def default_projects_dir() -> Path:
    env = os.environ.get("CLAUDE_PROJECTS_DIR", "").strip()
    return Path(env) if env else Path.home() / ".claude" / "projects"


def default_artifact_dir() -> Path:
    return ROOT / "release-artifacts" / "outcome-audit"


def binding_stats(units: list[Unit]) -> dict:
    """The Codex binding facts (D-358), from the units' in-memory ``extra``: of the units
    the hook DELEGATED, how many reached Claude as the sub-agent's result (bound) and
    how many were void (a Claude sub-agent ran anyway)."""
    d = [u for u in units if u.kind == "agent_route_codex" and u.extra.get("outcome") == "delegated"]
    bound = [u for u in d if u.extra.get("bound")]
    by_pairing: dict[str, int] = {}
    for u in d:
        by_pairing[u.extra.get("pairing", "none")] = by_pairing.get(u.extra.get("pairing", "none"), 0) + 1
    return {
        "metric": "delegated units whose Agent tool_result carries the Codex text AND for which "
                  "no Claude sub-agent was launched (per tool_use_id)",
        "delegated": len(d),
        "bound": len(bound),
        "void_claude_subagent_ran": sum(bool(u.extra.get("launched")) for u in d),
        "no_agent_call_found": sum(not u.extra.get("call_found") for u in d),
        "paired_in_sub_agent_transcript": sum(bool(u.extra.get("nested")) for u in d),
        "result_without_codex_text": sum(bool(u.extra.get("call_found")) and not u.extra.get("bound")
                                         and not u.extra.get("launched") for u in d),
        "respawned_after_bound": sum(bool(u.extra.get("respawned")) for u in d),
        "blocked_screen_among_bound": sum(bool(u.extra.get("blocked_screen")) for u in bound),
        "codex_blocked_not_bound": sum(u.kind == "agent_route_codex" and u.extra.get("outcome") == "codex_blocked"
                                       for u in units),
        "pairing": dict(sorted(by_pairing.items())),
    }


def _collect(days: int, projects_dir: Path, state_dir: Path, now: float | None, lo: float | None = None,
             overrides: dict[str, str] | None = None):
    """Every unit in [lo, now] with the facts the summary needs. `overrides` replaces the
    owner's session_kind_overrides.json (None: read it; {}: none), for a validation run."""
    hi = now if now is not None else time.time()
    lo = lo if lo is not None else hi - days * 86400
    if overrides is None:
        overrides = read_session_overrides(state_dir / OVERRIDES_FILENAME)
    skipped: list[str] = []
    sessions, n_files = load_sessions(projects_dir, lo, overrides, skipped)
    recs, verdicts = parse_debug_log(state_dir / "auto-route-debug.log")
    stats: dict = {}
    units: list[Unit] = []
    units += _draft_units(sessions, recs, verdicts, lo, hi)
    units += _mcp_and_edit_units(sessions, _read_jsonl(state_dir / "edit_outcomes.jsonl"), lo, hi, stats)
    ns_rows = _read_jsonl(state_dir / "north_star_units.jsonl")
    units += _codex_units(sessions, ns_rows, lo, hi)
    units += _proxy_units(sessions, _read_jsonl(state_dir / "proxy_calls.jsonl"), lo, hi)

    # delegate / bounded_operational: in scope only when joinable to an organic session
    unscoped: dict[str, dict] = {}
    for r in _read_jsonl(state_dir / "routing_quality.jsonl"):
        kind = r.get("route_kind")
        ts = r.get("ts")
        if kind not in ("delegate", "bounded_operational") or not isinstance(ts, (int, float)):
            continue
        if not (lo <= ts <= hi):
            continue
        label = label_delegate(r)
        if r.get("session_id") in sessions:
            units.append(Unit(kind, r["session_id"], float(ts), *label))
            continue
        u = unscoped.setdefault(kind, {"n": 0, "labels": {k: 0 for k in LABELS},
                                       "provenance": {"synthetic": 0, "production": 0, "unknown": 0}})
        u["n"] += 1
        u["labels"][label[0]] += 1
        prov = "unknown" if "synthetic" not in r else ("synthetic" if r["synthetic"] else "production")
        u["provenance"][prov] += 1

    units.sort(key=lambda u: (u.session_id, u.ts, u.kind))
    seen: dict[str, int] = {}
    for u in units:
        base = f"{u.kind}:{u.session_id}:{_iso(u.ts)}"
        seen[base] = seen.get(base, 0) + 1
        u.unit_id = base if seen[base] == 1 else f"{base}#{seen[base]}"

    excluded = _inflated_sources(state_dir, sessions, ns_rows, lo, hi)
    n_prompts = sum(1 for s in sessions.values() for i in s.prompt_idx
                    if lo <= (_ts(s.records[i]) or 0) <= hi)
    active_sessions = {u.session_id for u in units} | {
        s.sid for s in sessions.values() for i in s.prompt_idx if lo <= (_ts(s.records[i]) or 0) <= hi}
    n_holds = sum(1 for s in sessions.values() for i, _tid in s.hold_idx if lo <= (_ts(s.records[i]) or 0) <= hi)
    ctx = {"lo": lo, "hi": hi, "days": days, "n_files_read": n_files, "n_prompts": n_prompts,
           "n_sessions": len(active_sessions), "unscoped": unscoped, "excluded": excluded,
           "sessions": sessions, "overrides": {"file": OVERRIDES_FILENAME,
                                               "n_entries": len(overrides), "excluded_sessions": sorted(skipped)},
           "dedup": {"replayed_tool_uses": stats.get("replayed_tool_uses", 0)},
           "holds": {"n": n_holds, "n_prompts": n_prompts,
                     "per_100_prompts": round(100 * n_holds / n_prompts, 3) if n_prompts else None},
           "binding": binding_stats(units)}
    return units, ctx


def _inflated_sources(state_dir: Path, sessions, ns_rows, lo, hi) -> dict:
    out = {
        "door_call_verified_used": {"n": 0, "n_all_sessions": 0, "source": "usage.db execution_events "
                                    "adoption_method='door_call'", "counted_as_used": 0},
        "agentic_flat_credits": {"n": 0, "usd": 0.0, "source": "usage.db savings_stats "
                                 "model_used='llm_router-agentic-router'", "counted_as_used": 0},
        "codex_dispatch_flags": {"n": 0, "source": "north_star_units.jsonl outcome='delegated'",
                                 "counted_as_used": 0},
    }
    db = _sqlite_ro(state_dir / "usage.db")
    if db is not None:
        try:
            for sid, _ts_ in db.execute(
                    "SELECT session_id, ts FROM execution_events WHERE adoption_method='door_call' "
                    "AND ts BETWEEN ? AND ?", (lo, hi)):
                out["door_call_verified_used"]["n_all_sessions"] += 1
                if sid in sessions:
                    out["door_call_verified_used"]["n"] += 1
        except sqlite3.Error as exc:  # an unreadable source is reported, never a silent 0
            out["door_call_verified_used"]["read_error"] = str(exc)
        try:
            for stamp, sid, saved in db.execute(
                    "SELECT timestamp, session_id, estimated_claude_cost_saved FROM savings_stats "
                    "WHERE model_used='llm_router-agentic-router'"):
                try:
                    t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                    t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if lo <= t.timestamp() <= hi:
                    out["agentic_flat_credits"]["n"] += 1
                    out["agentic_flat_credits"]["usd"] += float(saved or 0.0)
        except sqlite3.Error as exc:
            out["agentic_flat_credits"]["read_error"] = str(exc)
        db.close()
    out["agentic_flat_credits"]["usd"] = round(out["agentic_flat_credits"]["usd"], 4)
    out["codex_dispatch_flags"]["n"] = sum(
        1 for r in ns_rows if r.get("outcome") == "delegated" and r.get("session_id") in sessions
        and isinstance(r.get("ts"), (int, float)) and lo <= r["ts"] <= hi)
    return out


def _rate(k: int, n: int) -> dict:
    ci = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n) if n else None,
            "ci95": [round(ci[0], 6), round(ci[1], 6)] if ci else None,
            "too_few_to_tell": n < MIN_N}


def summarize(rows: list[dict], ctx: dict, release: str, judge: dict,
              prompts_with_used: int) -> dict:
    by_kind = {}
    for kind in KINDS:
        ks = [r for r in rows if r["kind"] == kind]
        labels = {lab: sum(r["label"] == lab for r in ks) for lab in ALL_LABELS}
        attempted = len(ks) - labels[NOT_APPLICABLE]
        by_kind[kind] = {"n": attempted, "n_total": len(ks), "labels": labels,
                         "used": _rate(labels["used"], attempted),
                         "used_or_corrected": _rate(labels["used"] + labels["corrected"], attempted)}
    na = [r for r in rows if r["label"] == NOT_APPLICABLE]
    n = len(rows) - len(na)  # attempted: a not_applicable unit is outside the denominator
    used = sum(r["label"] == "used" for r in rows)
    corrected = sum(r["label"] == "corrected" for r in rows)
    excluded = ctx["excluded"]
    for name, sigs in INFLATED_SOURCES.items():
        excluded[name]["counted_as_used"] = sum(
            1 for r in rows if r["label"] in ("used", "corrected") and r["signal"] in sigs)
    head = _rate(used, n)
    na_signals: dict[str, int] = {}
    for r in na:
        na_signals[r["signal"]] = na_signals.get(r["signal"], 0) + 1
    return {
        "release": release,
        "audit_version": AUDIT_VERSION,
        "audit_changes": list(AUDIT_CHANGES),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": ctx["days"],
        "window": [_iso(ctx["lo"]), _iso(ctx["hi"])],
        "scope": {"entrypoint": "cli", "excludes": ["/tmp", "/var/folders", "worktree", "~/.rsi",
                                                    "synthetic session ids", "sdk-cli sub-agent sessions",
                                                    "sessions the owner's session_kind_overrides.json names non-organic"],
                  "n_sessions": ctx["n_sessions"], "n_files_read": ctx["n_files_read"],
                  "session_kind_overrides": ctx["overrides"]},
        "headline": {"metric": "used / attempted routed units (organic, window by unit ts; "
                               "not_applicable units are outside the denominator)",
                     "used": used, "attempted": n, "rate": head["rate"], "ci95": head["ci95"],
                     "too_few_to_tell": head["too_few_to_tell"], "not_applicable": len(na)},
        "not_applicable": {"n": len(na), "by_signal": dict(sorted(na_signals.items())),
                           "used_rate_if_included": (used / len(rows)) if rows else None,
                           "note": "excluded from attempted; the rate with them counted as attempted is "
                                   "given so the exclusion can be undone by a reader"},
        "used_or_corrected": _rate(used + corrected, n),
        "prompts": {"metric": "organic prompts followed by >=1 used unit before the next prompt",
                    **_rate(prompts_with_used, ctx["n_prompts"])},
        "labels": {lab: sum(r["label"] == lab for r in rows) for lab in ALL_LABELS},
        "human_reviewed": sum(bool(r["human_reviewed"]) for r in rows),
        "by_kind": by_kind,
        "binding": ctx["binding"],
        "holds": {"metric": "router holds (Routing directive BLOCKED denies) per 100 organic prompts",
                  **ctx["holds"]},
        "dedup": ctx["dedup"],
        "unscoped": ctx["unscoped"],
        "excluded_inflated_sources": excluded,
        "judge": judge,
        "headline_sources": ["outcome_audit rows only"],
    }


def _prompts_with_used(units: list[Unit], sessions) -> int:
    hit: set[tuple[str, int]] = set()
    for u in units:
        if u.label != "used":
            continue
        s = sessions.get(u.session_id)
        if s is None:
            continue
        best = None
        for i in s.prompt_idx:
            t = _ts(s.records[i])
            if t is not None and t <= u.ts + 2:
                best = i
        if best is not None:
            hit.add((u.session_id, best))
    return len(hit)


def _apply_human_labels(units: list[Unit], path: Path | None) -> None:
    if path is None:
        return
    reviews = {r["unit_id"]: r for r in _read_jsonl(path) if r.get("unit_id") and r.get("label") in ALL_LABELS}
    for u in units:
        r = reviews.get(u.unit_id)
        if r is not None:
            u.label, u.signal, u.confidence, u.human_reviewed = r["label"], "human_review", "high", True


# ── optional offline judge ───────────────────────────────────────────────────

_JUDGE_PROMPT = """You audit whether a routed model's output was used by the host assistant.
ROUTED OUTPUT:
<<<{routed}>>>
HOST'S NEXT REPLY:
<<<{host}>>>
Label exactly one of: used (host delivered the routed content as-is), corrected (host
delivered it with changes), redone (host did the same work itself), unused (host ignored
it), unknown (cannot tell). Reply with JSON only: {{"label": "..."}}"""


def judge_label(model: str, routed: str, host: str, timeout: float = 120.0) -> str | None:
    """One local-model call. `model` is 'ollama/<name>'. None on any failure."""
    if not model.startswith("ollama/"):
        raise ValueError("only ollama/<model> judges are supported (local, offline)")
    body = json.dumps({"model": model.split("/", 1)[1], "stream": False, "format": "json",
                       "options": {"temperature": 0},
                       "prompt": _JUDGE_PROMPT.format(routed=routed[:3000], host=host[:3000])}).encode()
    req = urllib.request.Request("http://localhost:11434/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(json.loads(resp.read().decode())["response"])
    except Exception:  # noqa: BLE001 — a judge that cannot answer abstains
        return None
    lab = out.get("label") if isinstance(out, dict) else None
    return lab if lab in LABELS else None


def _hand_truth(row: dict) -> str | None:
    """The 2026-09-29 sample recorded agree/disagree against northstar's
    outcome; map it onto this audit's labels."""
    v, o = row.get("hand_check_verdict"), row.get("northstar_outcome")
    if v == "agree":
        return {"used": "used", "discarded": "unused", "redo": "redone", "unknown": "unknown"}.get(o)
    if v and v.startswith("disagree") and o == "used":
        return "unknown"  # "cannot confirm": not used
    return None


def calibrate_judge(model: str, sample_path: Path, units: list[Unit]) -> dict:
    sample = _read_jsonl(sample_path)
    n = agree = binary = 0
    missing = 0
    for row in sample:
        truth = _hand_truth(row)
        try:
            ts = datetime.fromisoformat(row["ts"]).timestamp()
        except (KeyError, ValueError):
            continue
        u = next((u for u in units if u.session_id == row.get("session_id")
                  and u.kind == row.get("kind") and abs(u.ts - ts) < 2.0), None)
        if truth is None or u is None or not (u.evidence.get("routed") or u.evidence.get("host")):
            missing += 1
            continue
        got = judge_label(model, u.evidence.get("routed", ""), u.evidence.get("host", ""))
        if got is None:
            missing += 1
            continue
        n += 1
        agree += got == truth
        binary += (got in ("used", "corrected")) == (truth in ("used", "corrected"))
    rate = agree / n if n else None
    status = "ACTIVE" if (rate is not None and n >= JUDGE_MIN_N and rate >= JUDGE_ACTIVE_AGREEMENT) else "PROPOSED"
    return {"model": model, "sample": str(sample_path), "n": n, "not_judged": missing,
            "agreement": rate, "agreement_ci95": list(wilson(agree, n)) if n else None,
            "used_vs_not_agreement": (binary / n) if n else None, "status": status,
            "calibrated_at": datetime.now(timezone.utc).isoformat()}


def _apply_judge(units: list[Unit], model: str | None, calibration: dict | None) -> dict:
    if not model:
        return {"enabled": False, "status": "OFF"}
    status = (calibration or {}).get("status", "PROPOSED")
    shadow = {lab: 0 for lab in LABELS}
    judged = 0
    for u in units:
        if u.signal not in AMBIGUOUS_SIGNALS or u.human_reviewed:
            continue
        got = judge_label(model, u.evidence.get("routed", ""), u.evidence.get("host", ""))
        if got is None:
            continue
        judged += 1
        shadow[got] += 1
        if status == "ACTIVE":
            u.label, u.signal, u.confidence = got, f"{JUDGE_SIGNAL_PREFIX}{model}", "low"
    return {"enabled": True, "model": model, "status": status, "calibration": calibration,
            "ambiguous_judged": judged, "judge_labels": shadow,
            "applied_to_headline": status == "ACTIVE"}


# ── public entry points ──────────────────────────────────────────────────────

def run_audit(days: int = 30, projects_dir: Path | None = None, state_dir: Path | None = None,
              now: float | None = None, release: str = "unreleased",
              human_labels: Path | None = None, judge_model: str | None = None,
              judge_calibration: dict | None = None) -> tuple[list[dict], dict]:
    units, ctx = _collect(days, projects_dir or default_projects_dir(),
                          state_dir or default_state_dir(), now)
    return _finish(units, ctx, release, human_labels, judge_model, judge_calibration)


def _finish(units: list[Unit], ctx: dict, release: str, human_labels: Path | None = None,
            judge_model: str | None = None, judge_calibration: dict | None = None) -> tuple[list[dict], dict]:
    _apply_human_labels(units, human_labels)
    judge = _apply_judge(units, judge_model, judge_calibration)
    rows = [u.row() for u in units]
    summary = summarize(rows, ctx, release, judge, _prompts_with_used(units, ctx["sessions"]))
    return rows, summary


def write_artifacts(rows: list[dict], summary: dict, out_dir: Path, release: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = out_dir / f"outcome_audit_{release}.jsonl"
    summ = out_dir / f"outcome_audit_{release}.summary.json"
    jsonl.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    summ.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return jsonl, summ


def current_release() -> str:
    import tomllib
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _fmt(r: dict) -> str:
    if r["n"] == 0:
        return "n=0"
    ci = r["ci95"]
    s = f"{r['k']}/{r['n']} = {100 * r['rate']:.2f}% [{100 * ci[0]:.2f}%, {100 * ci[1]:.2f}%]"
    return s + (" (too few to tell)" if r["too_few_to_tell"] else "")


def print_summary(summary: dict) -> None:
    h = summary["headline"]
    print(f"outcome audit {summary['release']} (audit v{summary['audit_version']}): {summary['window_days']}d, "
          f"{summary['scope']['n_sessions']} organic sessions")
    print(f"  used (headline): {_fmt({'k': h['used'], 'n': h['attempted'], 'rate': h['rate'], 'ci95': h['ci95'], 'too_few_to_tell': h['too_few_to_tell']})}")
    print(f"  not_applicable (outside the denominator): {h['not_applicable']} {summary['not_applicable']['by_signal']}")
    print(f"  used or corrected: {_fmt(summary['used_or_corrected'])}")
    print(f"  prompts with a used unit: {_fmt(summary['prompts'])}")
    print(f"  labels: {summary['labels']}")
    for kind, k in summary["by_kind"].items():
        if k["n_total"]:
            print(f"  {kind:<20} n={k['n']:<5} used {_fmt(k['used'])}  labels={k['labels']}")
    b = summary["binding"]
    print(f"  codex binding: {b['bound']}/{b['delegated']} delegated units bound "
          f"(void {b['void_claude_subagent_ran']}, unpaired {b['no_agent_call_found']}, pairing {b['pairing']})")
    print(f"  holds: {summary['holds']['n']} in {summary['holds']['n_prompts']} prompts; "
          f"replayed tool_uses deduped: {summary['dedup']['replayed_tool_uses']}; "
          f"sessions excluded by override: {len(summary['scope']['session_kind_overrides']['excluded_sessions'])}")
    for name, v in summary["excluded_inflated_sources"].items():
        print(f"  excluded {name}: n={v['n']} counted_as_used={v['counted_as_used']}")
    for kind, v in summary["unscoped"].items():
        print(f"  unscoped {kind}: n={v['n']} provenance={v['provenance']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--until", default=None, metavar="ISO_UTC",
                    help="end of the window (default: now); fixes the window to reproduce an earlier run")
    ap.add_argument("--release", default=None, help="release label (default: pyproject version)")
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--projects-dir", type=Path, default=None)
    ap.add_argument("--state-dir", type=Path, default=None)
    ap.add_argument("--human-labels", type=Path, default=None,
                    help="jsonl of {unit_id, label} from a person's transcript check")
    ap.add_argument("--judge", default=None, help="ollama/<model>; off by default")
    ap.add_argument("--calibrate-judge", type=Path, default=None,
                    help="hand-labelled sample jsonl; calibrates --judge before use")
    args = ap.parse_args(argv)

    release = args.release or current_release()
    out_dir = args.out_dir or default_artifact_dir()
    state = args.state_dir or default_state_dir()
    if out_dir.resolve().is_relative_to(state.resolve()):
        print(f"refusing to write into the state dir {state}", file=sys.stderr)
        return 2
    until = datetime.fromisoformat(args.until.replace("Z", "+00:00")).timestamp() if args.until else None
    calibration = None
    if args.judge and args.calibrate_judge:
        units, _ctx = _collect(args.days, args.projects_dir or default_projects_dir(), state, until)
        calibration = calibrate_judge(args.judge, args.calibrate_judge, units)
        print(f"judge calibration: {json.dumps(calibration)}")
    rows, summary = run_audit(args.days, args.projects_dir, state, until, release,
                              args.human_labels, args.judge, calibration)
    jsonl, summ = write_artifacts(rows, summary, out_dir, release)
    print_summary(summary)
    print(f"  wrote {jsonl}\n  wrote {summ}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
