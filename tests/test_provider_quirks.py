"""Plan 07 Cat D.4 — provider-quirk registry tests.

Covers the Protocol contract, the identity default, each bundled concrete
quirk (OpenAI reasoning, Ollama, OpenRouter), and the registry surface.
The integration into ``providers.call_llm`` is covered indirectly by the
existing router/provider tests; we don't double-test the LiteLLM call site
here.
"""

from __future__ import annotations

from llm_router.provider_quirks import (
    AnthropicPxpipeQuirk,
    IdentityQuirk,
    OllamaQuirks,
    OpenAIReasoningQuirks,
    OpenRouterQuirks,
    ProviderQuirk,
    get_quirk,
    register_quirk,
    registered_providers,
)


# ── Identity default ────────────────────────────────────────────────────────


class TestIdentityQuirk:
    """Default no-op used when no provider-specific quirk is registered."""

    def test_implements_protocol(self):
        """Runtime-checkable Protocol must pass isinstance."""
        assert isinstance(IdentityQuirk(), ProviderQuirk)

    def test_all_methods_are_no_ops(self):
        q = IdentityQuirk()
        assert q.transform_model_name("anything") == "anything"
        payload = {"model": "x", "max_tokens": 1, "temperature": 0.5}
        assert q.transform_request(payload) == payload
        raw = {"content": "y", "tokens": 100}
        assert q.transform_response(raw) == raw

    def test_request_transform_does_not_mutate(self):
        """Caller's dict must survive an identity transform untouched."""
        original = {"model": "x", "temperature": 0.5}
        q = IdentityQuirk()
        q.transform_request(original)
        assert original == {"model": "x", "temperature": 0.5}


# ── OpenAI reasoning models ─────────────────────────────────────────────────


class TestOpenAIReasoningQuirks:
    """o1/o3/o4 require temperature=1; anything else returns a 400."""

    def test_o3_forces_temperature_one(self):
        q = OpenAIReasoningQuirks()
        out = q.transform_request({"model": "openai/o3", "temperature": 0.7})
        assert out["temperature"] == 1

    def test_o1_with_provider_prefix(self):
        """Prefix-tolerant: works whether or not the provider prefix is present."""
        q = OpenAIReasoningQuirks()
        assert q.transform_request({"model": "o1-preview", "temperature": 0})["temperature"] == 1

    def test_gpt_4o_unchanged(self):
        """Non-reasoning OpenAI models keep their requested temperature."""
        q = OpenAIReasoningQuirks()
        out = q.transform_request({"model": "openai/gpt-4o", "temperature": 0.5})
        assert out["temperature"] == 0.5

    def test_does_not_mutate_input(self):
        """Quirk must build a new dict, not mutate the caller's."""
        original = {"model": "openai/o3", "temperature": 0.5}
        OpenAIReasoningQuirks().transform_request(original)
        assert original["temperature"] == 0.5


# ── Ollama ──────────────────────────────────────────────────────────────────


class TestOllamaQuirks:
    """LiteLLM's Ollama transport breaks when ``max_tokens`` is set."""

    def test_strips_max_tokens(self):
        q = OllamaQuirks()
        out = q.transform_request({"model": "ollama/qwen", "max_tokens": 500})
        assert "max_tokens" not in out

    def test_keeps_other_params(self):
        """Only ``max_tokens`` is stripped; everything else passes through."""
        q = OllamaQuirks()
        out = q.transform_request(
            {"model": "ollama/qwen", "max_tokens": 500, "temperature": 0.5,
             "messages": [{"role": "user", "content": "hi"}]}
        )
        assert out["temperature"] == 0.5
        assert out["messages"] == [{"role": "user", "content": "hi"}]

    def test_non_ollama_model_unchanged(self):
        """Other providers keep ``max_tokens`` — the bug is Ollama-only."""
        q = OllamaQuirks()
        payload = {"model": "openai/gpt-4o", "max_tokens": 500}
        assert q.transform_request(payload) == payload

    def test_does_not_mutate_input(self):
        original = {"model": "ollama/qwen", "max_tokens": 500}
        OllamaQuirks().transform_request(original)
        assert original["max_tokens"] == 500


# ── OpenRouter (plan-spec example) ──────────────────────────────────────────


class TestOpenRouterQuirks:
    """OpenRouter needs the anthropic/ prefix that universal-name resolution strips."""

    def test_reprepends_anthropic_for_bare_claude(self):
        """The plan's headline example: ``claude-sonnet-4-6`` → ``anthropic/claude-sonnet-4-6``."""
        q = OpenRouterQuirks()
        assert q.transform_model_name("claude-sonnet-4-6") == "anthropic/claude-sonnet-4-6"

    def test_leaves_prefixed_names_alone(self):
        """Already-prefixed names must not double-prefix."""
        q = OpenRouterQuirks()
        assert q.transform_model_name("anthropic/claude-sonnet-4-6") == "anthropic/claude-sonnet-4-6"

    def test_other_providers_untouched(self):
        """Non-claude prefixes stay byte-identical."""
        q = OpenRouterQuirks()
        assert q.transform_model_name("qwen/qwen3-235b") == "qwen/qwen3-235b"
        assert q.transform_model_name("google/gemini-1.5-pro") == "google/gemini-1.5-pro"

    def test_transform_request_applies_name_fix(self):
        q = OpenRouterQuirks()
        out = q.transform_request({"model": "claude-haiku-4-5", "temperature": 0.5})
        assert out["model"] == "anthropic/claude-haiku-4-5"
        assert out["temperature"] == 0.5

    def test_transform_request_skips_when_already_prefixed(self):
        """Already-prefixed name must avoid a needless dict copy."""
        q = OpenRouterQuirks()
        payload = {"model": "anthropic/claude-haiku-4-5", "temperature": 0.5}
        # Same reference — no copy when no transform applies.
        assert q.transform_request(payload) is payload


class TestAnthropicPxpipeQuirk:
    """Route heavy-model Anthropic calls through a local pxpipe proxy."""

    def _configure(self, monkeypatch, *, enabled, heavy_models="claude-fable-5",
                    url="http://127.0.0.1:47821", reachable=True):
        import llm_router.config as config_module

        class _FakeConfig:
            llm_router_pxpipe_enabled = enabled
            llm_router_pxpipe_heavy_models = heavy_models
            llm_router_pxpipe_url = url

        monkeypatch.setattr(config_module, "get_config", lambda: _FakeConfig())
        monkeypatch.setattr(config_module, "probe_pxpipe", lambda _url: reachable)

    def test_disabled_by_default_is_noop(self, monkeypatch):
        """Opt-in flag off (the default) — heavy model, no injection."""
        self._configure(monkeypatch, enabled=False)
        q = AnthropicPxpipeQuirk()
        payload = {"model": "anthropic/claude-fable-5"}
        assert q.transform_request(payload) is payload

    def test_enabled_and_reachable_injects_api_base_for_heavy_model(self, monkeypatch):
        """Enabled + proxy reachable + model on the heavy list -> api_base set."""
        self._configure(monkeypatch, enabled=True, reachable=True)
        q = AnthropicPxpipeQuirk()
        payload = {"model": "anthropic/claude-fable-5", "messages": []}
        out = q.transform_request(payload)
        assert out["api_base"] == "http://127.0.0.1:47821"
        assert out is not payload  # copy, not mutation

    def test_non_heavy_model_unaffected(self, monkeypatch):
        """A cheap model not on the heavy list must never take the extra hop."""
        self._configure(monkeypatch, enabled=True, reachable=True)
        q = AnthropicPxpipeQuirk()
        payload = {"model": "anthropic/claude-haiku-4-5"}
        assert q.transform_request(payload) is payload

    def test_proxy_unreachable_falls_back_silently(self, monkeypatch):
        """No pxpipe running locally -> no injection, no error."""
        self._configure(monkeypatch, enabled=True, reachable=False)
        q = AnthropicPxpipeQuirk()
        payload = {"model": "anthropic/claude-fable-5"}
        assert q.transform_request(payload) is payload

    def test_respects_heavy_models_list_override(self, monkeypatch):
        """A custom heavy-model list is honored, not just the default."""
        self._configure(monkeypatch, enabled=True, heavy_models="gpt-5.6, claude-opus-4-8")
        q = AnthropicPxpipeQuirk()
        assert q.transform_request({"model": "anthropic/claude-opus-4-8"})["api_base"]
        # claude-fable-5 is the DEFAULT list, not this custom one — must be untouched.
        payload = {"model": "anthropic/claude-fable-5"}
        assert q.transform_request(payload) is payload

    def test_does_not_overwrite_existing_api_base(self, monkeypatch):
        """A caller-set api_base (e.g. from a different quirk/override) always wins."""
        self._configure(monkeypatch, enabled=True, reachable=True)
        q = AnthropicPxpipeQuirk()
        payload = {"model": "anthropic/claude-fable-5", "api_base": "http://custom"}
        assert q.transform_request(payload) is payload

    def test_transform_model_name_is_identity(self):
        """pxpipe intercepts by wire format at a fixed URL — the model string
        Anthropic itself would see must stay untouched."""
        q = AnthropicPxpipeQuirk()
        assert q.transform_model_name("anthropic/claude-fable-5") == "anthropic/claude-fable-5"


# ── Registry ────────────────────────────────────────────────────────────────


class TestRegistry:
    """Registration surface used by providers.call_llm."""

    def test_default_quirks_pre_registered(self):
        """openai/ollama/openrouter come pre-wired."""
        names = registered_providers()
        assert "openai" in names
        assert "ollama" in names
        assert "openrouter" in names

    def test_unknown_provider_returns_identity(self):
        """Identity is the universal fallback — call-sites never branch on None."""
        q = get_quirk("some_unmapped_provider")
        # Identity must be a no-op for every method.
        assert q.transform_model_name("x") == "x"
        payload = {"a": 1}
        assert q.transform_request(payload) is payload
        raw = {"b": 2}
        assert q.transform_response(raw) is raw

    def test_register_overwrites(self):
        """Replace-by-default: tests need to swap fakes without a teardown."""

        class _Fake:
            calls: list[str]

            def __init__(self):
                self.calls = []

            def transform_model_name(self, name: str) -> str:
                self.calls.append("model")
                return name

            def transform_request(self, payload):
                self.calls.append("request")
                return payload

            def transform_response(self, raw):
                return raw

        original = get_quirk("openai")
        try:
            fake = _Fake()
            register_quirk("openai", fake)
            assert get_quirk("openai") is fake
        finally:
            register_quirk("openai", original)

    def test_register_new_provider(self):
        """Adding a new provider quirk requires no edits to providers.py."""
        try:
            register_quirk("newprov", IdentityQuirk())
            assert "newprov" in registered_providers()
        finally:
            # Tidy up so the registry doesn't leak into other tests.
            from llm_router.provider_quirks import _REGISTRY
            _REGISTRY.pop("newprov", None)


# ── Smoke: integration point ────────────────────────────────────────────────


class TestProvidersIntegration:
    """Pin the provider call-site against the quirk registry.

    We don't run a real LiteLLM call here (covered elsewhere). We just check
    the right helper is imported at the right place so a future refactor
    can't quietly bypass the quirks.
    """

    def test_call_llm_imports_quirks(self):
        """If the import disappears, the quirk layer silently turns off."""
        from pathlib import Path

        src = (Path(__file__).parent.parent / "src" / "llm_router" / "providers.py").read_text()
        assert "from llm_router.provider_quirks import get_quirk" in src
        assert "_quirk.transform_request(kwargs)" in src
        # Both the sync and streaming call sites must apply the hook.
        assert src.count("_quirk.transform_request(kwargs)") >= 2
