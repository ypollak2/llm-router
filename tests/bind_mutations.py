#!/usr/bin/env python3
"""A mutation check of the BIND W3 audit, gate and report. This is not a test: pytest does not collect
the file (no ``test_`` prefix), and nothing imports it.

Each mutation is ONE textual change to one script under ``scripts/release/`` (a threshold moved a step, a
comparison flipped from ``>=`` to ``>``, a condition dropped). The runner copies ``tests/`` and ``scripts/``
into a scratch directory, applies the change to the copy, runs the named test files against it and reports

    KILLED    a test failed, so the suite notices the change
    SURVIVED  every test passed: the changed behaviour is not pinned
    BROKEN    the pattern was not found exactly once, or the run could not collect: this says nothing

The live tree is never modified. The test processes get a fresh HOME and LLM_ROUTER_HOME (the safety rule:
nothing here may reach the owner's ~/.llm-router or ~/.claude). A baseline run of the unmutated copy must pass
first, and its test count is printed, so "0 survivors" cannot mean "nothing ran".

    uv run --frozen --no-sync python tests/bind_mutations.py            # all mutations, 4 at a time
    uv run --frozen --no-sync python tests/bind_mutations.py --list
    uv run --frozen --no-sync python tests/bind_mutations.py --only BR-01 AU-12 --jobs 2

Exit code 0 only when no mutation SURVIVED (an ``equivalent`` mutation, one that cannot change behaviour, is
listed apart with its reason) and none is BROKEN. The result of the last full run is in the PR description.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RPT = "scripts/release/bind_report.py"
AUD = "scripts/release/outcome_audit.py"
GATE = "scripts/release/outcome_gate.py"
PRV = "scripts/release/pre-release-verify.sh"

#: The test files a mutation is run against. A file that does not exist is an error, not a skip.
TEST_FILES = (
    "tests/test_bind_audit.py",
    "tests/test_bind_report.py",
    "tests/test_bind_thresholds.py",
    "tests/test_outcome_audit.py",
    "tests/test_outcome_gate.py",
)


@dataclass(frozen=True)
class Mutation:
    id: str
    file: str
    old: str
    new: str
    what: str
    equivalent: str = ""  # a reason, when the change cannot alter behaviour


def _m(id_: str, file: str, old: str, new: str, what: str, equivalent: str = "") -> Mutation:
    return Mutation(id_, file, old, new, what, equivalent)


MUTATIONS: tuple[Mutation, ...] = (
    # ── bind_report.py: the D-358 bar ────────────────────────────────────────
    _m("BR-01", RPT, "BIND_RATE_MIN = 0.90", "BIND_RATE_MIN = 0.91", "binding rate bar 90% -> 91%"),
    _m("BR-02", RPT, "BIND_RATE_MIN = 0.90", "BIND_RATE_MIN = 0.89", "binding rate bar 90% -> 89%"),
    _m("BR-03", RPT, "rate >= BIND_RATE_MIN", "rate > BIND_RATE_MIN", "binding passes at exactly 90%: >= -> >"),
    _m("BR-04", RPT, "BIND_MIN_UNITS = 30", "BIND_MIN_UNITS = 31", "binding needs 30 units -> 31"),
    _m("BR-05", RPT, "BIND_MIN_UNITS = 30", "BIND_MIN_UNITS = 29", "binding needs 30 units -> 29"),
    _m("BR-06", RPT, "len(d) < BIND_MIN_UNITS", "len(d) <= BIND_MIN_UNITS", "30 units is enough: < -> <="),
    _m("BR-07", RPT, "BIND_MIN_DAYS = 3.0", "BIND_MIN_DAYS = 3.5", "binding needs 3 days -> 3.5"),
    _m("BR-08", RPT, "BIND_MIN_DAYS = 3.0", "BIND_MIN_DAYS = 2.5", "binding needs 3 days -> 2.5"),
    _m("BR-09", RPT, "days < BIND_MIN_DAYS", "days <= BIND_MIN_DAYS", "exactly 3 days is enough: < -> <="),
    _m("BR-10", RPT, "BLOCKED_SHARE_MAX = 0.10", "BLOCKED_SHARE_MAX = 0.11", "blocked share bar 10% -> 11%"),
    _m("BR-11", RPT, "BLOCKED_SHARE_MAX = 0.10", "BLOCKED_SHARE_MAX = 0.09", "blocked share bar 10% -> 9%"),
    _m("BR-12", RPT, "share < BLOCKED_SHARE_MAX", "share <= BLOCKED_SHARE_MAX", "exactly 10% fails: < -> <="),
    _m("BR-13", RPT, "HAND_CHECK_N = 20", "HAND_CHECK_N = 21", "hand check of 20 -> 21"),
    _m("BR-14", RPT, "HAND_CHECK_N = 20", "HAND_CHECK_N = 19", "hand check of 20 -> 19"),
    _m("BR-15", RPT, "len(bound) < HAND_CHECK_N", "len(bound) <= HAND_CHECK_N", "20 bound units are enough: < -> <="),
    _m("BR-16", RPT, "HOLDS_PER_100_MAX = 1.0", "HOLDS_PER_100_MAX = 1.5", "hold bar 1 per 100 -> 1.5"),
    _m("BR-17", RPT, "HOLDS_PER_100_MAX = 1.0", "HOLDS_PER_100_MAX = 0.5", "hold bar 1 per 100 -> 0.5"),
    _m("BR-18", RPT, "return p > 0 and 100 * h / p >= HOLDS_PER_100_MAX", "return p > 0 and 100 * h / p > HOLDS_PER_100_MAX",
       "exactly 1 per 100 fails: >= -> >"),
    _m("BR-19", RPT, "return bool(w_) and 100 * w_[0] >= HOLDS_PER_100_MAX", "return bool(w_) and 100 * w_[0] > HOLDS_PER_100_MAX",
       "Wilson lower bound at the bar: >= -> >", "the lower bound is irrational for every n; it cannot equal the bar"),
    _m("BR-20", RPT, "MIN_PROMPTS = 50", "MIN_PROMPTS = 51", "toll floor 50 prompts -> 51"),
    _m("BR-21", RPT, "MIN_PROMPTS = 50", "MIN_PROMPTS = 49", "toll floor 50 prompts -> 49"),
    _m("BR-22", RPT, "if p > 0 and p >= args.min_prompts]", "if p > 0 and p > args.min_prompts]", "50 prompts is enough: >= -> >"),
    _m("BR-23", RPT, "if n_probes > 0 or any(demonstrably_over", "if n_probes > 1 or any(demonstrably_over", "one probe fails the line: > 0 -> > 1"),
    _m("BR-24", RPT, "n_probes = max(len(probes), ledger_probes)", "n_probes = len(probes)", "the ledger's probe rows ignored"),
    _m("BR-25", RPT, "n_probes = max(len(probes), ledger_probes)", "n_probes = ledger_probes", "the transcripts' probes ignored"),
    _m("BR-26", RPT, '        sources.append(("hold ledger", hold_rows.get("blocked", 0), hold_rows["prompt"]))', "        pass",
       "the hold ledger is not a source"),
    _m("BR-27", RPT, "elif not enough:", "elif False:", "INSUFFICIENT never reported for a small source"),
    _m("BR-28", RPT, "    elif small_over:", "    elif False:", "a small source over the bar no longer blocks a PASS"),
    _m("BR-29", RPT, "or any(over_bar(h, p) for h, p in enough):", "or False:", "a sufficient source over the bar no longer fails"),
    _m("BR-30", RPT, "or any(demonstrably_over(h, p) for _n, h, p in sources)", "or False",
       "a demonstrably failing small source no longer fails"),
    _m("BR-32", RPT, 'HAND_CHECK_SEED = "D-358/hand-check/v1"', 'HAND_CHECK_SEED = "D-358/hand-check/v2"', "the sample seed changed"),
    _m("BR-33", RPT, "hexdigest())[:k]", "hexdigest(), reverse=True)[:k]", "the sample takes the other end of the order"),
    _m("BR-34", RPT, "hexdigest())[:k]", "hexdigest())[:k + 1]", "the sample is one unit too big"),
    _m("BR-35", RPT, "covered = {i: given[i] for i in sample if i in given}", "covered = dict(given)",
       "the hand check accepts entries for any unit"),
    _m("BR-36", RPT, "elif len(covered) < len(sample):", "elif len(covered) < len(sample) - 1:", "one unchecked unit is tolerated"),
    _m("BR-37", RPT, "bound = [u for u in d if u.extra.get(\"bound\")]", "bound = [u for u in d if u.extra.get(\"delivered\")]",
       "a re-issued unit counts as bound in the rate"),
    _m("BR-38", RPT, "win_units = [u for u in units if since <= u.ts <= until]", "win_units = [u for u in units if since < u.ts <= until]",
       "a unit at the very start of the window is outside"),
    _m("BR-39", RPT, "win_units = [u for u in units if since <= u.ts <= until]", "win_units = [u for u in units if since <= u.ts < until]",
       "a unit at the very end of the window is outside"),
    _m("BR-40", RPT, "if dt.tzinfo is None:", "if False:", "a time with no zone is accepted"),
    _m("BR-41", RPT, "lo = min(since, until - GATE_DAYS * 86400)", "lo = min(since, until - 29 * 86400)", "outcome look-back 30 days -> 29"),
    _m("BR-42", RPT, "lo = min(since, until - GATE_DAYS * 86400)", "lo = min(since, until - 31 * 86400)", "outcome look-back 30 days -> 31"),
    _m("BR-43", RPT, "live[\"n\"] >= MIN_REFERENCE_UNITS", "live[\"n\"] > MIN_REFERENCE_UNITS", "30 void units are enough: >= -> >"),
    _m("BR-44", RPT, "MIN_REFERENCE_UNITS = 30", "MIN_REFERENCE_UNITS = 31", "live reference needs 30 void units -> 31"),
    _m("BR-45", RPT, "MIN_REFERENCE_UNITS = 30", "MIN_REFERENCE_UNITS = 29", "live reference needs 30 void units -> 29"),
    _m("BR-46", RPT, "reissue_ts = [t for t in ctx.get(\"codex_reissue_ts\", []) if since <= t <= until]",
       "reissue_ts = [t for t in ctx.get(\"codex_reissue_ts\", [])]", "re-issue rows outside the window are counted"),
    _m("BR-47", RPT, '"disclosed": disclosed or []', '"disclosed": []', "the lines next to line 1 (blocked, failed, re-issued) are not printed"),
    _m("BR-48", RPT, 'sources = [("transcripts", derived, prompts_t)]', "sources = []", "the toll line ignores the transcripts"),
    _m("BR-49", RPT, 'verdict = "MET" if all(x["status"] == "PASS" for x in lines if x["status"] != "REPORTED") else "NOT MET"',
       'verdict = "MET" if any(x["status"] == "PASS" for x in lines if x["status"] != "REPORTED") else "NOT MET"',
       "the bar is met when ANY gated line passes"),
    # ── outcome_audit.py: scoring ────────────────────────────────────────────
    _m("AU-01", AUD, "USED_THRESHOLD = 0.60", "USED_THRESHOLD = 0.61", "used at 60% -> 61%"),
    _m("AU-02", AUD, "USED_THRESHOLD = 0.60", "USED_THRESHOLD = 0.59", "used at 60% -> 59%"),
    _m("AU-03", AUD, '    if frac >= USED_THRESHOLD:\n        return "used", "tool_result_reused"',
       '    if frac > USED_THRESHOLD:\n        return "used", "tool_result_reused"', "exactly 60% is used: >= -> >"),
    _m("AU-04", AUD, "AMBIGUOUS_FLOOR = 0.05", "AMBIGUOUS_FLOOR = 0.06", "ambiguous from 5% -> 6%"),
    _m("AU-05", AUD, "AMBIGUOUS_FLOOR = 0.05", "AMBIGUOUS_FLOOR = 0.04", "ambiguous from 5% -> 4%"),
    _m("AU-06", AUD, "if frac >= AMBIGUOUS_FLOOR:", "if frac > AMBIGUOUS_FLOOR:", "exactly 5% is ambiguous: >= -> >"),
    _m("AU-07", AUD, "MIN_ATTRIBUTABLE_WORDS = 8", "MIN_ATTRIBUTABLE_WORDS = 7", "a body is attributable from 8 words -> 7"),
    _m("AU-08", AUD, "MIN_ATTRIBUTABLE_WORDS = 8", "MIN_ATTRIBUTABLE_WORDS = 9", "a body is attributable from 8 words -> 9"),
    _m("AU-09", AUD, "(4 if n_words < 20 else 8)", "(4 if n_words < 19 else 8)", "8-word shingles from 20 words -> 19"),
    _m("AU-10", AUD, "(4 if n_words < 20 else 8)", "(4 if n_words < 21 else 8)", "8-word shingles from 20 words -> 21"),
    _m("AU-11", AUD, "MIN_N = 50", "MIN_N = 51", "too few to tell under 50 -> 51"),
    _m("AU-12", AUD, "MIN_N = 50", "MIN_N = 49", "too few to tell under 50 -> 49"),
    _m("AU-13", AUD, "return len(p) < 150", "return len(p) < 100", "probe: starts with reply, under 150 characters -> 100"),
    _m("AU-14", AUD, "return len(p) < 150", "return len(p) <= 150", "probe: under 150 characters -> up to 150"),
    _m("AU-15", AUD, "and len(p) < 200", "and len(p) < 150", "probe: ends with reply OK, under 200 characters -> 150"),
    _m("AU-16", AUD, "and len(p) < 200", "and len(p) <= 200", "probe: under 200 characters -> up to 200"),
    _m("AU-17", AUD, 'if not p or "?" in p or "\\n" in p:', 'if not p or "\\n" in p:', "a question mark no longer rules out a probe"),
    _m("AU-18", AUD, 'if not p or "?" in p or "\\n" in p:', 'if not p or "?" in p:', "a second line no longer rules out a probe"),
    # ── outcome_audit.py: pairing and binding ────────────────────────────────
    _m("AU-20", AUD, "HOOK_JOIN_BEFORE_S = 2.0", "HOOK_JOIN_BEFORE_S = 1.0", "hook run joins up to 2 s before the row -> 1"),
    _m("AU-21", AUD, "HOOK_JOIN_BEFORE_S = 2.0", "HOOK_JOIN_BEFORE_S = 3.0", "hook run joins up to 2 s before the row -> 3"),
    _m("AU-22", AUD, "HOOK_JOIN_AFTER_S = 20.0", "HOOK_JOIN_AFTER_S = 10.0", "hook run joins up to 20 s after the row -> 10"),
    _m("AU-23", AUD, "HOOK_JOIN_AFTER_S = 20.0", "HOOK_JOIN_AFTER_S = 30.0", "hook run joins up to 20 s after the row -> 30"),
    _m("AU-24", AUD, "AGENT_MATCH_WINDOW_S = 330.0", "AGENT_MATCH_WINDOW_S = 300.0", "last-resort window 330 s -> 300"),
    _m("AU-25", AUD, "AGENT_MATCH_WINDOW_S = 330.0", "AGENT_MATCH_WINDOW_S = 360.0", "last-resort window 330 s -> 360"),
    _m("AU-26", AUD, "call.ts > ts + 2 or ts - call.ts > AGENT_MATCH_WINDOW_S", "call.ts > ts + 1 or ts - call.ts > AGENT_MATCH_WINDOW_S",
       "last-resort slack after the row 2 s -> 1"),
    _m("AU-27", AUD, "call.ts > ts + 2 or ts - call.ts > AGENT_MATCH_WINDOW_S", "call.ts > ts + 5 or ts - call.ts > AGENT_MATCH_WINDOW_S",
       "last-resort slack after the row 2 s -> 5"),
    _m("AU-28", AUD, "if match is None or call.ts > match.ts:", "if match is None or call.ts < match.ts:",
       "last-resort pairing takes the earliest call, not the latest"),
    _m("AU-29", AUD, 'key = (bool(want) and (call.inp.get("subagent_type") or "general-purpose") != want, abs(h_ts - ts))',
       "key = (False, abs(h_ts - ts))", "the subagent_type tie-break between two hook runs is dropped"),
    _m("AU-30", AUD, 'key = (bool(want) and (call.inp.get("subagent_type") or "general-purpose") != want, abs(h_ts - ts))',
       'key = (bool(want) and (call.inp.get("subagent_type") or "general-purpose") != want, -abs(h_ts - ts))',
       "between equal types the FARTHEST hook run is taken"),
    _m("AU-31", AUD, "LOCAL_TASK_KEEP_WINDOW_S = 6 * 3600.0", "LOCAL_TASK_KEEP_WINDOW_S = 3 * 3600.0", "local_task follow-up window 6 h -> 3 h"),
    _m("AU-32", AUD, "LOCAL_TASK_KEEP_WINDOW_S = 6 * 3600.0", "LOCAL_TASK_KEEP_WINDOW_S = 12 * 3600.0", "local_task follow-up window 6 h -> 12 h"),
    _m("AU-33", AUD, "RESPAWN_WINDOW_S = 3600.0", "RESPAWN_WINDOW_S = 1800.0", "re-issue window 3600 s -> 1800"),
    _m("AU-34", AUD, "RESPAWN_WINDOW_S = 3600.0", "RESPAWN_WINDOW_S = 7200.0", "re-issue window 3600 s -> 7200"),
    _m("AU-35", AUD, 'and same_prompt(call.inp.get("prompt"), o.inp.get("prompt"))',
       'and reuse_fraction(call.inp.get("prompt"), o.inp.get("prompt"), n=4) >= 0.5',
       "the re-issue rule reverts to 4-word-shingle similarity"),
    _m("AU-36", AUD, "and bool(a.strip()) and a.strip() == b.strip()", "and bool(a.strip()) and a == b", "a re-issue must match without stripping"),
    _m("AU-37", AUD, "and bool(a.strip()) and a.strip() == b.strip()", "and a.strip() == b.strip()", "two empty prompts are a re-issue"),
    _m("AU-38", AUD, "o.src_path == call.src_path and call.ts < o.ts", "call.ts < o.ts", "a re-issue in another transcript file counts"),
    _m("AU-39", AUD, "delivered = marker and shape and not launched", "delivered = marker and not launched", "delivered ignores the deny shape"),
    _m("AU-40", AUD, "delivered = marker and shape and not launched", "delivered = marker and shape", "delivered ignores a launched Claude sub-agent"),
    _m("AU-41", AUD, "delivered = marker and shape and not launched", "delivered = shape and not launched", "delivered ignores the Codex wrapper"),
    _m("AU-42", AUD, "bound = delivered and not respawned", "bound = delivered", "a re-issued unit is bound"),
    _m("AU-43", AUD, "if all(m in low for m in CODEX_RESULT_MARKERS) or any(m in low for m in LEGACY_CODEX_MARKERS):",
       "if any(m in low for m in CODEX_RESULT_MARKERS) or any(m in low for m in LEGACY_CODEX_MARKERS):", "one wrapper phrase is enough"),
    _m("AU-44", AUD, 'spec.get("permissionDecision") == "deny"', 'spec.get("permissionDecision") is not None', "any permissionDecision is a deny"),
    _m("AU-45", AUD, "reuse_fraction(hook_reason, t) >= USED_THRESHOLD", "reuse_fraction(hook_reason, t) > 0", "any overlap with the hook's reason is a marker"),
    _m("AU-46", AUD, 'rendered = bool(call.result_is_error) and', "rendered = True and", "the rendered deny need not be an error result"),
    _m("AU-47", AUD, '.lstrip().startswith("PreToolUse:Agent hook error")', '.lstrip().startswith("")', "the rendered deny need not open with the hook-error prefix"),
    # ── outcome_audit.py: local_task effect ─────────────────────────────────
    _m("AU-51", AUD, r'(?<![\w.~/-])" + re.escape(wd) + r"(?![\w.-])"', r'(?<![\w.~/-])" + re.escape(wd) + r"(?![\w.])"',
       "a hyphenated sibling directory (wt-a-2) is read as the workdir"),
    _m("AU-52", AUD, r'(?<![\w.~/-])" + re.escape(wd)', r'(?<![.~/-])" + re.escape(wd)', "another tree with the same tail is read as the workdir"),
    _m("AU-53", AUD, 'everything = not paths and ("A" in short or "u" in short or "--all" in flags or "--update" in flags)',
       'everything = not paths', "any bare `git add` stages everything"),
    _m("AU-54", AUD, "covered = covered or everything or (bool(wanted) and any(_covers(p, wanted) for p in paths))",
       "covered = covered or everything or bool(paths)", "any named path counts as staging the changed file"),
    _m("AU-55", AUD, 'elif sub == "commit" and (covered or "a" in short or "--all" in flags):', 'elif sub == "commit":',
       "any commit counts as keeping the edit"),
    _m("AU-56", AUD, 'if wanted and any(_covers(p, wanted) for p in paths):', "if True:", "any checkout or restore is an undo"),
    _m("AU-57", AUD, "return p in wanted or any(f.startswith(p.rstrip(\"/\") + \"/\") for f in wanted)", "return True",
       "any pathspec covers the changed files"),
    _m("AU-58", AUD, 'amended = amended or (val.startswith(wd + "/") and val[len(wd) + 1:] in wanted)',
       "amended = amended or val.startswith(wd)", "an edit of a sibling's file reads as an amend"),
    # ── outcome_audit.py: each fix removed (the nine BINDAUDIT defects and the review's) ─────────
    _m("AF-01", AUD, 'if tu.get("id") in seen:', "if False:", "BINDAUDIT-7: replayed tool_use ids are counted twice"),
    _m("AF-02", AUD, "        overrides = read_session_overrides(state_dir / OVERRIDES_FILENAME)", "        overrides = {}",
       "BINDAUDIT-3: session_kind_overrides.json is ignored"),
    _m("AF-03", AUD, "if looks_launched(call.result_text) or call.tid in agents.launched:", "if looks_launched(call.result_text):",
       "BINDAUDIT-2: a sub-agent .meta.json that names the call is not read as a launch"),
    _m("AF-04", AUD, "if looks_launched(call.result_text) or call.tid in agents.launched:", "if call.tid in agents.launched:",
       "a launch announced in the result text is not read as a launch"),
    _m("AF-05", AUD, 'return isinstance(tur, dict) and bool(tur.get("agentId") or tur.get("status") in ("async_launched", "completed"))',
       "return False", "toolUseResult.agentId / status is not read as a launch"),
    _m("AF-06", AUD, "return n_words if n_words < MIN_ATTRIBUTABLE_WORDS else (4 if n_words < 20 else 8)",
       "return n_words if n_words < MIN_ATTRIBUTABLE_WORDS else 8", "BINDAUDIT-4: shingles are always 8 words"),
    _m("AF-07", AUD, "return len(p) < 150", "return len(p) < 300", "BINDAUDIT-6: a probe may be up to 300 characters"),
    _m("AF-08", AUD, "host, uses, _stop = s.reply_after(at, inputs=True)", "host, uses, _stop = s.reply_after(at, inputs=False)",
       "BINDAUDIT-5: tool inputs are not the host's output"),
    _m("AF-09", AUD, "return any(prev <= k < i for k, _tid in self.hold_idx)", "return False",
       "BINDAUDIT-6: a call made to clear a hold is not told from a probe"),
    _m("AF-10", AUD, "USED_THRESHOLD = 0.60", "USED_THRESHOLD = 0.30", "used at 60% -> 30%"),
    _m("AF-11", AUD, "attempted = len(ks) - labels[NOT_APPLICABLE]", "attempted = len(ks)",
       "BINDAUDIT-6: not_applicable units stay in the denominator"),
    _m("AF-12", AUD, "    for sep in _FOOTER_SEPS:", "    for sep in ():", "BINDAUDIT-4: the router's footer is not stripped from the body"),
    _m("AF-13", AUD, "for s in sorted(sessions.values(), key=lambda x: (x.first_ts, x.sid)):",
       "for s in sorted(sessions.values(), key=lambda x: (-x.first_ts, x.sid)):", "BINDAUDIT-7: a replay is credited to the newer session"),
    _m("AF-14", AUD, "stub = None if is_err else _STUB_RE.search(text)", "stub = None", "BINDAUDIT-8: a backgrounded call is scored on its stub"),
    _m("AF-15", AUD, 'if outcome == "codex_blocked":', "if False:", "a codex_blocked row is not labelled"),
    _m("AF-16", AUD, 'if outcome == "codex_failed":', "if False:", "a codex_failed row is not labelled"),
    _m("AF-17", AUD, "    if isinstance(tid, str) and tid:\n        claimed.add(tid)", "    if False:\n        claimed.add(tid)",
       "BINDAUDIT-1: a ledger tool_use_id is not used to pair"),
    _m("AF-18", AUD, '"was run by codex cli", "untrusted sub-agent result from codex cli"',
       '"was executed by codex cli", "untrusted sub-agent result from codex cli"',
       "the wrapper's wording drifts away from the audit's first marker"),
    _m("AF-19", AUD, '"was run by codex cli", "untrusted sub-agent result from codex cli"',
       '"was run by codex cli", "untrusted sub-agent output from codex cli"',
       "the wrapper's fence wording drifts away from the audit's second marker"),
    # ── outcome_gate.py: the sticky baseline ────────────────────────────────
    _m("GT-01", GATE, 'if own.get("artifact_sha256") == sha:', 'if own.get("artifact_sha256") != sha:', "the same artifact is read as a different one"),
    _m("GT-02", GATE, 'if rate < own["used_rate"]:', 'if rate <= own["used_rate"]:', "an equal rate from another artifact fails"),
    _m("GT-09", GATE, '        prev = same[-1]\n        if rate < prev["used_rate"]:', '        prev = same[-1]\n        if rate <= prev["used_rate"]:',
       "a later release equal to the previous one fails"),
    _m("GT-10", GATE, '            if rate < prev["used_rate"]:', '            if rate <= prev["used_rate"]:',
       "a --cross-version compare run equal to the earlier entry fails"),
    _m("GT-03", GATE, 'if rate < own["used_rate"]:', 'if rate < 0:', "a worse re-run of the baseline release passes"),
    _m("GT-04", GATE, "if own is not None and not args.force_baseline:", "if own is not None and False:",
       "a re-run replaces the baseline without --force-baseline"),
    _m("GT-05", GATE, '"artifact_sha256": sha}', '"artifact_sha256": None}', "the entry does not record the artifact hash"),
    _m("GT-06", GATE, 'entry["replaced"] = {', 'entry["replaced_"] = {', "a forced replacement does not record what it replaced"),
    _m("GT-07", GATE, "own = releases[idx] if idx is not None and audit_version(releases[idx]) == cur_v and not same else None",
       "own = None", "the baseline is never sticky"),
    _m("GT-08", GATE, "and not same else None", "else None", "a later release's re-run is read as the baseline's"),
    # ── outcome_gate.py: each fix removed ───────────────────────────────────
    _m("GF-01", GATE, 'choices=CROSS_VERSION_POLICIES, default="refuse",', 'choices=CROSS_VERSION_POLICIES, default="compare",',
       "BINDAUDIT-9: rates are compared across audit versions without being asked"),
    _m("GF-02", GATE, '        if args.cross_version == "refuse":', "        if False:", "BINDAUDIT-9: a version mismatch passes under the default policy"),
    _m("GF-03", GATE, 'if r.get("signal") not in _A.NOT_APPLICABLE_SIGNALS and not person:', "if False:",
       "BINDAUDIT-6: any signal may mark a unit not_applicable"),
    _m("GF-04", GATE, 'attempted = sum(r.get("label") != _A.NOT_APPLICABLE for r in rows)', "attempted = len(rows)",
       "BINDAUDIT-6: the recount counts not_applicable units as attempted"),
    _m("GF-05", GATE, "    if n < MIN_N:", "    if False:", "too few to tell no longer fails the gate"),
    # ── pre-release-verify.sh ───────────────────────────────────────────────
    _m("PV-01", PRV, '    OUTCOME_ARGS+=(--cross-version "$OUTCOME_CROSS_VERSION")', "    :", "the flag is not forwarded to the gate"),
    _m("PV-02", PRV, "OUTCOME_CROSS_VERSION=\"${2:-}\"", "OUTCOME_CROSS_VERSION=\"rebaseline\"", "the flag is always rebaseline"),
)

def _selected(only: list[str]) -> list[Mutation]:
    return [m for m in MUTATIONS if not only or m.id in only]


def _copy_tree(dst: Path) -> None:
    """tests/ and scripts/ are real copies (tests locate scripts by their own path); the rest is linked."""
    for name in ("tests", "scripts"):
        shutil.copytree(REPO / name, dst / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("conftest.py", "pyproject.toml", "setup.cfg"):
        if (REPO / name).exists():
            shutil.copy2(REPO / name, dst / name)
    for entry in REPO.iterdir():
        if entry.name in (".git", ".venv", "tests", "scripts", "conftest.py", "pyproject.toml", "setup.cfg", "__pycache__"):
            continue
        if not (dst / entry.name).exists():
            (dst / entry.name).symlink_to(entry)


def _env(tree: Path, home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("LLM_ROUTER_HOME", "CLAUDE_PROJECTS_DIR", "BIND_W1_HOOK")}
    env.update(HOME=str(home), LLM_ROUTER_HOME=str(home / ".llm-router"), PYTHONPATH=str(tree / "src"),
               PYTHONDONTWRITEBYTECODE="1")
    return env


def _pytest(tree: Path, home: Path, files: tuple[str, ...], stop: bool) -> tuple[int, str]:
    # no -q of its own: pyproject's addopts already has one, and a second hides the "N passed" line the baseline needs
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--no-header", "-p", "no:randomly", *(["-x"] if stop else []),
           *files]
    p = subprocess.run(cmd, cwd=tree, env=_env(tree, home), capture_output=True, text=True, timeout=900)
    return p.returncode, p.stdout + p.stderr


def _run_one(m: Mutation, tree: Path, home: Path) -> tuple[str, str]:
    path = tree / m.file
    original = path.read_text(encoding="utf-8")
    if original.count(m.old) != 1:
        return "BROKEN", f"pattern found {original.count(m.old)} times (needs exactly 1)"
    try:
        path.write_text(original.replace(m.old, m.new, 1), encoding="utf-8")
        rc, out = _pytest(tree, home, TEST_FILES, stop=True)
    finally:
        path.write_text(original, encoding="utf-8")
    if rc == 0:
        return "SURVIVED", ""
    if rc == 1:
        failed = [ln for ln in out.splitlines() if ln.startswith("FAILED ")]
        return "KILLED", (failed[0][7:100] if failed else "")
    return "BROKEN", f"pytest exit {rc}: " + (out.strip().splitlines() or [""])[-1][:100]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="print the mutations and exit")
    ap.add_argument("--only", nargs="*", default=[], metavar="ID")
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args(argv)
    todo = _selected(args.only)
    if args.list:
        for m in todo:
            print(f"{m.id}  {m.file.rsplit('/', 1)[-1]:<22} {m.what}" + (f"   [equivalent: {m.equivalent}]" if m.equivalent else ""))
        print(f"{len(todo)} mutations")
        return 0
    missing = [f for f in TEST_FILES if not (REPO / f).is_file()]
    if missing:
        print(f"missing test files: {missing}", file=sys.stderr)
        return 2
    results: dict[str, tuple[str, str]] = {}
    with tempfile.TemporaryDirectory(prefix="bind-mut-") as td:
        root = Path(td)
        n_jobs = max(1, min(args.jobs, len(todo) or 1))
        trees = []
        for j in range(n_jobs):
            tree = root / f"tree{j}"
            tree.mkdir()
            _copy_tree(tree)
            home = root / f"home{j}"
            home.mkdir()
            trees.append((tree, home))
        rc, out = _pytest(trees[0][0], trees[0][1], TEST_FILES, stop=False)
        tail = [ln for ln in out.splitlines() if " passed" in ln or " failed" in ln or " error" in ln]
        print("baseline (unmutated copy): " + (tail[-1].strip() if tail else out.strip()[-200:]))
        if rc != 0 or not any(re.search(r"\b[1-9]\d* passed", ln) for ln in tail):
            print("the unmutated copy does not pass (or ran no tests): nothing to compare against", file=sys.stderr)
            return 2

        def work(i_m: tuple[int, Mutation]):
            i, m = i_m
            return m.id, _run_one(m, *trees[i % n_jobs])

        # one worker per tree: round-robin assignment keeps a tree single-threaded
        buckets: list[list[tuple[int, Mutation]]] = [[] for _ in range(n_jobs)]
        for i, m in enumerate(todo):
            buckets[i % n_jobs].append((i, m))

        def run_bucket(b):
            return [work(x) for x in b]

        with cf.ThreadPoolExecutor(max_workers=n_jobs) as ex:
            for part in ex.map(run_bucket, buckets):
                for mid, res in part:
                    results[mid] = res
    by_id = {m.id: m for m in todo}
    for m in todo:
        status, detail = results[m.id]
        mark = status if not (status == "SURVIVED" and m.equivalent) else "EQUIVALENT"
        print(f"{mark:<10} {m.id}  {m.file.rsplit('/', 1)[-1]:<22} {m.what}" + (f"   <- {detail}" if detail and status != "KILLED" else ""))
    killed = [i for i, (s, _d) in results.items() if s == "KILLED"]
    survived = [i for i, (s, _d) in results.items() if s == "SURVIVED" and not by_id[i].equivalent]
    equiv = [i for i, (s, _d) in results.items() if s == "SURVIVED" and by_id[i].equivalent]
    broken = [i for i, (s, _d) in results.items() if s == "BROKEN"]
    print(f"\n{len(todo)} mutations: {len(killed)} killed, {len(survived)} survived, {len(equiv)} equivalent, {len(broken)} broken")
    if survived:
        print("survived: " + ", ".join(survived))
    if broken:
        print("broken: " + ", ".join(broken))
    return 0 if not survived and not broken else 1


if __name__ == "__main__":
    sys.exit(main())
