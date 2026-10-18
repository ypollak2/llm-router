"""F95: ``_fmt_cost`` never prints a signed zero ("-0.000¢"); a real loss stays signed."""
from __future__ import annotations

import pytest

from llm_router.observability.summary import _fmt_cost


@pytest.mark.parametrize("usd", [-1e-9, -4e-6, -1e-12])
def test_a_loss_below_the_display_precision_is_unsigned_zero(usd):
    out = _fmt_cost(usd)
    assert out == "0.000¢" and not out.startswith("-"), out


@pytest.mark.parametrize("usd, shown", [
    (-2e-5, "-0.002\u00a2"), (-0.0005, "-0.050\u00a2"), (-0.5, "-$0.5000"), (-12.345, "-$12.35"),
])
def test_a_real_loss_stays_signed(usd, shown):
    assert _fmt_cost(usd) == shown


def test_positive_and_zero_are_unchanged():
    assert _fmt_cost(0.0) == "$0.00" and _fmt_cost(-0.0) == "$0.00"
    assert _fmt_cost(5e-6) == "0.001¢" and _fmt_cost(2.0) == "$2.00"
