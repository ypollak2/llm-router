"""usage_outcome: used / redone / unknown per routed event, on synthetic transcripts.

The definition lives in ``llm_router.usage_outcome``'s docstring; every case
here is one sentence of it. Transcripts are built record by record so the
window (the rest of the event's turn plus the next 3 HUMAN turns) is explicit.
"""

from __future__ import annotations

import json

import pytest

from llm_router import paths, usage_outcome as uo

EDIT_TOOL = "mcp__llm_router__llm_edit"
LLM_TOOL = "mcp__llm_router__llm"
OLD, NEW = "def f():\n    return 1", "def f():\n    return 2"


class T:
    """A transcript builder. ``human`` opens a turn; everything else is within it."""

    def __init__(self):
        self.recs, self.n = [], 0

    def _add(self, rec):
        self.n += 1
        rec["timestamp"] = f"2026-10-01T10:{self.n // 60:02d}:{self.n % 60:02d}Z"
        self.recs.append(rec)
        return self

    def human(self, text="next please"):
        return self._add({"type": "user", "message": {"role": "user", "content": text}})

    def say(self, text):
        return self._add({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})

    def call(self, tid, name, inp):
        return self._add({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp}]}})

    def result(self, tid, text, error=False):
        block = {"type": "tool_result", "tool_use_id": tid, "content": text}
        if error:
            block["is_error"] = True
        return self._add({"type": "user", "message": {"content": [block]}})

    def judge(self):
        return uo.judge_transcript(self.recs, session_id="s1")


def _edit_result(pairs):
    return "**edits**\n---\n```json\n" + json.dumps(pairs) + "\n```"


def _pair(file="src/a.py", old=OLD, new=NEW):
    return {"file": file, "old_string": old, "new_string": new, "description": ""}


def _routed_edit(t, tid="e1", pairs=None):
    t.human("fix a.py").call(tid, EDIT_TOOL, {"task": "fix", "files": ["src/a.py"]})
    return t.result(tid, _edit_result(pairs or [_pair()]))


def _apply(t, file="/repo/src/a.py", old=OLD, new=NEW, tid="x1"):
    return t.call(tid, "Edit", {"file_path": file, "old_string": old, "new_string": new}) \
        .result(tid, "ok")


def _turns(t, n):
    for i in range(n):
        t.human(f"unrelated {i}").say("done")
    return t


def _only(rows):
    assert len(rows) == 1, rows  # an empty set would pass everything vacuously
    return rows[0]["outcome"], rows[0]["reason"]


# ── edits ────────────────────────────────────────────────────────────────────

def test_edit_applied_verbatim_and_never_touched_in_3_turns_is_used():
    t = _routed_edit(T())
    _apply(t)
    _turns(t, 3)
    assert _only(t.judge()) == ("used", "applied_verbatim")


def test_edit_applied_but_window_still_open_is_unknown_not_used():
    t = _routed_edit(T())
    _apply(t)
    _turns(t, 2)
    assert _only(t.judge()) == ("unknown", "window_open")


def test_edit_re_edited_inside_the_window_is_redone_even_while_open():
    t = _routed_edit(T())
    _apply(t)
    t.human("hmm that is wrong")
    _apply(t, old=NEW, new="def f():\n    return 3", tid="x2")
    assert _only(t.judge()) == ("redone", "re_edited")


def test_edit_reverted_by_restoring_the_old_text_is_redone():
    t = _routed_edit(T())
    _apply(t)
    t.human("undo that")
    _apply(t, old="return 2", new=OLD, tid="x2")
    assert _only(t.judge()) == ("redone", "reverted")


def test_edit_reverted_by_git_is_redone():
    t = _routed_edit(T())
    _apply(t)
    t.human("revert")
    t.call("b1", "Bash", {"command": "git checkout -- src/a.py"}).result("b1", "")
    assert _only(t.judge()) == ("redone", "reverted")


def test_git_undo_of_some_other_file_is_not_a_redo():
    t = _routed_edit(T())
    _apply(t)
    t.call("b1", "Bash", {"command": "git checkout -- docs/other.md"}).result("b1", "")
    _turns(t, 3)
    assert _only(t.judge()) == ("used", "applied_verbatim")


def test_edit_rewritten_with_write_is_redone():
    t = _routed_edit(T())
    _apply(t)
    t.call("w1", "Write", {"file_path": "/repo/src/a.py", "content": "print('x')"}).result("w1", "ok")
    assert _only(t.judge()) == ("redone", "rewritten")


def test_claude_edits_the_file_its_own_way_instead_is_redone():
    t = _routed_edit(T())
    _apply(t, new="def f():\n    return 99")
    assert _only(t.judge()) == ("redone", "edited_differently")


def test_a_later_unrelated_edit_to_the_same_file_is_not_a_redo():
    t = _routed_edit(T())
    _apply(t)
    _apply(t, old="import os", new="import sys", tid="x2")
    _turns(t, 3)
    assert _only(t.judge()) == ("used", "applied_verbatim")


def test_an_edit_that_lands_after_the_window_does_not_count():
    t = _routed_edit(T())
    _apply(t)
    _turns(t, 3)
    t.human("much later")
    _apply(t, old=NEW, new="def f():\n    return 3", tid="x9")
    assert _only(t.judge()) == ("used", "applied_verbatim")


def test_edit_never_applied_and_file_untouched_is_unknown():
    t = _routed_edit(T())
    _turns(t, 3)
    assert _only(t.judge()) == ("unknown", "not_applied_seen")


def test_edit_result_without_pairs_is_unknown():
    t = T().human("fix").call("e1", EDIT_TOOL, {"task": "x"}).result("e1", "edit rejected: no unique match")
    _turns(t, 3)
    assert _only(t.judge()) == ("unknown", "no_pairs")


def test_multi_file_edit_one_applied_one_not_is_partly_applied():
    t = _routed_edit(T(), pairs=[_pair("src/a.py"), _pair("src/b.py")])
    _apply(t)
    _turns(t, 3)
    assert _only(t.judge()) == ("unknown", "partly_applied")


def test_multi_file_edit_one_redone_is_redone():
    t = _routed_edit(T(), pairs=[_pair("src/a.py"), _pair("src/b.py")])
    _apply(t)
    _apply(t, file="/repo/src/b.py", new="other", tid="x2")
    assert _only(t.judge()) == ("redone", "edited_differently")


def test_mcp_result_envelope_is_unwrapped():
    """Real transcripts carry the llm_edit text as {"result": "<text>"}."""
    t = T().human("fix a.py").call("e1", EDIT_TOOL, {"task": "fix"})
    t.result("e1", json.dumps({"result": _edit_result([_pair()])}))
    _apply(t)
    _turns(t, 3)
    assert _only(t.judge()) == ("used", "applied_verbatim")


def test_multiedit_application_counts():
    t = _routed_edit(T())
    t.call("m1", "MultiEdit", {"file_path": "/repo/src/a.py", "edits": [
        {"old_string": OLD, "new_string": NEW}]}).result("m1", "ok")
    _turns(t, 3)
    assert _only(t.judge()) == ("used", "applied_verbatim")


# ── answers ──────────────────────────────────────────────────────────────────

ASK = "what does the retry decorator in the http client do when the server returns a 503 status"
ANSWER = ("The retry decorator wraps the request, catches 503 responses, waits with exponential "
          "backoff starting at two hundred milliseconds and gives up after five attempts raising the last error")


def _routed_answer(t, tid="a1", prompt=ASK, answer=ANSWER):
    t.human("explain").call(tid, LLM_TOOL, {"prompt": prompt, "task": "query"})
    return t.result(tid, answer)


def test_answer_not_redone_after_3_turns_is_used_weakly():
    t = _routed_answer(T()).say("ok, moving on to the tests")
    _turns(t, 3)
    assert _only(t.judge()) == ("used", "not_redone")


def test_answer_relayed_by_claude_is_used_with_strong_evidence():
    t = _routed_answer(T()).say(ANSWER)
    _turns(t, 3)
    assert _only(t.judge()) == ("used", "result_reused")


def test_answer_with_window_open_is_unknown():
    t = _routed_answer(T()).say("ok")
    _turns(t, 1)
    assert _only(t.judge()) == ("unknown", "window_open")


def test_re_asking_the_same_question_is_redone():
    t = _routed_answer(T())
    t.human("not what I meant").call("a2", LLM_TOOL, {"prompt": ASK + " please", "task": "query"})
    t.result("a2", "something else")
    rows = t.judge()
    assert [(r["event_id"], r["outcome"], r["reason"]) for r in rows][0] == ("a1", "redone", "re_asked")


def test_re_asking_through_an_agent_is_redone():
    t = _routed_answer(T())
    t.human("dig deeper").call("g1", "Agent", {"prompt": ASK, "subagent_type": "general-purpose"})
    assert t.judge()[0]["outcome"] == "redone"


def test_a_different_question_is_not_a_re_ask():
    t = _routed_answer(T()).say("thanks")
    t.human("next").call("a2", LLM_TOOL, {"prompt": "summarise the changelog entries for the last three releases "
                                                    "and list every breaking change", "task": "query"})
    t.result("a2", "ok")
    _turns(t, 3)
    assert t.judge()[0]["outcome"] == "used"


def test_human_override_prefix_is_redone():
    t = _routed_answer(T())
    t.human("claude: answer that yourself")
    assert _only(t.judge()[:1]) == ("redone", "overridden")


def test_answer_with_no_result_or_an_error_is_unknown():
    t = T().human("q").call("a1", LLM_TOOL, {"prompt": ASK})
    _turns(t, 3)
    assert _only(t.judge()) == ("unknown", "no_result")
    t2 = T().human("q").call("a1", LLM_TOOL, {"prompt": ASK}).result("a1", "boom", error=True)
    _turns(t2, 3)
    assert _only(t2.judge()) == ("unknown", "no_result")


def test_delegation_to_codex_is_judged_like_an_answer():
    t = T().human("do it").call("d1", "mcp__llm_router__llm_router_agent_route", {"task": ASK})
    t.result("d1", ANSWER)
    _turns(t, 3)
    row = t.judge()[0]
    assert (row["tool"], row["kind"], row["outcome"]) == ("llm_router_agent_route", "answer", "used")


# ── window and turn semantics ────────────────────────────────────────────────

def test_tool_results_and_system_wrappers_are_not_human_turns():
    t = _routed_edit(T())
    _apply(t)
    for i in range(5):  # five records that look like users but are not human turns
        t._add({"type": "user", "message": {"content": "<system-reminder>hi</system-reminder>"}})
        t._add({"type": "user", "isMeta": True, "message": {"content": "meta"}})
    assert _only(t.judge()) == ("unknown", "window_open")


def test_non_router_tools_produce_no_events():
    t = T().human("q").call("r1", "Read", {"file_path": "/x"}).result("r1", "text")
    _turns(t, 4)
    assert t.judge() == []


def test_other_router_server_prefix_is_recognised():
    t = T().human("q").call("a1", "mcp__plugin_llm-router_llm-router__llm", {"prompt": ASK})
    t.result("a1", ANSWER)
    _turns(t, 3)
    assert _only(t.judge())[0] == "used"


def test_every_row_has_the_ledger_fields():
    t = _routed_answer(T())
    _turns(t, 3)
    row = t.judge()[0]
    assert set(row) >= {"event_id", "session_id", "session_kind", "ts", "tool", "kind", "outcome",
                        "reason", "turns_after", "window_closed"}
    assert row["outcome"] in ("used", "redone", "unknown") and row["window_closed"] is True


def test_session_kind_is_looked_up_for_the_row():
    t = _routed_answer(T())
    _turns(t, 3)
    rows = uo.judge_transcript(t.recs, session_id="s1", kind_lookup=lambda sid: "research")
    assert rows[0]["session_kind"] == "research"


# ── ledger ───────────────────────────────────────────────────────────────────

def _row(eid="e1", outcome="unknown", reason="window_open"):
    return {"event_id": eid, "session_id": "s", "outcome": outcome, "reason": reason}


def test_ledger_is_idempotent_and_final_verdicts_are_never_rewritten():
    assert uo.record_outcomes([_row("e1", "unknown", "window_open")]) == 1
    assert uo.record_outcomes([_row("e1", "unknown", "window_open")]) == 0       # unchanged
    assert uo.record_outcomes([_row("e1", "used", "not_redone")]) == 1          # window closed
    assert uo.record_outcomes([_row("e1", "redone", "re_edited")]) == 0         # final stays final
    assert uo.load_ledger()["e1"]["outcome"] == "used"
    lines = paths.state_path(uo.LEDGER_FILENAME).read_text().splitlines()
    assert len(lines) == 2 and all("judged_at" in json.loads(x) for x in lines)


def test_empty_ledger_reads_as_empty():
    assert uo.load_ledger() == {}


def test_sweep_reads_transcripts_and_dry_run_writes_nothing(tmp_path):
    t = _routed_answer(T())
    _turns(t, 3)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "sess-9.jsonl").write_text("\n".join(json.dumps(r) for r in t.recs))
    (proj / "plain.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "hello"}}))
    out = uo.sweep(days=365, root=tmp_path, persist=False)
    assert out["events"] == 1 and out["used"] == 1 and out["written"] == 0
    assert not paths.state_path(uo.LEDGER_FILENAME).exists()
    out = uo.sweep(days=365, root=tmp_path)
    assert out["written"] == 1 and uo.load_ledger()
    assert next(iter(uo.load_ledger().values()))["session_id"] == "sess-9"


@pytest.mark.parametrize("a,b,expected", [
    ("fix the bug", "fix the bug", True),
    ("fix the bug", "fix the build", False),
    (ASK, ASK + " now", True),
    (ASK, "list every breaking change in the last three releases of the changelog file please", False),
    ("", ASK, False),
])
def test_similarity(a, b, expected):
    assert uo._similar(a, b) is expected
