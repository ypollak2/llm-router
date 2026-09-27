"""NS1 — the North Star metric.

Each scenario below is a minimal, hand-built transcript/log pair standing in
for one classification path in ``llm_router.northstar``. Assertions check the
REASON (``signal``/``outcome``) a unit was given, not only a count, per
CLAUDE.md's "assert the reason, not just the boolean."
"""
from __future__ import annotations

import json
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
        "[2026-09-27 10:00:00] [INVOCATION 1800000000.000] ZERO_CLAUDE ROUTE_ATTEMPT",
        "[2026-09-27 10:00:00] [INVOCATION 1800000000.000] DIRECT SUCCESS: model=codex/gpt-5.5 latency=900ms",
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
        "[2026-09-27 10:00:00] [INVOCATION 1800000000.000] ZERO_CLAUDE ROUTE_ATTEMPT",
        "[2026-09-27 10:00:00] [INVOCATION 1800000000.000] DIRECT SUCCESS: model=codex/gpt-5.5 latency=900ms",
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
