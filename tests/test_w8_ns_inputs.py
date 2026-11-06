"""W8 (owner decision D-364): the North Star counts what its own definition says.

"All user prompts + all LLM calls in the session, including sub-agent calls". Three inputs were wrong
(``docs/bugs/NSSUB-1.md``): the sub-agent transcripts (``<project>/<session>/subagents/**/agent-*.jsonl``)
were never read, a call was a transcript RECORD (one per content block, about 2.15 per API message) and
an MCP unit had no model. Each test builds the transcript by hand and asserts the unit it must yield.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from llm_router import northstar as ns
from tests.test_northstar import _home_dir, _iso, _project, _user, _write_jsonl

SID = "aaaaaaaa-1111-2222-3333-444444444444"
T0 = 1_800_000_000
OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"
LOCAL = "ollama/qwen3.5:latest"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))


def _asst(ts, mid, model, *blocks):
    """One transcript RECORD of an assistant message: Claude Code writes one per content block."""
    message = {"role": "assistant", "content": list(blocks)}
    if mid is not None:
        message["id"] = mid
    if model is not None:
        message["model"] = model
    return {"type": "assistant", "isSidechain": False, "timestamp": _iso(ts), "sessionId": SID,
            "message": message}


def _text(s="x"):
    return {"type": "text", "text": s}


def _tool(use_id, name="mcp__llm_router__llm", **inp):
    return {"type": "tool_use", "id": use_id, "name": name, "input": inp}


def _main(tmp_path, records, name="-Users-x-proj", sid=SID):
    proj = _project(tmp_path, name)
    _write_jsonl(proj / f"{sid}.jsonl", [_user(sid, "please look at the first task in this repo", T0 - 10), *records])
    return proj


def _sub(proj, rel, records, sid=SID):
    """A sub-agent transcript at ``<project>/<session>/subagents/<rel>``: its brief, then its calls."""
    _write_jsonl(proj / sid / "subagents" / rel,
                 [_user(sid, "Investigate the failing test and report back with the cause.", T0 - 5), *records])


def _units(proj, **kw):
    return list(ns.units(days=None, root=proj.parent, **kw))


def _calls(units, kind):
    return [u for u in units if u["kind"] == kind]


def _routing_db(tmp_path, rows):
    """A usage.db with just the columns the MCP join reads. ``rows``: (tool_use_id, model, reason_code, ts)."""
    path = _home_dir(tmp_path) / "usage.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY, timestamp TEXT, tool_use_id TEXT, "
                "final_model TEXT, final_provider TEXT, reason_code TEXT, provenance TEXT)")
    for tid, model, reason, ts in rows:
        con.execute("INSERT INTO routing_decisions (timestamp, tool_use_id, final_model, final_provider, "
                    "reason_code, provenance) VALUES (?,?,?,?,?, 'runtime')",
                    (ts, tid, model, model.split("/")[0] if model else None, reason))
    con.commit()
    con.close()


# ── one API message is one unit ──────────────────────────────────────────────────────────────────

def test_the_records_of_one_message_are_one_call_with_the_model_that_answered(tmp_path):
    proj = _main(tmp_path, [
        _asst(T0 + 1, "msg_1", OPUS, _text("a")),
        _asst(T0 + 2, "msg_1", OPUS, _tool("tu1", "Read")),          # the same message, its second block
        _asst(T0 + 3, "msg_1", OPUS, _text("c")),
        _asst(T0 + 4, "msg_2", HAIKU, _text("d")),
    ])
    calls = _calls(_units(proj), ns.UNIT_CLAUDE_MAIN)
    assert [(u["model"], u["outcome"]) for u in calls] == [(OPUS, "not_routed"), (HAIKU, "not_routed")]
    assert calls[0]["ts"] < calls[1]["ts"]          # the unit sits at its FIRST record


def test_a_record_with_no_id_cannot_be_deduped_and_stays_one_unit(tmp_path):
    proj = _main(tmp_path, [_asst(T0 + 1, None, None, _text("a")), _asst(T0 + 2, None, None, _text("b"))])
    calls = _calls(_units(proj), ns.UNIT_CLAUDE_MAIN)
    assert len(calls) == 2 and all(u["model"] is None for u in calls)


def test_a_synthetic_record_is_no_api_call(tmp_path):
    proj = _main(tmp_path, [_asst(T0 + 1, "msg_ok", OPUS, _text()), _asst(T0 + 2, "msg_syn", "<synthetic>", _text("No response requested."))])
    assert [u["model"] for u in _calls(_units(proj), ns.UNIT_CLAUDE_MAIN)] == [OPUS]


def test_tool_use_blocks_in_the_later_records_of_a_message_still_count(tmp_path):
    """Only the FIRST record is the call, but a message's tool_use blocks sit in different records."""
    proj = _main(tmp_path, [
        _asst(T0 + 1, "msg_1", OPUS, _text("thinking out loud")),
        _asst(T0 + 2, "msg_1", OPUS, _tool("tu_late", task="query")),
    ])
    rows = _units(proj)
    assert len(_calls(rows, ns.UNIT_CLAUDE_MAIN)) == 1
    mcp = _calls(rows, ns.UNIT_ROUTED_MCP)
    assert len(mcp) == 1 and mcp[0]["task_type"] == "query"


def test_a_served_proxy_message_is_one_attempted_unit_that_carries_the_served_model(tmp_path):
    proj = _main(tmp_path, [
        _asst(T0 + 1, "msg_lr_1", OPUS, _text("a")),
        _asst(T0 + 2, "msg_lr_1", OPUS, _text("b")),
        _asst(T0 + 3, "msg_other", OPUS, _text("c")),
    ])
    ledger = _home_dir(tmp_path) / "proxy_calls.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text('{"ts": %d, "session_id": "%s", "msg_id": "msg_lr_1", "decision": "served", '
                      '"model": "%s", "task_type": "code"}\n' % (T0 + 1, SID, LOCAL))
    calls = _calls(_units(proj), ns.UNIT_CLAUDE_MAIN)
    assert [(u["lever"], u["model"]) for u in calls] == [("proxy", LOCAL), ("none", OPUS)]
    # Claude Code's own request said Opus; the call was answered by the local backend, and says so
    assert calls[0]["signal"] and calls[0]["task_type"] == "code"


# ── sub-agent transcripts ────────────────────────────────────────────────────────────────────────

def test_subagent_calls_fold_into_the_parent_session_when_asked(tmp_path):
    proj = _main(tmp_path, [_asst(T0 + 1, "msg_main", OPUS, _text())])
    _sub(proj, "agent-a1.jsonl", [
        _asst(T0 + 10, "msg_s1", SONNET, _text("a")),
        _asst(T0 + 11, "msg_s1", SONNET, _tool("tu_s1", "Bash")),     # a second record of the same message
        _asst(T0 + 12, "msg_s2", SONNET, _text("b")),
    ])
    _sub(proj, "workflows/wf_1/agent-b2.jsonl", [_asst(T0 + 20, "msg_s3", SONNET, _text("c"))])   # one level deeper
    rows = _units(proj, subagents=True)
    side = _calls(rows, ns.UNIT_SIDECHAIN)
    assert len(side) == 3
    assert {u["session_id"] for u in rows} == {SID}                  # parent = the directory name
    assert all(u["model"] == SONNET and u["lever"] == "agent_route" for u in side)
    assert len(_calls(rows, ns.UNIT_CLAUDE_MAIN)) == 1


def test_subagent_files_are_not_read_by_default(tmp_path):
    """The Stop hook and the quality breaker call units() on hot paths: they keep reading the main files."""
    proj = _main(tmp_path, [_asst(T0 + 1, "msg_main", OPUS, _text())])
    _sub(proj, "agent-a1.jsonl", [_asst(T0 + 10, "msg_s1", SONNET, _text())])
    assert _calls(_units(proj), ns.UNIT_SIDECHAIN) == []
    assert len(_calls(_units(proj, subagents=True), ns.UNIT_SIDECHAIN)) == 1


def test_a_subagent_brief_and_tool_results_are_not_user_prompts(tmp_path):
    proj = _main(tmp_path, [_asst(T0 + 1, "msg_main", OPUS, _text())])
    _sub(proj, "agent-a1.jsonl", [
        _asst(T0 + 10, "msg_s1", SONNET, _text()),
        _user(SID, "a follow up that looks like a typed prompt to the classifier", T0 + 11),
    ])
    rows = _units(proj, subagents=True)
    assert len(_calls(rows, ns.UNIT_SIDECHAIN)) == 1            # the file WAS read ...
    assert len(_calls(rows, ns.UNIT_USER_PROMPT)) == len(_calls(_units(proj), ns.UNIT_USER_PROMPT)) == 1   # ... and added no prompt


def test_the_same_message_in_two_subagent_files_is_one_call(tmp_path):
    proj = _main(tmp_path, [_asst(T0 + 1, "msg_main", OPUS, _text())])
    _sub(proj, "agent-a1.jsonl", [_asst(T0 + 10, "msg_dup", SONNET, _text())])
    _sub(proj, "agent-a2.jsonl", [_asst(T0 + 11, "msg_dup", SONNET, _text()), _asst(T0 + 12, "msg_new", SONNET, _text())])
    assert len(_calls(_units(proj, subagents=True), ns.UNIT_SIDECHAIN)) == 2


def test_subagent_files_of_a_synthetic_or_sandbox_session_are_skipped(tmp_path):
    synthetic = "deadbee2-0000-0000-0000-000000000000"          # matches _HEX_FIXTURE_STEMS
    proj = _main(tmp_path, [_asst(T0 + 1, "msg_main", OPUS, _text())])
    _sub(proj, "agent-a1.jsonl", [_asst(T0 + 10, "msg_x", SONNET, _text())], sid=synthetic)
    sandbox = _project(tmp_path, "-private-tmp-bq-claude")
    _sub(sandbox, "agent-a1.jsonl", [_asst(T0 + 10, "msg_y", SONNET, _text())])
    assert _calls(_units(proj, subagents=True), ns.UNIT_SIDECHAIN) == []


def test_old_subagent_files_are_skipped_by_the_window_prefilter(tmp_path):
    import os
    import time

    proj = _main(tmp_path, [_asst(time.time() - 60, "msg_main", OPUS, _text())])
    _sub(proj, "agent-old.jsonl", [_asst(time.time() - 60, "msg_old", SONNET, _text())])
    path = proj / SID / "subagents" / "agent-old.jsonl"
    os.utime(path, (time.time() - 30 * 86400, time.time() - 30 * 86400))
    assert _calls(list(ns.units(days=7, root=proj.parent, subagents=True)), ns.UNIT_SIDECHAIN) == []


# ── MCP units get a model ────────────────────────────────────────────────────────────────────────

def test_an_mcp_unit_takes_the_model_of_the_routing_row_that_answered_it(tmp_path):
    proj = _main(tmp_path, [
        _asst(T0 + 1, "msg_1", OPUS, _tool("toolu_A", task="query")),
        _asst(T0 + 2, "msg_2", OPUS, _tool("toolu_B", "mcp__llm_router__llm_router_status")),   # an admin tool: no row
        _asst(T0 + 3, "msg_3", OPUS, _tool("toolu_C", task="code")),                            # a retry left two rows
    ])
    _routing_db(tmp_path, [
        ("toolu_A", LOCAL, "router_unhinted", "2026-11-03 10:00:00"),
        ("toolu_C", "gemini/gemini-2.5-flash", "error_timeout", "2026-11-03 10:01:00"),         # the failed attempt
        ("toolu_C", "codex/gpt-5.5", "router_unhinted", "2026-11-03 10:01:05"),                 # the one that served
    ])
    mcp = {u["task_type"] or u["lever"]: u["model"] for u in _calls(_units(proj), ns.UNIT_ROUTED_MCP)}
    assert mcp["query"] == LOCAL
    assert mcp["code"] == "codex/gpt-5.5"
    assert mcp["mcp_llm"] is None                      # toolu_B names no row: unknown stays unknown


def test_a_subagents_mcp_call_is_joined_too(tmp_path):
    proj = _main(tmp_path, [_asst(T0 + 1, "msg_main", OPUS, _text())])
    _sub(proj, "agent-a1.jsonl", [_asst(T0 + 10, "msg_s1", SONNET, _tool("toolu_S", task="query"))])
    _routing_db(tmp_path, [("toolu_S", LOCAL, "router_unhinted", "2026-11-03 10:00:00")])
    mcp = _calls(_units(proj, subagents=True), ns.UNIT_ROUTED_MCP)
    assert [(u["model"], u["session_id"]) for u in mcp] == [(LOCAL, SID)]


def test_no_database_leaves_mcp_models_unknown_and_breaks_nothing(tmp_path):
    proj = _main(tmp_path, [_asst(T0 + 1, "msg_1", OPUS, _tool("toolu_A", task="query"))])
    assert [u["model"] for u in _calls(_units(proj), ns.UNIT_ROUTED_MCP)] == [None]


def test_the_model_helpers():
    assert [ns.claude_family(m) for m in (OPUS, SONNET, HAIKU, "claude-3-5-sonnet-20241022", "anthropic/claude-opus-4",
                                          "claude-fable-1", LOCAL, "gpt-6-astra", None, "", "<synthetic>")] == \
        ["opus", "sonnet", "haiku", "sonnet", "opus", "claude-other", None, None, None, None, None]
    assert ns.claude_family("ollama/opus-finetune") is None          # a tier word in a non-Claude name is not Claude
    assert ns.is_claude_model("Claude-Opus-4") and not ns.is_claude_model(LOCAL)


def test_the_unit_stream_is_deterministic_across_reads(tmp_path):
    proj = _main(tmp_path, [_asst(T0 + 1, "msg_1", OPUS, _text()), _asst(T0 + 2, "msg_1", OPUS, _text())]
                 + [_asst(T0 + 3 + i, f"msg_{i + 2}", OPUS, _text()) for i in range(5)])
    _sub(proj, "agent-a1.jsonl", [_asst(T0 + 30 + i, f"msg_s{i}", SONNET, _text()) for i in range(4)])
    a, b = _units(proj, subagents=True), _units(proj, subagents=True)
    assert a == b and len(_calls(a, ns.UNIT_CLAUDE_MAIN)) == 6 and len(_calls(a, ns.UNIT_SIDECHAIN)) == 4


# ── an llm_edit made inside a sub-agent ──────────────────────────────────────────────────────────

def _edit_row(tmp_path, ts, *, session_id=SID, source=None):
    path = _home_dir(tmp_path) / "edit_outcomes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": ts, "session_id": session_id, "file": "/nonexistent/foo.py", "model": LOCAL, "applied": False, "survived": None}
    if source:
        row["source"] = source
    path.write_text(json.dumps(row) + "\n")


def test_an_llm_edit_made_inside_a_subagent_is_one_routed_edit_not_also_a_generic_mcp_unit(tmp_path):
    """The ledger row carries the session id the MCP server saw (the parent's), so the join must see the sub-agent's call."""
    proj = _main(tmp_path, [_asst(T0 + 1, "msg_main", OPUS, _text())])
    _sub(proj, "agent-a1.jsonl", [_asst(T0 + 10, "msg_s1", SONNET,
                                        _tool("toolu_E", "mcp__llm_router__llm_edit", task="refactor foo", files=["/nonexistent/foo.py"]))])
    _edit_row(tmp_path, T0 + 15)
    rows = _units(proj, subagents=True)
    assert [u["outcome"] for u in _calls(rows, ns.UNIT_ROUTED_EDIT)] == ["discarded"]
    assert _calls(rows, ns.UNIT_ROUTED_MCP) == []                  # never both for the one call


def test_without_the_ledger_row_the_subagents_edit_call_stays_a_generic_mcp_unit(tmp_path):
    proj = _main(tmp_path, [_asst(T0 + 1, "msg_main", OPUS, _text())])
    _sub(proj, "agent-a1.jsonl", [_asst(T0 + 10, "msg_s1", SONNET, _tool("toolu_E", "mcp__llm_router__llm_edit", files=["/x.py"]))])
    rows = _units(proj, subagents=True)
    assert len(_calls(rows, ns.UNIT_ROUTED_MCP)) == 1 and _calls(rows, ns.UNIT_ROUTED_EDIT) == []


def test_only_one_subagent_file_is_held_in_memory_at_a_time(tmp_path, monkeypatch):
    """A busy session holds hundreds of MB of sub-agent transcripts: the units outlive a file, its records do not."""
    import gc
    import weakref

    proj = _main(tmp_path, [_asst(T0 + 1, "msg_main", OPUS, _text())])
    for i in range(4):
        _sub(proj, f"agent-{i}.jsonl", [_asst(T0 + 10 + i, f"msg_s{i}", SONNET, _text())])
    alive, peaks = [], []
    real = ns._iter_jsonl

    class Tracked(dict):
        pass

    def spy(path):
        for rec in real(path):
            if "subagents" in str(path):
                tracked = Tracked(rec)
                alive.append(weakref.ref(tracked))
                yield tracked
            else:
                yield rec

    monkeypatch.setattr(ns, "_iter_jsonl", spy)
    original_add = ns.Unit

    def counting_unit(*a, **kw):
        gc.collect()
        peaks.append(sum(1 for r in alive if r() is not None))
        return original_add(*a, **kw)

    monkeypatch.setattr(ns, "Unit", counting_unit)
    assert len(_calls(_units(proj, subagents=True), ns.UNIT_SIDECHAIN)) == 4
    assert max(peaks) <= 2 * 2          # at most the current file's records (its brief + one call) are alive, not all four files'
