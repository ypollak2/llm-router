"""F6 (D-141 part C): can any provider on this machine serve a task type?

The UserPromptSubmit hook names one ``llm(task=...)`` door per prompt and, in
smart/hard enforcement, writes the pending state the PreToolUse hook holds tools
against until that door is called. When the router cannot serve the task
(``router.route_and_call`` raises "No providers available for <task>/<profile>"),
the call never succeeds and the hold can only be released by a dummy call to a
different door. The hook asks :func:`resolve` before it names a door.

Hook-safe: no network, no ``llm_router.config`` import (pydantic-settings,
~31-52 ms), no ``llm_router.router`` import. The inputs are cached state:

* the resource map, through :func:`map_reader.load_data` (fresh map only): its
  connected rows give the seats (claude, codex, gemini_cli), the Ollama models and
  the API keys :mod:`llm_router.discovery.keys` found (env, ``.env``, keychain);
* the API-key NAMES set in the process environment, into which the hook has
  already loaded the ``.env`` files through ``env_loader`` (this module reads no
  file itself, D-153). A value is only
  tested for being non-empty; it is never kept, logged or returned. This covers
  providers the map's key table does not list (xai, minimax, zhipu, ...), which
  ``config.available_providers`` does count;
* the chains in ``profiles.ROUTING_TABLE`` and the active policy, which the hook
  already imports for its selected-model display.

Fail-open direction (decided for F6): when availability is UNKNOWN (no map, or a
missing, corrupt, other-schema or stale one), :func:`servable` returns ``None``
and :func:`resolve` keeps the task, so the hook behaves exactly as before. Only a
fresh map can turn a directive off. Every approximation of the router's chain
filter below errs towards "servable":

* research: the research door routes at PREMIUM when no Perplexity key is set
  (``tools/text.py: llm_research``) and the router injects local models only on
  cheap tiers (``router._cheap_tier``), so local providers do not count; a
  Perplexity key does;
* query/analyze/generate/code: local providers and codex/gemini_cli count, as the
  router injects them into these chains;
* anthropic counts with an API key, or with ``LLM_ROUTER_CLAUDE_SUBSCRIPTION``
  set (the router's ``_claude_sub_ok``), never from a claude.ai login alone.

Known hole: a key resolved only through a secrets-vault backend for a provider
the map's key table does not list is not seen; such a task can look unservable.
"""
from __future__ import annotations

import os
import re

#: The text task types the hook names a door for. Anything else is not checked.
TEXT_TASKS: tuple[str, ...] = ("query", "research", "generate", "analyze", "code")

#: Nearest servable door for a task no provider can serve, tried in order. The
#: hook names the first one that is servable; with none, it names no door.
#: research -> query/analyze: same text-in/text-out shape, minus web grounding
#: (which an unservable research door could not provide either). code has no
#: fallback: a completion door for code would answer a different question.
FALLBACKS: dict[str, tuple[str, ...]] = {
    "research": ("query", "analyze"),
    "analyze": ("query",),
    "generate": ("query",),
    "query": ("analyze",),
    "code": (),
}

#: Reason codes for the hook's debug log and routing ledger (never prompt text).
SERVABLE = "servable"
UNKNOWN = "availability_unknown"
UNCHECKED = "task_not_checked"
FALLBACK = "F6_NO_PROVIDER_FALLBACK"
NO_PROVIDER = "F6_NO_PROVIDER_NO_DIRECTIVE"

_KEY_RE = re.compile(r"^([A-Z0-9]+)_API_KEY$")
_TRUTHY = ("1", "true", "yes", "on")
# Router-injected subprocess providers for these tasks (router.py: Codex / Gemini CLI
# injection, `_codex_eligible_tasks` / `_gemini_eligible_tasks`).
_INJECTED = frozenset({"codex", "gemini_cli"})


def _env_names() -> dict[str, str]:
    """Upper-cased setting name -> value, from the process environment ONLY.

    This module never opens a ``.env`` file itself (D-153: a raw read of a FIFO
    named ``.env`` in the cwd hung a prompt for >8 s). The hook has already merged
    the ``.env`` files into ``os.environ`` at import (``auto-route._load_dotenv`` ->
    ``env_loader.load_dotenv_files``: regular files only, SEC-002/003 allowlist for
    the cwd file). An in-process caller that wants the file keys too calls
    ``env_loader.load_dotenv_files()`` first.
    """
    return {name.upper(): value for name, value in os.environ.items()}


def available_providers() -> frozenset[str] | None:
    """Providers the router could dispatch to here; ``None`` when unknown (no fresh map)."""
    from llm_router import map_reader

    data = map_reader.load_data()
    if data is None:
        return None
    names = _env_names()
    claude_sub = names.get("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").strip().lower() in _TRUTHY
    found: set[str] = set()
    for row in data.get("resources") or ():
        if not isinstance(row, dict) or row.get("status") != "connected":
            continue
        provider = str(row.get("chain_id", "")).split("/", 1)[0]
        if provider == "anthropic" and row.get("kind") == "subscription" and not claude_sub:
            continue  # a claude.ai login alone does not put anthropic in the chain
        found.add(provider)
    for name, value in names.items():
        match = _KEY_RE.match(name)
        if match and value.strip():
            found.add(match.group(1).lower())
    if names.get("OPENAI_COMPAT_BASE_URL", "").strip():
        found.add("openai_compat")
    if claude_sub:
        found.add("anthropic")
    return frozenset(found)


def _policy_chains() -> dict:
    """The active policy's ``chains`` (profile -> task -> models); ``{}`` when it cannot be read."""
    try:
        from llm_router.policy import get_active_policy

        return dict(getattr(get_active_policy(), "chains", None) or {})
    except Exception:  # noqa: BLE001 -- the standard chains alone
        return {}


def candidate_providers(task: str, policy_chains: dict | None = None) -> frozenset[str]:
    """Providers whose models the router may try for *task* (over-inclusive).

    ``policy_chains``: the active policy's chains, when the caller already loaded them
    (:func:`resolve` loads once per call, not once per task it tries); None loads them here.
    """
    from llm_router.profiles import ROUTING_TABLE, provider_from_model
    from llm_router.types import LOCAL_PROVIDERS

    chains = [chain for (_profile, task_type), chain in ROUTING_TABLE.items()
              if task_type.value == task]
    if policy_chains is None:
        policy_chains = _policy_chains()
    for tasks in policy_chains.values():
        chain = tasks.get(task) if isinstance(tasks, dict) else None
        if chain:
            chains.append(list(chain))
    out = {provider_from_model(m) for chain in chains for m in chain}
    local = set(LOCAL_PROVIDERS) | {"openai_compat"}
    if task == "research":
        return frozenset((out - local) | {"perplexity"})
    return frozenset(out | local | _INJECTED)


def servable(
    task: str, available: frozenset[str] | None = None, policy_chains: dict | None = None,
) -> bool | None:
    """True/False for a checked task with a fresh map; ``None`` when unknown or unchecked."""
    if task not in TEXT_TASKS:
        return None
    if available is None:
        available = available_providers()
        if available is None:
            return None
    return bool(candidate_providers(task, policy_chains) & available)


def resolve(task: str) -> tuple[str | None, str]:
    """The door to name for *task*, and a reason code.

    ``(task, SERVABLE | UNKNOWN | UNCHECKED)``: name *task* as today.
    ``(fallback, FALLBACK)``: *task* is unservable; name the fallback instead.
    ``(None, NO_PROVIDER)``: neither *task* nor any fallback is servable; name none.
    Any error is UNKNOWN: this check must never cost a turn its directive.
    """
    try:
        if task not in TEXT_TASKS:
            return task, UNCHECKED
        available = available_providers()
        if available is None:
            return task, UNKNOWN
        chains = _policy_chains()  # one policy read for the task and every fallback
        if servable(task, available, chains):
            return task, SERVABLE
        for fallback in FALLBACKS.get(task, ()):
            if servable(fallback, available, chains):
                return fallback, FALLBACK
        return None, NO_PROVIDER
    except Exception:  # noqa: BLE001 -- fail open to today's directive
        return task, UNKNOWN
