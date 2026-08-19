"""G-004 — prefix-spoof closure in ``rbac_routing.check_model``.

Pre-fix, a forged candidate ``anthropic/openai-gpt-4o`` matched an
allow-list entry ``openai-gpt-4o`` because the implementation
stripped the provider prefix off both sides before comparing. The
strip discarded the very provider information the bare entry was
implicitly trusting.

Post-fix:

* A ``provider/model`` candidate matches only a prefixed allow-list
  entry whose ``provider/model`` is identical (case-insensitive).
* A bare candidate matches only a bare entry whose value matches
  (case-insensitive).
* The two forms NEVER cross-match — the bug class is gone.

These tests pin both the security closure and the legitimate
operator workflows that still work (prefixed entries, bare entries,
case-insensitive matching).
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from llm_router.rbac_routing import check_model



@dataclass
class FakeIdentity:
    """Minimal stand-in for TurnIdentity carrying ``allowed_models``."""
    user_id: str = "u"
    org_id: str = "o"
    tenant_id: str | None = None
    allowed_models: tuple[str, ...] | None = None


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    monkeypatch.delenv("LLM_ROUTER_RBAC_MODE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROFILE", raising=False)
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "strict")


# ── 1. Adversarial closure (the actual G-004 attack) ────────────────────────


def test_forged_prefix_does_not_match_bare_entry() -> None:
    """The core G-004 spoof. Pre-fix this returned ``True`` because
    ``"anthropic/openai-gpt-4o".split("/")[1]`` == ``"openai-gpt-4o"``."""
    identity = FakeIdentity(allowed_models=("openai-gpt-4o",))
    _, allowed = check_model(identity, "anthropic/openai-gpt-4o")
    assert allowed is False


def test_forged_prefix_does_not_match_bare_entry_different_casing() -> None:
    identity = FakeIdentity(allowed_models=("openai-gpt-4o",))
    _, allowed = check_model(identity, "Anthropic/OpenAI-GPT-4o")
    assert allowed is False


def test_swapped_provider_does_not_match_prefixed_entry() -> None:
    """A forged candidate ``ollama/sonnet`` must NOT match a prefixed
    allow-list entry ``anthropic/sonnet``."""
    identity = FakeIdentity(allowed_models=("anthropic/sonnet",))
    _, allowed = check_model(identity, "ollama/sonnet")
    assert allowed is False


# ── 2. Legitimate workflows still work ──────────────────────────────────────


def test_prefixed_entry_matches_exact_prefixed_candidate() -> None:
    identity = FakeIdentity(allowed_models=("anthropic/claude-sonnet-4-6",))
    _, allowed = check_model(identity, "anthropic/claude-sonnet-4-6")
    assert allowed is True


def test_prefixed_entry_case_insensitive() -> None:
    identity = FakeIdentity(allowed_models=("Anthropic/Claude-Sonnet-4-6",))
    _, allowed = check_model(identity, "anthropic/claude-sonnet-4-6")
    assert allowed is True


def test_bare_entry_matches_bare_candidate() -> None:
    """An admin who deliberately writes a bare entry ``local-model``
    (a model id with no provider prefix at all) gets that exact match."""
    identity = FakeIdentity(allowed_models=("local-model",))
    _, allowed = check_model(identity, "local-model")
    assert allowed is True


def test_prefixed_candidate_against_only_bare_allow_list_denied() -> None:
    """A prefixed candidate never falls back to bare matching."""
    identity = FakeIdentity(allowed_models=("claude-sonnet-4-6",))
    _, allowed = check_model(identity, "anthropic/claude-sonnet-4-6")
    assert allowed is False


def test_bare_candidate_against_only_prefixed_allow_list_denied() -> None:
    """A bare candidate never falls back to a prefixed entry."""
    identity = FakeIdentity(allowed_models=("anthropic/claude-sonnet-4-6",))
    _, allowed = check_model(identity, "claude-sonnet-4-6")
    assert allowed is False


def test_mixed_allow_list_matches_correct_form() -> None:
    """Admin lists both forms — each form matches only itself."""
    identity = FakeIdentity(allowed_models=(
        "anthropic/claude-sonnet-4-6",
        "local-model",
    ))
    # Prefixed candidate matches the prefixed entry only.
    _, claude_ok = check_model(identity, "anthropic/claude-sonnet-4-6")
    assert claude_ok is True
    # Bare candidate matches the bare entry only.
    _, local_ok = check_model(identity, "local-model")
    assert local_ok is True
    # A forged hybrid is still denied.
    _, spoof_ok = check_model(identity, "ollama/claude-sonnet-4-6")
    assert spoof_ok is False


# ── 3. Off / warn modes preserved ───────────────────────────────────────────


def test_off_mode_returns_true_regardless(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "off")
    identity = FakeIdentity(allowed_models=("openai-gpt-4o",))
    _, allowed = check_model(identity, "anthropic/openai-gpt-4o")
    assert allowed is True  # off mode never enforces


def test_no_allow_list_returns_true() -> None:
    """``allowed_models`` is ``None`` → legacy allow-all behaviour."""
    identity = FakeIdentity(allowed_models=None)
    _, allowed = check_model(identity, "anthropic/anything")
    assert allowed is True


def test_warn_mode_returns_actual_decision(monkeypatch) -> None:
    """Warn mode surfaces the real decision without raising. The
    caller decides whether to log+allow or deny."""
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "warn")
    identity = FakeIdentity(allowed_models=("openai-gpt-4o",))
    mode, allowed = check_model(identity, "anthropic/openai-gpt-4o")
    assert mode == "warn"
    assert allowed is False
