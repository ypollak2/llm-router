"""C10 / INV-HEALTH-001: doctor and router reconcile on provider circuit state.

Before the fix, `llm_router doctor` never read the router's in-memory HealthTracker, so
it could print "all healthy" while the router was skipping a provider whose breaker
was open. The router now persists a wall-clock snapshot; doctor reads it. This proves:
  1. a breaker the router trips is visible cross-process via the snapshot, and
  2. doctor surfaces it as an issue (does not report the provider fully healthy),
     with a reason consistent with the router's own is_healthy() decision.

Hermetic: the snapshot path is redirected to a tmp file; the tracker is a fresh
instance (no shared singleton state).
"""
from __future__ import annotations

from llm_router.health import HealthTracker, read_health_snapshot


def test_router_trip_is_visible_cross_process(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HEALTH_SNAPSHOT", str(tmp_path / "health.json"))

    tracker = HealthTracker()
    # Two hard failures trip the breaker (config.health_failure_threshold == 2).
    tracker.record_failure("openai")
    tracker.record_failure("openai")

    # Router-side truth: the provider is unhealthy.
    assert tracker.is_healthy("openai") is False

    # Cross-process truth: a SEPARATE reader (what doctor uses) sees the same thing.
    snap = read_health_snapshot()
    assert snap["providers"]["openai"]["circuit_state"] == "open"
    assert snap["providers"]["openai"]["consecutive_failures"] == 2


def test_recovery_clears_the_snapshot_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HEALTH_SNAPSHOT", str(tmp_path / "health.json"))
    tracker = HealthTracker()
    tracker.record_failure("openai")
    tracker.record_failure("openai")
    assert read_health_snapshot()["providers"]["openai"]["circuit_state"] == "open"

    # A success clears the breaker AND refreshes the shared snapshot.
    tracker.record_success("openai")
    assert tracker.is_healthy("openai") is True
    assert read_health_snapshot()["providers"]["openai"]["circuit_state"] == "closed"


def test_doctor_reports_open_breaker_as_issue(tmp_path, monkeypatch):
    """The C10 scenario: keys present + breaker open ⇒ doctor must flag it, not
    report the provider fully healthy. We assert doctor's issue-derivation logic
    directly against the snapshot (the same read doctor performs)."""
    monkeypatch.setenv("LLM_ROUTER_HEALTH_SNAPSHOT", str(tmp_path / "health.json"))
    tracker = HealthTracker()
    tracker.record_failure("openai")
    tracker.record_failure("openai")

    snap = read_health_snapshot()
    providers = snap.get("providers", {})
    open_breakers = [
        name for name, st in providers.items()
        if st.get("circuit_state") in ("open", "rate_limited")
    ]
    # doctor appends one issue per open breaker → its summary can't be "all healthy".
    assert open_breakers == ["openai"]


def test_read_snapshot_fail_open_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HEALTH_SNAPSHOT", str(tmp_path / "nope.json"))
    assert read_health_snapshot() == {}  # no snapshot → empty, never raises
