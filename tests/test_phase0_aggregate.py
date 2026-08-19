"""Phase 0 Step 2 — `_aggregate` net-realized-savings math + adoption gating + quota.

`_aggregate` is a pure function of `(scope, scope_id, rows)`; these tests build
`LedgerEvent` rows directly (via `record_event`) and read back `Accounting` via
`get_route_accounting`, exercising:
  * classifier/failed-attempt-cost totals and net_realized_savings_usd,
  * hook_overhead_usd gated on row-level host_mode (marginal-$0 on subscription),
  * adoption-method gating of realized_savings_usd (door_call/agent_marked count;
    content_match → likely_used_routes; NULL on verified_used → door_call back-compat),
  * host-mode split of realized savings + quota-tokens-saved,
  * overhead_as_pct_of_gross guarded for a zero-gross route.
"""
from __future__ import annotations

import uuid

import pytest

from llm_router.execution_ledger import LedgerEvent, get_route_accounting, record_event


def _attempt(route_id, *, measured=0.0, baseline=0.0, classifier=None, failed=None,
             baseline_tokens=None, input_tokens=None, output_tokens=None,
             host_mode="unknown", hook_in=None, hook_out=None, provider=""):
    return LedgerEvent(
        session_id="s-agg",
        route_id=route_id,
        attempt_id=str(uuid.uuid4()),
        event_type="attempt_completed",
        measured_cost_usd=measured,
        baseline_equivalent_cost_usd=baseline,
        classifier_cost_usd=classifier,
        failed_attempt_cost_usd=failed,
        baseline_tokens=baseline_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        host_mode=host_mode,
        hook_input_tokens=hook_in,
        hook_output_tokens=hook_out,
        provider=provider,
    )


def _realized(route_id, *, adoption_method=None, status="verified_used"):
    return LedgerEvent(
        session_id="s-agg",
        route_id=route_id,
        event_type="route_realized",
        realization_status=status,
        adoption_method=adoption_method,
        used_by_host=True,
        accepted=True,
    )


def test_net_realized_savings_subtracts_classifier_failed_and_hook_overhead(tmp_path, monkeypatch):
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))

    rid = "r-net"
    record_event(
        _attempt(
            rid, measured=0.01, baseline=0.05, classifier=0.002, failed=0.003,
            host_mode="metered", hook_in=1_000, hook_out=500,
        ),
        path=db_path,
    )
    record_event(_realized(rid, adoption_method="door_call"), path=db_path)

    acc = get_route_accounting(rid, path=db_path)
    assert acc.realized_savings_usd == pytest.approx(0.04)  # 0.05 - 0.01
    assert acc.classifier_cost_usd_total == pytest.approx(0.002)
    assert acc.failed_attempt_cost_usd_total == pytest.approx(0.003)
    # A small number of hook tokens on a metered row → nonzero but modest overhead.
    assert acc.hook_overhead_usd > 0.0
    expected_net = acc.realized_savings_usd - 0.002 - 0.003 - acc.hook_overhead_usd
    assert acc.net_realized_savings_usd == pytest.approx(expected_net)
    # overhead_as_pct_of_gross is computed (not zero/error) whenever gross > 0; it is
    # NOT bounded to <=1.0 in general (that's a property CI checks on realistic soak
    # data, not a mathematical invariant _aggregate() enforces on arbitrary rows).
    assert acc.overhead_as_pct_of_gross > 0.0


def test_hook_overhead_is_zero_on_subscription_host_mode(tmp_path, monkeypatch):
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))

    rid = "r-sub"
    record_event(
        _attempt(
            rid, measured=0.01, baseline=0.05, host_mode="subscription",
            hook_in=1_000_000, hook_out=500_000,
        ),
        path=db_path,
    )
    record_event(_realized(rid, adoption_method="door_call"), path=db_path)

    acc = get_route_accounting(rid, path=db_path)
    assert acc.hook_overhead_usd == 0.0  # marginal-$0 rule — never fabricate a $ figure
    assert acc.realized_savings_by_host_mode == {"subscription": pytest.approx(0.04)}


def test_content_match_excluded_from_realized_goes_to_likely_used(tmp_path, monkeypatch):
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))

    rid = "r-cm"
    record_event(_attempt(rid, measured=0.01, baseline=0.05, host_mode="metered"), path=db_path)
    record_event(_realized(rid, adoption_method="content_match"), path=db_path)

    acc = get_route_accounting(rid, path=db_path)
    assert acc.realized_savings_usd == 0.0
    assert acc.likely_used_routes == 1
    assert acc.realized_routes == 1  # still counted as a realized (verified_used) ROUTE...
    assert acc.realized_savings_by_host_mode == {}  # ...but contributes no $ to realized


def test_agent_marked_counts_as_realized(tmp_path, monkeypatch):
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))

    rid = "r-am"
    record_event(_attempt(rid, measured=0.01, baseline=0.05, host_mode="subscription"), path=db_path)
    record_event(_realized(rid, adoption_method="agent_marked"), path=db_path)

    acc = get_route_accounting(rid, path=db_path)
    assert acc.realized_savings_usd == pytest.approx(0.04)
    assert acc.realized_by_adoption_method == {"agent_marked": pytest.approx(0.04)}


def test_null_adoption_on_verified_used_backcompat_treated_as_door_call(tmp_path, monkeypatch):
    """Pre-migration rows: verified_used with adoption_method=None must still count
    as realized (treated as door_call) — otherwise every existing verified_used
    route silently drops out of realized_savings_usd on upgrade."""
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))

    rid = "r-null"
    record_event(_attempt(rid, measured=0.01, baseline=0.05, host_mode="metered"), path=db_path)
    record_event(_realized(rid, adoption_method=None), path=db_path)

    acc = get_route_accounting(rid, path=db_path)
    assert acc.realized_savings_usd == pytest.approx(0.04)
    assert acc.realized_by_adoption_method == {"door_call": pytest.approx(0.04)}


def test_quota_tokens_saved_only_on_realized_routes_bucketed_by_host_mode(tmp_path, monkeypatch):
    """Phase 0.1 reframe: quota-tokens-saved = "Claude tokens NOT consumed" — the
    Σ(input+output) tokens actually served by a NON-Claude model, on a route that
    is realized (verified_used + adoption in _COUNTS_AS_REALIZED). It is no longer
    a baseline-minus-actual delta (that was a structural tautology — baseline_tokens
    was written as the SAME accepted attempt's own token count)."""
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))

    r1, r2 = "r-quota-sub", "r-quota-cm"
    # r1: realized (door_call), subscription, served by a non-Claude model
    # ("openai") — quota should accrue as the full served-token count.
    record_event(
        _attempt(r1, measured=0.0, baseline=0.0,
                 input_tokens=200, output_tokens=100, host_mode="subscription",
                 provider="openai"),
        path=db_path,
    )
    record_event(_realized(r1, adoption_method="door_call"), path=db_path)
    # r2: content_match (NOT realized) — quota must NOT accrue even though the
    # route was also served by a non-Claude model.
    record_event(
        _attempt(r2, measured=0.0, baseline=0.0,
                 input_tokens=200, output_tokens=100, host_mode="subscription",
                 provider="openai"),
        path=db_path,
    )
    record_event(_realized(r2, adoption_method="content_match"), path=db_path)

    acc1 = get_route_accounting(r1, path=db_path)
    assert acc1.realized_quota_tokens_saved == 300  # 200 + 100, served off-Claude
    assert acc1.quota_tokens_saved_by_host_mode == {"subscription": 300}

    acc2 = get_route_accounting(r2, path=db_path)
    assert acc2.realized_quota_tokens_saved == 0
    assert acc2.quota_tokens_saved_by_host_mode == {}


def test_quota_tokens_saved_zero_when_final_model_is_claude(tmp_path, monkeypatch):
    """If the route's final (accepted) attempt was itself served by Claude — e.g.
    escalated back to the frontier model — Claude ran, so the route saved 0 quota,
    even though it's realized and would otherwise qualify."""
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))

    rid = "r-quota-claude"
    record_event(
        _attempt(rid, measured=0.0, baseline=0.0,
                 input_tokens=200, output_tokens=100, host_mode="subscription",
                 provider="anthropic"),
        path=db_path,
    )
    record_event(_realized(rid, adoption_method="door_call"), path=db_path)

    acc = get_route_accounting(rid, path=db_path)
    assert acc.realized_quota_tokens_saved == 0
    assert acc.quota_tokens_saved_by_host_mode == {}


def test_quota_tokens_saved_zero_when_provider_unknown(tmp_path, monkeypatch):
    """Never fabricate: a route with no recorded provider (e.g. a pre-migration
    row) must NOT be assumed non-Claude — it contributes 0 quota, not a guessed
    saving we can't actually verify."""
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))

    rid = "r-quota-unknown-provider"
    record_event(
        _attempt(rid, measured=0.0, baseline=0.0,
                 input_tokens=200, output_tokens=100, host_mode="subscription"),
        path=db_path,
    )
    record_event(_realized(rid, adoption_method="door_call"), path=db_path)

    acc = get_route_accounting(rid, path=db_path)
    assert acc.realized_quota_tokens_saved == 0
    assert acc.quota_tokens_saved_by_host_mode == {}


def test_overhead_as_pct_of_gross_guarded_for_zero_realized_savings(tmp_path, monkeypatch):
    """A route with no realized savings (e.g. unknown realization) must report
    overhead_as_pct_of_gross == 0.0, never a ZeroDivisionError or inf."""
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))

    rid = "r-zero"
    record_event(
        _attempt(rid, measured=0.01, baseline=0.05, classifier=0.001, host_mode="metered"),
        path=db_path,
    )
    # No realization event at all → realization stays unset/"unknown"-ish, never verified_used.

    acc = get_route_accounting(rid, path=db_path)
    assert acc.realized_savings_usd == 0.0
    assert acc.overhead_as_pct_of_gross == 0.0
    assert acc.net_realized_savings_usd == pytest.approx(-0.001)  # 0 - classifier - 0 - 0


def test_verified_overridden_never_counts_as_realized_even_with_adoption_method(tmp_path, monkeypatch):
    """Sanity: Gate 18's original guarantee still holds post-Phase-0 — a
    verified_overridden route contributes to potential but never realized,
    regardless of what adoption_method happens to be set."""
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))

    rid = "r-override"
    record_event(_attempt(rid, measured=0.01, baseline=0.05, host_mode="metered"), path=db_path)
    record_event(_realized(rid, adoption_method="door_call", status="verified_overridden"), path=db_path)

    acc = get_route_accounting(rid, path=db_path)
    assert acc.potential_savings_usd == pytest.approx(0.04)
    assert acc.realized_savings_usd == 0.0
    assert acc.overridden_routes == 1
