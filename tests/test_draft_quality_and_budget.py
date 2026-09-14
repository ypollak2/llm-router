"""Draft quality and wall-clock fixes, 2026-09-14.

Measured on 200 real prompts: of 144 drafts produced, 35 were unusable and 30 of
those were the model acting like a live chat assistant (20 asked a question back,
10 claimed an action it had not performed). Zero cited a nonexistent file, so
grounding was never the gap — situational awareness was. Separately, 72 of 166
model attempts hit the deadline and returned nothing at all, burning 50 of the
run's 99 minutes.
"""
from __future__ import annotations

import json
import time

import pytest

from llm_router.grounding import draft_is_memorable
from llm_router.hooks import direct_executor as de


class TestSystemPromptStatesTheSituation:
    """The prompt used to say "displayed directly in the user's terminal", which
    taught the model it was in a conversation where a question would be answered.
    It is generated before the user sees anything."""

    @pytest.mark.parametrize("clause", [
        "ONE SHOT", "no next turn", "NO shell", "have not run anything",
    ])
    def test_it_says_there_is_no_human_and_no_tools(self, clause):
        # Collapse whitespace: the prompt is hard-wrapped, so a clause may span
        # two lines. Asserting on the wrapping would make reflowing the text a
        # test failure.
        flat = " ".join(de.DIRECT_SYSTEM_PROMPT.lower().split())
        assert clause.lower() in flat, f"the draft model is never told {clause!r}"

    def test_it_forbids_the_two_largest_failure_modes(self):
        p = " ".join(de.DIRECT_SYSTEM_PROMPT.lower().split())
        assert "would you like me to" in p, "asks-back (20/35) is not named"
        assert "tests pass" in p, "false status claims (10/35) are not named"

    def test_it_no_longer_claims_a_live_terminal(self):
        flat = " ".join(de.DIRECT_SYSTEM_PROMPT.lower().split())
        assert "displayed directly in the user's terminal" not in flat


class TestGenerationFitsTheBudget:
    """num_predict was a flat 2048. At the measured ~16 tok/s that authorises
    ~128s of generation inside a 36s window, so the model was still writing when
    the socket was cut and everything it had written was discarded."""

    def test_a_short_budget_cannot_authorise_a_long_generation(self):
        assert de._num_predict_for(10) <= 10 * de._TOKENS_PER_SEC
        assert de._num_predict_for(36) < 2048

    def test_a_long_budget_still_caps_at_the_ceiling(self):
        assert de._num_predict_for(10_000) == de._NUM_PREDICT_CEILING

    def test_a_zero_budget_still_asks_for_something(self):
        assert de._num_predict_for(0) == de._NUM_PREDICT_FLOOR


class TestPartialDraftsAreSalvaged:
    """A non-streamed call that runs out of time returns nothing — the tokens the
    model already produced die with the socket."""

    def _fake_stream(self, chunks, stall_after=None):
        class _Resp:
            def __enter__(_): return _
            def __exit__(*_): return False
            def __iter__(_):
                for i, c in enumerate(chunks):
                    if stall_after is not None and i == stall_after:
                        time.sleep(0.25)
                    yield json.dumps(c).encode()
        return _Resp()

    def test_a_completed_stream_is_joined_and_not_labelled(self, monkeypatch):
        chunks = [{"message": {"content": "The chain is empty. "}},
                  {"message": {"content": "That is why no model is offered."}},
                  {"message": {"content": ""}, "done": True, "eval_count": 9}]
        monkeypatch.setattr(de.urllib.request, "urlopen",
                            lambda *a, **k: self._fake_stream(chunks))
        text, usage = de.call_ollama("q", "m", timeout=30)
        assert text == "The chain is empty. That is why no model is offered."
        assert "cut off" not in text
        assert usage["output_tokens"] == 9

    def test_running_out_of_time_yields_a_labelled_partial_not_none(self, monkeypatch):
        chunks = [{"message": {"content": "The chain is built in chain_builder. "}},
                  {"message": {"content": "It returns empty for research tasks. "}},
                  {"message": {"content": "Then the fallback nev"}}]
        monkeypatch.setattr(de.urllib.request, "urlopen",
                            lambda *a, **k: self._fake_stream(chunks, stall_after=2))
        text, _ = de.call_ollama("q", "m", timeout=0.2)
        assert text is not None, "a timed-out call threw away tokens it already had"
        assert "cut off" in text, "a truncated draft must say so"
        assert text.rstrip().endswith(("_", "]_")), text[-40:]
        assert "Then the fallback nev" not in text, "cut mid-word instead of mid-sentence"

    def test_too_little_to_be_worth_keeping_still_returns_none(self, monkeypatch):
        chunks = [{"message": {"content": "Th"}}]
        monkeypatch.setattr(de.urllib.request, "urlopen",
                            lambda *a, **k: self._fake_stream(chunks, stall_after=0))
        text, _ = de.call_ollama("q", "m", timeout=0.05)
        assert text is None


class TestSessionMemoryIsGatedOnAnswerShape:
    """A draft becomes the NEXT turn's context. Until now the only gate was file
    grounding, so a fabricated status became ground truth and compounded: real
    session data shows "63.2% complete (5,309/8,400)" becoming "78.5% complete
    (6,600/8,400)" one turn later, for a project that does not exist."""

    def test_a_real_answer_is_remembered(self):
        ok, _ = draft_is_memorable(
            "build_chain returns [] for research tasks, so the chain is empty and "
            "no free-tier model is offered for those prompts.")
        assert ok

    @pytest.mark.parametrize("body,why", [
        ("All tests passed and the branch was merged to main successfully.", "claims an action"),
        ("✅ Task complete. The deployment finished without errors at 14:02.", "claims an action"),
        ("Would you like me to proceed with that, or should I wait for you?", "defers"),
        ("Could you share the workflow file so I can tell you what to change?", "defers"),
        ("Sure.", "too short"),
    ])
    def test_fabrications_and_deferrals_are_not_remembered(self, body, why):
        ok, reason = draft_is_memorable(body)
        assert not ok, f"{why}: this would become the next turn's context"
        assert reason
