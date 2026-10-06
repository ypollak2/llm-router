"""M0.9 redo detector: patterns, human-turn handling, and the offload_share source-4 wiring.

Fixtures carry placeholder text and transcript SHAPES only (roles, block types, origins), never
real prompts (PLAN 3.6)."""
from __future__ import annotations

import json

import pytest

from llm_router import offload_share as osh
from llm_router import redo_signal as rs
from llm_router.proxy import escalation

from tests._o3_fixture import NOW, proxy_row

T0 = 1_790_000_000.0  # fixed epoch for transcript timestamps


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def human(text, ts=T0, **extra):
    rec = {"type": "user", "timestamp": _iso(ts), "origin": {"kind": "human"}, "promptSource": "typed",
           "message": {"role": "user", "content": text}}
    rec.update(extra)
    return rec


def assistant(text, ts=T0 + 1):
    return {"type": "assistant", "timestamp": _iso(ts),
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def tool_result(ts=T0 + 2):
    return {"type": "user", "timestamp": _iso(ts),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}


def convo(*prompts, step=60.0):
    out = []
    for i, p in enumerate(prompts):
        out.append(human(p, ts=T0 + i * step))
        out.append(assistant("placeholder answer %d" % i, ts=T0 + i * step + 1))
    return out


# ── patterns ──────────────────────────────────────────────────────────────────

def test_one_source_of_patterns_with_the_proxy_escalation_detector():
    assert rs._CLAUDE_REASK_RE is escalation._CLAUDE_REASK_RE
    assert rs._OPUS_PIN_RE is escalation._OPUS_PIN_RE
    assert rs._CONTRADICTION_RE is escalation._CONTRADICTION_RE


def test_neutral_prompts_are_not_flagged():
    assert rs.redo_flags(convo("please add a retry to the fetch helper", "now write the docs for it")) == []


@pytest.mark.parametrize("prefix", ["claude:", "Native:", "OPUS: "])
def test_prefix_reask_is_flagged_on_a_later_turn(prefix):
    flags = rs.redo_flags(convo("explain the cache", prefix + " explain the cache"))
    assert flags == [(1, rs.SIGNAL_REASK)]


def test_first_prompt_can_never_be_a_redo():
    assert rs.redo_flags(convo("claude: explain the cache")) == []


@pytest.mark.parametrize("text", ["No, that is not what I asked", "that's wrong, the file is elsewhere",
                                  "you missed the second case", "incorrect. use the other helper"])
def test_contradiction_opening(text):
    assert rs.redo_flags(convo("do the thing", text)) == [(1, rs.SIGNAL_CONTRADICTION)]


@pytest.mark.parametrize("text", ["I ran it and it didn't work", "this doesn't work at all", "try again please",
                                  "it is still failing on the same line"])
def test_failure_phrase_near_the_start(text):
    assert rs.redo_flags(convo("do the thing", text)) == [(1, rs.SIGNAL_CONTRADICTION)]


@pytest.mark.parametrize("text", [
    "the button doesn't show the saved value", "it never works on the second run", "please run it again",
    "can you check them again", "I still cannot see the result", "the ci failed, check it",
    "you forgot the second file", "you should have asked first", "this is not right", "that is not correct",
    "please redo the table", "I'm missing the column list", "let's rerun the checks", "run again the checks"])
def test_v1_v2_failure_phrases(text):
    assert rs.redo_flags(convo("do the thing", text)) == [(1, rs.SIGNAL_CONTRADICTION)]


@pytest.mark.parametrize("text", ["add a retry to the fetch helper", "now write the docs", "what does this do",
                                  "good, run the tests"])
def test_ordinary_follow_ups_are_not_flagged(text):
    assert rs.redo_flags(convo("do the thing", text)) == []


def test_known_false_positive_shape_is_documented():
    # v2 matches "I am missing" anywhere in the first 200 chars, whoever is missing what (test-half precision 2/5).
    assert rs.redo_flags(convo("do the thing", "I am missing coffee, let's continue")) == [
        (1, rs.SIGNAL_CONTRADICTION)]


def test_repeat_of_a_short_prompt_is_not_a_redo_but_a_long_one_is():
    short = "what is the status of the run now"                  # 8 words
    assert rs.redo_flags(convo(short, short)) == [(1, rs.SIGNAL_REPEAT)]
    shorter = "what is the status of the run"                    # 7 words
    assert rs.redo_flags(convo(shorter, shorter)) == []


def test_failure_phrase_deep_in_a_long_prompt_is_not_flagged():
    long_prompt = "please review the attached design notes and summarise the open questions " * 6 + "try again"
    assert rs.redo_flags(convo("do the thing", long_prompt)) == []


def test_repeat_needs_five_gram_jaccard_of_at_least_point_eight():
    a = "list every function in the billing module that reads the customer table and explain each one"
    same = a + " now"
    assert rs.redo_flags(convo(a, same)) == [(1, rs.SIGNAL_REPEAT)]
    different = "write a changelog entry for the billing module release covering the new invoice export"
    assert rs.redo_flags(convo(a, different)) == []


def test_repeat_ignores_prompts_under_five_words():
    assert rs.redo_flags(convo("yes go on", "yes go on")) == []


def test_jaccard_helper_values():
    assert rs.five_gram_jaccard("a b c d e f", "a b c d e f") == 1.0
    assert rs.five_gram_jaccard("a b c d e f", "x y z w v u") == 0.0
    assert rs.five_gram_jaccard("a b c d", "a b c d") is None


# ── human turns ───────────────────────────────────────────────────────────────

def test_non_human_records_do_not_shift_the_turn_index():
    entries = [human("first question here", ts=T0), assistant("answer", ts=T0 + 1), tool_result(),
               {"type": "user", "timestamp": _iso(T0 + 3), "origin": {"kind": "task-notification"},
                "promptSource": "system", "message": {"role": "user", "content": "notice"}},
               {"type": "user", "timestamp": _iso(T0 + 4), "isSidechain": True, "origin": {"kind": "human"},
                "message": {"role": "user", "content": "claude: side"}},
               {"type": "user", "timestamp": _iso(T0 + 5), "origin": {"kind": "human"},
                "promptSource": "suggestion_accepted", "message": {"role": "user", "content": "claude: sugg"}},
               {"type": "user", "timestamp": _iso(T0 + 6), "origin": {"kind": "human"}, "promptSource": "sdk",
                "message": {"role": "user", "content": "claude: sdk"}},
               human("claude: first question here", ts=T0 + 60)]
    assert [p.text for p in rs.human_prompts(entries)] == ["first question here", "claude: first question here"]
    assert rs.redo_flags(entries) == [(1, rs.SIGNAL_REASK)]


def test_system_reminders_are_stripped_before_matching():
    text = "<system-reminder>hook says hi</system-reminder>No, that is wrong"
    assert rs.redo_flags(convo("do the thing", text)) == [(1, rs.SIGNAL_CONTRADICTION)]


def test_old_transcripts_without_origin_or_prompt_source_still_count():
    e = [{"type": "user", "timestamp": _iso(T0), "message": {"role": "user", "content": "first ask here"}},
         assistant("a"),
         {"type": "user", "timestamp": _iso(T0 + 60), "message": {"role": "user", "content": "claude: again"}}]
    assert rs.redo_flags(e) == [(1, rs.SIGNAL_REASK)]


def test_redo_flag_times_carry_the_prompt_timestamp():
    out = rs.redo_flag_times(convo("do the thing", "try again", step=120.0))
    assert len(out) == 1 and out[0][1:] == (1, rs.SIGNAL_CONTRADICTION)
    assert abs(out[0][0] - (T0 + 120.0)) < 1.0


def test_malformed_entries_never_raise():
    junk = [None, 3, {"type": "user"}, {"type": "user", "message": None}, {"type": "assistant", "message": []},
            {"type": "user", "timestamp": "not a time", "message": {"content": [None, {"type": "text"}]}}]
    assert rs.redo_flags(junk) == []
    assert rs.turn_pairs(junk) == []


def test_turn_pairs_hold_the_answer_tail_and_the_next_prompt():
    e = convo("first ask here", "second ask here")
    e[1] = assistant("x" * 3000)
    pairs = rs.turn_pairs(e)
    assert len(pairs) == 1
    p = pairs[0]
    assert p.turn_index == 1 and p.prompt == "second ask here"
    assert p.answer_tail == "x" * rs.ANSWER_TAIL_CHARS


def test_turn_pairs_skip_a_turn_with_no_assistant_text():
    e = [human("first ask here"), tool_result(), human("second ask here", ts=T0 + 60)]
    assert rs.turn_pairs(e) == []


def test_pattern_version_is_recorded():
    assert isinstance(rs.PATTERN_VERSION, str) and rs.PATTERN_VERSION


# ── transcript loader ─────────────────────────────────────────────────────────

def test_session_flags_reads_one_transcript_and_returns_nothing_for_a_missing_one(tmp_path):
    proj = tmp_path / "-some-project"
    proj.mkdir()
    (proj / "sess-a.jsonl").write_text("\n".join(json.dumps(r) for r in convo("do it", "claude: do it")) + "\n")
    load = rs.session_flags_loader(tmp_path)
    assert [(s) for _, s in load("sess-a")] == [rs.SIGNAL_REASK]
    assert load("sess-missing") == []


# ── offload_share source 4 ────────────────────────────────────────────────────

def _rows(sid, t0, n_turns=4):
    rows = []
    for k in range(n_turns):
        ts = t0 + k * 100
        rows.append(proxy_row(2 * k, sid=sid, tier="haiku" if k == 0 else "sonnet", ts=ts, kind="organic"))
        rows.append(proxy_row(2 * k + 1, sid=sid, tier="sonnet", step="continuation", ts=ts + 1, kind="organic"))
    return rows


def _units(rows, flags):
    return osh.build_units(rows, [], now=NOW, days=7, allowed=frozenset({"organic"}),
                           kind_of=lambda sid, stamp: stamp, detector_flags=lambda sid: flags.get(sid, []))


def test_source4_is_off_by_default_and_flags_are_only_counted():
    assert osh.REDO_SOURCE4_ENABLED is False
    t0 = NOW - 3000
    built = _units(_rows("s1", t0), {"s1": [(t0 + 100 - 5, rs.SIGNAL_REASK)]})
    first = [u for u in built["units"] if u["first"]][0]
    assert first["class"] == "haiku" and first["redone"] is False
    assert built["n_detector_flags"] == 1


def test_source4_when_enabled_marks_a_flag_in_the_next_two_turns(monkeypatch):
    monkeypatch.setattr(osh, "REDO_SOURCE4_ENABLED", True)
    t0 = NOW - 3000
    built = _units(_rows("s1", t0), {"s1": [(t0 + 200 - 5, rs.SIGNAL_CONTRADICTION)]})   # turn 3 = unit turn + 2
    first = [u for u in built["units"] if u["first"]][0]
    assert first["redone"] is True and first["why"] == "detector"


def test_source4_ignores_flags_on_the_unit_turn_and_beyond_two_turns(monkeypatch):
    monkeypatch.setattr(osh, "REDO_SOURCE4_ENABLED", True)
    t0 = NOW - 3000
    own = _units(_rows("s1", t0), {"s1": [(t0 - 5, rs.SIGNAL_REASK)]})                    # unit's own prompt
    far = _units(_rows("s1", t0, n_turns=5), {"s1": [(t0 + 300 - 5, rs.SIGNAL_REASK)]})   # 3 turns later
    for built in (own, far):
        assert [u for u in built["units"] if u["first"]][0]["redone"] is False


def test_source4_flag_with_no_proxy_turn_near_it_is_unmapped(monkeypatch):
    monkeypatch.setattr(osh, "REDO_SOURCE4_ENABLED", True)
    t0 = NOW - 3000
    built = _units(_rows("s1", t0), {"s1": [(t0 + 1500, rs.SIGNAL_REASK)]})
    assert built["n_detector_flags"] == 0 and built["n_detector_unmapped"] == 1
    assert [u for u in built["units"] if u["first"]][0]["redone"] is False


def test_build_units_without_detector_flags_is_unchanged():
    t0 = NOW - 3000
    a = osh.build_units(_rows("s1", t0), [], now=NOW, days=7, allowed=frozenset({"organic"}),
                        kind_of=lambda sid, stamp: stamp)
    assert a["n_detector_flags"] == 0 and all(u["why"] is None for u in a["units"])


# ── kpi: the breakdown reports the detector's volume, disabled ────────────────

def test_kpi_o3_reports_detector_flags_while_source4_is_disabled(monkeypatch, tmp_path):
    from llm_router import paths, session_kind
    from llm_router.commands import kpi
    from tests._o3_fixture import write_jsonl

    proj = tmp_path / "claude-projects"
    (proj / "-p").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(proj))
    session_kind._FOUND.clear()
    session_kind.tag_session("s-org", "/Users/someone/Projects/app", env={})
    t0 = NOW - 5000
    entries = [human("first ask here", ts=t0 - 3), assistant("a", ts=t0 + 1),
               human("claude: first ask here", ts=t0 + 100 - 3), assistant("b", ts=t0 + 101)]
    (proj / "-p" / "s-org.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    rows = []
    for k in range(2):
        rows.append(proxy_row(2 * k, sid="s-org", tier="haiku", ts=t0 + 100 * k, kind="organic"))
        rows.append(proxy_row(2 * k + 1, sid="s-org", tier="haiku", step="continuation", ts=t0 + 100 * k + 1,
                              kind="organic"))
    write_jsonl(paths.state_path("proxy_calls.jsonl"), rows)
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    bd = o3["breakdown"]
    assert bd["redo_detector_n"] == 1 and bd["redo_detector_enabled"] is False
    assert bd["haiku_redone"] == 0          # disabled: the flag changed no unit
    monkeypatch.setattr(osh, "REDO_SOURCE4_ENABLED", True)
    on = kpi.compute_scorecard(days=7, now=NOW)["o3"]["breakdown"]
    assert on["haiku_redone"] == 1 and on["per_call"]["haiku_redone"] == 2    # turn 1 and its continuation
    assert on["redo_detector_enabled"] is True and on["redo_detector_n"] == 1
    session_kind._FOUND.clear()
