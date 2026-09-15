"""The routing reward must not be a constant.

A2 of docs/ACTIONS_REMEDIATION_RUN.md. `telemetry.py` computes
`AVG(CASE WHEN success = 1 ...)` and every write site on the routing path passed
`success=True` unconditionally — there was no `success=False` anywhere. So the
bandit's reward `success_rate / avg_cost` collapsed to `1 / avg_cost`: it ranked by
cheapness with a constant numerator and learned nothing about quality.
`savings_logger` built its content as `getattr(result, "text", "") or ""`, so an
EMPTY response logged success too.

Measured 2026-09-14: of 144 drafts produced, 35 were unusable (20 asked the user a
question, 10 claimed an action never performed). Each of those reinforced the model
that produced it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from llm_router.grounding import draft_is_memorable, response_is_usable

SRC = Path(__file__).resolve().parent.parent / "src/llm_router"


class TestTheSignalIsNoLongerHardcoded:

    @pytest.mark.parametrize("module", ["router.py", "hooks/savings_logger.py"])
    def test_no_unconditional_success_on_the_routing_path(self, module):
        text = (SRC / module).read_text()
        assert "success=True" not in text, (
            f"{module} still logs success unconditionally, so success_rate is 1.0 "
            "by construction and the bandit reward is 1/cost"
        )

    def test_at_least_one_site_computes_it(self):
        both = (SRC / "router.py").read_text() + (SRC / "hooks/savings_logger.py").read_text()
        assert re.search(r"success=_(usable|response_is_usable)", both), (
            "nothing computes success; the column would never be written at all"
        )


class TestWhatCountsAsSuccess:

    def test_a_real_answer_succeeds(self):
        assert response_is_usable(
            "build_chain returns [] for research tasks, so the chain is empty and "
            "no free-tier model is offered.")

    @pytest.mark.parametrize("body,why", [
        ("", "an empty response used to log success"),
        ("   \n  ", "whitespace is not an answer"),
        ("All tests passed and the branch was merged to main.", "claims an action it never performed"),
        ("Would you like me to proceed with that approach instead?", "asks the user instead of answering"),
        ("Could you share the workflow file so I can advise?", "the same deferral, reworded"),
    ])
    def test_these_are_not_success(self, body, why):
        assert not response_is_usable(body), why

    def test_it_is_the_same_predicate_as_remembering(self):
        # If these drift, a draft can be good enough to reinforce a model but not
        # good enough to remember, or the reverse. One rule, two callers.
        for body in ("Would you like me to continue with the next step here?",
                     "The chain is built in chain_builder.build_chain, which returns [].",
                     "✅ Task complete, all tests passed successfully."):
            assert response_is_usable(body) == draft_is_memorable(body)[0]


class TestItFailsOpenNotClosed:

    def test_a_broken_grounding_import_does_not_kill_routing(self, monkeypatch):
        import llm_router.router as router
        monkeypatch.setitem(__import__("sys").modules, "llm_router.grounding", None)
        # Falls back to "non-empty", which is the old behaviour — degraded, not broken.
        assert router._response_is_usable("some answer text") is True
        assert router._response_is_usable("") is False
