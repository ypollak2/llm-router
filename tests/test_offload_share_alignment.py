"""M0.3: O3 matches the owner's definition (PLAN M0.3).

* (a) a local MCP unit is not a turn: reported as ``local_assist_n`` outside n and the numerator;
* (b) sub-agent first calls are not turns (proxy ``msg_id`` joined to the transcript);
* (c) a zero-Claude edit is one local turn per (session_id, turn_id); ``llm_edit`` rows never are;
* (d) G3 session_kind completeness < 95% prints ``o3.bound``.

Fixtures hold no prompt text: turn ids are made-up hashes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_router import edit_ledger, offload_share as osh, paths, prompt_key, session_kind
from llm_router import northstar as ns
from llm_router.commands import kpi

from tests import _o3_fixture as fx
from tests._o3_fixture import NOW, proxy_row

SID = "s-org"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    proj = tmp_path / "claude-projects" / "-Users-x-app"
    proj.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(proj.parent))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    from llm_router import failopen

    failopen.reset_unpersisted()
    failopen.reset_cache()
    session_kind._FOUND.clear()
    session_kind.tag_session(SID, "/Users/someone/Projects/app", env={})
    monkeypatch.setattr(ns, "local_shadow_units", lambda days=30, db_path=None: iter(()))
    yield proj
    session_kind._FOUND.clear()
    failopen.reset_unpersisted()
    failopen.reset_cache()


def _assistant_line(msg_id: str, *, sidechain: bool) -> str:
    return json.dumps({"type": "assistant", "isSidechain": sidechain, "sessionId": SID,
                       "timestamp": "2026-10-06T10:00:00.000Z",
                       "message": {"id": msg_id, "role": "assistant", "content": []}})


def _write_transcripts(proj: Path, main_ids, sub_ids) -> None:
    (proj / f"{SID}.jsonl").write_text("".join(_assistant_line(m, sidechain=False) + "\n" for m in main_ids))
    sub = proj / SID / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a1.jsonl").write_text("".join(_assistant_line(m, sidechain=True) + "\n" for m in sub_ids))


def _ledger(rows):
    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), rows)


def _edit_rows(rows):
    fx.write_jsonl(paths.state_path("edit_outcomes.jsonl"), rows)


def _edit(ts, *, source, file, turn_id=None, sid=SID, applied=True):
    return {"ts": ts, "session_id": sid, "session_kind": "organic", "file": file,
            "model": "ollama/qwen3.5", "applied": applied, "survived": None,
            "source": source, "turn_id": turn_id}


def _fixture_rows():
    """3 main turns + 1 continuation, 2 sidechain first calls (+ 1 sidechain continuation)."""
    t = NOW - 3000
    rows = [proxy_row(1, sid=SID, ts=t + 0, kind="organic", msg_id="m1"),
            proxy_row(2, sid=SID, ts=t + 5, kind="organic", msg_id="m2", step="continuation"),
            proxy_row(3, sid=SID, ts=t + 100, kind="organic", msg_id="sub1"),            # sidechain first
            proxy_row(4, sid=SID, ts=t + 110, kind="organic", msg_id="sub1b", step="continuation"),
            proxy_row(5, sid=SID, ts=t + 200, kind="organic", msg_id="m3"),
            proxy_row(6, sid=SID, ts=t + 300, kind="organic", msg_id="sub2"),            # sidechain first
            proxy_row(7, sid=SID, ts=t + 400, kind="organic", msg_id="m4")]
    return rows


# ── the combined fixture of the PLAN ─────────────────────────────────────────

def test_plan_fixture_n4_numerator1(_isolated, monkeypatch):
    _write_transcripts(_isolated, ["m1", "m2", "m3", "m4"], ["sub1", "sub1b", "sub2"])
    _ledger(_fixture_rows())
    turn = prompt_key.key("hash this typed prompt")
    _edit_rows([_edit(NOW - 2000, source="zero_claude", file="a.py", turn_id=turn),
                _edit(NOW - 2000 + 1, source="zero_claude", file="test_a.py", turn_id=turn),   # same turn
                _edit(NOW - 1900, source="llm_edit", file="b.py", turn_id=None)])
    monkeypatch.setattr(ns, "local_shadow_units", lambda days=30, db_path=None: iter(
        [{"session_id": SID, "ts": "2026-10-06T14:00:00+00:00", "task_type": "query"}]))
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    b = o3["breakdown"]
    assert (b["n"], b["offload_kept"], b["local_n"]) == (4, 1, 1)
    assert (b["local_assist_n"], b["local_assist_redone"]) == (1, 0)
    assert o3["excluded"]["subagent_first"] == 2
    assert o3["excluded"]["unjoined"] == 0
    assert b["claude_n"] == 3


def test_llm_edit_routed_mcp_unit_is_still_joined_next_to_a_zero_claude_row(tmp_path, monkeypatch):
    """northstar: a source=zero_claude row must not match or drop an llm_edit routed_mcp unit."""
    sid = "aaaaaaaa-1111-2222-3333-444444444444"
    home = tmp_path / "router_home"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    call_ts = 1_800_000_001
    rows = [{"ts": call_ts + 5, "session_id": sid, "file": "/nonexistent/foo.py", "model": "ollama/q",
             "applied": False, "survived": None, "source": "zero_claude", "turn_id": "t" * 16}]
    (home / "edit_outcomes.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    from tests.test_northstar import _assistant, _bulk_user_prompts, _tool_use, _user, _write_jsonl

    proj = tmp_path / "claude_projects" / "-Users-x-proj"
    records = [_user(sid, "please refactor this function", 1_800_000_000)]
    records.append(_assistant(sid, call_ts, tool_uses=[_tool_use(
        "toolu_1", "mcp__llm_router__llm_edit", {"task": "t", "files": ["/nonexistent/foo.py"]})]))
    records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
    _write_jsonl(proj / f"{sid}.jsonl", records)
    units = list(ns.units(days=None, session_id=sid, root=proj.parent))
    assert len([u for u in units if u["kind"] == ns.UNIT_ROUTED_MCP]) == 1   # NOT dropped
    # the zero-Claude row is still its own routed_edit unit
    assert len([u for u in units if u["kind"] == ns.UNIT_ROUTED_EDIT]) == 1


def test_llm_edit_row_without_source_still_dedups_as_before(tmp_path, monkeypatch):
    sid = "aaaaaaaa-1111-2222-3333-444444444444"
    home = tmp_path / "router_home"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    call_ts = 1_800_000_001
    for src in (None, "llm_edit"):
        rows = [{"ts": call_ts + 5, "session_id": sid, "file": "/nonexistent/foo.py", "model": "ollama/q",
                 "applied": False, "survived": None, "source": src}]
        (home / "edit_outcomes.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        from tests.test_northstar import _assistant, _bulk_user_prompts, _tool_use, _user, _write_jsonl

        proj = tmp_path / "claude_projects" / "-Users-x-proj"
        records = [_user(sid, "please refactor this function", 1_800_000_000)]
        records.append(_assistant(sid, call_ts, tool_uses=[_tool_use(
            "toolu_1", "mcp__llm_router__llm_edit", {"task": "t", "files": ["/nonexistent/foo.py"]})]))
        records += _bulk_user_prompts(sid, 60, start_ts=1_800_000_100)
        _write_jsonl(proj / f"{sid}.jsonl", records)
        units = list(ns.units(days=None, session_id=sid, root=proj.parent))
        assert len([u for u in units if u["kind"] == ns.UNIT_ROUTED_MCP]) == 0
        assert len([u for u in units if u["kind"] == ns.UNIT_ROUTED_EDIT]) == 1


# ── (a) local MCP units ──────────────────────────────────────────────────────

def test_local_mcp_unit_is_assist_not_a_turn():
    local = [{"session_id": SID, "ts": "2026-10-06T14:00:00+00:00"}]
    built = osh.build_units([proxy_row(1, sid=SID, kind="organic")], local, now=NOW, days=7,
                            allowed=frozenset({"organic"}), kind_of=lambda s, st: st or "organic")
    assert [u["class"] for u in built["units"]] == ["claude"]
    assert built["local_assist"] and built["local_assist"][0]["class"] == "local"


def test_local_assist_redo_is_still_reported():
    outcomes = [{"ts": NOW - 3600 + 30, "session_id": SID, "outcome": "redone"}]
    local = [{"session_id": SID, "ts": NOW - 3600 + 30}]
    built = osh.build_units([proxy_row(1, sid=SID, kind="organic")], local, now=NOW, days=7,
                            allowed=frozenset({"organic"}), kind_of=lambda s, st: st or "organic",
                            outcome_redos=outcomes)
    assert [u["redone"] for u in built["local_assist"]] == [True]


# ── (b) sub-agent first calls ────────────────────────────────────────────────

def test_subagent_first_call_is_not_a_turn_and_not_a_redo_boundary():
    """Unit = a Haiku turn. Two sub-agent first calls follow, then an escalation. Without the
    exclusion the escalation is in turn 4 (> unit turn + 2): not redone. With it, the
    escalation is the next human turn: redone."""
    rows = [proxy_row(0, sid=SID, tier="haiku", ts=NOW - 900, kind="organic", msg_id="h"),
            proxy_row(1, sid=SID, ts=NOW - 800, kind="organic", msg_id="s1"),
            proxy_row(2, sid=SID, ts=NOW - 700, kind="organic", msg_id="s2"),
            proxy_row(3, sid=SID, tier="opus", reason="escalation", ts=NOW - 600, kind="organic", msg_id="e")]
    kw = dict(now=NOW, days=7, allowed=frozenset({"organic"}), kind_of=lambda s, st: st)
    plain = osh.build_units(rows, [], **kw)
    assert [u["redone"] for u in plain["units"] if u["class"] == "haiku"] == [False]
    sub = osh.build_units(rows, [], sidechain_of=lambda sid, m: {"s1": True, "s2": True}.get(m, False), **kw)
    assert [u["redone"] for u in sub["units"] if u["class"] == "haiku"] == [True]
    assert sub["subagent_first"] == 2 and sub["unjoined"] == 0
    assert len(osh.turn_units(sub["units"])) == 2          # the haiku turn and the escalation turn


def test_unjoined_rows_stay_in_and_are_counted():
    rows = [proxy_row(1, sid=SID, kind="organic", msg_id="x"), proxy_row(2, sid=SID, kind="organic", msg_id="y")]
    built = osh.build_units(rows, [], now=NOW, days=7, allowed=frozenset({"organic"}),
                            kind_of=lambda s, st: st, sidechain_of=lambda sid, m: None)
    assert len(osh.turn_units(built["units"])) == 2 and built["unjoined"] == 2
    assert built["subagent_first"] == 0


def test_continuation_rows_are_never_counted_as_unjoined_or_subagent_first():
    rows = [proxy_row(1, sid=SID, kind="organic", msg_id="x"),
            proxy_row(2, sid=SID, kind="organic", msg_id="y", step="continuation")]
    built = osh.build_units(rows, [], now=NOW, days=7, allowed=frozenset({"organic"}),
                            kind_of=lambda s, st: st, sidechain_of=lambda sid, m: m == "y")
    assert built["subagent_first"] == 0 and built["unjoined"] == 0


def test_sidechain_index_reads_main_and_subagent_files(_isolated):
    _write_transcripts(_isolated, ["m1"], ["s1"])
    idx = osh_transcripts().sidechain_index({SID})
    assert idx == {"m1": False, "s1": True}


def test_sidechain_index_reaches_workflow_agents_one_level_deeper(_isolated):
    wf = _isolated / SID / "subagents" / "workflows" / "wf_abc"
    wf.mkdir(parents=True)
    (wf / "agent-w1.jsonl").write_text(_assistant_line("w1", sidechain=True) + "\n")
    assert osh_transcripts().sidechain_index({SID}) == {"w1": True}


def test_sidechain_index_flags_isSidechain_entries_in_the_main_file(_isolated):
    (_isolated / f"{SID}.jsonl").write_text(_assistant_line("m1", sidechain=False) + "\n"
                                            + _assistant_line("m2", sidechain=True) + "\n")
    assert osh_transcripts().sidechain_index({SID}) == {"m1": False, "m2": True}


def test_sidechain_index_ignores_other_sessions_and_bad_lines(_isolated):
    (_isolated / "other.jsonl").write_text(_assistant_line("zz", sidechain=True) + "\n")
    (_isolated / f"{SID}.jsonl").write_text("not json\n" + _assistant_line("m1", sidechain=False) + "\n")
    assert osh_transcripts().sidechain_index({SID}) == {"m1": False}


def osh_transcripts():
    from llm_router import o3_transcripts
    return o3_transcripts


# ── (c) zero-Claude turns ────────────────────────────────────────────────────

def _build_edits(rows, edits, **kw):
    args = dict(now=NOW, days=7, allowed=frozenset({"organic"}), kind_of=lambda s, st: st or "organic")
    args.update(kw)
    return osh.build_units(rows, [], edit_rows=edits, **args)


def test_zero_claude_turn_is_counted_once_per_session_and_turn_id():
    edits = [_edit(NOW - 100, source="zero_claude", file="a.py", turn_id="t1"),
             _edit(NOW - 99, source="zero_claude", file="b.py", turn_id="t1"),
             _edit(NOW - 50, source="zero_claude", file="c.py", turn_id="t2")]
    built = _build_edits([], edits)
    assert [(u["class"], u["first"]) for u in built["units"]] == [("local", True), ("local", True)]
    assert built["units"][0]["zero_claude"] is True


def test_only_applied_zero_claude_rows_are_turns():
    edits = [_edit(NOW - 100, source="zero_claude", file="a.py", turn_id="t1", applied=False)]
    assert _build_edits([], edits)["units"] == []


def test_llm_edit_and_sourceless_rows_are_never_turns():
    edits = [_edit(NOW - 100, source="llm_edit", file="a.py", turn_id="t1"),
             _edit(NOW - 100, source=None, file="a.py", turn_id="t1")]
    assert _build_edits([], edits)["units"] == []


def test_zero_claude_rows_without_session_or_turn_id_are_excluded_and_counted():
    edits = [_edit(NOW - 100, source="zero_claude", file="a.py", turn_id="t1", sid=None),
             _edit(NOW - 100, source="zero_claude", file="a.py", turn_id=None)]
    built = _build_edits([], edits)
    assert built["units"] == []
    assert built["edit_no_session"] == 1 and built["edit_no_turn_id"] == 1


def test_zero_claude_rows_outside_the_window_or_kind_are_dropped():
    edits = [_edit(NOW - 8 * 86400, source="zero_claude", file="a.py", turn_id="t1"),
             _edit(NOW - 100, source="zero_claude", file="a.py", turn_id="t2", sid="s-res")]
    built = _build_edits([], edits, kind_of=lambda sid, st: "research" if sid == "s-res" else "organic")
    assert built["units"] == []


def test_claude_reask_on_the_next_turn_marks_the_zero_claude_turn_redone():
    rows = [proxy_row(1, sid=SID, tier="opus", reason="escalation", ts=NOW - 50, kind="organic", msg_id="e")]
    edits = [_edit(NOW - 100, source="zero_claude", file="a.py", turn_id="t1")]
    built = _build_edits(rows, edits)
    z = [u for u in built["units"] if u.get("zero_claude")]
    assert len(z) == 1 and z[0]["redone"] is True and z[0]["why"] == "escalation"


# ── ledger writer ────────────────────────────────────────────────────────────

def test_record_edit_outcome_carries_source_session_turn_and_returns_ts(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "from-env")
    ts = edit_ledger.record_edit_outcome(file="a.py", model="ollama/x", applied=True, source="zero_claude",
                                         session_id="hook-sid", turn_id="abcd")
    row = json.loads((tmp_path / edit_ledger.LEDGER_FILENAME).read_text().splitlines()[0])
    assert row["source"] == "zero_claude" and row["session_id"] == "hook-sid" and row["turn_id"] == "abcd"
    assert row["ts"] == ts and isinstance(ts, float)


def test_record_edit_outcome_old_call_shape_still_works_and_returns_ts(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "from-env")
    ts = edit_ledger.record_edit_outcome(file="a.py", model="ollama/x", applied=True)
    row = json.loads((tmp_path / edit_ledger.LEDGER_FILENAME).read_text().splitlines()[0])
    assert row["source"] is None and row["turn_id"] is None and row["session_id"] == "from-env"
    assert ts == row["ts"]


def test_record_edit_outcome_rejects_unknown_source_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    edit_ledger.record_edit_outcome(file="a.py", model="x", applied=True, source="bogus")
    row = json.loads((tmp_path / edit_ledger.LEDGER_FILENAME).read_text().splitlines()[0])
    assert row["source"] is None
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(blocker / "sub"))
    assert edit_ledger.record_edit_outcome(file="a.py", model="x", applied=True) is None


def test_llm_edit_tool_records_source_llm_edit():
    import inspect

    from llm_router.tools import text
    src = inspect.getsource(text)
    assert src.count('source="llm_edit"') >= 2          # both record_edit_outcome call sites


# ── (d) the bound ────────────────────────────────────────────────────────────

def test_bound_is_printed_when_session_kind_completeness_is_below_95pct():
    rows = []
    for i in range(80):                                   # organic Haiku turns
        rows.append(proxy_row(i, sid=f"o{i}", tier="haiku", ts=NOW - 5000 + i, kind="organic", msg_id=f"o{i}"))
    for i in range(80):                                   # untagged Sonnet turns
        rows.append(proxy_row(100 + i, sid=f"u{i}", tier="sonnet", ts=NOW - 4000 + i, kind=None,
                              msg_id=f"u{i}"))
    _ledger(rows)
    o3 = kpi.compute_scorecard(days=7, now=NOW, schema_since=0.0)["o3"]
    bound = o3["bound"]
    assert bound["g3_session_kind_coverage"] == pytest.approx(0.5)
    assert bound["lower"] == pytest.approx(min(80 / 80, 80 / 160))
    assert bound["upper"] == pytest.approx(max(80 / 80, 80 / 160))
    assert any("bound" in ln for ln in o3["lines"])


def test_no_bound_when_session_kind_is_complete():
    rows = [proxy_row(i, sid=f"o{i}", tier="haiku", ts=NOW - 5000 + i, kind="organic", msg_id=f"o{i}")
            for i in range(80)]
    _ledger(rows)
    o3 = kpi.compute_scorecard(days=7, now=NOW, schema_since=0.0)["o3"]
    assert "bound" not in o3
