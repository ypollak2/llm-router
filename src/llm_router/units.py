"""The units table (P35-1, D-220): how a ledger figure is printed, by what it is measured in.

| unit         | quantity     | prints            | meaning                                          |
|--------------|--------------|-------------------|--------------------------------------------------|
| subscription | pct          | ``37.5% of window`` | share of the quota window                        |
| subscription | work_hours   | ``~2.5 work-hours`` | quota-% saved / median pp per active hour        |
| api          | usd          | ``$1.50``           | money actually metered                           |
| estimate     | usd          | ``~$1.50``          | a counterfactual dollar figure                   |

Rules, enforced here and nowhere else:

* An estimate is ALWAYS marked ``~``: unit ``estimate``, a figure whose ``kind`` is ``estimated``,
  and every ``work_hours`` value. A measured figure is never marked ``~``.
* ``with_n`` prints the n behind the figure and its window; an estimate that has a second basis
  (the rate behind ``work_hours``) prints both n's: ``(n=12, rate n=10, 7d)``.
* No plan name and no price lives here (D-205): plan and price come from the user's config or
  discovery. This module only formats numbers it is handed.
* A combination the table does not name raises ``ValueError``: it is a caller bug, not a figure.
"""
from __future__ import annotations

UNITS = {
    "subscription": ("pct", "work_hours"),
    "api": ("usd",),
    "estimate": ("usd",),
}


def _num(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def render(value: float, n: int, window: str, *, kind: str, unit: str, quantity: str | None,
           n_basis: int | None = None, with_n: bool = False) -> str:
    """Print ``value`` in ``unit``/``quantity``; the caller has already dropped unknown figures."""
    if unit not in UNITS:
        raise ValueError(f"unknown unit {unit!r}; one of {sorted(UNITS)}")
    if quantity not in UNITS[unit]:
        raise ValueError(f"unit {unit!r} has no quantity {quantity!r}; one of {UNITS[unit]}")
    estimated = unit == "estimate" or kind == "estimated" or quantity == "work_hours"
    mark = "~" if estimated else ""
    if quantity == "pct":
        text = f"{mark}{_num(value)}% of window"
    elif quantity == "work_hours":
        text = f"{mark}{_num(value)} work-hours"
    else:
        text = f"{mark}${value:,.2f}"
    if not with_n:
        return text
    ns = f"n={n:,}" if n_basis is None else f"n={n:,}, rate n={n_basis:,}"
    return f"{text} ({ns}, {window})"
