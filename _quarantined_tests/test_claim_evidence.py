"""B6 / INV-CLAIM-001..004: the claim-evidence registry + validator.

Proves the validator rejects each dishonest-claim failure mode and that the shipped
registry (scripts/claim_evidence.json) is itself valid — replacing the old
"grandfather forever" model with per-claim, expiring, evidence-linked validation.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_VALIDATOR = _ROOT / "scripts" / "validate_claim_evidence.py"
_REGISTRY = _ROOT / "scripts" / "claim_evidence.json"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_claim_evidence", _VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


V = _load_validator()


def test_shipped_registry_is_valid():
    data = json.loads(_REGISTRY.read_text())
    assert V.validate_registry(data, today=date(2026, 7, 26)) == []


def test_unsupported_claim_may_not_assert_a_magnitude():
    data = {"allowed_metrics": ["net_verified_token_reduction_percent"], "claims": [{
        "claim_id": "C1", "status": "unsupported",
        "claim_text": "35-80% cost savings, proven",
    }]}
    v = V.validate_registry(data)
    assert any("numeric magnitude" in m for m in v)


def test_supported_claim_needs_evidence_and_allowed_metric():
    data = {"allowed_metrics": ["task_success_rate"], "claims": [{
        "claim_id": "C2", "status": "supported", "claim_text": "does X",
        "benchmark_id": None, "evidence_tests": [], "metric": "made_up_metric",
    }]}
    v = V.validate_registry(data)
    assert any("no benchmark_id or evidence_tests" in m for m in v)
    assert any("not in allowed_metrics" in m for m in v)


def test_expired_evidence_fails():
    data = {"allowed_metrics": ["task_success_rate"], "claims": [{
        "claim_id": "C3", "status": "supported", "claim_text": "does X",
        "benchmark_id": "B", "metric": "task_success_rate",
        "expires_after_days": 90, "last_verified_at": "2026-01-01",
    }]}
    v = V.validate_registry(data, today=date(2026, 7, 26))  # >90 days later
    assert any("expired" in m for m in v)


def test_subscription_dollar_claim_rejected():
    data = {"allowed_metrics": ["real_metered_dollars_avoided_usd"], "claims": [{
        "claim_id": "C4", "status": "supported",
        "claim_text": "saves $200 in real dollars", "benchmark_id": "B",
        "metric": "real_metered_dollars_avoided_usd",
        "host_mode": "subscription", "expires_after_days": 365,
        "last_verified_at": "2026-07-01",
    }]}
    v = V.validate_registry(data, today=date(2026, 7, 26))
    assert any("subscription" in m for m in v)


def test_proven_backed_by_simulation_rejected():
    data = {"allowed_metrics": ["net_verified_token_reduction_percent"], "claims": [{
        "claim_id": "C5", "status": "supported",
        "claim_text": "proven cost reduction", "benchmark_id": "sim-counterfactual-1",
        "metric": "net_verified_token_reduction_percent",
        "expires_after_days": 365, "last_verified_at": "2026-07-01",
    }]}
    v = V.validate_registry(data, today=date(2026, 7, 26))
    assert any("simulated counterfactual" in m for m in v)


def test_valid_supported_claim_passes():
    data = {"allowed_metrics": ["task_success_rate"], "claims": [{
        "claim_id": "C6", "status": "supported", "claim_text": "blocks the turn",
        "evidence_tests": ["tests/test_zero_claude_bypass.py"],
        "metric": "task_success_rate", "expires_after_days": 365,
        "last_verified_at": "2026-07-20",
    }]}
    assert V.validate_registry(data, today=date(2026, 7, 26)) == []
