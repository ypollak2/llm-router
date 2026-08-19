"""Regression: CHZ-DRAFT-01 / RED2-01 — block-mode draft must never replace a
turn outside zero-Claude.

The UserPromptSubmit hook could render a routed local-model draft in "block"
mode ({"decision":"block","reason":<draft>}), which REPLACES the user's turn —
the terminal shows the stateless draft as if it were Claude's answer. That was
gated on `_is_context_dependent(prompt)`, a fixed noun list with a measured ~60%
false-negative rate (RED2-01): ordinary repo questions ("how does our scheduler
retry?") slipped through and got a fabricated answer.

Fix: `_resolve_auto_render_mode` resolves "auto" to block ONLY in zero-Claude;
otherwise advisory echo. This makes the predicate's accuracy irrelevant to the
fabrication risk. These tests assert the invariant directly and document that
the underlying false negative still exists (so the fix — not the gate — is what
protects).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"


def _load():
    spec = importlib.util.spec_from_file_location("llm_router_auto_route_draft01", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load()


def test_auto_never_blocks_outside_zero_claude():
    # The whole point: outside zero-Claude, "auto" is never block.
    assert hook._resolve_auto_render_mode("auto", zero_claude=False) == "echo"


def test_auto_blocks_only_in_zero_claude():
    assert hook._resolve_auto_render_mode("auto", zero_claude=True) == "block"


def test_explicit_modes_are_honored_unchanged():
    # Power-user overrides via LLM_ROUTER_RENDER_MODE are not touched.
    assert hook._resolve_auto_render_mode("block", zero_claude=False) == "block"
    assert hook._resolve_auto_render_mode("echo", zero_claude=True) == "echo"


def test_false_negative_prompts_still_evade_the_gate_but_are_now_safe():
    """Document that _is_context_dependent still false-negatives on these repo
    nouns (RED2-01) — proving it is the render-mode gating, not the predicate,
    that now prevents fabrication."""
    fn_prompts = [
        "how does our scheduler handle retries",
        "explain our permission model",
        "what does our queue do on overflow",
        "walk me through our algorithm",
    ]
    # At least some of these are false negatives (predicate says not-context-dependent).
    fn_count = sum(1 for p in fn_prompts if not hook._is_context_dependent(p))
    assert fn_count >= 1, "expected the documented false negatives to persist"
    # But regardless of the predicate, none of them can block outside zero-Claude:
    for _p in fn_prompts:
        assert hook._resolve_auto_render_mode("auto", zero_claude=False) == "echo"
