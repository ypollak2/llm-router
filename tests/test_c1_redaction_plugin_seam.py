"""C-1 Plugin seam: redaction inversion of control.

Tests that redaction_routing.py no longer imports llm_router.enterprise.redaction
directly. Instead, it calls get_redactor() from the plugin registry.
Enterprise code registers a concrete redactor at bootstrap time.
"""
from __future__ import annotations

import os

import pytest

from llm_router.plugins.redaction import (
    Redactor,
    RedactionResult,
    get_redactor,
    register_redactor,
)
from llm_router.redaction_routing import RedactionUnavailable, maybe_redact


# Test utilities

class MockRedactor(Redactor):
    """Test redactor that prefixes with [MOCK]."""

    def redact_prompt(self, prompt: str) -> RedactionResult:
        # Match enterprise redaction behavior: empty prompts return unchanged
        if not prompt:
            return RedactionResult(text=prompt, counts={}, any_redactions=False)
        return RedactionResult(
            text=f"[MOCK] {prompt}",
            counts={"mock": 1},
            any_redactions=True,
        )


class FailingRedactor(Redactor):
    """Test redactor that always fails."""

    def redact_prompt(self, prompt: str) -> RedactionResult:
        raise ValueError("redactor is broken")


# Tests

class TestPluginRegistry:
    """Test the redactor plugin registry mechanics."""

    def setup_method(self):
        """Clear registry before each test."""
        # Access private dict to clear it
        import llm_router.plugins.redaction as r
        r._REDACTORS.clear()

    def test_register_and_get_redactor(self):
        """Redactors can be registered and retrieved."""
        mock = MockRedactor()
        register_redactor(mock)
        assert get_redactor() is mock

    def test_get_redactor_when_not_registered(self):
        """get_redactor() returns None when nothing is registered."""
        assert get_redactor() is None

    def test_register_named_redactor(self):
        """Redactors can be registered with custom names."""
        mock = MockRedactor()
        register_redactor(mock, name="custom")
        assert get_redactor(name="custom") is mock
        assert get_redactor() is None  # default is still empty


class TestMaybeRedactNoPlugin:
    """Test maybe_redact behavior when no plugin is registered."""

    def setup_method(self):
        """Clear registry and disable redaction env."""
        import llm_router.plugins.redaction as r
        r._REDACTORS.clear()
        os.environ.pop("LLM_ROUTER_REDACTION", None)

    def test_redaction_off_no_plugin(self):
        """LLM_ROUTER_REDACTION=off returns prompt unchanged, no plugin needed."""
        os.environ["LLM_ROUTER_REDACTION"] = "off"
        prompt = "secret api key sk-ant-12345"
        text, counts = maybe_redact(prompt)
        assert text == prompt
        assert counts == {}

    def test_redaction_on_but_no_plugin_fails_closed(self):
        """LLM_ROUTER_REDACTION=on with no plugin REFUSES rather than sending the prompt.

        Was `..._fails_open`, asserting the prompt came back unchanged. Enabled but
        unconfigured is the worst of both: the operator asked for redaction and got
        none, with only a log line to say so. Note the prompt here is an API key —
        the assertion it used to make was that the key goes out.
        """
        os.environ["LLM_ROUTER_REDACTION"] = "on"
        prompt = "secret api key sk-ant-12345"
        with pytest.raises(RedactionUnavailable):
            maybe_redact(prompt)


class TestMaybeRedactWithPlugin:
    """Test maybe_redact behavior when a plugin is registered."""

    def setup_method(self):
        """Clear registry and register a test redactor."""
        import llm_router.plugins.redaction as r
        r._REDACTORS.clear()
        os.environ.pop("LLM_ROUTER_REDACTION", None)
        register_redactor(MockRedactor())

    def test_redaction_off_ignores_plugin(self):
        """LLM_ROUTER_REDACTION=off skips redaction even if plugin is registered."""
        os.environ["LLM_ROUTER_REDACTION"] = "off"
        prompt = "test prompt"
        text, counts = maybe_redact(prompt)
        assert text == prompt
        assert counts == {}

    def test_redaction_on_uses_plugin(self):
        """LLM_ROUTER_REDACTION=on uses the registered plugin."""
        os.environ["LLM_ROUTER_REDACTION"] = "on"
        prompt = "test prompt"
        text, counts = maybe_redact(prompt)
        assert text == "[MOCK] test prompt"
        assert counts == {"mock": 1}

    def test_redaction_on_empty_prompt(self):
        """Redaction skips empty prompts."""
        os.environ["LLM_ROUTER_REDACTION"] = "on"
        text, counts = maybe_redact("")
        assert text == ""
        assert counts == {}


class TestMaybeRedactFailureHandling:
    """Test that broken redactors fail CLOSED."""

    def setup_method(self):
        """Clear registry and register a failing redactor."""
        import llm_router.plugins.redaction as r
        r._REDACTORS.clear()
        os.environ.pop("LLM_ROUTER_REDACTION", None)
        register_redactor(FailingRedactor())

    def teardown_method(self):
        """Clear registry after test to avoid pollution."""
        import llm_router.plugins.redaction as r
        r._REDACTORS.clear()

    def test_failing_redactor_fails_closed(self):
        """If plugin.redact_prompt() raises, REFUSE — do not return the prompt.

        Was `..._fails_open`. Inverted with the production change: returning the
        prompt when the scrub failed is the leak, not the recovery.
        """
        os.environ["LLM_ROUTER_REDACTION"] = "on"
        prompt = "test prompt"
        with pytest.raises(RedactionUnavailable):
            maybe_redact(prompt)


class TestEnterpriseBootstrap:
    """Test that enterprise bootstrap registers the redactor correctly."""

    def setup_method(self):
        """Ensure registry has enterprise redactor."""
        import llm_router.plugins.redaction as r
        r._REDACTORS.clear()
        # Re-import enterprise to re-run bootstrap
        import importlib
        import llm_router.enterprise
        importlib.reload(llm_router.enterprise)

    def test_enterprise_bootstrap_registers_redactor(self):
        """Enterprise bootstrap registers redactor on module import."""
        # After reload, redactor should be registered
        redactor = get_redactor()
        assert redactor is not None

    def test_enterprise_redactor_redacts_api_keys(self):
        """Enterprise redactor should redact known API key patterns."""
        os.environ["LLM_ROUTER_REDACTION"] = "on"
        prompt = "Use this key: sk-ant-abcd1234efgh5678ijkl9012"
        text, counts = maybe_redact(prompt)

        # Should contain redaction marker
        assert "[REDACTED:" in text
        assert "anthropic_key" in counts
