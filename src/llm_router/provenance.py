"""RED2-02 — a number you could not compute is not zero.

``get_savings_summary()`` returned ``{"cost_saved_usd": 0.0, ...}`` from two
completely different situations: the user genuinely saved nothing, and the query
failed. Those rendered identically. Every dashboard, digest and session-end
banner downstream then reported "$0.00 saved" with total confidence, and a
broken telemetry path was indistinguishable from an honest quiet week.

Zero is a measurement. Unknown is the absence of one. Collapsing them is not a
rounding error, it is reporting a fact you do not have — and it fails in the
direction that looks harmless, which is why it survived.

``Measured`` keeps the two apart at the TYPE level, so a caller has to decide
what to do rather than being handed a number that silently lies. It also carries
the provenance tag WP-05 requires on every displayed figure: a user shown "$4.12
saved" deserves to know whether that was measured, estimated, or guessed.

Arithmetic is deliberately explicit. There is no ``__add__``: summing an unknown
into a total is precisely the mistake this type exists to prevent, and making it
convenient would reintroduce it. Use :func:`total`, which propagates unknown
rather than swallowing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

#: measured  — computed from observed data.
#: estimated — derived from a model, a calibration, or a stated assumption.
#: unknown   — could not be determined. NOT zero.
Provenance = Literal["measured", "estimated", "unknown"]


@dataclass(frozen=True)
class Measured:
    """A number plus how we came to know it. ``value is None`` means unknown."""

    value: float | None
    provenance: Provenance = "measured"
    #: Optional short note for the unknown case — "usage DB unreadable" tells a
    #: user something; a bare "unknown" invites them to assume zero anyway.
    detail: str = ""

    # ── constructors ─────────────────────────────────────────────────────────

    @classmethod
    def measured(cls, value: float) -> "Measured":
        return cls(float(value), "measured")

    @classmethod
    def estimated(cls, value: float, detail: str = "") -> "Measured":
        return cls(float(value), "estimated", detail)

    @classmethod
    def unknown(cls, detail: str = "") -> "Measured":
        return cls(None, "unknown", detail)

    # ── interrogation ────────────────────────────────────────────────────────

    @property
    def known(self) -> bool:
        return self.value is not None

    def or_zero(self) -> float:
        """The value, treating unknown as 0.0.

        Named to be conspicuous at the call site. Legitimate for a progress bar
        or a sort key, where a missing figure has to become *something*; never
        legitimate for a number shown to a user as a result, which is the use
        that produced RED2-02. If you are reaching for this to build a total,
        use :func:`total` instead.
        """
        return 0.0 if self.value is None else self.value

    # ── display ──────────────────────────────────────────────────────────────

    def render(self, *, unit: str = "$", places: int = 4) -> str:
        """User-facing text, tagged. Unknown never renders as a number."""
        if self.value is None:
            return f"unknown{f' ({self.detail})' if self.detail else ''}"
        body = f"{unit}{self.value:.{places}f}" if unit else f"{self.value:.{places}f}"
        return body if self.provenance == "measured" else f"~{body} (estimated)"

    def __str__(self) -> str:  # pragma: no cover — convenience only
        return self.render()


def total(values: Iterable[Measured]) -> Measured:
    """Sum, propagating unknown instead of absorbing it.

    If any input is unknown the total is unknown, because it is: a sum missing
    one of its terms is not a smaller sum, it is an unanswered question. The
    provenance degrades to ``estimated`` if any term was estimated — a total is
    only as measured as its least-measured part.
    """
    items = list(values)
    if not items:
        return Measured.measured(0.0)
    unknowns = [m for m in items if not m.known]
    if unknowns:
        detail = unknowns[0].detail or f"{len(unknowns)} of {len(items)} terms unknown"
        return Measured.unknown(detail)
    provenance: Provenance = (
        "estimated" if any(m.provenance == "estimated" for m in items) else "measured"
    )
    return Measured(sum(m.value or 0.0 for m in items), provenance)
