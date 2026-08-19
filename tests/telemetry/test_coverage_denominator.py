"""WP-07 (immutable asset) — every rate must state what it failed to observe.

Finding I-1: `auto-route.py` has twelve `sys.exit(0)` sites. Six of them are
SILENT BYPASSES -- the hook exits without emitting a routing directive and
without recording that it declined to. The worst is the catch-all at the bottom
of `main()`, which swallows any unhandled exception and exits 0; even its debug
log is best-effort. A run in which 97.7% of prompts crashed out through that
path is byte-identical, in every user-visible surface, to a clean run. The
codebase documents that incident as previously indistinguishable; these tests
exist so it cannot be indistinguishable again.

The rule these pin: a rate whose denominator is unknown must render `Unknown`,
never a number. A percentage computed over an unknown denominator is not a
conservative estimate -- it is a fabricated one, and it fails in the direction
that looks healthy.

DO NOT EDIT — immutable test asset for WP-07.
"""

from __future__ import annotations

import pytest

from llm_router import coverage


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every DB-touching test asserts its resolved path is inside a tmpdir.

    LLM_ROUTER_HOME did not isolate cost._get_db() and that gap destroyed real data
    once during this audit (RED2-07). The assertion is the point, not the setenv.
    """
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    coverage.reset_cache()
    resolved = coverage.store_path()
    assert str(resolved).startswith(str(tmp_path)), (
        f"coverage store escaped the tmpdir: {resolved}"
    )
    yield
    coverage.reset_cache()


# ── The denominator rule ──────────────────────────────────────────────────────

def test_rate_with_no_observations_renders_unknown():
    snap = coverage.snapshot()
    assert snap.observed_n == 0
    assert snap.unobserved_n == 0
    assert snap.coverage_pct is None
    assert snap.render_pct() == "Unknown"


def test_rate_never_renders_a_number_it_cannot_justify():
    """A denominator of zero must not become 0%, 100%, or NaN."""
    snap = coverage.snapshot()
    rendered = snap.render_pct()
    for forbidden in ("0%", "100%", "nan", "0.0"):
        assert forbidden.lower() not in rendered.lower(), rendered


def test_observed_and_unobserved_are_both_exposed():
    coverage.record_observed("llm_query")
    coverage.record_unobserved(coverage.Reason.CLASSIFY_FAILED)
    snap = coverage.snapshot()

    assert snap.observed_n == 1
    assert snap.unobserved_n == 1
    assert snap.total_n == 2
    assert snap.coverage_pct == pytest.approx(50.0)


# ── The historical incident must be visibly different from a clean run ────────

def test_mass_bypass_is_visibly_different_from_a_clean_run():
    """The exact scenario documented as previously indistinguishable.

    97.7% of directives bypassed must NOT produce the same telemetry as a run
    where everything routed.
    """
    for _ in range(1000):
        coverage.record_observed("llm_query")
    clean = coverage.snapshot()

    coverage.reset_cache()
    coverage.clear()

    # 977 of 1000 bypassed, 23 routed.
    for _ in range(23):
        coverage.record_observed("llm_query")
    for _ in range(977):
        coverage.record_unobserved(coverage.Reason.UNHANDLED_EXCEPTION)
    incident = coverage.snapshot()

    assert clean.coverage_pct == pytest.approx(100.0)
    assert incident.coverage_pct == pytest.approx(2.3, abs=0.1)
    assert incident.render_pct() != clean.render_pct()
    assert incident.is_degraded and not clean.is_degraded


def test_degraded_threshold_is_90_percent():
    for _ in range(89):
        coverage.record_observed("llm_query")
    for _ in range(11):
        coverage.record_unobserved(coverage.Reason.CONTINUATION_BYPASS)
    assert coverage.snapshot().is_degraded

    coverage.reset_cache()
    coverage.clear()
    for _ in range(91):
        coverage.record_observed("llm_query")
    for _ in range(9):
        coverage.record_unobserved(coverage.Reason.CONTINUATION_BYPASS)
    assert not coverage.snapshot().is_degraded


# ── Every silent bypass must have a reason code ───────────────────────────────

def test_every_silent_bypass_site_has_a_distinct_reason():
    """Six silent-bypass sites in auto-route.py, six reasons. An 'other' bucket
    would let a new bypass hide inside an existing count."""
    required = {
        "EMPTY_PROMPT",
        "SELF_REFERENCE_BYPASS",
        "EXPLICIT_CLAUDE_PREFIX",
        "CONTINUATION_BYPASS",
        "CLASSIFY_FAILED",
        "UNHANDLED_EXCEPTION",
    }
    actual = {r.name for r in coverage.Reason}
    assert required <= actual, f"missing reason codes: {required - actual}"


def test_unobserved_events_are_attributed_by_reason():
    coverage.record_unobserved(coverage.Reason.UNHANDLED_EXCEPTION)
    coverage.record_unobserved(coverage.Reason.UNHANDLED_EXCEPTION)
    coverage.record_unobserved(coverage.Reason.EMPTY_PROMPT)
    snap = coverage.snapshot()

    assert snap.by_reason["UNHANDLED_EXCEPTION"] == 2
    assert snap.by_reason["EMPTY_PROMPT"] == 1


# ── Recording must never break the hook ───────────────────────────────────────

def test_recording_failure_is_swallowed_not_raised(monkeypatch):
    """A hook that raises while recording coverage would turn an observability
    feature into an outage. It must fail silently."""
    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(coverage, "_append_event", _boom)
    coverage.record_observed("llm_query")  # must not raise
    coverage.record_unobserved(coverage.Reason.EMPTY_PROMPT)  # must not raise


# ── The rule applied to a real rate metric ────────────────────────────────────

@pytest.mark.asyncio
async def test_router_efficiency_with_no_decisions_is_unknown_not_zero(tmp_path, monkeypatch):
    """An empty routing_decisions table is not a 0%-effective router.

    The previous return was `efficiency_pct: 0.0`, indistinguishable from a
    router that got every single decision wrong -- and it failed in the
    direction that looks like a real measurement.
    """
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "eff.db"))
    from llm_router.cost import get_router_efficiency

    result = await get_router_efficiency(period="all")

    assert result["efficiency_pct"] is None, result
    assert result["provenance"] == "unknown"
    assert result["efficiency_pct"] != 0.0


@pytest.mark.asyncio
async def test_router_efficiency_carries_its_denominator(tmp_path, monkeypatch):
    """A rate must ship with what it was computed over."""
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "eff2.db"))
    from llm_router.cost import get_router_efficiency

    result = await get_router_efficiency(period="all")
    assert "observed_n" in result
    assert "unobserved_n" in result


def test_unreadable_store_yields_unknown_not_zero():
    """A store that cannot be read is not a run with no traffic. RED2-02 shape."""
    coverage.store_path().write_text("{ this is not json\n")
    coverage.reset_cache()
    snap = coverage.snapshot()
    assert snap.coverage_pct is None
    assert snap.render_pct() == "Unknown"
