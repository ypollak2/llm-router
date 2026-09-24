"""L: the classifier ensemble must not default to a model that is not installed.

Claude Desktop's llm_router server logged, on every start from Sep 13 through
at least Sep 18, "ensemble: local classify via ollama/qwen2.5:7b failed: model
'qwen2.5:7b' not found" — DEFAULT_PRIMARY names a model this machine never
pulled, so classification silently fell back. Claude Code's server was fine
only because its MCP env sets LLM_ROUTER_ENSEMBLE_PRIMARY=ollama/qwen3.8:latest
and SECONDARY=ollama/qwen3-coder:30b — the preference order below mirrors it.
"""
from __future__ import annotations

import pytest

from llm_router import ensemble

INSTALLED = ["ollama/qwen3.5:latest", "ollama/qwen3.8:latest", "ollama/qwen3-coder:30b"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_ENSEMBLE_PRIMARY", raising=False)
    monkeypatch.delenv("LLM_ROUTER_ENSEMBLE_SECONDARY", raising=False)


def _installed(monkeypatch, names):
    monkeypatch.setattr(ensemble, "_installed_local_models", lambda: list(names))


def test_a_missing_default_falls_back_to_an_installed_model(monkeypatch):
    _installed(monkeypatch, INSTALLED)
    assert not ensemble.model_installed(ensemble.DEFAULT_PRIMARY,
                                        [m.split("/", 1)[1] for m in INSTALLED]), "premise"
    assert ensemble.primary_model() == "ollama/qwen3.8:latest"
    assert ensemble.secondary_model() == "ollama/qwen3-coder:30b"


def test_primary_and_secondary_differ_when_they_can(monkeypatch):
    _installed(monkeypatch, ["ollama/qwen3.8:latest", "ollama/llama3:8b"])
    assert ensemble.primary_model() != ensemble.secondary_model()


def test_an_explicit_setting_always_wins(monkeypatch):
    _installed(monkeypatch, INSTALLED)
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE_PRIMARY", "ollama/whatever:1b")
    assert ensemble.primary_model() == "ollama/whatever:1b"


def test_an_installed_default_is_kept(monkeypatch):
    _installed(monkeypatch, INSTALLED + ["ollama/qwen2.5:7b"])
    assert ensemble.primary_model() == ensemble.DEFAULT_PRIMARY


def test_nothing_installed_or_unknown_keeps_the_default(monkeypatch):
    _installed(monkeypatch, [])
    assert ensemble.primary_model() == ensemble.DEFAULT_PRIMARY
