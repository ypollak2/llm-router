"""G-039 — self-reference bypass refused under enterprise profile.

The README-documented "prompts mentioning llm_router + debug keywords
skip enforcement" bypass exists for a legitimate developer reason:
when llm_router itself is broken, routing creates a circular
dependency where the broken router blocks the tools needed to
repair it. For developer / single-user mode this is the right
trade-off — a debugging human can read the README and recognise the
escape hatch.

Under ``LLM_ROUTER_PROFILE=enterprise`` the trade-off flips. A
llm_router-flavoured prompt should not route as un-audited just because
an attacker (or a confused agent) chose the right phrasing. G-039
closes this by *refusing* the bypass under enterprise profile while
logging the attempted match for forensics. Normal routing proceeds.

Developer profile preserves the pre-G-039 behaviour exactly — no
behaviour change on upgrade for existing dev installs.

These tests import the hook script's helpers via ``importlib``
because ``auto-route.py`` uses a hyphenated filename that ``import``
cannot resolve directly.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def hook_module():
    """Load ``src/llm_router/hooks/auto-route.py`` as a module."""
    repo_root = Path(__file__).resolve().parent.parent
    hook_path = repo_root / "src" / "llm_router" / "hooks" / "auto-route.py"
    assert hook_path.is_file(), hook_path
    spec = importlib.util.spec_from_file_location(
        "_llm_router_auto_route_test", hook_path
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    monkeypatch.delenv("LLM_ROUTER_PROFILE", raising=False)


# ── 1. Inline enterprise detector mirrors llm_router.profile ───────────────────


def test_developer_profile_not_enterprise(hook_module) -> None:
    assert hook_module._is_enterprise_profile() is False


def test_enterprise_profile_detected(hook_module, monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    assert hook_module._is_enterprise_profile() is True


def test_enterprise_aliases_detected(hook_module, monkeypatch) -> None:
    """The aliases (``prod``, ``production``, casing variants) must
    track ``llm_router.profile`` exactly so a profile flip on one path
    can't be silently bypassed on the other."""
    for value in ("enterprise", "ENTERPRISE", "prod", "production"):
        monkeypatch.setenv("LLM_ROUTER_PROFILE", value)
        assert hook_module._is_enterprise_profile() is True, value


def test_unknown_profile_falls_back_to_not_enterprise(
    hook_module, monkeypatch
) -> None:
    """A typo in LLM_ROUTER_PROFILE must not silently enable the bypass
    gate's enterprise refusal — it falls back to developer behaviour
    (bypass allowed) just like ``llm_router.profile.resolve_profile``.
    Both modules' typo-handling must agree."""
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "entrprise")  # typo
    assert hook_module._is_enterprise_profile() is False


# ── 2. Pre-G-039 regex contract unchanged ──────────────────────────────────


@pytest.mark.parametrize(
    "prompt",
    [
        "llm_router is stuck",
        "the llm_router debug log is full",
        "fix llm_router hooks",
        "MANDATORY ROUTE is blocking me",
        "see ~/.llm-router/auto-route-debug.log",
        "LLM_ROUTER_ENFORCE=off how to set",
    ],
)
def test_self_reference_regex_still_matches_legit_debug_prompts(
    hook_module, prompt: str
) -> None:
    """The regex itself didn't change — only the action on match did.
    Pin the matching behaviour so a future regex tweak doesn't quietly
    break the developer bypass workflow."""
    assert hook_module._SELF_REFERENCE_RE.search(prompt) is not None


@pytest.mark.parametrize(
    "prompt",
    [
        "explain how the audit log works in a generic LLM system",
        "what's the weather today?",
        "write a python function for fizzbuzz",
        "refactor this React component",
    ],
)
def test_unrelated_prompts_still_not_matched(
    hook_module, prompt: str
) -> None:
    assert hook_module._SELF_REFERENCE_RE.search(prompt) is None


# ── 3. Profile-detector + regex composed (the integration G-039 closes) ──


def _would_bypass(hook_module, prompt: str, monkeypatch=None) -> bool:
    """Replicate the early-exit decision from ``main()`` — returns
    True iff (a) the prompt matches the self-reference regex AND
    (b) the enterprise profile is not active. Mirrors the runtime
    contract without spawning a subprocess for each parametrize."""
    if not hook_module._SELF_REFERENCE_RE.search(prompt):
        return False
    return not hook_module._is_enterprise_profile()


def test_developer_profile_bypass_allowed(hook_module) -> None:
    """Pre-G-039 contract: a llm_router-debug prompt under developer
    profile skips routing entirely."""
    assert _would_bypass(hook_module, "llm_router is stuck again") is True


def test_enterprise_profile_bypass_refused(
    hook_module, monkeypatch
) -> None:
    """G-039 closure: the SAME prompt under enterprise profile must
    NOT bypass. Normal routing proceeds."""
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    assert _would_bypass(hook_module, "llm_router is stuck again") is False


def test_enterprise_unrelated_prompt_still_normal(
    hook_module, monkeypatch
) -> None:
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    # Unrelated prompt — neither bypassed under developer nor
    # under enterprise. The enterprise gate flip doesn't affect
    # the non-self-reference path.
    assert _would_bypass(hook_module, "what is 2+2?") is False


def test_developer_unrelated_prompt_still_normal(hook_module) -> None:
    assert _would_bypass(hook_module, "what is 2+2?") is False
