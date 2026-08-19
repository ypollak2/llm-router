"""CF-3 regression: a dynamically-selected agentic model must honor block_providers.

`block_providers` filters the base chain, but the dynamic agentic pick
(`best_agentic_model`) is chosen independently and re-injected at the FRONT of the
chain for agentic task types. Without the guard in `_build_and_filter_chain`, that
re-injection smuggles a blocked provider past the user's `.llm_router.yml` block rule.

These tests pin the dynamic-pick path (no explicit env/repo agentic model) and assert:
  * blocked  → the dynamic ollama model is NOT re-injected (guard fires)
  * unblocked → the same dynamic ollama model IS pinned front (proves the guard,
                not general filtering, is what removes it)
  * explicit pin → survives the block (explicit user intent overrides the block list)
"""

from __future__ import annotations

import pytest

from llm_router.config import RouterConfig
from llm_router.profiles import provider_from_model
from llm_router.repo_config import RepoConfig
from llm_router.router import _build_and_filter_chain
from llm_router.types import Complexity, RoutingProfile, TaskType

DYN = "ollama/qwen3.5:latest"  # what best_agentic_model() dynamically picks


def _isolate_dynamic(monkeypatch, *, block: list[str]) -> None:
    """Force the DYNAMIC agentic-pick path deterministically, machine-independent.

    - no explicit env/repo agentic pin (caller leaves cfg.llm_router_agentic_model empty
      and repo_cfg.agentic_model None) so line ``769`` falls through to the dynamic pick
    - all_ollama_models() returns DYN so ``ollama_models`` is truthy and DYN is a
      valid local candidate
    - best_agentic_model() returns DYN so the dynamic pick resolves to it
    - repo_cfg carries the block list under test
    """
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.0)
    monkeypatch.setattr(
        "llm_router.router.get_repo_config",
        lambda *a, **k: RepoConfig(block_providers=list(block)),
    )
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)
    monkeypatch.setattr(
        "llm_router.config.RouterConfig.all_ollama_models", lambda self: [DYN]
    )
    monkeypatch.setattr(
        "llm_router.agentic_registry.best_agentic_model", lambda: DYN
    )


async def _chain(cfg: RouterConfig):
    return await _build_and_filter_chain(
        TaskType.ANALYZE, RoutingProfile.BALANCED, None, None, Complexity.MODERATE, cfg,
    )


@pytest.mark.asyncio
async def test_dynamic_agentic_model_blocked_not_reinjected(monkeypatch):
    """block_providers=[ollama] → the dynamic ollama pick is never pinned/re-injected,
    and a structured policy-skip log event is emitted (§18 CF-3 checklist).

    The log is asserted via structlog.testing.capture_logs, which captures the event at
    the structlog layer independent of the global render pipeline — so the assertion is
    not order-dependent on whatever else in the suite reconfigures logging.
    """
    from structlog.testing import capture_logs

    _isolate_dynamic(monkeypatch, block=["ollama"])
    cfg = RouterConfig()
    cfg.llm_router_agentic_model = ""  # dynamic-pick path
    with capture_logs() as logs:
        chain = await _chain(cfg)
    assert all(provider_from_model(m) != "ollama" for m in chain), (
        f"blocked ollama provider leaked into chain via agentic pin: {chain}"
    )
    assert any(
        e.get("event") == "policy_rejection" and e.get("provider") == "ollama"
        and e.get("scope") == "block_provider"
        for e in logs
    ), f"expected a policy_rejection log event for the blocked agentic model; got {logs}"


@pytest.mark.asyncio
async def test_dynamic_agentic_model_pinned_when_not_blocked(monkeypatch):
    """Control: with no block, the SAME dynamic pick IS pinned front.

    Proves the guard (not some unrelated filter) is what removes it above.
    """
    _isolate_dynamic(monkeypatch, block=[])
    cfg = RouterConfig()
    cfg.llm_router_agentic_model = ""
    chain = await _chain(cfg)
    assert chain and chain[0] == DYN, (
        f"dynamic agentic pick should lead an unblocked chain, got: {chain[:3]}"
    )


@pytest.mark.asyncio
async def test_explicit_agentic_pin_survives_block(monkeypatch):
    """An EXPLICIT env pin is user intent and is NOT cleared by the guard."""
    _isolate_dynamic(monkeypatch, block=["ollama"])
    cfg = RouterConfig()
    cfg.llm_router_agentic_model = DYN  # explicit pin, even though ollama is blocked
    chain = await _chain(cfg)
    assert chain and chain[0] == DYN, (
        f"explicit agentic pin must survive the block, got: {chain[:3]}"
    )
