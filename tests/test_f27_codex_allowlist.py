"""F27: the Codex allow-list rewrite is logged once per pair, and the external
fallback ranking never offers a Codex model outside CODEX_MODELS.

No network, no codex call: availability is patched.
"""

from __future__ import annotations

import logging

import pytest

from llm_router import codex_agent, provider_budget
from llm_router.codex_agent import accepted_codex_entry

ALLOW_LOGGER = "llm_router.codex_agent"


@pytest.fixture(autouse=True)
def _fresh_log_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_agent, "_REWRITES_LOGGED", set())
    monkeypatch.setattr(codex_agent, "is_codex_available", lambda: True)


def test_rewrite_logged_once_per_pair(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=ALLOW_LOGGER):
        for _ in range(5):
            assert accepted_codex_entry("codex/gpt-4o", ["gpt-5.5"]) == "codex/gpt-5.5"
        assert accepted_codex_entry("codex/o3", ["gpt-5.5"]) == "codex/gpt-5.5"
        assert accepted_codex_entry("codex/o3", ["gpt-6-astra"]) == "codex/gpt-6-astra"
    msgs = [r.getMessage() for r in caplog.records if r.name == ALLOW_LOGGER]
    assert len(msgs) == 3  # (4o,5.5) once, (o3,5.5), (o3,astra)
    assert all(r.levelno == logging.INFO for r in caplog.records if r.name == ALLOW_LOGGER)
    assert "codex/gpt-4o" in msgs[0] and "codex/gpt-5.5" in msgs[0]


def test_accepted_entry_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=ALLOW_LOGGER):
        assert accepted_codex_entry("codex/gpt-5.5", ["gpt-5.5"]) == "codex/gpt-5.5"
        assert accepted_codex_entry("gemini/x", ["gpt-5.5"]) == "gemini/x"
    assert not [r for r in caplog.records if r.name == ALLOW_LOGGER]


@pytest.mark.parametrize("env", [None, "gpt-6-astra", "gpt-5.5,o3"])
def test_rank_external_models_only_allowed_codex(
    monkeypatch: pytest.MonkeyPatch, env: str | None
) -> None:
    if env is None:
        monkeypatch.delenv("LLM_ROUTER_CODEX_MODELS", raising=False)
    else:
        monkeypatch.setenv("LLM_ROUTER_CODEX_MODELS", env)
    allowed = codex_agent._load_codex_models()
    monkeypatch.setattr(codex_agent, "CODEX_MODELS", allowed)

    ranked = provider_budget.rank_external_models({}, min_quality=0.0)
    codex = [m for m, _q, _c in ranked if m.startswith("codex/")]

    assert codex, "check found nothing to check"
    assert len(codex) == len(set(codex))
    assert all(m[len("codex/"):] in allowed for m in codex), codex
    if env is None:
        assert codex == ["codex/gpt-5.4", "codex/gpt-5.5"]
    if env == "gpt-6-astra":
        assert codex == ["codex/gpt-6-astra"]


def test_auto_profile_only_allowed_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_router import auto_profile

    monkeypatch.setattr(codex_agent, "CODEX_MODELS", ["gpt-6-astra"])
    detected = auto_profile.ServiceDetection(codex_available=True)
    yaml_text = auto_profile.generate_profile_yaml(detected)
    assert "codex/gpt-6-astra" in yaml_text
    for rejected in ("codex/gpt-5.4", "codex/o3"):
        assert rejected not in yaml_text
