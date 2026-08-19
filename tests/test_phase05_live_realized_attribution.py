"""Phase 0.5 / T7 — live realized-savings attribution integration test.

This is the decisive proof for the whole phase: before Option A, adoption rows
(``enforce-route.py::_record_realization_used``) were written against a
hook-minted ``route_id`` while billable rows (``router.py``'s
``_emit_ledger_attempt``) were written against a completely independent
``correlation_id`` minted fresh per ``route_and_call`` invocation. Two
unrelated id schemes meant ``execution_ledger._aggregate``'s join on
``route_id`` never fired in production, so ``realized_savings_usd`` was
always 0 no matter how faithfully the host actually routed.

Option A threads a caller-supplied ``route_directive_id`` into
``route_and_call``; when present, it becomes the billable row's
``route_id`` instead of the fresh ``correlation_id``. When the hook's
directive id and the adoption row's ``route_id`` are the SAME string, the
join fires and realized savings appear.

Each test below drives the REAL ``route_and_call`` (mocked only at the
provider/config/cost-tracking boundary — see ``_Cfg``/``_drive_route_and_call``,
lifted from ``test_phase0_writesite_baseline.py``) so the billable row is
genuine, and pairs it with the REAL writers
(``enforce-route.py::_record_realization_used``,
``stop-enforce.py::_record_override``) rather than hand-crafted ledger rows,
so this proves the actual wiring rather than merely re-testing
``_aggregate``'s pure math (already covered by ``test_phase0_aggregate.py``).

Guardrail respected: ``execution_ledger._aggregate`` and its schema are never
touched or monkeypatched here — only exercised read-only via its public
accessors.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.types import LLMResponse, RoutingProfile, TaskType

ROOT = Path(__file__).resolve().parents[1]
ENFORCE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"
STOP_HOOK = ROOT / "src" / "llm_router" / "hooks" / "stop-enforce.py"


def _load(path: Path, name: str):
    """In-process module load for the hyphenated hook scripts (they aren't
    importable as normal packages). Mirrors the established pattern in
    tests/test_red1_0506_routeid_and_dedup.py."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


enforce = _load(ENFORCE_HOOK, "llm_router_enforce_route_p05_live")
stop = _load(STOP_HOOK, "llm_router_stop_enforce_p05_live")


class _Cfg:
    """Minimal get_config() stand-in — copied from
    test_phase0_writesite_baseline.py's proven _Cfg (all attrs route_and_call
    actually touches when its provider/cost/envelope/semantic-cache
    dependencies are mocked out)."""

    llm_router_claude_subscription = False
    llm_router_gemini_subscription = False
    llm_router_claw_code = False
    llm_router_routing_policy = "balanced"
    llm_router_agentic_model = ""
    llm_router_profile = RoutingProfile.BALANCED
    llm_router_monthly_budget = 0.0
    llm_router_daily_spend_limit = 0.0
    llm_router_escalate_above = 0.0
    llm_router_hard_stop_above = 0.0
    codex_daily_limit = 1000
    compaction_mode = "off"
    compaction_threshold = 4000
    prompt_cache_enabled = False
    prompt_cache_min_tokens = 1024
    context_enabled = False
    caveman_mode = "off"
    available_providers = {"openai"}

    def all_ollama_models(self):
        return []

    def all_openai_compat_models(self):
        return []


async def _drive_route_and_call(
    *,
    monkeypatch,
    tmp_path,
    ledger_db: Path,
    route_directive_id: str,
    session_id: str,
    provider: str = "openai",
    prompt: str = "what is the capital of France?",
    classifier_cost_usd: float = 0.0025,
):
    """Drive a real route_and_call(route_directive_id=...) through the actual
    dispatch loop with a mocked provider response, so the ACCEPTED attempt's
    billable ledger row lands with route_id == route_directive_id (Option A)
    and a nonzero baseline_equivalent_cost_usd (Phase 0 Item 1, already live).
    """
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(tmp_path / f"rq-{session_id}.jsonl"))
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(ledger_db))
    monkeypatch.setenv("LLM_ROUTER_SESSION_ID", session_id)

    async def successful_call(model, messages, **kwargs):
        # measured cost must stay BELOW the realistic query-task baseline
        # (haiku: 100in*0.80 + 50out*4.0 per 1M = $0.00028 — see
        # cost._get_baseline_for_task/_get_baseline_cost) or route_potential's
        # max(0, baseline − actual) floors to 0 and no route ever shows a
        # positive potential/realized saving, regardless of routing correctness.
        return LLMResponse(
            content="answer", model=model, input_tokens=100, output_tokens=50,
            cost_usd=0.00005, latency_ms=12.0, provider=provider,
        )

    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    mock_log = MagicMock()
    mock_log.bind.return_value = MagicMock()

    from llm_router.router import route_and_call

    with (
        patch("llm_router.router.get_config", return_value=_Cfg()),
        patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock,
              return_value=[f"{provider}/some-model"]),
        patch("llm_router.router.providers.call_llm", new_callable=AsyncMock,
              side_effect=successful_call),
        patch("llm_router.router.get_tracker", return_value=tracker),
        patch("llm_router.router.log", mock_log),
        patch("llm_router.router._native_notify", lambda *a, **k: None),
        patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.get_daily_spend_by_task_type", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.log_usage", new_callable=AsyncMock),
        patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, None)),
        patch("llm_router.router.commit_envelope", new_callable=AsyncMock),
        patch("llm_router.router.release_envelope", new_callable=AsyncMock),
        patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None),
        patch("llm_router.semantic_cache.store", new_callable=AsyncMock),
    ):
        return await route_and_call(
            TaskType.QUERY,
            prompt,
            profile=RoutingProfile.BALANCED,
            complexity_hint="moderate",
            classification_data={"classifier_cost_usd": classifier_cost_usd},
            route_directive_id=route_directive_id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,expected_quota", [("openai", 150), ("anthropic", 0)])
async def test_realized_savings_flips_zero_to_positive_on_matching_route_id(
    temp_db, tmp_path, monkeypatch, provider, expected_quota
):
    """THE decisive Phase 0.5 proof: realized_savings_usd goes from 0 (wrong
    route_id — the pre-fix bug) to >0 (matching route_id — Option A) against
    the SAME billable row, varying only the route_id the adoption writer uses.
    """
    ledger_db = tmp_path / "ledger.db"
    session_id = f"sess-flip-{provider}"
    did = "sess:1785000000:llm_query:deadbeef"
    wrong_id = "sess:1785000000:llm_query:ffffffff"

    from llm_router import execution_ledger

    resp = await _drive_route_and_call(
        monkeypatch=monkeypatch, tmp_path=tmp_path, ledger_db=ledger_db,
        route_directive_id=did, session_id=session_id, provider=provider,
    )
    assert resp.content == "answer"

    # BEFORE anything is recorded: the billable row is there (potential > 0)
    # but nothing has claimed it as realized yet.
    acc_before = execution_ledger.get_route_accounting(did, path=ledger_db)
    assert acc_before.potential_savings_usd > 0.0
    assert acc_before.realized_savings_usd == 0.0

    # Negative control (5a): realization recorded against a DIFFERENT
    # route_id — reproduces the pre-Option-A bug (hook-minted id !=
    # router-minted correlation_id). The join must NOT fire.
    enforce._record_realization_used(
        session_id, {"route_id": wrong_id, "turn_id": 1, "task_type": "query"}
    )
    acc_wrong = execution_ledger.get_route_accounting(did, path=ledger_db)
    assert acc_wrong.realized_savings_usd == 0.0, (
        "a realization row with a MISMATCHED route_id must never count toward "
        "this route's realized savings — this is the bug Phase 0.5 fixes"
    )
    assert acc_wrong.potential_savings_usd == acc_before.potential_savings_usd

    # THE FIX (Option A): realization recorded against the MATCHING
    # route_id — the hook-minted directive id threaded through
    # route_directive_id into the billable row.
    enforce._record_realization_used(
        session_id, {"route_id": did, "turn_id": 1, "task_type": "query"}
    )
    acc_after = execution_ledger.get_route_accounting(did, path=ledger_db)

    assert acc_after.realized_savings_usd > 0.0, (
        "THE decisive proof: realized_savings_usd must flip from 0 to >0 once "
        "the adoption row's route_id matches the billable row's route_id"
    )
    assert acc_after.realized_savings_usd == pytest.approx(acc_after.potential_savings_usd)
    assert acc_after.realized_quota_tokens_saved == expected_quota
    assert acc_after.realized_by_adoption_method == {"door_call": acc_after.realized_savings_usd}
    assert acc_after.net_realized_savings_usd == pytest.approx(
        acc_after.realized_savings_usd
        - acc_after.classifier_cost_usd_total
        - acc_after.failed_attempt_cost_usd_total
        - acc_after.hook_overhead_usd
    )

    # Also assert via get_period_accounting (brief's explicit ask) — a wide
    # window covering every ts stamped in this test, isolated to this test's
    # own ledger_db so no other route pollutes the sum.
    now = time.time()
    acc_period = execution_ledger.get_period_accounting(now - 3600, now + 3600, path=ledger_db)
    assert acc_period.realized_savings_usd == pytest.approx(acc_after.realized_savings_usd)
    assert acc_period.potential_savings_usd == pytest.approx(acc_after.potential_savings_usd)


@pytest.mark.asyncio
async def test_content_match_adoption_never_counts_as_realized(temp_db, tmp_path, monkeypatch):
    """Negative control (5b): adoption_method="content_match" is corroborating
    evidence, not proof (Gate 18) — it must land in likely_used_routes and
    NEVER in realized_savings_usd, even though the row's realization_status
    is verified_used. T8 (the actual content_match Stop-hook writer) is out
    of Phase 0.5's scope, so this hand-writes the row T8 would eventually
    produce, to prove _aggregate's existing gate holds for it."""
    ledger_db = tmp_path / "ledger.db"
    session_id = "sess-content-match"
    did = "sess:1785000000:llm_query:c0ffee00"

    from llm_router import execution_ledger
    from llm_router.execution_ledger import LedgerEvent, record_event

    resp = await _drive_route_and_call(
        monkeypatch=monkeypatch, tmp_path=tmp_path, ledger_db=ledger_db,
        route_directive_id=did, session_id=session_id, provider="openai",
    )
    assert resp.content == "answer"

    record_event(
        LedgerEvent(
            event_id="content-match-neg-control",
            session_id=session_id,
            route_id=did,
            turn_id="1",
            event_type="route_realized",
            task_type="query",
            realization_status="verified_used",
            adoption_method="content_match",
            used_by_host=True,
        ),
        path=ledger_db,
    )

    acc = execution_ledger.get_route_accounting(did, path=ledger_db)
    assert acc.potential_savings_usd > 0.0
    assert acc.realized_savings_usd == 0.0
    assert acc.likely_used_routes == 1
    assert acc.realized_by_adoption_method == {}


@pytest.mark.asyncio
async def test_verified_overridden_never_counts_as_realized(temp_db, tmp_path, monkeypatch):
    """Negative control (5c): a plain-text override (stop-enforce.py's
    _record_override — the REAL writer, not a hand-crafted row) must show up
    as overridden_routes == 1 and contribute 0 to realized_savings_usd,
    regardless of the route's positive potential."""
    ledger_db = tmp_path / "ledger.db"
    session_id = "sess-overridden"
    did = "sess:1785000000:llm_query:0verride1"

    from llm_router import execution_ledger

    resp = await _drive_route_and_call(
        monkeypatch=monkeypatch, tmp_path=tmp_path, ledger_db=ledger_db,
        route_directive_id=did, session_id=session_id, provider="openai",
    )
    assert resp.content == "answer"

    stop._record_override(session_id, "query", {"route_id": did, "turn_id": 1})

    acc = execution_ledger.get_route_accounting(did, path=ledger_db)
    assert acc.potential_savings_usd > 0.0
    assert acc.overridden_routes == 1
    assert acc.realized_routes == 0
    assert acc.realized_savings_usd == 0.0


def _run_advise_hook(payload: dict, *, home: Path, extra_env: dict[str, str] | None = None):
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(home)
    env["LLM_ROUTER_ENFORCE"] = "advise"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(ENFORCE_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _write_pending(home: Path, session_id: str, **overrides) -> Path:
    router_dir = home / ".llm-router"
    router_dir.mkdir(parents=True, exist_ok=True)
    pending_path = router_dir / f"pending_route_{session_id}.json"
    data = {
        "expected_tool": "llm_query",
        "task_type": "query",
        "complexity": "simple",
        "issued_at": time.time(),
        "session_id": session_id,
    }
    data.update(overrides)
    pending_path.write_text(json.dumps(data), encoding="utf-8")
    return pending_path


@pytest.mark.asyncio
async def test_advise_mode_end_to_end_realizes_savings_via_hook_subprocess(
    temp_db, tmp_path, monkeypatch
):
    """T7 step 6 — the full live pipeline in advise mode, end to end: a real
    route_and_call billable row at route_id=DID, then a genuine PreToolUse
    subprocess hook run under LLM_ROUTER_ENFORCE=advise that HONORS the pending
    directive. Proves: (1) advise NEVER emits a blocking decision, (2) one
    door_call realization row lands at DID via the real hook subprocess (not
    a mocked writer), (3) pending is cleared, and (4) realized_savings_usd
    flips positive on the SAME route the billable row used."""
    ledger_db = tmp_path / "ledger.db"
    session_id = "sess-advise-e2e"
    did = f"{session_id}:1785000000:llm_query:advise001"

    from llm_router import execution_ledger

    resp = await _drive_route_and_call(
        monkeypatch=monkeypatch, tmp_path=tmp_path, ledger_db=ledger_db,
        route_directive_id=did, session_id=session_id, provider="openai",
    )
    assert resp.content == "answer"

    acc_before = execution_ledger.get_route_accounting(did, path=ledger_db)
    assert acc_before.potential_savings_usd > 0.0
    assert acc_before.realized_savings_usd == 0.0

    pending_path = _write_pending(tmp_path, session_id, route_id=did, turn_id=1)

    result = _run_advise_hook(
        {"session_id": session_id, "tool_name": "llm_query"},
        home=tmp_path,
        extra_env={"LLM_ROUTER_EXECUTION_LEDGER_DB": str(ledger_db)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", "advise must NEVER emit a blocking decision"
    assert not pending_path.exists(), "pending must be cleared once honored"

    acc_after = execution_ledger.get_route_accounting(did, path=ledger_db)
    assert acc_after.realized_routes == 1
    assert acc_after.realized_savings_usd > 0.0
    assert acc_after.realized_savings_usd == pytest.approx(acc_after.potential_savings_usd)
    assert acc_after.realized_by_adoption_method == {"door_call": acc_after.realized_savings_usd}
