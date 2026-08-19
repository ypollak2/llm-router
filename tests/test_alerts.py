"""Tests for the best-effort operational alert sink (llm_router.alerts).

Covers the fail-open contract: emit_alert always logs, honours the
disable flag, POSTs to a configured webhook, and NEVER raises — a broken
alert path must not break the caller that triggered it.
"""
from __future__ import annotations

import json

import pytest

from llm_router.alerts import (
    AGENT_EMERGENCY_STOP,
    AUDIT_TAMPER,
    BUDGET_POSTGRES_FALLBACK,
    RUNAWAY_BREAKER_TRIP,
    emit_alert,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_ROUTER_ALERTS_DISABLED", raising=False)
    monkeypatch.delenv("LLM_ROUTER_ALERT_WEBHOOK", raising=False)


def test_event_constants_are_stable() -> None:
    # Dashboards / greps key on these — pin the exact strings.
    assert AUDIT_TAMPER == "audit_chain_tamper"
    assert RUNAWAY_BREAKER_TRIP == "runaway_agent_breaker_trip"
    assert AGENT_EMERGENCY_STOP == "agent_emergency_stop"
    assert BUDGET_POSTGRES_FALLBACK == "budget_postgres_fallback"


def test_emit_without_webhook_only_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {}

    def fake_urlopen(*a, **k):  # pragma: no cover - must NOT be called
        calls["hit"] = True
        raise AssertionError("urlopen should not be called without a webhook")

    monkeypatch.setattr("llm_router.alerts.urllib.request.urlopen", fake_urlopen)
    emit_alert(AUDIT_TAMPER, detail={"tamper_row": 7})
    assert "hit" not in calls


def test_disabled_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_ALERTS_DISABLED", "1")
    monkeypatch.setenv("LLM_ROUTER_ALERT_WEBHOOK", "http://example.test/hook")

    def fake_urlopen(*a, **k):  # pragma: no cover
        raise AssertionError("disabled alerts must not POST")

    monkeypatch.setattr("llm_router.alerts.urllib.request.urlopen", fake_urlopen)
    emit_alert(AGENT_EMERGENCY_STOP, detail={"session_id": "s1"})  # must be a no-op


def test_webhook_receives_json_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setenv("LLM_ROUTER_ALERT_WEBHOOK", "http://example.test/hook")
    monkeypatch.setattr("llm_router.alerts.urllib.request.urlopen", fake_urlopen)

    emit_alert(BUDGET_POSTGRES_FALLBACK, severity="critical", detail={"error": "no dsn"})

    assert captured["url"] == "http://example.test/hook"
    assert captured["timeout"] == 3
    assert captured["body"] == {
        "event": "budget_postgres_fallback",
        "severity": "critical",
        "detail": {"error": "no dsn"},
    }


def test_webhook_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setenv("LLM_ROUTER_ALERT_WEBHOOK", "http://example.test/hook")
    monkeypatch.setattr("llm_router.alerts.urllib.request.urlopen", boom)
    # Must NOT raise — fail-open contract.
    emit_alert(AUDIT_TAMPER, detail={"tamper_row": 1})


def test_never_raises_on_bad_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    # Non-JSON-serialisable detail must not blow up the caller when a
    # webhook is configured; the log path may drop it, the function returns.
    monkeypatch.setenv("LLM_ROUTER_ALERT_WEBHOOK", "http://example.test/hook")
    monkeypatch.setattr(
        "llm_router.alerts.urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not reach")),
    )
    emit_alert(RUNAWAY_BREAKER_TRIP, detail={"obj": object()})  # unserialisable


# ── Invoice discrepancy alerting (#57) ───────────────────────────────────────


def _diff(diff_pct: float):
    from llm_router.invoice_reconciliation import ReconciliationDiff

    return ReconciliationDiff(
        provider="anthropic", period="2026-06",
        provider_reported_usd=100.0, llm_router_reported_usd=100.0 * (1 - diff_pct),
        diff_usd=100.0 * diff_pct, diff_pct=diff_pct,
        provider_call_count=None, llm_router_call_count=0,
    )


def test_alert_if_discrepant_within_tolerance_no_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_router import invoice_reconciliation as ir

    fired = []
    monkeypatch.setattr("llm_router.alerts.emit_alert", lambda *a, **k: fired.append((a, k)))
    assert ir.alert_if_discrepant(_diff(0.01)) is False  # 1% < 2% default
    assert fired == []


def test_alert_if_discrepant_over_tolerance_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_router import invoice_reconciliation as ir

    fired = []
    monkeypatch.setattr("llm_router.alerts.emit_alert", lambda event, **k: fired.append((event, k)))
    assert ir.alert_if_discrepant(_diff(0.05)) is True  # 5% > 2%
    assert fired and fired[0][0] == "invoice_discrepancy"
    assert fired[0][1]["detail"]["diff_pct"] == 0.05


def test_alert_if_discrepant_env_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_router import invoice_reconciliation as ir

    fired = []
    monkeypatch.setenv("LLM_ROUTER_INVOICE_DISCREPANCY_PCT", "0.10")  # 10% tolerance
    monkeypatch.setattr("llm_router.alerts.emit_alert", lambda *a, **k: fired.append(1))
    assert ir.alert_if_discrepant(_diff(0.05)) is False  # 5% < 10% → no alert
    assert fired == []
