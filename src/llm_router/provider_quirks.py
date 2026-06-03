"""Per-provider transformation hooks — Plan 07 Cat D.4 (extensibility).

D.1–D.3 patched provider-specific quirks inline in :mod:`llm_router.providers`:

* D.1 — Thinking models emit to ``message.reasoning`` when ``content`` is null.
* D.2 — ``max_tokens`` must be capped at the model's documented output limit.
* D.3 — Empty-content responses must surface as a routing failure, not a
  silent zero-token success.

Each of those patches lives at the right call-site for its symptom, but the
pattern is universal: every provider has at least one quirk, and the next
one we hit (the plan's example: OpenRouter dropping the ``anthropic/``
prefix from model names) shouldn't require another inline patch.

This module is the registry: a thin Protocol plus a name-keyed table. The
provider call-site looks up the active quirk by provider prefix and runs
three identity-by-default hooks around the LiteLLM call:

1. ``transform_model_name`` — fix-ups to the model identifier before it
   hits LiteLLM (e.g. re-prepending ``anthropic/`` for OpenRouter).
2. ``transform_request`` — payload tweaks (dropping ``max_tokens`` for
   Ollama, forcing ``temperature=1`` for OpenAI o-series).
3. ``transform_response`` — post-processing on the extracted content +
   usage metadata before it becomes an ``LLMResponse``.

Default :class:`IdentityQuirk` keeps every method a no-op so providers
without a registered quirk skip the entire hook chain at zero cost.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ProviderQuirk",
    "IdentityQuirk",
    "OpenAIReasoningQuirks",
    "OllamaQuirks",
    "OpenRouterQuirks",
    "register_quirk",
    "get_quirk",
    "registered_providers",
]


# ── Protocol ────────────────────────────────────────────────────────────────


@runtime_checkable
class ProviderQuirk(Protocol):
    """Provider-specific transformation hooks.

    All three methods are identity by default so concrete classes implement
    only the hook(s) they need. The caller invokes them unconditionally and
    pays only for the transforms that actually fire.
    """

    def transform_model_name(self, name: str) -> str:
        """Mutate the model identifier passed to LiteLLM."""

    def transform_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Mutate the kwargs dict sent to ``litellm.acompletion``."""

    def transform_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Mutate the post-call dict (content + usage + cost) before wrapping."""


# ── Default identity ───────────────────────────────────────────────────────


class IdentityQuirk:
    """Pass-through implementation registered for providers with no quirks.

    Lets the caller skip ``if quirk is not None`` branching at every hook
    site — a permanently-registered identity is cheaper than the guard.
    """

    def transform_model_name(self, name: str) -> str:
        return name

    def transform_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    def transform_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw


# ── Concrete quirks ─────────────────────────────────────────────────────────


class OpenAIReasoningQuirks:
    """OpenAI o-series / o1 / o3 / o4 reasoning models.

    Two known constraints:

    * ``temperature`` is ignored and must be 1; passing anything else
      historically returned a 400 error from the v2 endpoint.
    * These models bill heavily on reasoning tokens and benefit from a
      lower ``max_tokens`` ceiling so unbounded chains-of-thought don't
      drain credits silently. We don't override max_tokens here (callers
      already pass an intentional value) but flag it for future tuning.
    """

    def transform_model_name(self, name: str) -> str:
        return name

    def transform_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Detect the reasoning prefix; honour both raw and provider-prefixed forms.
        model_str = payload.get("model", "")
        short = model_str.split("/", 1)[-1] if "/" in model_str else model_str
        if not short.startswith(("o1", "o3", "o4")):
            return payload
        out = dict(payload)
        out["temperature"] = 1
        return out

    def transform_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw


class OllamaQuirks:
    """Ollama (local inference via LiteLLM).

    LiteLLM's Ollama transport has a long-standing bug where passing
    ``max_tokens`` truncates the generation at the *prompt* length instead
    of the completion length, producing empty responses. Dropping the key
    entirely lets Ollama generate naturally; quality stays acceptable
    because local models default to reasonable output lengths.
    """

    def transform_model_name(self, name: str) -> str:
        return name

    def transform_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = payload.get("model", "")
        if not model.startswith("ollama/"):
            return payload
        if "max_tokens" not in payload:
            return payload
        out = dict(payload)
        out.pop("max_tokens", None)
        return out

    def transform_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw


class OpenRouterQuirks:
    """OpenRouter universal-name resolution.

    Plan 07 D.4 calls this out specifically: when we resolve a universal
    name like ``"claude-sonnet-4-6"`` for OpenRouter, the leading
    ``"anthropic/"`` provider prefix is stripped during normalization but
    OpenRouter requires it to be present in the model field. Re-prepend
    it here so the universal-name resolution upstream doesn't need to
    know which downstream provider it's routing to.
    """

    _CLAUDE_PREFIX_NEEDED = ("anthropic/",)

    def transform_model_name(self, name: str) -> str:
        if name.startswith("claude-") and "/" not in name:
            return "anthropic/" + name
        return name

    def transform_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "model" in payload:
            transformed = self.transform_model_name(payload["model"])
            if transformed != payload["model"]:
                out = dict(payload)
                out["model"] = transformed
                return out
        return payload

    def transform_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw


# ── Registry ────────────────────────────────────────────────────────────────


_IDENTITY = IdentityQuirk()
_REGISTRY: dict[str, ProviderQuirk] = {
    "openai": OpenAIReasoningQuirks(),
    "ollama": OllamaQuirks(),
    "openrouter": OpenRouterQuirks(),
}


def register_quirk(provider: str, quirk: ProviderQuirk) -> None:
    """Register or replace the quirk for ``provider``.

    Replace-by-default mirrors :mod:`llm_router.benchmark`'s registry — tests
    that swap in fakes shouldn't need a tear-down helper, and the alternative
    (a separate ``override_quirk`` API) would just be a thinner version of
    the same call.
    """
    _REGISTRY[provider] = quirk


def get_quirk(provider: str) -> ProviderQuirk:
    """Return the registered quirk or the identity no-op.

    Returning :class:`IdentityQuirk` (rather than ``None``) lets call-sites
    invoke the hook chain unconditionally — no ``if quirk is not None``
    plumbing throughout :mod:`llm_router.providers`.
    """
    return _REGISTRY.get(provider, _IDENTITY)


def registered_providers() -> list[str]:
    """Sorted list of providers with a non-identity quirk registered."""
    return sorted(_REGISTRY)
