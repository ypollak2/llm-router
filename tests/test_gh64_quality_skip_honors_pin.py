"""Regression tests for GH#64 — quality_feedback.should_skip_model() silently
overriding an explicit routing.yaml per-task pin, permanently and invisibly.

Reporter's repro (paraphrased): pin ``ollama/llama3.2:3b`` for query/simple via
routing.yaml. After ~3-8 terse-but-correct answers, the heuristic scorer's
running average for that (model, task_type, complexity) pattern crosses below
QUALITY_THRESHOLD, and should_skip_model() starts returning True for it. The
router's only exemption was `model != model_override` — routing.yaml pins are
NOT model_override, so they entered the fallback chain as ordinary members and
became silently, permanently skippable. The skip happened via a bare
`continue` BEFORE the model was appended to chain_attempts, so the exclusion
left no trace anywhere a caller could see (not in the response's
chain_attempts, not in routing_quality.jsonl).

The fix has three parts, each covered here:
  1. `_dispatch_model_loop` now exempts `pinned_model` exactly like
     `model_override` (CHZ-AUD-C-02 extended).
  2. A skip now still gets recorded in chain_attempts as a visible marker
     (`quality_feedback.format_skip_marker` / `is_skip_marker`), instead of
     vanishing via a bare `continue`.
  3. The thresholds are configurable (LLM_ROUTER_QUALITY_MIN_CALLS,
     LLM_ROUTER_QUALITY_SKIP_THRESHOLD), a kill switch exists
     (LLM_ROUTER_QUALITY_SKIP=off), and query/simple is exempted from
     *skipping* (not from scoring — score_response is untouched).

No real Ollama calls: `_build_and_filter_chain` and `providers.call_llm` are
stubbed exactly like the existing CHZ-AUD-C-02 test
(test_c02_model_override_honored.py) and the TQ-007 harness.
"""
from __future__ import annotations

import os
import textwrap
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.test_tq007_daily_cap_downgrade as t
from llm_router import repo_config as repo_config_module
from llm_router import router as router_module
from llm_router.quality_feedback import (
    format_skip_marker,
    get_model_quality,
    is_skip_marker,
    record_quality,
    reset_quality_store,
    should_skip_model,
)
from llm_router.types import LLMResponse, RoutingProfile, TaskType


@pytest.fixture(autouse=True)
def clean_store():
    reset_quality_store()
    yield
    reset_quality_store()


def _load_repo_config_from_yaml(tmp_path, pinned_model: str):
    """Write a REAL routing.yaml and parse it through the real loader —
    mirrors the reporter's actual configuration mechanism rather than
    hand-building a RepoConfig object."""
    yaml_path = tmp_path / "routing.yaml"
    yaml_path.write_text(textwrap.dedent(f"""\
        routing:
          code:
            model: "{pinned_model}"
        """))
    raw = repo_config_module._parse_yaml(yaml_path)
    return repo_config_module._dict_to_config(raw, str(yaml_path))


def _response(model: str) -> LLMResponse:
    return LLMResponse(
        content=f"ok from {model}", model=model, input_tokens=7, output_tokens=3,
        cost_usd=0.0 if model.split("/")[0] in {"ollama", "codex", "gemini_cli"} else 0.001,
        latency_ms=5.0, provider=model.split("/", 1)[0],
    )


async def _run(*, chain, repo_cfg=None, env_extra=None):
    """Drive route_and_call with a stubbed chain, mirroring
    test_c02_model_override_honored.py's harness (no model_override here —
    the pin comes from repo_config instead)."""
    called: list[str] = []

    async def fake_call_llm(model, *a, **k):
        called.append(model)
        return _response(model)

    route_log = MagicMock()
    mock_log = MagicMock()
    mock_log.bind.return_value = route_log
    tracker = MagicMock()
    tracker.is_healthy.return_value = True

    env = {"LLM_ROUTER_ENFORCE": "off", "LLM_ROUTER_BANDIT": "off"}
    if env_extra:
        env.update(env_extra)

    with ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, env))
        p(patch("llm_router.router.get_config", return_value=t._Cfg()))
        if repo_cfg is not None:
            p(patch("llm_router.router.get_repo_config", return_value=repo_cfg))
        p(patch("llm_router.router.get_tracker", return_value=tracker))
        p(patch("llm_router.router.log", mock_log))
        p(patch("llm_router.router._native_notify", lambda *a, **k: None))
        for fn in ("get_monthly_spend", "get_daily_spend", "get_daily_spend_by_task_type"):
            p(patch(f"llm_router.router.cost.{fn}", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.router.cost.log_usage", new_callable=AsyncMock))
        p(patch("llm_router.policy.load_org_policy", return_value=None))
        p(patch("llm_router.policy.get_active_policy", return_value=None))
        p(patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, "k")))
        p(patch("llm_router.router.commit_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router.release_envelope", new_callable=AsyncMock))
        p(patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None))
        p(patch("llm_router.semantic_cache.store", new_callable=AsyncMock))
        p(patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock, return_value=list(chain)))
        p(patch("llm_router.router.providers.call_llm", side_effect=fake_call_llm))
        try:
            resp = await router_module.route_and_call(
                TaskType.CODE, "hello", profile=RoutingProfile.BALANCED,
                complexity_hint="moderate",
            )
        finally:
            await router_module.drain_bg_tasks(2.0)
    return resp, called


@pytest.mark.asyncio
async def test_pinned_model_is_attempted_despite_poisoned_quality_store(tmp_path):
    """GH#64 case 1: a routing.yaml pin survives a poisoned quality store."""
    pinned = "ollama/llama3.2:3b"
    fallback = "openai/gpt-4o"

    # Poison the store: 5 calls averaging 0.2 — well past _MIN_CALLS_FOR_SIGNAL
    # (3) and below QUALITY_THRESHOLD (0.4). Without the fix this alone makes
    # should_skip_model() return True for `pinned`.
    for _ in range(5):
        record_quality(pinned, "code", "moderate", 0.2)
    assert should_skip_model(pinned, "code", "moderate") is True, (
        "test setup invalid: the store must actually be poisoned"
    )

    repo_cfg = _load_repo_config_from_yaml(tmp_path, pinned)
    assert repo_cfg.model_override("code") == pinned  # sanity: real YAML round-trip

    resp, called = await _run(chain=[pinned, fallback], repo_cfg=repo_cfg)

    assert pinned in called, (
        f"GH#64: the routing.yaml pin was skipped by the quality circuit-breaker. "
        f"Models actually called: {called}"
    )
    assert resp.content == f"ok from {pinned}"
    assert pinned in resp.chain_attempts, (
        "the pinned model must appear as a REAL (unmarked) attempt in chain_attempts, "
        f"got: {resp.chain_attempts}"
    )


@pytest.mark.asyncio
async def test_nonpinned_poisoned_model_is_skipped_and_visible(tmp_path):
    """GH#64 case 2: an equally-poisoned, NON-pinned model is still skipped —
    the fix must not disable the breaker altogether — and the skip must leave
    a visible trace in chain_attempts (previously it vanished silently)."""
    poisoned = "ollama/badmodel"
    fallback = "openai/gpt-4o"

    for _ in range(5):
        record_quality(poisoned, "code", "moderate", 0.2)

    # No pin configured at all for "code".
    repo_cfg = repo_config_module.RepoConfig()

    resp, called = await _run(chain=[poisoned, fallback], repo_cfg=repo_cfg)

    assert poisoned not in called, f"expected {poisoned} to be skipped, but it was called"
    assert fallback in called

    assert poisoned not in resp.chain_attempts, (
        "a skipped model must NOT appear as a bare/real attempt entry"
    )
    skip_entries = [e for e in resp.chain_attempts if is_skip_marker(e)]
    assert skip_entries, f"expected a visible quality-skip marker, got chain_attempts={resp.chain_attempts}"
    assert poisoned in skip_entries[0]
    assert "avg=0.20" in skip_entries[0]
    assert "n=5" in skip_entries[0]


@pytest.mark.asyncio
async def test_quality_skip_off_disables_breaker_entirely(tmp_path):
    """GH#64 case 3: LLM_ROUTER_QUALITY_SKIP=off is a full kill switch, even
    for a non-pinned, badly-poisoned model."""
    poisoned = "ollama/badmodel"
    fallback = "openai/gpt-4o"

    for _ in range(5):
        record_quality(poisoned, "code", "moderate", 0.0)

    repo_cfg = repo_config_module.RepoConfig()
    resp, called = await _run(
        chain=[poisoned, fallback], repo_cfg=repo_cfg,
        env_extra={"LLM_ROUTER_QUALITY_SKIP": "off"},
    )

    assert poisoned in called, f"LLM_ROUTER_QUALITY_SKIP=off must disable skipping. Called: {called}"
    assert poisoned in resp.chain_attempts
    assert not any(is_skip_marker(e) for e in resp.chain_attempts)


class TestThresholdEnvVars:
    """GH#64 case 4: LLM_ROUTER_QUALITY_MIN_CALLS / LLM_ROUTER_QUALITY_SKIP_THRESHOLD
    actually move the skip boundary. Pure unit tests of quality_feedback —
    no router/dispatch machinery involved."""

    def test_default_threshold_skips_below_0_4(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_QUALITY_SKIP_THRESHOLD", raising=False)
        for _ in range(3):
            record_quality("m", "code", "moderate", 0.35)
        assert should_skip_model("m", "code", "moderate") is True

    def test_lower_threshold_env_stops_the_skip(self, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_QUALITY_SKIP_THRESHOLD", "0.2")
        for _ in range(3):
            record_quality("m", "code", "moderate", 0.35)
        assert should_skip_model("m", "code", "moderate") is False

    def test_default_min_calls_requires_three(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_QUALITY_MIN_CALLS", raising=False)
        record_quality("m", "code", "moderate", 0.1)
        record_quality("m", "code", "moderate", 0.1)
        assert should_skip_model("m", "code", "moderate") is False
        assert get_model_quality("m", "code", "moderate") is None

    def test_lower_min_calls_env_trusts_two_calls(self, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_QUALITY_MIN_CALLS", "2")
        record_quality("m", "code", "moderate", 0.1)
        record_quality("m", "code", "moderate", 0.1)
        assert should_skip_model("m", "code", "moderate") is True


class TestQuerySimpleExemption:
    """GH#64 §4a(iii): query/simple is exempt from *skipping only* — the
    heuristic (score_response) is untouched, so escalation still sees the
    real low score. Proven by contrast against an identically-poisoned
    code/simple pattern, which IS still skipped."""

    def test_query_simple_exempt_even_unpinned(self):
        for _ in range(10):
            record_quality("ollama/terse", "query", "simple", 0.1)
        assert should_skip_model("ollama/terse", "query", "simple") is False

    def test_control_same_poison_different_task_type_is_skipped(self):
        for _ in range(10):
            record_quality("ollama/terse", "code", "simple", 0.1)
        assert should_skip_model("ollama/terse", "code", "simple") is True

    def test_exemption_does_not_touch_scoring(self):
        from llm_router.quality_feedback import score_response
        qs = score_response("Lima.", "query", model="ollama/terse", complexity="simple")
        # Base (0.1) + no-refusal (0.2) + complete (0.1) = 0.4, right at
        # QUALITY_THRESHOLD — it can never earn the "detailed"/"structured"
        # bonuses reserved for >100 tokens or headings/lists. The exemption
        # changes should_skip_model, not this score.
        assert qs.score <= 0.4, "the heuristic bias itself must be untouched by the exemption"
        assert "detailed" not in qs.reasons
        assert "structured" not in qs.reasons
