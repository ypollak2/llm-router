"""A draft PRODUCED is not work routed.

`DIRECT SUCCESS` records that a local model returned a draft and the hook
injected it. Claude is then told to discard that draft whenever the answer
depends on anything the draft model could not see — which, in a repo, is most
of the time. So the old routing rate counted drafts produced and reported them
as work routed.

The correction is not cosmetic: a session whose log showed successful routing
throughout drove subscription quota from 49% to 79%, because every draft in it
was discarded and Claude did the work anyway.

Usage is observable, because a relayed draft must open with the
`🎯 LLM Router routed →` line the hook asks for by name.
"""
from __future__ import annotations

import json
import time

import pytest

from llm_router.hooks import draft_usage


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))


def test_a_relayed_draft_counts_as_used():
    draft_usage.record_draft("s1", 1.0, "ollama/qwen")
    verdict, rec = draft_usage.audit(
        "s1", "🎯 LLM Router routed → ollama/qwen · query/simple · 3s\n\nThe answer.")
    assert verdict == draft_usage.USED
    assert rec["invocation_id"] == 1.0


def test_a_discarded_draft_counts_as_unused():
    draft_usage.record_draft("s1", 2.0, "ollama/qwen")
    verdict, _ = draft_usage.audit("s1", "I read the file; the answer is X.")
    assert verdict == draft_usage.UNUSED


def test_explaining_that_the_draft_was_discarded_is_not_a_relay():
    """This session produced exactly this text repeatedly. A whole-body search
    for the marker would score every one of those as a successful relay."""
    draft_usage.record_draft("s1", 3.0, "ollama/qwen")
    body = ("I discarded the draft — it claimed all tests passed.\n\n"
            "The marker it wanted was 🎯 LLM Router routed → ollama/qwen, but "
            "the draft was wrong, so I answered from the real output instead.\n"
            * 3)
    verdict, _ = draft_usage.audit("s1", body)
    assert verdict == draft_usage.UNUSED


def test_leading_blank_lines_do_not_break_the_match():
    draft_usage.record_draft("s1", 4.0, "ollama/qwen")
    verdict, _ = draft_usage.audit(
        "s1", "\n\n🎯 LLM Router routed → ollama/qwen · query · 2s\n\nAnswer.")
    assert verdict == draft_usage.USED


def test_a_marker_below_any_prose_is_not_a_relay():
    """The hook asks for the marker as the FIRST line. Anything above it means
    Claude wrote its own opening, which means it was not relaying."""
    draft_usage.record_draft("s1", 41.0, "ollama/qwen")
    verdict, _ = draft_usage.audit(
        "s1", "Here is what I found.\n\n🎯 LLM Router routed → ollama/qwen")
    assert verdict == draft_usage.UNUSED


def test_no_pending_draft_means_nothing_to_judge():
    assert draft_usage.audit("s1", "any reply at all") is None


def test_the_record_is_consumed_so_one_draft_is_judged_once():
    """Left in place, the next prompt would re-judge the same draft against a
    different reply and inflate whichever way that reply went."""
    draft_usage.record_draft("s1", 5.0, "ollama/qwen")
    assert draft_usage.audit("s1", "x") is not None
    assert draft_usage.audit("s1", "x") is None


def test_a_stale_record_is_not_judged(tmp_path):
    """An abandoned session's draft must not be scored against whatever turn
    appears hours later."""
    draft_usage.record_draft("s1", 6.0, "ollama/qwen")
    path = draft_usage._pending_path("s1")
    data = json.loads(path.read_text())
    data["at"] = time.time() - 7200
    path.write_text(json.dumps(data))
    assert draft_usage.audit("s1", "🎯 LLM Router routed → x") is None


def test_sessions_are_judged_independently():
    draft_usage.record_draft("s1", 7.0, "ollama/a")
    draft_usage.record_draft("s2", 8.0, "ollama/b")
    assert draft_usage.audit("s1", "plain answer")[0] == draft_usage.UNUSED
    assert draft_usage.audit("s2", "🎯 LLM Router routed → ollama/b")[0] == draft_usage.USED


def test_an_empty_reply_is_unused_not_a_crash():
    draft_usage.record_draft("s1", 9.0, "ollama/qwen")
    assert draft_usage.audit("s1", "")[0] == draft_usage.UNUSED


def test_a_missing_session_id_records_nothing():
    assert draft_usage.record_draft("", 1.0, "ollama/x") is False
    assert draft_usage.audit("", "anything") is None


def test_a_hostile_session_id_cannot_escape_the_directory(tmp_path):
    draft_usage.record_draft("../../etc/passwd", 1.0, "ollama/x")
    written = list((tmp_path / "pending_drafts").glob("*.json"))
    assert len(written) == 1
    assert written[0].parent == tmp_path / "pending_drafts"


def test_a_corrupt_record_is_ignored_rather_than_raising():
    draft_usage.record_draft("s1", 1.0, "ollama/x")
    draft_usage._pending_path("s1").write_text("{not json")
    assert draft_usage.audit("s1", "x") is None


def test_an_unwritable_home_never_raises(monkeypatch):
    """This runs inside the hook; a measurement that breaks the prompt is worse
    than no measurement."""
    monkeypatch.setenv("LLM_ROUTER_HOME", "/proc/nonexistent-and-unwritable")
    assert draft_usage.record_draft("s1", 1.0, "ollama/x") is False
