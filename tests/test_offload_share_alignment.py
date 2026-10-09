"""M0.3: O3 matches the owner's definition (PLAN M0.3).

* (a) a local MCP unit is not a turn: reported as ``local_assist_n`` outside n and the numerator;
* (b) the owner's turn definition (PLAN section 1.2 O3): a proxy row that is not a side call and not a
  ``continuation``. The transcript join (proxy ``msg_id`` to ``message.id``) takes out sub-agent first
  calls ONLY; unjoined rows stay in, and injected-input first calls stay in (both are counted);
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


def _assistant_line(msg_id: str, *, sidechain: bool = False) -> str:
    return json.dumps({"type": "assistant", "isSidechain": sidechain, "sessionId": SID,
                       "timestamp": "2026-10-06T10:00:00.000Z",
                       "message": {"id": msg_id, "role": "assistant", "content": []}})


def _user_line(kind: str = "typed", *, sidechain: bool = False) -> str:
    """``typed``: a human prompt. ``tool``: a tool_result only. ``command``: a slash command.
    ``meta``: injected input (peer / sub-agent hand-back, isMeta)."""
    content: object = {"typed": "please do the thing", "command": "<command-name>/x</command-name>",
                       "meta": "[Subagent hand-back] report",
                       "tool": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}[kind]
    return json.dumps({"type": "user", "isSidechain": sidechain, "isMeta": kind == "meta",
                       "timestamp": "2026-10-06T10:00:00.000Z",
                       "message": {"role": "user", "content": content}})


def _write_main(proj: Path, *spec: str) -> None:
    """``spec`` items: ``u:<kind>`` for a user entry, ``a:<msg id>`` for an assistant entry."""
    lines = [_user_line(x[2:]) if x.startswith("u:") else _assistant_line(x[2:]) for x in spec]
    (proj / f"{SID}.jsonl").write_text("".join(ln + "\n" for ln in lines))


def _write_transcripts(proj: Path, main_spec, sub_ids) -> None:
    _write_main(proj, *main_spec)
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
    _write_transcripts(_isolated, ["u:typed", "a:m1", "u:tool", "a:m2", "u:typed", "a:m3", "u:typed", "a:m4"],
                       ["sub1", "sub1b", "sub2"])
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
    assert o3["kept_in"] == {"meta_first": 0, "unjoined": 0, "no_transcript": 0}
    assert b["claude_n"] == 3


def test_scorecard_turns_follow_the_owner_definition_the_transcript_only_removes_subagents(_isolated):
    """M0.3 review fix, end to end through the scorecard. Owner's rule: a turn is a proxy row that is
    not a side call and not a continuation. The transcript takes out the sub-agent first call and
    nothing else: the classifier calls it never saw and the answer to a hand-back STAY IN (counted
    in ``kept_in``), and a row the proxy flagged side_call is NOT promoted to a turn."""
    _write_transcripts(_isolated,
                       ["u:typed", "a:m1", "u:tool", "a:m1c",       # typed turn + continuation
                        "u:typed", "a:m2",                           # typed turn, proxy flagged it side_call
                        "u:meta", "a:peer"],                         # hand-back
                       ["sub1"])
    t = NOW - 3000
    _ledger([proxy_row(1, sid=SID, ts=t, kind="organic", msg_id="m1"),
             proxy_row(2, sid=SID, ts=t + 5, kind="organic", msg_id="m1c", step="continuation"),
             proxy_row(3, sid=SID, ts=t + 100, kind="organic", msg_id="m2", reason="side_call"),
             proxy_row(4, sid=SID, ts=t + 150, kind="organic", msg_id="sub1"),
             proxy_row(5, sid=SID, ts=t + 160, kind="organic", msg_id="classifier-1"),
             proxy_row(6, sid=SID, ts=t + 170, kind="organic", msg_id="classifier-2"),
             proxy_row(7, sid=SID, ts=t + 200, kind="organic", msg_id="peer")])
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    assert o3["breakdown"]["n"] == 4                      # m1, classifier-1, classifier-2, peer
    assert o3["excluded"]["subagent_first"] == 1 and o3["excluded"]["side_call"] == 1
    assert o3["kept_in"] == {"meta_first": 1, "unjoined": 2, "no_transcript": 0}
    assert "unjoined" not in o3["excluded"] and "meta_first" not in o3["excluded"]


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
    sub = osh.build_units(rows, [], thread_of=lambda sid, m: "sidechain" if m in ("s1", "s2") else "turn", **kw)
    assert [u["redone"] for u in sub["units"] if u["class"] == "haiku"] == [True]
    assert sub["subagent_first"] == 2 and sub["unjoined"] == 0
    assert len(osh.turn_units(sub["units"])) == 2          # the haiku turn and the escalation turn


def _kw():
    return dict(now=NOW, days=7, allowed=frozenset({"organic"}), kind_of=lambda s, st: st)


def test_calls_the_transcript_never_saw_stay_in_as_turns_and_are_counted():
    """PLAN M0.3(b): "Unjoined rows stay in". (They were taken out by the first repair, which
    swapped in a different turn definition; the owner's is restored.)"""
    rows = [proxy_row(1, sid=SID, kind="organic", msg_id="x"), proxy_row(2, sid=SID, kind="organic", msg_id="y")]
    built = osh.build_units(rows, [], thread_of=lambda sid, m: "orphan", **_kw())
    assert len(osh.turn_units(built["units"])) == 2 and built["unjoined"] == 2
    assert built["subagent_first"] == 0 and built["no_transcript"] == 0


def test_session_without_a_transcript_keeps_the_proxy_only_rule_and_says_so():
    rows = [proxy_row(1, sid=SID, kind="organic", msg_id="x"), proxy_row(2, sid=SID, kind="organic", msg_id="y")]
    built = osh.build_units(rows, [], thread_of=lambda sid, m: None, **_kw())
    assert len(osh.turn_units(built["units"])) == 2 and built["no_transcript"] == 2
    assert built["unjoined"] == 0 and built["subagent_first"] == 0


def test_first_call_of_injected_input_stays_a_turn_and_is_counted():
    rows = [proxy_row(1, sid=SID, kind="organic", msg_id="t"), proxy_row(2, sid=SID, kind="organic", msg_id="p")]
    built = osh.build_units(rows, [], thread_of=lambda sid, m: {"t": "turn", "p": "meta"}[m], **_kw())
    assert len(osh.turn_units(built["units"])) == 2 and built["meta_first"] == 1


def test_transcript_does_not_override_step_class_or_the_side_call_flag():
    """The owner's rule is on the proxy row. Whatever role the transcript gives a row, only
    ``sidechain`` changes the turn count."""
    rows = [proxy_row(1, sid=SID, kind="organic", msg_id="a", reason="side_call"),       # flagged: not a turn
            proxy_row(2, sid=SID, kind="organic", msg_id="b", step="continuation"),      # continuation: not a turn
            proxy_row(3, sid=SID, kind="organic", msg_id="c"),                           # turn
            proxy_row(4, sid=SID, kind="organic", msg_id="d")]                           # turn
    roles = {"a": "turn", "b": "turn", "c": "continuation", "d": "continuation"}
    with_join = osh.build_units(rows, [], thread_of=lambda sid, m: roles[m], **_kw())
    plain = osh.build_units(rows, [], **_kw())
    assert [u["msg_id"] for u in osh.turn_units(with_join["units"])] == ["c", "d"]
    assert [u["msg_id"] for u in osh.turn_units(plain["units"])] == ["c", "d"]
    assert with_join["side_call_excluded"] == plain["side_call_excluded"] == 1


def test_side_call_rows_of_a_session_with_no_transcript_stay_excluded():
    rows = [proxy_row(1, sid=SID, kind="organic", msg_id="a", reason="side_call")]
    built = osh.build_units(rows, [], thread_of=lambda sid, m: None, **_kw())
    assert built["side_call_excluded"] == 1 and built["units"] == []


# ── performance: the transcript join is only paid for admitted sessions inside the window ──────

def test_transcripts_are_read_only_for_admitted_sessions_inside_the_window():
    """Review fix: the join used to run for every session in the whole ledger because the
    conversation index called the lookup on every row. Red before: the lookup saw "old" (before the
    window) and "res" (not an admitted kind)."""
    t = NOW - 3000
    rows = [proxy_row(1, sid="org", kind="organic", ts=t, msg_id="o1"),
            proxy_row(2, sid="org", kind="organic", ts=t + 5, msg_id="o2", step="continuation"),
            proxy_row(3, sid="res", kind="research", ts=t, msg_id="r1"),
            proxy_row(4, sid="old", kind="organic", ts=NOW - 30 * 86400, msg_id="x1")]
    seen: list[str] = []

    def thread_of(sid, mid):
        seen.append(sid)
        return "turn"

    kind = {"org": "organic", "res": "research", "old": "organic"}
    built = osh.build_units(rows, [], thread_of=thread_of, now=NOW, days=7, allowed=frozenset({"organic"}),
                            kind_of=lambda sid, st: kind[sid])
    assert set(seen) == {"org"}
    assert len(osh.turn_units(built["units"])) == 1


def test_scorecard_reads_no_transcript_of_sessions_outside_the_window_or_kind(_isolated, monkeypatch):
    from llm_router import o3_transcripts

    opened: list[str] = []
    real = o3_transcripts.thread_index

    def spy(sid, **kw):
        opened.append(sid)
        return real(sid, **kw)

    monkeypatch.setattr(o3_transcripts, "thread_index", spy)
    t = NOW - 3000
    _write_main(_isolated, "u:typed", "a:m1")
    session_kind.tag_session("s-res", "/Users/someone/Projects/app", env={"LLM_ROUTER_SESSION_KIND": "research"})
    _ledger([proxy_row(1, sid=SID, ts=t, kind="organic", msg_id="m1"),
             proxy_row(2, sid="s-res", ts=t, kind="research", msg_id="r1"),
             proxy_row(3, sid="s-old", ts=NOW - 30 * 86400, kind="organic", msg_id="x1")])
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    assert o3["breakdown"]["n"] == 1
    assert opened == [SID]


def test_continuation_rows_are_never_counted_as_unjoined_or_subagent_first():
    rows = [proxy_row(1, sid=SID, kind="organic", msg_id="x"),
            proxy_row(2, sid=SID, kind="organic", msg_id="y", step="continuation")]
    built = osh.build_units(rows, [], thread_of=lambda sid, m: "sidechain" if m == "y" else "turn", **_kw())
    assert built["subagent_first"] == 0 and built["unjoined"] == 0


def test_thread_index_reads_main_and_subagent_files(_isolated):
    _write_transcripts(_isolated, ["u:typed", "a:m1"], ["s1"])
    assert osh_transcripts().thread_index(SID) == {"m1": "turn", "s1": "sidechain"}


def test_thread_index_reaches_workflow_agents_one_level_deeper(_isolated):
    wf = _isolated / SID / "subagents" / "workflows" / "wf_abc"
    wf.mkdir(parents=True)
    (wf / "agent-w1.jsonl").write_text(_assistant_line("w1", sidechain=True) + "\n")
    assert osh_transcripts().thread_index(SID) == {"w1": "sidechain"}


def test_thread_index_flags_isSidechain_entries_in_the_main_file(_isolated):
    (_isolated / f"{SID}.jsonl").write_text(_user_line() + "\n" + _assistant_line("m1") + "\n"
                                            + _assistant_line("m2", sidechain=True) + "\n")
    assert osh_transcripts().thread_index(SID) == {"m1": "turn", "m2": "sidechain"}


def test_thread_index_ignores_other_sessions_and_bad_lines(_isolated):
    (_isolated / "other.jsonl").write_text(_assistant_line("zz", sidechain=True) + "\n")
    (_isolated / f"{SID}.jsonl").write_text("not json\n" + _user_line() + "\n" + _assistant_line("m1") + "\n")
    assert osh_transcripts().thread_index(SID) == {"m1": "turn"}


def test_thread_roles_follow_what_started_the_turn(_isolated):
    _write_main(_isolated,
                "u:typed", "a:t1", "u:tool", "a:t1c", "u:tool", "a:t1d",       # one typed turn with 2 follow-ups
                "u:command", "a:cmd",                                         # slash command: meta
                "u:meta", "a:peer",                                           # peer / hand-back: meta
                "u:typed", "u:meta", "a:t2",                                  # injected input after a typed prompt: still typed
                "u:tool", "u:meta", "a:t2c",                                  # injected input inside a tool loop: continuation
                "a:t2c2",                                                     # assistant after assistant: continuation
                "u:typed", "a:t3", "a:t3")                                    # a message written twice: one role
    assert osh_transcripts().thread_index(SID) == {
        "t1": "turn", "t1c": "continuation", "t1d": "continuation", "cmd": "meta", "peer": "meta",
        "t2": "turn", "t2c": "continuation", "t2c2": "continuation", "t3": "turn"}


def test_thread_lookup_orphan_versus_no_transcript(_isolated):
    _write_main(_isolated, "u:typed", "a:m1")
    of = osh_transcripts().thread_lookup()
    assert of(SID, "m1") == "turn"
    assert of(SID, "not-in-it") == "orphan"            # the session has a transcript, the id is not in it
    assert of("another-session", "m1") is None          # no transcript at all: nothing can be said
    assert of(SID, None) is None and of(None, "m1") is None


def test_typed_prompt_definition_matches_the_integrity_check():
    ot = osh_transcripts()
    typed = json.loads(_user_line("typed"))
    assert ot.is_typed_prompt(typed)
    assert not ot.is_typed_prompt(json.loads(_user_line("tool")))
    assert not ot.is_typed_prompt(json.loads(_user_line("command")))
    assert not ot.is_typed_prompt(json.loads(_user_line("meta")))
    assert not ot.is_typed_prompt(json.loads(_user_line("typed", sidechain=True)))


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


# ── O3-STEP-1: a null step_class is not a kind, so it cannot start a turn ───────────────────────

def test_null_step_and_subagent_first_rows_cannot_start_a_turn():
    """Red on main: null-step rows (pre-GE1 rows) and ``subagent_first`` rows counted as turns, and as
    human-turn boundaries of the redo window. Only ``step_class == turn_first`` starts a turn."""
    t = NOW - 3000
    rows = [proxy_row(1, sid=SID, kind="organic", ts=t, msg_id="a", tier="haiku"),                 # turn 1
            proxy_row(2, sid=SID, kind="organic", ts=t + 1, msg_id="b", step=None),                 # old row
            proxy_row(3, sid=SID, kind="organic", ts=t + 2, msg_id="c", step=None, tier="haiku"),   # old row
            proxy_row(4, sid=SID, kind="organic", ts=t + 3, msg_id="d", step="subagent_first"),
            proxy_row(5, sid=SID, kind="organic", ts=t + 4, msg_id="e", step="continuation"),
            proxy_row(6, sid=SID, kind="organic", ts=t + 5, msg_id="f", reason="side_call", step=None),
            proxy_row(7, sid=SID, kind="organic", ts=t + 6, msg_id="g")]                            # turn 2
    for thread_of in (None, lambda sid, m: None):
        built = osh.build_units(rows, [], thread_of=thread_of, **_kw())
        assert [u["msg_id"] for u in osh.turn_units(built["units"])] == ["a", "g"]
        assert len(built["units"]) == 6                      # per-call units are all still counted
        assert built["side_call_excluded"] == 1
        assert built["no_step_class"] == 2 and built["step_subagent_first"] == 1


def test_null_step_rows_do_not_close_the_redo_window_of_a_turn():
    """A Haiku turn followed by 3 null-step rows and then an escalation: the old rows are not human turns, so
    the escalation is still inside the unit's window (turn 2) and the unit is redone. Red on main, where the 3
    rows were turns 2-4 and pushed the escalation out of the window."""
    t = NOW - 3000
    rows = [proxy_row(1, sid=SID, kind="organic", ts=t, msg_id="a", tier="haiku")]
    rows += [proxy_row(2 + i, sid=SID, kind="organic", ts=t + 1 + i, msg_id=f"n{i}", step=None) for i in range(3)]
    rows += [proxy_row(9, sid=SID, kind="organic", ts=t + 10, msg_id="x", tier="opus", reason="escalation")]
    turn = osh.turn_units(osh.build_units(rows, [], **_kw())["units"])
    assert [u["msg_id"] for u in turn] == ["a", "x"]
    assert turn[0]["redone"] is True and turn[0]["why"] == "escalation"


def test_scorecard_prints_the_null_step_exclusion_and_keeps_the_small_n_rule(monkeypatch):
    t = NOW - 3000
    rows = [proxy_row(i, sid=SID, kind="organic", ts=t + i, msg_id=f"m{i}", step=None) for i in range(60)]
    rows += [proxy_row(100 + i, sid=SID, kind="organic", ts=t + 100 + i, msg_id=f"t{i}") for i in range(3)]
    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), rows)
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    assert o3["breakdown"]["n"] == 3 and o3["excluded"]["no_step_class"] == 60
    assert "no rate is printed below" in o3["lines"][0]       # n=3 < MIN_N: still "too few", not 60+3
    assert any("60 with no step_class (not a turn)" in line for line in o3["lines"])
