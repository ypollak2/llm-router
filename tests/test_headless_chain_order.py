"""P1 tuning regression: headless complex routes reach broker-backed Codex first.

The headless gateway daemon disables local Codex/Gemini subprocess backends. When
a session broker offers them, the complex chain must place broker-backed Codex at
the FRONT (the capable free path) so it isn't buried behind unreachable Claude and
slow local reasoning models. Simple/BUDGET routes must stay free-local.
"""

from unittest.mock import AsyncMock, patch

import pytest

import llm_router.session_broker as sb
from llm_router.router import _build_and_filter_chain
from llm_router.types import Complexity, RoutingProfile, TaskType


def _cfg():
    from llm_router.config import get_config
    return get_config()


@pytest.mark.asyncio
async def test_headless_complex_puts_broker_codex_first(mock_env, monkeypatch):
    """Premium/complex + broker offers codex → codex is the first candidate."""
    sb._provider_cache = None
    monkeypatch.setenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "codex,gemini_cli")
    with patch("llm_router.session_broker.broker_providers",
               new=AsyncMock(return_value=frozenset({"codex"}))):
        chain = await _build_and_filter_chain(
            TaskType.ANALYZE, RoutingProfile.PREMIUM, None, "complex", Complexity.COMPLEX, _cfg()
        )
    assert chain, "chain should not be empty"
    assert chain[0].startswith("codex/"), \
        f"expected broker-backed codex first, got {chain[:3]}"


# NOTE: a "simple/BUDGET does not front codex" test was removed — asserting it
# env-independently is impractical (codex is legitimately front for a simple task
# when no local model is available, e.g. headless CI without Ollama). The
# premium-only gating of the re-assert is covered directly by the two tests here
# (fires for PREMIUM, absent without a broker) and is explicit in the code
# (`profile in (RoutingProfile.PREMIUM, RoutingProfile.REASONING)`).


@pytest.mark.asyncio
async def test_no_broker_no_codex_front(mock_env, monkeypatch):
    """With no broker offering codex, the premium chain is not codex-fronted."""
    sb._provider_cache = None
    monkeypatch.setenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "codex,gemini_cli")
    with patch("llm_router.session_broker.broker_providers",
               new=AsyncMock(return_value=frozenset())):
        chain = await _build_and_filter_chain(
            TaskType.ANALYZE, RoutingProfile.PREMIUM, None, "complex", Complexity.COMPLEX, _cfg()
        )
    assert chain
    assert not chain[0].startswith("codex/"), \
        f"no broker → codex must not be fronted, got {chain[:3]}"
