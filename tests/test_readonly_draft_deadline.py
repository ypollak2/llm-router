"""NS3: the read-only Q&A draft loop must not be handed the ENTIRE hook
deadline, because `execute_chain` (the text-chain fallback) runs right after
it inside the SAME hook invocation, on the SAME deadline.

Regression for 2026-09-27: `~/.llm-router/direct_samples.jsonl` held 20/20
samples at elapsed_s ~= 54-55s, all `timed_out: true`. `auto-route-debug.log`
on the same machine showed why: the read-only draft loop (`_execute_agent`
called with `deadline_s=_hook_deadline()`) sometimes burns the WHOLE hook
budget and returns nothing ("READ-ONLY DRAFT LOOP: nothing, text chain next"),
at which point `_execute_chain` — ALSO called with `deadline_s=_hook_deadline()`
— has zero seconds left, so every model in it logs "out of hook budget before
the call" without a single HTTP request. `_readonly_draft_deadline()` reserves
one fallback's worth of time (`_FALLBACK_RESERVE_S`) so the text chain always
gets a real attempt.

Loading auto-route.py is awkward because of the hyphen in the filename; uses
the same importlib.util.spec_from_file_location pattern as
tests/test_pressure_override_keys.py.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest


def _load_auto_route():
    cached = sys.modules.get("auto_route_under_test_readonly_deadline")
    if cached is not None:
        return cached
    path = (
        Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "auto-route.py"
    )
    spec = importlib.util.spec_from_file_location(
        "auto_route_under_test_readonly_deadline", path
    )
    assert spec and spec.loader, f"Could not load spec for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_under_test_readonly_deadline"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def auto_route():
    return _load_auto_route()


def test_readonly_draft_deadline_reserves_room_for_the_text_chain(auto_route, monkeypatch):
    # Freeze the hook's own budget to a known, small value so the math is exact.
    monkeypatch.setattr(auto_route, "_HOOK_STARTED_AT", time.monotonic())
    monkeypatch.setenv("LLM_ROUTER_HOOK_BUDGET_S", "55")

    hook_deadline = auto_route._hook_deadline()
    draft_deadline = auto_route._readonly_draft_deadline()

    assert draft_deadline < hook_deadline, (
        "the read-only draft loop got the exact same deadline as the hook — "
        "nothing is reserved for the text-chain fallback that runs after it"
    )
    reserved = hook_deadline - draft_deadline
    assert reserved >= 17.5, (
        f"only {reserved:.1f}s reserved for the text chain — needs a full "
        "fallback's worth (_FALLBACK_RESERVE_S, ~18s) or the first ollama "
        "model in the text chain can still be skipped as 'out of hook budget'"
    )


def test_readonly_draft_deadline_never_goes_negative_on_an_already_late_hook(auto_route, monkeypatch):
    # Simulate a hook invocation where classification etc. already ate almost
    # the whole budget before the draft loop is even reached.
    monkeypatch.setattr(auto_route, "_HOOK_STARTED_AT", time.monotonic() - 54.0)
    monkeypatch.setenv("LLM_ROUTER_HOOK_BUDGET_S", "55")

    draft_deadline = auto_route._readonly_draft_deadline()
    assert draft_deadline > time.monotonic(), (
        "the draft loop's deadline must stay in the future even when the "
        "reserve would otherwise push it into the past"
    )
