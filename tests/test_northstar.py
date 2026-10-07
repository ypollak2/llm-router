"""NS1 — the North Star metric.

Each scenario below is a minimal, hand-built transcript/log pair standing in
for one classification path in ``llm_router.northstar``. Assertions check the
REASON (``signal``/``outcome``) a unit was given, not only a count, per
CLAUDE.md's "assert the reason, not just the boolean."
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

import pytest

from llm_router import northstar as ns

SID_MAIN = "aaaaaaaa-1111-2222-3333-444444444444"


def _iso(epoch: float) -> str:
    """Claude Code transcripts carry ISO8601 'Z' timestamps, never raw epoch floats."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _user(sid, text, ts, tool_results=None):
    content = text if tool_results is None else (tool_results + ([{"type": "text", "text": text}] if text else []))
    return {"parentUuid": None, "isSidechain": False, "type": "user", "uuid": str(ts),
            "timestamp": _iso(ts), "sessionId": sid, "message": {"role": "user", "content": content}}


def _assistant(sid, ts, text=None, tool_uses=None):
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    for tu in (tool_uses or []):
        content.append(tu)
    return {"parentUuid": None, "isSidechain": False, "type": "assistant", "uuid": str(ts),
            "timestamp": _iso(ts), "sessionId": sid, "message": {"role": "assistant", "content": content}}


def _tool_use(id_, name, input_=None):
    return {"type": "tool_use", "id": id_, "name": name, "input": input_ or {}}


def _tool_result(use_id, text):
    return {"type": "tool_result", "tool_use_id": use_id, "content": [{"type": "text", "text": text}]}


def _project(tmp_path, name="-Users-x-proj"):
    return tmp_path / "claude_projects" / name


def _debug_log(router_home):
    return router_home / "auto-route-debug.log"


def _write_debug_log(tmp_path, lines):
    p = _home_dir(tmp_path) / "auto-route-debug.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _home_dir(tmp_path):
    return tmp_path / "router_home"


def _bulk_user_prompts(sid, n, start_ts=1_800_000_000):
    """n throwaway real user_prompt units, to clear MIN_UNITS without noise."""
    out = []
    for i in range(n):
        out.append(_user(sid, f"please help with unrelated task number {i}", start_ts + i))
    return out


# ── used-by-attribution: signal (a), the hook's own DRAFT USED verdict ──────

def test_draft_used_via_hook_verdict(tmp_path):
    sid = SID_MAIN
    _write_debug_log(tmp_path, [
        f"[2026-09-27 10:00:00] [INVOCATION 1800000000.000] prompt_len=20 session_id={sid[:8]}",
        "[2026-09-27 10:00:00] [INVOCATION 1800000000.000] DIRECT SUCCESS: model=ollama/qwen3 latency=500ms",
        f"[2026-09-27 10:00:05] [INVOCATION 1800000005.000] prompt_len=10 session_id={sid[:8]}",
        "[2026-09-27 10:00:05] [INVOCATION 1800000005.000] DRAFT USED",
    ])
    records = [_user(sid, "what does this function do", 1_800_000_000)]
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    drafts = [u for u in rows if u["kind"] == ns.UNIT_DRAFT]
    assert len(drafts) == 1
    assert drafts[0]["outcome"] == ns.OUTCOME_USED
    assert drafts[0]["signal"] == "draft_verdict"


def test_draft_unused_is_discarded_not_redo(tmp_path):
    sid = SID_MAIN
    _write_debug_log(tmp_path, [
        f"[2026-09-27 10:00:00] [INVOCATION 1800000000.000] prompt_len=20 session_id={sid[:8]}",
        "[2026-09-27 10:00:00] [INVOCATION 1800000000.000] DIRECT SUCCESS: model=ollama/qwen3 latency=500ms",
        f"[2026-09-27 10:00:05] [INVOCATION 1800000005.000] prompt_len=10 session_id={sid[:8]}",
        "[2026-09-27 10:00:05] [INVOCATION 1800000005.000] DRAFT UNUSED",
    ])
    records = [_user(sid, "what does this function do", 1_800_000_000)]
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    drafts = [u for u in rows if u["kind"] == ns.UNIT_DRAFT]
    assert drafts[0]["outcome"] == ns.OUTCOME_DISCARDED
    assert drafts[0]["signal"] == "draft_verdict"


# ── used-by-attribution fallback: signal (b), reconstructed from transcript ─

def test_draft_used_reconstructed_from_transcript_when_hook_verdict_missing(tmp_path):
    sid = SID_MAIN
    _write_debug_log(tmp_path, [
        f"[2026-09-27 10:00:00] [INVOCATION 1800000000.000] prompt_len=20 session_id={sid[:8]}",
        "[2026-09-27 10:00:00] [INVOCATION 1800000000.000] DIRECT SUCCESS: model=ollama/qwen3 latency=500ms",
        # no next invocation, no DRAFT USED/UNUSED line at all — reconstruct from transcript
    ])
    records = [_user(sid, "what does this function do", 1_800_000_000)]
    records.append(_assistant(sid, 1_800_000_002,
                               text="🎯 LLM Router routed → ollama/qwen3 · query/simple\n\nIt parses the file."))
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    drafts = [u for u in rows if u["kind"] == ns.UNIT_DRAFT]
    assert drafts[0]["outcome"] == ns.OUTCOME_USED
    assert drafts[0]["signal"] == "attribution_reconstructed"


# ── used-by-reuse: signal (c), routed_mcp tool result reused verbatim ───────

def test_routed_mcp_result_reused_counts_as_used(tmp_path):
    sid = SID_MAIN
    result_text = " ".join(f"word{i}" for i in range(20))  # 13 distinct 8-gram shingles
    records = [_user(sid, "please analyze this design for correctness", 1_800_000_000)]
    records.append(_assistant(sid, 1_800_000_001, tool_uses=[
        _tool_use("toolu_1", "mcp__llm_router__llm", {"task": "analyze"}),
    ]))
    records.append(_user(sid, "", 1_800_000_002, tool_results=[_tool_result("toolu_1", result_text)]))
    # Claude's reply repeats the routed content near-verbatim -> reused
    records.append(_assistant(sid, 1_800_000_003, text=result_text))
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    mcp = [u for u in rows if u["kind"] == ns.UNIT_ROUTED_MCP]
    assert len(mcp) == 1
    assert mcp[0]["outcome"] == ns.OUTCOME_USED
    assert mcp[0]["signal"] == "tool_result_reused"
    assert mcp[0]["task_type"] == "analyze"
    assert mcp[0]["lever"] == "mcp_llm"


def test_routed_mcp_result_rewritten_and_edited_counts_as_redo(tmp_path):
    sid = SID_MAIN
    result_text = " ".join(f"routedword{i}" for i in range(20))
    records = [_user(sid, "please fix this bug in the parser", 1_800_000_000)]
    records.append(_assistant(sid, 1_800_000_001, tool_uses=[
        _tool_use("toolu_1", "mcp__llm_router__llm", {"task": "code"}),
    ]))
    records.append(_user(sid, "", 1_800_000_002, tool_results=[_tool_result("toolu_1", result_text)]))
    # Claude ignores the routed text and does its own edit instead -> redo
    records.append(_assistant(sid, 1_800_000_003, text="Actually I'll fix this differently.",
                               tool_uses=[_tool_use("toolu_2", "Edit", {"file_path": "x.py"})]))
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    mcp = [u for u in rows if u["kind"] == ns.UNIT_ROUTED_MCP][0]
    assert mcp["outcome"] == ns.OUTCOME_REDO
    assert mcp["signal"] == "tool_result_reused"


def test_routed_mcp_with_no_following_turn_is_unknown(tmp_path):
    sid = SID_MAIN
    records = [_user(sid, "please research this topic thoroughly", 1_800_000_000)]
    records.append(_assistant(sid, 1_800_000_001, tool_uses=[
        _tool_use("toolu_1", "mcp__llm_router__llm", {"task": "research"}),
    ]))
    # no tool_result, no following assistant turn: no evidence either way
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    mcp = [u for u in rows if u["kind"] == ns.UNIT_ROUTED_MCP][0]
    assert mcp["outcome"] == ns.OUTCOME_UNKNOWN


# ── DIRECT (zero-Claude replacement): signal (d) ────────────────────────────

def test_direct_replacement_with_no_reask_is_used(tmp_path):
    sid = SID_MAIN
    _write_debug_log(tmp_path, [
        f"[2026-09-27 10:00:00] [INVOCATION 1800000000.000] prompt_len=20 session_id={sid[:8]}",
        "[2026-09-27 10:00:00] [INVOCATION 1800000000.000] DIRECT SUCCESS: model=codex/gpt-5.5 latency=900ms",
        "[2026-09-27 10:00:00] [INVOCATION 1800000000.000] ZERO_CLAUDE REPLACED: render_mode=block model=codex/gpt-5.5",
    ])
    records = [_user(sid, "what year did apollo 11 land", 1_800_000_000)]
    records.append(_user(sid, "thanks, what about apollo 12", 1_800_000_050))
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    direct = [u for u in rows if u["kind"] == ns.UNIT_DIRECT][0]
    assert direct["outcome"] == ns.OUTCOME_USED
    assert direct["signal"] == "zero_claude_stood"
    assert direct["lever"] == "direct"


def test_direct_replacement_followed_by_claude_reask_is_redo(tmp_path):
    sid = SID_MAIN
    _write_debug_log(tmp_path, [
        f"[2026-09-27 10:00:00] [INVOCATION 1800000000.000] prompt_len=20 session_id={sid[:8]}",
        "[2026-09-27 10:00:00] [INVOCATION 1800000000.000] DIRECT SUCCESS: model=codex/gpt-5.5 latency=900ms",
        "[2026-09-27 10:00:00] [INVOCATION 1800000000.000] ZERO_CLAUDE REPLACED: render_mode=block model=codex/gpt-5.5",
    ])
    records = [_user(sid, "what year did apollo 11 land", 1_800_000_000)]
    records.append(_user(sid, "claude: no really, what year", 1_800_000_050))
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    direct = [u for u in rows if u["kind"] == ns.UNIT_DIRECT][0]
    assert direct["outcome"] == ns.OUTCOME_REDO
    assert direct["signal"] == "zero_claude_stood"


# ── DIRECT only when the turn was actually replaced (#212 audit) ──────────
# Line shapes copied from ~/.llm-router/auto-route-debug.log (prompt text
# paraphrased). Before this fix any "ZERO_CLAUDE" substring made the unit
# direct, so the scoped-edit DECLINE lines -- logged on every prompt when
# LLM_ROUTER_ZERO_CLAUDE_SCOPE=edit is set -- turned ordinary advisory drafts
# into "direct / used". All 9 used direct units in the 2026-09-29 audit were
# this.

_DECLINE_LINES = [
    "ZERO_CLAUDE_EDIT: not edit-class — no imperative edit verb (fix/change/rename/add ...)",
    "ZERO_CLAUDE_EDIT: not edit-class — names 8 files; scoped edit handles at most 3",
    "ZERO_CLAUDE_EDIT: explicit claude: prefix — native use",
    "ZERO_CLAUDE_EDIT: target file(s) have uncommitted changes — ['pkg/mod01.py']",
    "ZERO_CLAUDE_EDIT: cwd is not inside a git repository",
    "ZERO_CLAUDE_EDIT: quality breaker open — edit class failing",
    "ZERO_CLAUDE_EDIT BLOCKED: model output failed validation",
    "ZERO_CLAUDE DIRECT_FAILED",
    "ZERO_CLAUDE BLOCKED_EXTERNAL_FAILURE",
    "ZERO_CLAUDE BLOCKED_EMPTY_PROMPT",
    "ZERO_CLAUDE EXPLICIT_NATIVE",
]


@pytest.mark.parametrize("decline", _DECLINE_LINES)
def test_zero_claude_decline_line_does_not_make_a_draft_direct(tmp_path, decline):
    sid = SID_MAIN
    _write_debug_log(tmp_path, [
        f"[2026-09-27 10:00:00] [INVOCATION 1800000000.000] prompt_len=20 session_id={sid[:8]}",
        f"[2026-09-27 10:00:00] [INVOCATION 1800000000.000] {decline}",
        "[2026-09-27 10:00:09] [INVOCATION 1800000000.000] DIRECT SUCCESS: model=ollama/qwen3-coder:30b latency=9878ms files_read=0",
        f"[2026-09-27 10:00:30] [INVOCATION 1800000030.000] prompt_len=10 session_id={sid[:8]}",
        "[2026-09-27 10:00:30] [INVOCATION 1800000030.000] DRAFT UNUSED: the draft from invocation 1800000000.0 (ollama/qwen3-coder:30b) was discarded; Claude answered instead",
    ])
    records = [_user(sid, "how does the cache key get built", 1_800_000_000)]
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    assert [u for u in rows if u["kind"] == ns.UNIT_DIRECT] == []
    drafts = [u for u in rows if u["kind"] == ns.UNIT_DRAFT]
    assert len(drafts) == 1
    assert drafts[0]["outcome"] == ns.OUTCOME_DISCARDED
    assert drafts[0]["signal"] == "draft_verdict"


@pytest.mark.parametrize("line,replaced", [
    ("ZERO_CLAUDE REPLACED: render_mode=block model=ollama/qwen3.5:latest", True),
    ("ZERO_CLAUDE_EDIT APPLIED: model=qwen3.5:latest files=['pkg/mod01.py']", True),
] + [(d, False) for d in _DECLINE_LINES] + [
    ("DIRECT SUCCESS: model=ollama/qwen3.5:latest latency=500ms files_read=0", False),
])
def test_invocation_replaced_turn_recognises_only_replacement_lines(line, replaced):
    assert ns._invocation_replaced_turn([line]) is replaced


# ── exclusions ───────────────────────────────────────────────────────────────

def test_synthetic_session_is_excluded_entirely(tmp_path):
    """The file-level ``is_synthetic_session`` skip in ``build_sessions``, not
    just ``classify_drop``'s per-user-prompt check.

    A fixture built only from user_prompt text is exonerated twice over:
    ``classify_drop`` independently re-checks ``is_synthetic_session`` on
    every user turn's text (``scripts/groundtruth/sources.py``), so it alone
    empties the session even if the file-level skip in ``build_sessions`` is
    disabled entirely. This assistant turn is NOT covered by that per-text
    check (only user-type records go through ``classify_drop``), so it is
    the thing that actually exercises the file-level skip.
    """
    sid = "deadbee2-0000-0000-0000-000000000000"  # matches _HEX_FIXTURE_STEMS
    records = [_user(sid, "what is the capital of Portugal", 1_800_000_000)]
    records.append(_assistant(sid, 1_800_000_001, text="an assistant turn"))
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    data = ns.report(days=None, session_id=sid, root=proj.parent)
    assert data["sessions"] == []
    assert data["aggregate"]["n_sessions"] == 0


def test_sandbox_workspace_is_excluded(tmp_path):
    """The file-level ``sandbox`` skip in ``build_sessions``, not just
    ``classify_drop``'s per-user-prompt ``workspace_is_sandbox`` check.

    Same gap as ``test_synthetic_session_is_excluded_entirely``: a
    user-prompt-only fixture is exonerated twice over, once here and once by
    ``classify_drop``. The assistant turn is not covered by that per-text
    check, so it is what actually exercises the file-level skip.
    """
    sid = SID_MAIN
    records = [_user(sid, "what is the current value of MAX_VALUE", 1_800_000_000)]
    records.append(_assistant(sid, 1_800_000_001, text="an assistant turn"))
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path, name="-private-tmp-bq-claude")
    _write_jsonl(proj / f"{sid}.jsonl", records)

    data = ns.report(days=None, session_id=sid, root=proj.parent)
    assert data["sessions"] == []


# ── too few to tell ──────────────────────────────────────────────────────────

def test_below_min_units_reports_no_share(tmp_path):
    sid = SID_MAIN
    records = [_user(sid, "a short real question about the repo", 1_800_000_000)]
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    data = ns.report(days=None, session_id=sid, root=proj.parent)
    row = data["sessions"][0]
    assert row["units"] < ns.MIN_UNITS
    assert row["share"] is None
    assert data["aggregate"]["too_few"] is True
    assert data["aggregate"]["median"] is None


# ── report() schema, and that it is computed FROM units() ───────────────────

def test_report_schema_is_pinned(tmp_path):
    sid = SID_MAIN
    records = [_user(sid, "please help me understand this repository", 1_800_000_000)]
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    data = ns.report(days=None, session_id=sid, root=proj.parent)
    assert set(data.keys()) == {"window_days", "generated_at", "aggregate", "sessions", "by_kind"}
    assert set(data["aggregate"].keys()) == {"n_sessions", "median", "p25", "max", "too_few"}
    assert set(data["sessions"][0].keys()) == {
        "session_id", "units", "used", "attempted", "unknown", "redo", "share",
        "strict_used", "strict_share",  # P0.8: the Stop line reads the strict rule
    }
    for kind in ns.ALL_KINDS:
        assert set(data["by_kind"][kind].keys()) == {"units", "attempted", "used", "redo", "unknown"}


def test_report_matches_units(tmp_path):
    """report()'s aggregate counts must be exactly recomputed from units()."""
    sid = SID_MAIN
    result_text = " ".join(f"word{i}" for i in range(20))
    records = [_user(sid, "please analyze this design for correctness", 1_800_000_000)]
    records.append(_assistant(sid, 1_800_000_001, tool_uses=[
        _tool_use("toolu_1", "mcp__llm_router__llm", {"task": "analyze"}),
    ]))
    records.append(_user(sid, "", 1_800_000_002, tool_results=[_tool_result("toolu_1", result_text)]))
    records.append(_assistant(sid, 1_800_000_003, text=result_text))
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    data = ns.report(days=None, session_id=sid, root=proj.parent)
    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))

    total_used = sum(1 for u in rows if u["outcome"] == ns.OUTCOME_USED)
    total_attempted = sum(1 for u in rows if u["kind"] in ns.ATTEMPTED_KINDS)
    session_row = data["sessions"][0]
    assert session_row["units"] == len(rows)
    assert session_row["used"] == total_used
    assert session_row["attempted"] == total_attempted

    by_kind_units = sum(v["units"] for v in data["by_kind"].values())
    assert by_kind_units == len(rows)
    by_kind_used = sum(v["used"] for v in data["by_kind"].values())
    assert by_kind_used == total_used


# ── sub-agent join ───────────────────────────────────────────────────────────

def test_child_session_folds_into_parent_as_sidechain(tmp_path):
    parent_sid = "bbbbbbbb-1111-2222-3333-444444444444"
    child_sid = "cccccccc-1111-2222-3333-444444444444"
    dispatch_prompt = "Fix the verdict-integrity bug and open a PR. Work autonomously."

    agent_calls_path = _home_dir(tmp_path) / "agent_calls.json"
    agent_calls_path.parent.mkdir(parents=True, exist_ok=True)
    agent_calls_path.write_text(json.dumps({"calls": [
        {"timestamp": 1_800_000_000.0, "subagent_type": "general-purpose",
         "prompt": dispatch_prompt, "decision": "allowed_routed_spawn",
         "session_id": parent_sid},
    ]}), encoding="utf-8")

    proj = _project(tmp_path)
    parent_records = [_user(parent_sid, "please delegate this to a sub-agent", 1_799_999_990)]
    parent_records += _bulk_user_prompts(parent_sid, 55, start_ts=1_800_000_200)
    _write_jsonl(proj / f"{parent_sid}.jsonl", parent_records)

    child_records = [_user(child_sid, dispatch_prompt, 1_800_000_030)]
    for i in range(5):
        child_records.append(_assistant(child_sid, 1_800_000_031 + i, text=f"working step {i}"))
    _write_jsonl(proj / f"{child_sid}.jsonl", child_records)

    data = ns.report(days=None, root=proj.parent)
    session_ids = {row["session_id"] for row in data["sessions"]}
    assert parent_sid in session_ids
    assert child_sid not in session_ids

    rows = list(ns.units(days=None, root=proj.parent))
    sidechain = [u for u in rows if u["kind"] == ns.UNIT_SIDECHAIN]
    assert len(sidechain) == 5
    assert all(u["session_id"] == parent_sid for u in sidechain)
    assert all(u["lever"] == "agent_route" for u in sidechain)

    # the child's dispatch message is an orchestration instruction, not a
    # human prompt, and must not inflate user_prompt: parent has 55 bulk +
    # its own 1 real prompt = 56, and nothing from the child.
    user_prompts = [u for u in rows if u["kind"] == ns.UNIT_USER_PROMPT]
    assert len(user_prompts) == 56
    assert all(u["session_id"] == parent_sid for u in user_prompts)


def test_child_session_folds_via_ledger_only_when_absent_from_agent_calls_json(tmp_path):
    """The join #180 could only ever make against ``agent_calls.json``'s
    50-cap rolling file. This spawn is recorded ONLY in the 30-day
    ``agent_calls_ledger.jsonl`` companion (the realistic case once a busy
    session has evicted it from the 50-cap file) — the fold must still
    succeed via ``_load_agent_calls``'s union of the two.
    """
    parent_sid = "dddddddd-1111-2222-3333-444444444444"
    child_sid = "eeeeeeee-1111-2222-3333-444444444444"
    dispatch_prompt = "Audit the release checklist and report gaps. Work autonomously."

    ledger_path = _home_dir(tmp_path) / "agent_calls_ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps({
        "timestamp": 1_800_000_000.0, "subagent_type": "general-purpose",
        "prompt": dispatch_prompt, "decision": "allowed_routed_spawn",
        "session_id": parent_sid,
    }) + "\n", encoding="utf-8")
    # agent_calls.json deliberately absent/empty: this call has already rolled
    # off the 50-cap file, which is exactly the gap #180 could not close.

    proj = _project(tmp_path)
    parent_records = [_user(parent_sid, "please delegate this to a sub-agent", 1_799_999_990)]
    parent_records += _bulk_user_prompts(parent_sid, 55, start_ts=1_800_000_200)
    _write_jsonl(proj / f"{parent_sid}.jsonl", parent_records)

    child_records = [_user(child_sid, dispatch_prompt, 1_800_000_030)]
    for i in range(3):
        child_records.append(_assistant(child_sid, 1_800_000_031 + i, text=f"working step {i}"))
    _write_jsonl(proj / f"{child_sid}.jsonl", child_records)

    data = ns.report(days=None, root=proj.parent)
    session_ids = {row["session_id"] for row in data["sessions"]}
    assert parent_sid in session_ids
    assert child_sid not in session_ids  # folded, not a standalone session

    rows = list(ns.units(days=None, root=proj.parent))
    sidechain = [u for u in rows if u["kind"] == ns.UNIT_SIDECHAIN and u["session_id"] == parent_sid]
    assert len(sidechain) == 3


# ── NS3 lever 1: llm_edit ledger (#181) → routed_edit units ─────────────────

def _git(args, cwd, env=None):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                    capture_output=True, text=True, env=env)


def _init_repo(repo_dir):
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], cwd=repo_dir)
    _git(["config", "user.email", "t@example.com"], cwd=repo_dir)
    _git(["config", "user.name", "Test"], cwd=repo_dir)
    return repo_dir


def _commit_file(repo_dir, relpath, content, ts):
    path = repo_dir / relpath
    path.write_text(content, encoding="utf-8")
    _git(["add", relpath], cwd=repo_dir)
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": iso, "GIT_COMMITTER_DATE": iso,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    _git(["commit", "-q", "-m", "msg"], cwd=repo_dir, env=env)


def _write_edit_outcomes(tmp_path, rows):
    p = _home_dir(tmp_path) / "edit_outcomes.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_edit_ledger_applied_and_survived_is_used(tmp_path):
    sid = SID_MAIN
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "foo.py", "def foo():\n    pass\n", 1_700_000_000)
    row_ts = 1_700_000_100  # after the only commit; nothing touches it again
    _write_edit_outcomes(tmp_path, [
        {"ts": row_ts, "session_id": sid, "file": str(repo / "foo.py"),
         "model": "ollama/qwen3.5", "applied": True, "survived": None},
    ])
    records = [_user(sid, "please add a docstring to foo", 1_800_000_000)]
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    edits = [u for u in rows if u["kind"] == ns.UNIT_ROUTED_EDIT]
    assert len(edits) == 1
    assert edits[0]["outcome"] == ns.OUTCOME_USED
    assert edits[0]["signal"] == "edit_ledger_survived"
    assert edits[0]["lever"] == "llm_edit"
    assert edits[0]["task_type"] == "code"


def test_edit_ledger_applied_and_rewritten_is_redo(tmp_path):
    sid = SID_MAIN
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "foo.py", "def foo():\n    pass\n", 1_700_000_000)
    row_ts = 1_700_000_100
    # Claude (or anything) touches the file again AFTER the ledger row's ts —
    # the conservative proxy edit_survival.py uses for "this got redone."
    _commit_file(repo, "foo.py", "def foo():\n    return 42\n", 1_700_000_200)
    _write_edit_outcomes(tmp_path, [
        {"ts": row_ts, "session_id": sid, "file": str(repo / "foo.py"),
         "model": "ollama/qwen3.5", "applied": True, "survived": None},
    ])
    records = [_user(sid, "please add a docstring to foo", 1_800_000_000)]
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    edits = [u for u in rows if u["kind"] == ns.UNIT_ROUTED_EDIT]
    assert len(edits) == 1
    assert edits[0]["outcome"] == ns.OUTCOME_REDO
    assert edits[0]["signal"] == "edit_ledger_redone"


def test_edit_ledger_not_applied_is_discarded(tmp_path):
    sid = SID_MAIN
    _write_edit_outcomes(tmp_path, [
        {"ts": 1_700_000_100, "session_id": sid, "file": "/nonexistent/foo.py",
         "model": "ollama/qwen3.5", "applied": False, "survived": None},
    ])
    records = [_user(sid, "please add a docstring to foo", 1_800_000_000)]
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    edits = [u for u in rows if u["kind"] == ns.UNIT_ROUTED_EDIT]
    assert len(edits) == 1
    assert edits[0]["outcome"] == ns.OUTCOME_DISCARDED
    assert edits[0]["signal"] == "edit_ledger_not_applied"


def test_edit_ledger_unresolved_survival_is_unknown(tmp_path):
    sid = SID_MAIN
    no_repo_dir = tmp_path / "no_repo"
    no_repo_dir.mkdir(parents=True, exist_ok=True)
    f = no_repo_dir / "foo.py"
    f.write_text("def foo():\n    pass\n", encoding="utf-8")
    _write_edit_outcomes(tmp_path, [
        {"ts": 1_700_000_100, "session_id": sid, "file": str(f),
         "model": "ollama/qwen3.5", "applied": True, "survived": None},
    ])
    records = [_user(sid, "please add a docstring to foo", 1_800_000_000)]
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    edits = [u for u in rows if u["kind"] == ns.UNIT_ROUTED_EDIT]
    assert len(edits) == 1
    assert edits[0]["outcome"] == ns.OUTCOME_UNKNOWN
    assert edits[0]["signal"] == "edit_ledger_unresolved"


def test_edit_ledger_dedups_against_transcript_routed_mcp(tmp_path):
    """The mcp__llm_router__llm_edit tool_use call appears in the transcript
    (would otherwise become a generic routed_mcp unit); the ledger row for
    the SAME call, matched by the join key (nearest-preceding llm_edit call
    within EDIT_LEDGER_JOIN_WINDOW_S), must replace it — never both.
    """
    sid = SID_MAIN
    call_ts = 1_800_000_001
    row_ts = call_ts + 5  # inside the 300s join window
    _write_edit_outcomes(tmp_path, [
        {"ts": row_ts, "session_id": sid, "file": "/nonexistent/foo.py",
         "model": "ollama/qwen3.5", "applied": False, "survived": None},
    ])
    records = [_user(sid, "please refactor this function", 1_800_000_000)]
    records.append(_assistant(sid, call_ts, tool_uses=[
        _tool_use("toolu_1", "mcp__llm_router__llm_edit",
                   {"task": "refactor foo", "files": ["/nonexistent/foo.py"]}),
    ]))
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    mcp = [u for u in rows if u["kind"] == ns.UNIT_ROUTED_MCP]
    edits = [u for u in rows if u["kind"] == ns.UNIT_ROUTED_EDIT]
    assert len(mcp) == 0  # the ledger-backed unit replaced it — never both
    assert len(edits) == 1
    assert edits[0]["outcome"] == ns.OUTCOME_DISCARDED


def test_edit_ledger_null_session_id_falls_back_to_ts_and_file_join(tmp_path):
    """Audit 2026-09-28: the MCP server's edit_ledger.record_edit_outcome
    cannot always resolve a session id, so the row it writes carries
    ``session_id: null`` (never "" — see edit_ledger.py). The strict,
    session_id-keyed join above can never claim such a row (its filter is
    ``row["session_id"] == sid``, and null matches no real sid). This is the
    FALLBACK JOIN documented in the module docstring: match by ts (within
    EDIT_LEDGER_JOIN_WINDOW_S) AND file path across the WHOLE corpus.

    Two sessions exist so the test proves the row lands on the RIGHT one,
    not just on the only one available: session B's llm_edit call touches a
    different file at a close-by timestamp, and must NOT claim the row.
    """
    sid_a = SID_MAIN
    sid_b = "bbbbbbbb-1111-2222-3333-444444444444"
    call_ts_a = 1_800_000_001
    call_ts_b = 1_800_000_002  # close in time to A's call — file must disambiguate
    row_ts = call_ts_a + 5  # inside the 300s join window of A's call

    _write_edit_outcomes(tmp_path, [
        {"ts": row_ts, "session_id": None, "file": "/nonexistent/foo.py",
         "model": "codex/gpt-5.5", "applied": False, "survived": None},
    ])

    records_a = [_user(sid_a, "please refactor this function", 1_800_000_000)]
    records_a.append(_assistant(sid_a, call_ts_a, tool_uses=[
        _tool_use("toolu_a", "mcp__llm_router__llm_edit",
                   {"task": "refactor foo", "files": ["/nonexistent/foo.py"]}),
    ]))
    records_a += _bulk_user_prompts(sid_a, 60, start_ts=1_800_000_100)

    records_b = [_user(sid_b, "please refactor a different function", 1_800_000_000)]
    records_b.append(_assistant(sid_b, call_ts_b, tool_uses=[
        _tool_use("toolu_b", "mcp__llm_router__llm_edit",
                   {"task": "refactor bar", "files": ["/nonexistent/bar.py"]}),
    ]))
    records_b += _bulk_user_prompts(sid_b, 60, start_ts=1_800_000_100)

    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid_a}.jsonl", records_a)
    _write_jsonl(proj / f"{sid_b}.jsonl", records_b)

    rows_a = list(ns.units(days=None, session_id=sid_a, root=proj.parent))
    rows_b = list(ns.units(days=None, session_id=sid_b, root=proj.parent))

    edits_a = [u for u in rows_a if u["kind"] == ns.UNIT_ROUTED_EDIT]
    edits_b = [u for u in rows_b if u["kind"] == ns.UNIT_ROUTED_EDIT]
    mcp_a = [u for u in rows_a if u["kind"] == ns.UNIT_ROUTED_MCP]

    assert len(edits_a) == 1  # attributed to A, the file's owner
    assert edits_a[0]["outcome"] == ns.OUTCOME_DISCARDED
    assert edits_a[0]["signal"] == "edit_ledger_not_applied"
    assert edits_a[0]["session_id"] == sid_a
    assert len(mcp_a) == 0  # the matched call's generic routed_mcp unit was dropped, same as the keyed join
    assert len(edits_b) == 0  # NOT attributed to B, despite the close-by timestamp


# ── NS3 lever 2: Codex sub-agent delegation ledger (#184) ───────────────────

def _write_north_star_units(tmp_path, rows):
    p = _home_dir(tmp_path) / "north_star_units.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_agent_route_codex_delegation_is_unknown_not_used(tmp_path):
    """Dispatch is not adoption: a bare `delegated` row says Codex ran, not that
    its result was kept. The used/redone verdict belongs to the release-time
    outcome audit (PLAN Phase 0.1); at runtime the unit stays `unknown` but is
    still an attempt, so the dispatch rate stays measurable (audit C6)."""
    sid = SID_MAIN
    _write_north_star_units(tmp_path, [
        {"ts": 1_800_000_050.0, "lever": "agent_route_codex", "model": "codex/gpt-5.5",
         "outcome": "delegated", "subagent_type": "general-purpose", "task_type": "code",
         "complexity": "moderate", "session_id": sid, "duration_sec": 12.3},
    ])
    records = [_user(sid, "please delegate this analysis", 1_800_000_000)]
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    codex = [u for u in rows if u["kind"] == ns.UNIT_AGENT_ROUTE_CODEX]
    assert len(codex) == 1
    assert codex[0]["outcome"] == ns.OUTCOME_UNKNOWN
    assert codex[0]["outcome"] != ns.OUTCOME_USED
    assert codex[0]["signal"] == "agent_route_codex_delegated"
    assert codex[0]["lever"] == "agent_route_codex"
    assert codex[0]["task_type"] == "code"
    assert codex[0]["model"] == "codex/gpt-5.5"


def test_agent_route_codex_failed_is_discarded(tmp_path):
    sid = SID_MAIN
    _write_north_star_units(tmp_path, [
        {"ts": 1_800_000_050.0, "lever": "agent_route_codex", "model": "",
         "outcome": "codex_failed", "subagent_type": "general-purpose", "task_type": "research",
         "complexity": "complex", "session_id": sid, "reason": "run_codex raised: timeout"},
    ])
    records = [_user(sid, "please research this thoroughly", 1_800_000_000)]
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    codex = [u for u in rows if u["kind"] == ns.UNIT_AGENT_ROUTE_CODEX]
    assert len(codex) == 1
    assert codex[0]["outcome"] == ns.OUTCOME_DISCARDED
    assert codex[0]["signal"] == "agent_route_codex_failed"


def test_agent_route_codex_unsuitable_decision_is_not_a_unit(tmp_path):
    """A decision NOT to invoke Codex is not an attempt — no unit at all,
    same treatment as a drafting decision the router never made."""
    sid = SID_MAIN
    _write_north_star_units(tmp_path, [
        {"ts": 1_800_000_050.0, "lever": "agent_route_codex", "model": "",
         "outcome": "unsuitable", "subagent_type": "fork", "task_type": "code",
         "complexity": "moderate", "session_id": sid},
    ])
    records = [_user(sid, "please help with this", 1_800_000_000)]
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=None, session_id=sid, root=proj.parent))
    codex = [u for u in rows if u["kind"] == ns.UNIT_AGENT_ROUTE_CODEX]
    assert len(codex) == 0


def test_agent_route_codex_delegated_counts_as_attempted_not_used_in_report(tmp_path):
    sid = SID_MAIN
    _write_north_star_units(tmp_path, [
        {"ts": 1_800_000_050.0, "lever": "agent_route_codex", "model": "codex/gpt-5.5",
         "outcome": "delegated", "task_type": "code", "session_id": sid},
    ])
    records = [_user(sid, "please delegate this analysis", 1_800_000_000)]
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    bk = ns.report(days=None, session_id=sid, root=proj.parent)["by_kind"][ns.UNIT_AGENT_ROUTE_CODEX]
    assert bk == {"units": 1, "attempted": 1, "used": 0, "redo": 0, "unknown": 1}


# ── --days window: filter by each unit's own ts, not the file mtime (audit C7) ─

def test_days_window_excludes_old_units_in_a_file_touched_today(tmp_path):
    """A session file whose mtime is now, holding only units from 3 days ago,
    contributes nothing to a 1-day window: not its prompts, not its turns, not
    its Codex ledger rows. Before the fix the whole file passed on mtime alone."""
    import time
    sid = SID_MAIN
    old = time.time() - 3 * 86400
    _write_north_star_units(tmp_path, [
        {"ts": old + 50, "lever": "agent_route_codex", "model": "codex/gpt-5.5",
         "outcome": "delegated", "task_type": "code", "session_id": sid},
    ])
    records = [_user(sid, "please look into this", old), _assistant(sid, old + 1, text="ok")]
    records += _bulk_user_prompts(sid, 60, start_ts=old + 100)
    proj = _project(tmp_path)
    path = proj / f"{sid}.jsonl"
    _write_jsonl(path, records)
    os.utime(path, None)  # touched today

    assert list(ns.units(days=1, root=proj.parent)) == []
    rep = ns.report(days=1, root=proj.parent)
    assert rep["sessions"] == []
    assert all(v["units"] == 0 for v in rep["by_kind"].values())
    # Sanity: the same file is fully visible to a window that covers it.
    assert len(list(ns.units(days=7, root=proj.parent))) == 63


def test_days_window_keeps_only_in_window_units_of_a_mixed_file(tmp_path):
    import time
    sid = SID_MAIN
    now = time.time()
    old = now - 3 * 86400
    _write_north_star_units(tmp_path, [
        {"ts": old + 5, "lever": "agent_route_codex", "model": "codex/gpt-5.5",
         "outcome": "codex_failed", "task_type": "code", "session_id": sid},
        {"ts": now - 60, "lever": "agent_route_codex", "model": "codex/gpt-5.5",
         "outcome": "delegated", "task_type": "code", "session_id": sid},
    ])
    records = _bulk_user_prompts(sid, 10, start_ts=old)
    records += _bulk_user_prompts(sid, 4, start_ts=now - 600)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{sid}.jsonl", records)

    rows = list(ns.units(days=1, root=proj.parent))
    cutoff = now - 86400
    assert rows, "the in-window units must still be found (an empty set passes everything)"
    assert sum(1 for u in rows if u["kind"] == ns.UNIT_USER_PROMPT) == 4
    codex = [u for u in rows if u["kind"] == ns.UNIT_AGENT_ROUTE_CODEX]
    assert [u["signal"] for u in codex] == ["agent_route_codex_delegated"]
    for u in rows:
        assert datetime.fromisoformat(u["ts"]).timestamp() >= cutoff
