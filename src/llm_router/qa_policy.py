"""D-14/D-114/D-115/D-116: local Q&A fallback after non-Claude providers, before Claude.

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
    """Order local Q&A fallbacks after the last non-Claude entry (D-116).

    D-114/D-115 amended D-14 for MCP ``route_and_call`` (including gateway and
    route server), SDK, and zero-Claude hook callers. ``keep_if_only_local=True``
    now retains locals even when a non-Claude provider is listed: insert them
    after the last non-Claude entry in the non-local chain, or at the start if
    none exists. Relative order within the local and kept groups is preserved.
    A listed provider may fail at call time, so local ($0) is tried before the
    trailing Claude entries. A weaker local answer may win over a working Claude.

    With False (normal hook DIRECT path only), strip all local entries even if
    the chain becomes empty or Claude-only; the host Claude session handles the
    turn without an API key. Non-Q&A types pass through unchanged. ``task_type``
    may be a ``TaskType`` or string. No local entries means the chain is unchanged.

    The caller must not apply this to an explicit ``model_override``: that is the
    caller's own pin, not routing.
    """
    tt = getattr(task_type, "value", task_type)
    if tt not in QA_TASK_TYPES:
        return models
    kept = [m for m in models if _provider(m) not in QA_STRIP_PROVIDERS]
    local = [m for m in models if _provider(m) in QA_STRIP_PROVIDERS]
    if not local:
        return models
    if keep_if_only_local:
        position = max(
            (i for i, m in enumerate(kept) if _provider(m) not in CLAUDE_PROVIDERS),
            default=-1,
        ) + 1
        log.debug(
            "D-116: re-inserted %d local model(s) at position %d in the %s chain",
            len(local), position, tt,
        )
        return kept[:position] + local + kept[position:]
    if len(kept) != len(models):
        log.debug("D-14: dropped %d local model(s) from the %s chain", len(models) - len(kept), tt)
    return kept
