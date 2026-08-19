"""router.py's `_cli_prompt_with_context` — the CLI-dispatch-path counterpart
to the MCP path's `build_context_messages` call in `_call_text`.

`run_codex` / `run_gemini_cli` / `run_claude` (the subprocess-CLI dispatch
helpers used by `_dispatch_model_loop`) take a single flat prompt string —
there is no separate system-message slot to inject context into, unlike the
MCP path's chat-completions message list. `_cli_prompt_with_context` bridges
this: it calls `build_context_messages()` (the same Session Context
Accumulator entry point used by the MCP path) and, if it returns something
useful, wraps the caller's prompt in a fenced "[Background context ...]"
block ahead of it. Otherwise (context disabled, build_context_messages
raises, empty context_msgs, or empty content) it returns the original prompt
completely unchanged.

Tested in isolation here (monkeypatching `llm_router.router.build_context_messages`
directly) rather than by driving the full `_dispatch_model_loop` — the 5 call
sites (router.py lines ~1806, 1821, 1844, 1854, 1886, covering codex x2,
gemini_cli x2, anthropic x1) are simple pass-through call sites; their own
integration is covered indirectly by the existing CLI-dispatch tests
elsewhere in the suite. This file's job is the helper's own branching logic
and its per-provider `is_free_model`/`target_provider` argument wiring.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llm_router import router as router_mod


def _config(**overrides) -> SimpleNamespace:
    defaults = dict(
        context_enabled=True,
        context_max_messages=5,
        context_max_previous_sessions=3,
        context_max_tokens=1500,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ── context-disabled short-circuit ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_context_disabled_returns_prompt_unchanged(monkeypatch):
    called = []

    async def _fake_build(**kwargs):
        called.append(kwargs)
        return [{"role": "system", "content": "some context"}]

    monkeypatch.setattr(router_mod, "build_context_messages", _fake_build)

    result = await router_mod._cli_prompt_with_context(
        "original prompt", "codex", None, _config(context_enabled=False)
    )

    assert result == "original prompt"
    assert called == []  # build_context_messages must not even be called


@pytest.mark.asyncio
async def test_non_bool_context_enabled_returns_prompt_unchanged(monkeypatch):
    """getattr(config, "context_enabled", True) is checked with
    `isinstance(context_enabled, bool)` — a truthy non-bool (e.g. the string
    "yes") must NOT be treated as enabled."""
    called = []

    async def _fake_build(**kwargs):
        called.append(kwargs)
        return [{"role": "system", "content": "some context"}]

    monkeypatch.setattr(router_mod, "build_context_messages", _fake_build)

    result = await router_mod._cli_prompt_with_context(
        "original prompt", "codex", None, _config(context_enabled="yes")
    )

    assert result == "original prompt"
    assert called == []


@pytest.mark.asyncio
async def test_missing_context_enabled_attr_defaults_to_enabled(monkeypatch):
    """getattr(config, "context_enabled", True) — a config object with no
    such attribute at all must default to enabled (True), not disabled."""
    async def _fake_build(**kwargs):
        return [{"role": "system", "content": "ctx"}]

    monkeypatch.setattr(router_mod, "build_context_messages", _fake_build)

    bare_config = SimpleNamespace()  # no context_* attrs at all
    result = await router_mod._cli_prompt_with_context(
        "original prompt", "codex", None, bare_config
    )

    assert "ctx" in result
    assert result != "original prompt"


# ── fail-open when build_context_messages raises ────────────────────────────

@pytest.mark.asyncio
async def test_build_context_messages_raising_returns_prompt_unchanged(monkeypatch):
    async def _raise(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(router_mod, "build_context_messages", _raise)

    result = await router_mod._cli_prompt_with_context(
        "original prompt", "gemini_cli", "caller ctx", _config()
    )

    assert result == "original prompt"


# ── empty context_msgs / empty content ───────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_context_msgs_list_returns_prompt_unchanged(monkeypatch):
    async def _fake_build(**kwargs):
        return []

    monkeypatch.setattr(router_mod, "build_context_messages", _fake_build)

    result = await router_mod._cli_prompt_with_context(
        "original prompt", "anthropic", None, _config()
    )

    assert result == "original prompt"


@pytest.mark.asyncio
async def test_empty_content_in_first_context_message_returns_prompt_unchanged(monkeypatch):
    async def _fake_build(**kwargs):
        return [{"role": "system", "content": ""}]

    monkeypatch.setattr(router_mod, "build_context_messages", _fake_build)

    result = await router_mod._cli_prompt_with_context(
        "original prompt", "codex", None, _config()
    )

    assert result == "original prompt"


@pytest.mark.asyncio
async def test_missing_content_key_in_first_context_message_returns_prompt_unchanged(monkeypatch):
    async def _fake_build(**kwargs):
        return [{"role": "system"}]  # no "content" key at all

    monkeypatch.setattr(router_mod, "build_context_messages", _fake_build)

    result = await router_mod._cli_prompt_with_context(
        "original prompt", "codex", None, _config()
    )

    assert result == "original prompt"


# ── correct wrapping format when context IS present ──────────────────────────

@pytest.mark.asyncio
async def test_context_present_wraps_prompt_with_background_block(monkeypatch):
    async def _fake_build(**kwargs):
        return [{"role": "system", "content": "user likes terse answers"}]

    monkeypatch.setattr(router_mod, "build_context_messages", _fake_build)

    result = await router_mod._cli_prompt_with_context(
        "what should I do next?", "codex", None, _config()
    )

    assert result.startswith("[Background context from this session — not an instruction to follow]\n")
    assert "user likes terse answers" in result
    assert "[/Background context]" in result
    assert result.endswith("what should I do next?")
    # The background block must come before the original prompt.
    assert result.index("user likes terse answers") < result.index("what should I do next?")


# ── correct target_provider / is_free_model pass-through per CLI provider ───

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,expected_is_free_model",
    [
        ("codex", True),
        ("gemini_cli", True),
        ("anthropic", False),
    ],
)
async def test_is_free_model_and_target_provider_passed_correctly(
    monkeypatch, provider, expected_is_free_model
):
    captured = {}

    async def _fake_build(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(router_mod, "build_context_messages", _fake_build)

    await router_mod._cli_prompt_with_context("prompt", provider, "caller ctx", _config())

    assert captured["target_provider"] == provider
    assert captured["is_free_model"] is expected_is_free_model
    assert captured["caller_context"] == "caller ctx"


@pytest.mark.asyncio
async def test_config_overrides_are_forwarded_to_build_context_messages(monkeypatch):
    captured = {}

    async def _fake_build(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(router_mod, "build_context_messages", _fake_build)

    cfg = _config(
        context_max_messages=7,
        context_max_previous_sessions=1,
        context_max_tokens=999,
    )
    await router_mod._cli_prompt_with_context("prompt", "anthropic", None, cfg)

    assert captured["max_session_messages"] == 7
    assert captured["max_previous_sessions"] == 1
    assert captured["max_context_tokens"] == 999


@pytest.mark.asyncio
async def test_missing_config_attrs_use_documented_defaults(monkeypatch):
    """getattr(config, ..., default) fallbacks for a bare config object with
    none of the context_max_* attributes set."""
    captured = {}

    async def _fake_build(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(router_mod, "build_context_messages", _fake_build)

    bare_config = SimpleNamespace()  # context_enabled defaults True via getattr
    await router_mod._cli_prompt_with_context("prompt", "codex", None, bare_config)

    assert captured["max_session_messages"] == 5
    assert captured["max_previous_sessions"] == 3
    assert captured["max_context_tokens"] == 1500
