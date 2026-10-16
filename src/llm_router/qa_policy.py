"""D-14, amended by D-114: strip local Q&A models, with an MCP last resort before Claude.

Evidence behind the rule: real Q&A prompts, local qwen 4/37 acceptable vs Sonnet 34/37
(PLAN §0.3 [RX]); 65 local Q&A answers in 7 days [U]. M3.0 (#297) applied it to MCP
``route_and_call`` only; P0.3 (PLAN v16) moved it here so the hook DIRECT path and the
in-process SDK (both build chains with ``hooks.chain_builder.build_chain``) apply the
same rule.

Why a separate module: the hook path must not import ``llm_router.router`` (cold import
~3.6 s; import time was ~77% of the slow hook tail [M41]). This module imports only
``llm_router.types`` and the lightweight ``llm_router.contracts``.
"""

from __future__ import annotations

import logging
from typing import Any

from llm_router.contracts import CLAUDE_PROVIDERS
from llm_router.types import LOCAL_PROVIDERS

log = logging.getLogger("llm_router.qa_policy")

# The Q&A task types (PLAN M0.2). Defined here, re-exported by ``northstar`` (which owns
# the metrics that use it) so the hook path does not have to import northstar.
QA_TASK_TYPES: frozenset[str] = frozenset({
    "query", "research", "generate", "analyze", "coordinate", "introspect",
    "summary", "classification", "extraction",
})

# Providers that run on the user's own machine. One source of truth: ``types.LOCAL_PROVIDERS``
# (ollama, lm_studio, vllm, llamacpp) plus ``openai_compat``, which is local by definition
# (config.py: "OpenAI-compatible local inference (llama.cpp, vLLM, TGI, LM Studio)", base URL
# e.g. http://localhost:8080/v1) but is not in ``LOCAL_PROVIDERS``. That set is shared with
# budget.py, so it is extended here rather than changed there. Do not add a literal set here.
QA_STRIP_PROVIDERS: frozenset[str] = LOCAL_PROVIDERS | {"openai_compat"}


def _provider(entry: Any) -> str:
    """Provider of a chain entry: a ``ModelSpec`` (hook) or a ``provider/model`` string."""
    provider = getattr(entry, "provider", None)
    if provider is not None:
        return provider
    # Same rule as ``profiles.provider_from_model``, inlined to keep this import light.
    return entry.split("/")[0] if "/" in entry else "unknown"


def strip_local_for_qa(models: list, task_type: Any, *, keep_if_only_local: bool = True) -> list:
    """Strip local Q&A models while a non-Claude provider remains (D-14, D-114).

    With ``keep_if_only_local=True`` (MCP ``route_and_call``, also serving the
    gateway and route server; SDK; zero-Claude hook), retain locals before Claude
    when stripping leaves no non-Claude provider: D-114 follows the 2026-10-15
    env-doctor evidence of a fresh-process simple Q&A call failing on Haiku without an
    Anthropic API key despite installed Ollama and ``LLM_ROUTER_CLAUDE_SUBSCRIPTION=true``.

    With False (normal hook DIRECT path only), return the stripped chain even if
    empty or Claude-only; the turn falls through to the host Claude session,
    which needs no API key.
    Non-Q&A types pass through unchanged. ``task_type`` may be a ``TaskType`` or string.
    Relative order within the local and kept groups is preserved.

    The caller must not apply this to an explicit ``model_override``: that is the
    caller's own pin, not routing.
    """
    tt = getattr(task_type, "value", task_type)
    if tt not in QA_TASK_TYPES:
        return models
    kept = [m for m in models if _provider(m) not in QA_STRIP_PROVIDERS]
    if keep_if_only_local and not any(_provider(m) not in CLAUDE_PROVIDERS for m in kept):
        local = [m for m in models if _provider(m) in QA_STRIP_PROVIDERS]
        log.debug(
            "D-114: kept %d local model(s) as last resort in the %s chain", len(local), tt
        )
        return local + kept
    if len(kept) != len(models):
        log.debug("D-14: dropped %d local model(s) from the %s chain", len(models) - len(kept), tt)
    return kept
