"""RED2-02 / RED8-05 (WP-05) — a number you could not compute is not zero.

``get_savings_summary()`` returned ``{"cost_saved_usd": 0.0, ...}`` from two
completely different situations: the user genuinely saved nothing, and the query
failed. Byte-identical output. Every dashboard, digest and session-end banner
then reported "$0.00 saved" with full confidence, so a broken telemetry path was
indistinguishable from an honest quiet week.

It fails in the direction that looks harmless, which is why it survived: nobody
investigates a zero.
"""

from __future__ import annotations

import pytest

from llm_router.provenance import Measured, total


# ── the type-level distinction ───────────────────────────────────────────────


def test_unknown_is_not_equal_to_zero():
    """The acceptance criterion, stated directly."""
    assert Measured.unknown() != Measured.measured(0.0)
    assert Measured.unknown().value is None
    assert Measured.measured(0.0).value == 0.0


def test_unknown_never_renders_as_a_number():
    """Whatever else happens, a user must not read a figure we do not have."""
    rendered = Measured.unknown("usage DB unreadable").render()
    assert "unknown" in rendered
    assert "0.00" not in rendered
    assert "usage DB unreadable" in rendered


def test_estimated_is_marked_as_estimated():
    """WP-05: every displayed number carries measured | estimated | unknown."""
    assert Measured.measured(1.5).render() == "$1.5000"
    assert "estimated" in Measured.estimated(1.5).render()


def test_a_total_containing_an_unknown_is_unknown():
    """A sum missing one of its terms is not a smaller sum.

    This is the aggregation form of the same defect: treat one unknown as zero
    and the total is quietly wrong and confidently displayed.
    """
    result = total([Measured.measured(1.0), Measured.unknown("db error")])
    assert not result.known
    assert "db error" in result.detail


def test_a_total_of_measured_values_stays_measured():
    result = total([Measured.measured(1.0), Measured.measured(2.5)])
    assert result.known and result.value == pytest.approx(3.5)
    assert result.provenance == "measured"


def test_a_total_degrades_to_estimated_if_any_term_was_estimated():
    """A total is only as measured as its least-measured part."""
    result = total([Measured.measured(1.0), Measured.estimated(2.0)])
    assert result.provenance == "estimated"


def test_there_is_no_implicit_addition():
    """Making unknown convenient to sum would reintroduce the defect."""
    with pytest.raises(TypeError):
        Measured.measured(1.0) + Measured.unknown()  # type: ignore[operator]


def test_or_zero_exists_but_is_explicit():
    """Coercion is allowed where it is honest — and has to be spelled out."""
    assert Measured.unknown().or_zero() == 0.0
    assert Measured.measured(2.0).or_zero() == 2.0


# ── the reporting path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_query_is_distinguishable_from_a_genuine_zero(tmp_path, monkeypatch):
    """The two branches must not produce the same answer.

    Both used to return the identical dict. This asserts the pair differ, rather
    than asserting one specific shape, because the defect was about them being
    the SAME — a test pinning either one alone would have passed before.
    """
    import llm_router.cost as cost

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "usage.db"))

    genuine_zero = await cost.get_savings_summary("today")

    class _Boom:
        async def execute(self, *_a, **_kw):
            raise RuntimeError("disk I/O error")

        async def close(self):
            return None

    async def _broken_db():
        return _Boom()

    monkeypatch.setattr(cost, "_get_db", _broken_db)
    failed = await cost.get_savings_summary("today")

    assert genuine_zero["provenance"] == "measured"
    assert failed["provenance"] == "unknown"
    assert genuine_zero != failed, (
        "a failed savings query is indistinguishable from a zero-saving period"
    )
    assert failed["saved"] != genuine_zero["saved"]
    assert not failed["saved"].known


# ── quota is not cash ────────────────────────────────────────────────────────


def test_the_weekly_quota_value_is_not_a_hardcoded_fifty(monkeypatch):
    """RED8-05: the $50 was justified by the RETIRED $15/$75 Opus rate.

    The figure survives scrutiny but not for the stated reason. Quota value is
    anchored to what the subscription COSTS, not to a token rate — under the old
    reasoning it would have had to fall by two thirds when Opus repriced, and
    nobody would have known to change it.
    """
    from llm_router.quota_savings import _default_weekly_quota_usd

    monkeypatch.delenv("LLM_ROUTER_WEEKLY_QUOTA_USD", raising=False)
    monkeypatch.delenv("LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH", raising=False)
    default = _default_weekly_quota_usd()
    assert default == pytest.approx(200.0 / 4.345, abs=0.01)


def test_the_quota_value_follows_the_users_plan(monkeypatch):
    """A hardcoded constant could not be corrected without editing the source."""
    from llm_router.quota_savings import _default_weekly_quota_usd

    monkeypatch.delenv("LLM_ROUTER_WEEKLY_QUOTA_USD", raising=False)
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH", "100")
    assert _default_weekly_quota_usd() == pytest.approx(100.0 / 4.345, abs=0.01)

    monkeypatch.setenv("LLM_ROUTER_WEEKLY_QUOTA_USD", "12.5")
    assert _default_weekly_quota_usd() == pytest.approx(12.5)


def test_quota_savings_are_labelled_as_quota_not_cash():
    """WP-05: a subscription user has been handed headroom, not money.

    The two must never be summed, so the module that produces quota figures has
    to say what they are.
    """
    import llm_router.quota_savings as qs

    doc = (qs.__doc__ or "") + (qs._default_weekly_quota_usd.__doc__ or "")
    assert "quota" in doc.lower()
    assert "not cash" in doc.lower() or "not be added" in doc.lower()
