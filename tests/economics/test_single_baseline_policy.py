"""WP-05: exactly one savings-baseline policy, and every surface obeys it.

Three policies coexisted, two of them added specifically to override the other:

  A  cost._get_baseline_for_task   research/complex -> opus, query -> haiku,
                                   else sonnet. Justified in-comment as stopping
                                   savings being "overstated".
  B  savings_logger._BASELINE_MODEL_BY_COMPLEXITY
                                   flat claude-opus-4-8, justified in-comment by
                                   the tiered baseline "not reflecting how the
                                   user actually works" -- i.e. the direct
                                   negation of A.
  C  session-end._host_baseline / dashboard_data._BASELINE_MODEL
                                   flat opus list rate.

For a QUERY task A credits haiku ($1/$5) and B credits opus ($5/$25): a 5x
difference in reported savings for the identical call, decided by which surface
happened to render it. That is the "two disagreeing figures" WP-05 names.

Resolved in favour of the flat counterfactual: what the user would have spent
WITHOUT LLM Router is their subscription's top model, not a cheaper Claude they
would have had to select by hand. These tests pin that there is one policy
symbol and that no surface carries a private copy of it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from llm_router import pricing

_SRC = Path(__file__).resolve().parent.parent.parent / "src" / "llm_router"


def test_policy_symbol_exists_and_is_opus_5():
    assert pricing.savings_baseline_model() == "claude-opus-5"
    in_rate, out_rate = pricing.savings_baseline_rates()
    assert (in_rate, out_rate) == (5.0, 25.0)


def test_env_override_still_resolves_through_the_policy(monkeypatch):
    """The override is retained, but it must go through the one policy function
    rather than each surface reading the env var for itself.

    Asserts against the registry rather than a price literal. The previous
    version hardcoded (2.0, 10.0) -- Sonnet 5's introductory rate, which
    pricing.py itself documents as running only "through 2026-08-31". The
    rollover to (3.0, 15.0) fired on schedule and broke this test on main
    overnight, while the code it guards was behaving exactly as designed.

    A test that encodes a price with an expiry date is a scheduled failure. What
    this test is actually for is that the override resolves THROUGH the policy;
    the number is incidental, so it now comes from the same source the policy
    reads.
    """
    monkeypatch.setenv("LLM_ROUTER_SAVINGS_BASELINE", "claude-sonnet-5")
    assert pricing.savings_baseline_model() == "claude-sonnet-5"

    expected = (
        pricing.input_rate("claude-sonnet-5"),
        pricing.output_rate("claude-sonnet-5"),
    )
    assert pricing.savings_baseline_rates() == expected
    # And it is genuinely resolving the override, not the default baseline.
    assert pricing.savings_baseline_rates() != (5.0, 25.0)


def test_unknown_override_falls_back_rather_than_pricing_at_zero(monkeypatch):
    """A typo'd baseline must not silently resolve to a 0.0 rate -- that would
    render every routed call as saving nothing, the RED2-02 failure shape."""
    monkeypatch.setenv("LLM_ROUTER_SAVINGS_BASELINE", "not-a-real-model")
    assert pricing.savings_baseline_model() == "claude-opus-5"
    assert pricing.savings_baseline_rates() == (5.0, 25.0)


def test_no_claude_model_costs_nothing():
    """A known Claude model must never price at zero for non-zero tokens.

    Found by the WP-14 mutation sample (M2): forcing the rate lookup in
    _claude_cost to miss made it return 0.0 for EVERY Claude model, and the full
    suite stayed green. _claude_cost is the actual-cost side of every savings
    subtraction, so a zero there makes savings = baseline - 0 = baseline: every
    surface OVERSTATES savings by the full actual cost and labels it "measured".

    The existing tests pin specific inputs to specific outputs, which cannot
    catch the lookup going dead. This gates the contract instead: not "this model
    costs 0.015" but "no Claude model costs nothing".
    """
    from llm_router.cost import CLAUDE_RATES_PER_M, _claude_cost

    zero_priced = [
        model for model in CLAUDE_RATES_PER_M
        if _claude_cost(model, 1_000, 1_000) <= 0.0
    ]
    assert not zero_priced, f"Claude models pricing at zero: {zero_priced}"

    # And via the canonical ids, which is the path the baseline policy uses.
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
        assert _claude_cost(model, 1_000, 1_000) > 0.0, model


def test_cost_module_delegates_to_the_policy():
    from llm_router import cost

    assert cost.BASELINE_MODEL_FOR_SAVINGS == pricing.savings_baseline_model()
    assert (cost._HOST_INPUT_PER_M, cost._HOST_OUTPUT_PER_M) == pricing.savings_baseline_rates()


def test_savings_logger_has_no_private_baseline():
    """Policy B's per-complexity table must be gone, not merely re-pointed --
    a table keyed by complexity is the shape that let A and B diverge."""
    from llm_router.hooks import savings_logger

    assert not hasattr(savings_logger, "_BASELINE_MODEL_BY_COMPLEXITY")
    for complexity in ("simple", "moderate", "complex"):
        cost_at = savings_logger._baseline_cost(complexity, 1_000_000, 0)
        assert cost_at == pytest.approx(5.0), complexity


def test_dashboard_delegates_to_the_policy():
    from llm_router import dashboard_data

    assert dashboard_data._BASELINE_MODEL == pricing.savings_baseline_model()


def test_session_end_hook_delegates_to_the_policy():
    spec = importlib.util.spec_from_file_location(
        "_session_end_wp05", _SRC / "hooks" / "session-end.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert (mod.HOST_INPUT_PER_M, mod.HOST_OUTPUT_PER_M) == pricing.savings_baseline_rates()
    # 1M input tokens against the one baseline.
    assert mod._host_baseline(1_000_000, 0) == pytest.approx(5.0)


def test_tiered_policy_is_gone():
    """Policy A's entry points must not survive as dead code -- a second
    baseline function left importable is a second policy waiting to be called."""
    from llm_router import cost

    assert not hasattr(cost, "_get_baseline_for_task")
    assert not hasattr(cost, "_get_baseline_model")


# Baselines that are deliberately NOT the savings baseline. Each compares
# something other than "what would this have cost without routing", so folding
# them into the savings policy would be wrong, not tidy. Listed explicitly so a
# NEW baseline literal anywhere still trips the guard below.
_NON_SAVINGS_BASELINES = {
    # Quality comparator: which completion the router measures its own output
    # against. Never used in a cost subtraction.
    ("router.py", "_BASELINE_COMPLETION_MODEL"),
    # Benchmark comparator for `llm_router test`; a fixed reference model so scores
    # are comparable across runs.
    ("commands/test.py", "BASELINE"),
}


def _baseline_assignments() -> list[tuple[str, str, int, str]]:
    """Every `<NAME containing BASELINE> = "<a claude model>"` under src/llm_router.

    Assignment-only (not docstrings or prose) so the guard flags real bindings,
    and name-scoped so it cannot be satisfied by renaming a variable.
    """
    import ast

    found: list[tuple[str, str, int, str]] = []
    for path in _SRC.rglob("*.py"):
        if path.name == "pricing.py":
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        rel = str(path.relative_to(_SRC))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not any("BASELINE" in n.upper() for n in names):
                continue
            literals = [
                v.value for v in ast.walk(node.value)
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
                and v.value.lower().startswith(("claude-", "anthropic/claude-"))
            ]
            for name in names:
                for lit in literals:
                    found.append((rel, name, node.lineno, lit))
    return found


def test_no_surface_carries_a_private_savings_baseline():
    """Guards re-divergence: outside pricing.py, no module may bind a Claude
    model as a savings baseline of its own."""
    offenders = [
        f"{rel}:{lineno}: {name} = {lit!r}"
        for rel, name, lineno, lit in _baseline_assignments()
        if (rel, name) not in _NON_SAVINGS_BASELINES
    ]
    assert not offenders, "private savings-baseline literals:\n" + "\n".join(offenders)


def test_the_non_savings_allowlist_is_not_stale():
    """If an allowlisted baseline is deleted or renamed, drop it from the list --
    a stale exemption silently widens the guard above."""
    live = {(rel, name) for rel, name, _, _ in _baseline_assignments()}
    stale = _NON_SAVINGS_BASELINES - live
    assert not stale, f"allowlist entries no longer present: {stale}"
