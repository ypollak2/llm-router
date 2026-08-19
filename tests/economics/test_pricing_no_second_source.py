"""INV-COST-004 — there is exactly one price for a model, everywhere.

Companion to ``test_pricing_single_source.py``, which pins the *values* in
``llm_router.pricing``. This file asserts the property that module cannot assert
about itself: that nothing else in the tree carries a second opinion.

Why both are needed. The audit's finding was never "the Opus rate is wrong" —
it was "the Opus rate is wrong in three of the five places that hold it".
A test that reads ``pricing.py`` and checks $5/$25 passes just as happily when
four other tables still say $15/$75. The three checks here are the ones that
would have failed at ``c2c2882``:

* every module that consumes a rate *imports* the canonical one (AST, not grep)
* every table that mentions a model agrees with every other table (runtime)
* no module outside ``pricing.py`` defines a rate table (the CI lint, run here
  too, so the guarantee does not depend on anyone remembering to wire up CI)
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from llm_router import benchmarks, calibration, cost, pricing, types
from llm_router.hooks import savings_logger

SRC = Path(__file__).resolve().parents[2] / "src"
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

#: Modules that used to hold their own price table and must now import the
#: canonical one. Each entry is a module that WP-03 migrated; a module dropping
#: off this list means someone removed the import, which is how the second
#: source comes back.
PRICE_CONSUMING_MODULES = [
    "llm_router/cost.py",
    "llm_router/types.py",
    "llm_router/calibration.py",
    "llm_router/benchmarks.py",
    "llm_router/model_registry.py",
    "llm_router/tools/text.py",
    "llm_router/hooks/savings_logger.py",
    "llm_router/hooks/cc-usage-track.py",
    "llm_router/hooks/usage-refresh.py",
]


def _imports_canonical_pricing(path: Path) -> bool:
    """True when ``path`` imports ``llm_router.pricing`` at any scope.

    AST rather than a substring search: the string "pricing" appears in
    docstrings, comments and unrelated identifiers all over this codebase, and
    a grep-based version of this test would pass on a module that only *talks*
    about pricing.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in ("llm_router", "llm_router.pricing") and (
                node.module == "llm_router.pricing"
                or any(a.name == "pricing" for a in node.names)
            ):
                return True
            # Relative `from . import pricing`
            if node.level and any(a.name == "pricing" for a in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(a.name == "llm_router.pricing" for a in node.names):
                return True
    return False


@pytest.mark.parametrize("rel", PRICE_CONSUMING_MODULES)
def test_price_consuming_module_imports_the_canonical_source(rel: str) -> None:
    path = SRC / rel
    assert path.exists(), f"{rel} moved or was deleted — update this list deliberately"
    assert _imports_canonical_pricing(path), (
        f"{rel} consumes model prices but does not import llm_router.pricing. "
        f"Every rate must come from there (INV-COST-004)."
    )


def _load_script_module(name: str, rel: str):
    """Import a hyphenated hook script, which normal import syntax cannot name."""
    spec = importlib.util.spec_from_file_location(name, SRC / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _every_table_rate_for(model: str) -> dict[str, tuple[float, float]]:
    """(input, output) $/Mtok for ``model`` from every table that carries it."""
    cc_usage = _load_script_module("_cc_usage_track", "llm_router/hooks/cc-usage-track.py")
    canonical = pricing.price_for(model)
    found: dict[str, tuple[float, float]] = {
        "llm_router.pricing": (canonical.input, canonical.output),
    }

    for label, table, key in (
        ("cost.CLAUDE_RATES_PER_M", cost.CLAUDE_RATES_PER_M, "haiku"),
        ("cost.OPENAI_RATES_PER_M", cost.OPENAI_RATES_PER_M, "o3"),
        ("cost.BASELINE_PRICING", cost.BASELINE_PRICING, "haiku"),
        ("calibration", calibration._PRICING_PER_M, "claude-haiku-4-5"),
        ("calibration", calibration._PRICING_PER_M, "o3"),
    ):
        row = table.get(key)
        if row and pricing.resolve(key) == pricing.resolve(model):
            found[f"{label}[{key}]"] = (row["input"], row["output"])

    for provider, name in savings_logger._PRICING_PER_MTOK:
        if name != "*" and pricing.resolve(name) == pricing.resolve(model):
            found[f"savings_logger[{provider}/{name}]"] = savings_logger._PRICING_PER_MTOK[
                (provider, name)
            ]

    for name, (in_per_token, out_per_token) in cc_usage._PRICES.items():
        if pricing.resolve(name) == pricing.resolve(model):
            found[f"cc-usage-track[{name}]"] = (
                in_per_token * 1_000_000,
                out_per_token * 1_000_000,
            )

    return found


@pytest.mark.parametrize("model", ["haiku", "o3"])
def test_every_table_agrees_on_the_same_model(model: str) -> None:
    """Haiku and o3 are the two the audit caught disagreeing with themselves.

    Haiku was carried at 0.80, 0.25 and 1.00 across four tables — a 3.2x spread
    for one model — and o3 at $15/$60 in three tables while a fourth had already
    corrected it to $2/$8.
    """
    rates = _every_table_rate_for(model)
    assert len(rates) > 1, f"expected {model} in several tables; found {list(rates)}"
    distinct = {(round(i, 9), round(o, 9)) for i, o in rates.values()}
    assert len(distinct) == 1, (
        f"{model} has {len(distinct)} different prices across the tree:\n"
        + "\n".join(f"  {k:44s} {v}" for k, v in sorted(rates.items()))
    )


def test_blended_and_per_1k_views_agree_with_the_canonical_rate() -> None:
    """The derived views must not become a third opinion of their own."""
    for family in ("haiku", "sonnet", "opus"):
        price = pricing.price_for(family)
        assert types.MODEL_COST_PER_1K[family] == pytest.approx(
            (price.input + price.output) / 2 / 1000
        )
    opus = pricing.price_for("opus")
    assert benchmarks._MODEL_COST_PER_1K["anthropic/claude-opus-5"] == pytest.approx(
        (opus.input + opus.output) / 2 / 1000
    )


def test_no_module_outside_pricing_defines_a_rate_table() -> None:
    """Run the CI lint in-process so the guarantee survives a CI misconfiguration.

    A structural invariant enforced only by a workflow file is enforced only as
    long as nobody edits the workflow file.
    """
    spec = importlib.util.spec_from_file_location("_lint_pricing", SCRIPTS / "lint_pricing.py")
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)

    violations = []
    for path in sorted(SRC.rglob("*.py")):
        violations.extend(lint._check_file(path, path.relative_to(SRC).as_posix()))

    baseline = lint._load_baseline()
    new = [v for v in violations if lint._key(v, SRC) not in baseline]
    assert not new, "new pricing violations:\n" + "\n".join(str(v) for v in new)
