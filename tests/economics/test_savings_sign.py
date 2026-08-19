"""WP-04 (immutable asset) — savings keep their sign, all the way to the screen.

AUD-06 (P0). Routing can cost MORE than the baseline: routing overhead, a failed
cheap attempt that escalates, or a paid external provider on a prompt Claude
would have handled inside the subscription. When that happens the honest figure
is negative.

The compute layer already knows this. ``cost.calc_savings`` returns negatives,
``contract_gates.compute_receipt`` asserts them, and the bench-savings gate fails
on them. The DISPLAY layer independently clamped with ``max(0.0, ...)`` at three
sites in the session-end hook, so the one surface users actually read showed
"0% saved" for a session that lost money -- and a test,
``test_savings_never_negative_in_display``, asserted that clamp as the intended
contract.

That is the inverse of every other defect in this audit. Not a missing gate: a
gate pointed the wrong way. A user overspending sees the same "0% saved" as a
user who broke exactly even, and no surface anywhere tells them apart.

This asset was declared in the remediation plan as WP-04's pass condition and was
never written. It is written now, and WP-04 re-scored against it.

DO NOT EDIT — immutable test asset for WP-04.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src" / "llm_router"


@pytest.fixture
def hook():
    spec = importlib.util.spec_from_file_location(
        "_session_end_sign", _SRC / "hooks" / "session-end.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _overspending_tools() -> dict[str, dict]:
    """A session that cost MORE than the Opus baseline it is measured against.

    100 in / 50 out at $5/$25 per M is a baseline of about $0.00175. Spending
    $10.00 to route it is an unambiguous, no-rounding-argument loss.
    """
    return {
        "query": {
            "count": 1, "in": 100, "out": 50, "cost": 10.0,
            "models": {"o3": 1},
        },
    }


def _profitable_tools() -> dict[str, dict]:
    return {
        "query": {
            "count": 10, "in": 500_000, "out": 100_000, "cost": 0.01,
            "models": {"ollama/qwen3-coder:30b": 10},
        },
    }


# ── The sign survives to the screen ───────────────────────────────────────────

def test_overspend_is_not_rendered_as_zero(hook):
    """The exact defect: a losing session must not read as a break-even one."""
    rendered = _strip_ansi("\n".join(
        hook._format_routing_section(_overspending_tools(), subscription=False)
    ))
    headline = rendered.splitlines()[0]

    assert "0% saved" not in headline, (
        "overspend rendered as '0% saved' -- the clamp is back:\n" + headline
    )


def test_overspend_is_labelled_as_overspend(hook):
    """Suppressing the false zero is not enough; the loss must be legible."""
    rendered = _strip_ansi("\n".join(
        hook._format_routing_section(_overspending_tools(), subscription=False)
    )).lower()

    assert "overspent" in rendered or "-" in rendered.split("saved")[0], rendered


def test_total_saved_keeps_its_sign(hook):
    """_total_saved is consumed by other surfaces; clamping here launders the
    loss before any caller can see it."""
    assert hook._total_saved(_overspending_tools()) < 0.0


def test_profitable_session_still_reports_a_positive_figure(hook):
    """Removing the clamp must not invert or break the ordinary case."""
    assert hook._total_saved(_profitable_tools()) > 0.0
    rendered = _strip_ansi("\n".join(
        hook._format_routing_section(_profitable_tools(), subscription=False)
    ))
    assert "% saved" in rendered


def test_break_even_is_distinguishable_from_overspend(hook):
    """Zero and negative must not collapse to the same rendering -- that
    collapse IS the defect."""
    break_even = {
        "query": {"count": 1, "in": 100, "out": 50, "cost": 0.00175,
                  "models": {"o3": 1}},
    }
    a = _strip_ansi("\n".join(hook._format_routing_section(break_even, subscription=False)))
    b = _strip_ansi("\n".join(hook._format_routing_section(_overspending_tools(), subscription=False)))
    assert a.splitlines()[0] != b.splitlines()[0]


def test_no_max_zero_clamp_remains_in_the_savings_paths(hook):
    """Guards reintroduction: the clamp was three independent sites, so fixing
    one and leaving two would look fixed on the panel under test."""
    source = (_SRC / "hooks" / "session-end.py").read_text()
    offenders = [
        f"{i}: {line.strip()}"
        for i, line in enumerate(source.splitlines(), 1)
        if "max(0.0," in line and ("saved" in line or "baseline" in line or "base -" in line)
    ]
    assert not offenders, "savings clamp reintroduced:\n" + "\n".join(offenders)


# ── The compute layer must stay honest too ────────────────────────────────────

def test_calc_savings_still_returns_negative_on_overspend():
    """Pins the layer that was already correct, so a future "consistency" pass
    cannot resolve the display/compute disagreement in the wrong direction."""
    from llm_router.cost import calc_savings

    gross, _ = calc_savings(
        "o3", tokens_used=0, input_tokens=100, output_tokens=50,
        task_type="query", complexity="simple", routing_overhead_usd=10.0,
    )
    assert gross < 0.0, gross
