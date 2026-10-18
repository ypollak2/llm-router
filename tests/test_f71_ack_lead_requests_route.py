"""F71: an acknowledgement / short-report lead must not swallow a real request.

Before F71 the "ambient" paths of ``_is_coordination_task`` (an ack-style opener whose
remainder had no real-task verb, and a bare deictic with no commit/push) were
deny-lists, so "ok add a docstring to foo", "done, review the diff", "Set it to prod"
and "ran pytest, 3 failures, fix them" were classed coordination: never routed, Bash
never held. Both paths are now whole-prompt allow-lists (closed token vocabulary), so
anything with a content verb or noun falls through to normal classification.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

DEFAULT_HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"
# F71_HOOK_UNDER_TEST lets a reviewer point this file at an older hook copy to see the
# negatives fail there; unset, it tests the shipped hook.
HOOK = Path(os.environ.get("F71_HOOK_UNDER_TEST", DEFAULT_HOOK))

REPORTED = [
    "ok add a docstring to foo", "great, refactor it", "done, review the diff",
    "ran pytest, 3 failures, fix them", "looks good, ship it",
    "yes please do the migration", "I deployed it, now add logging to the worker",
    "done - please add retries to the client",
    "I tried it. Generate a unit test for foo()", "ok so what happened with the build",
    "Speed it", "Set it to prod", "Proceed with the plan", "Checked it for bugs",
]
INVENTED = [
    "ok, write the release notes", "okay; fix the flaky test", "yes. Refactor the parser",
    "sure - rename foo to bar", "cool, list the open PRs", "nice! summarize the diff",
    "thanks, now translate the readme", "done: update the changelog",
    "ok, list it", "yes, start it", "ok start the server", "great, document it",
    "perfect, optimize it", "yep, delete the old branch", "done, merge the PR",
    "alright, explain it", "ok then implement the cache", "good, test it",
    "right, migrate the schema", "ok, check it for bugs", "Fix it", "Document it",
    "Ship it", "Rename it to bar", "Delete it", "Install it", "Deploy the worker",
    "I ran it, now summarize the output", "we merged it; draft the notes",
    "ok add retries", "so build the image", "yes, generate the report",
]
MUST_ROUTE = REPORTED + INVENTED

STAYS_COORDINATION = [
    "ok", "thanks", "yes, go ahead", "sure, go ahead and proceed", "ok, thanks a lot",
    "done", "Yeah, I reconnected the mcp", "I merged it into main", "copied the hooks",
    "ok, do it", "continue", "keep going", "ok, what's on the list?",
    "so, what's the score?", "yes, do the first two and skip the rest",
    "can you check on it?", "why is it failing again?", "use this plan and get started",
    "just go with your recommendation", "why weren't those drafts used?",
    "are you sure you fixed it? it happens again [Image #6]",
]


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("_f71_hook_under_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("prompt", MUST_ROUTE)
def test_request_behind_ack_or_report_is_not_coordination(hook, prompt):
    assert not hook._is_coordination_task(prompt)


@pytest.mark.parametrize("prompt", STAYS_COORDINATION)
def test_genuine_coordination_stays_coordination(hook, prompt):
    assert hook._is_coordination_task(prompt)


def test_both_lists_are_populated():
    # an empty parametrize set would pass everything
    assert len(MUST_ROUTE) >= 29 and len(STAYS_COORDINATION) >= 15
