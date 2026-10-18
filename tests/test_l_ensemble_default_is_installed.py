"""L / D-205: the classifier ensemble never names a model; it follows the user's setup.

History: Claude Desktop's server logged "ensemble: local classify via ollama/<x> failed:
model not found" on every start because DEFAULT_PRIMARY named a model the machine never
pulled. The first fix substituted a family preference list (still a named guess). The
D-205 shape: explicit env > the user's local model (LLM_ROUTER_OLLAMA_MODEL, else the
discovered inventory) > "" (no local model: classification fails open to the heuristic).
"""
from __future__ import annotations

import asyncio

import pytest

from llm_router import ensemble, model_discovery


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("LLM_ROUTER_ENSEMBLE_PRIMARY", "LLM_ROUTER_ENSEMBLE_SECONDARY", "LLM_ROUTER_OLLAMA_MODEL"):
        monkeypatch.delenv(k, raising=False)


def _inventory(monkeypatch, names):
    """The user's discovered inventory (bare ollama names), cache/env only."""
    monkeypatch.setattr(model_discovery, "_inventory", lambda allow_probe=False: list(names))
    monkeypatch.setattr(model_discovery, "available_ollama_models",
                        lambda allow_probe=True: list(names))


def test_empty_inventory_and_no_setting_means_no_local_model(monkeypatch):
    _inventory(monkeypatch, [])
    assert ensemble.primary_model() == ""
    assert ensemble.secondary_model() == ""
    assert not hasattr(ensemble, "DEFAULT_PRIMARY") and not hasattr(ensemble, "DEFAULT_SECONDARY")


def test_no_local_model_fails_open_without_calling_a_provider(monkeypatch):
    called = []

    async def boom(**kw):
        called.append(kw)
        raise AssertionError("must not call a model when none is discovered")

    monkeypatch.setattr(ensemble.providers, "call_llm", boom)
    res = asyncio.run(ensemble.local_llm_classify("hello", ""))
    assert not called
    assert res.confidence == 0.0


def test_discovered_inventory_drives_primary_and_a_different_secondary(monkeypatch):
    _inventory(monkeypatch, ["alpha:1b", "beta:2b", "gamma:3b"])
    assert ensemble.primary_model() == "ollama/alpha:1b"
    assert ensemble.secondary_model() == "ollama/beta:2b"


def test_single_model_has_no_tiebreak_secondary(monkeypatch):
    _inventory(monkeypatch, ["alpha:1b"])
    assert ensemble.primary_model() == "ollama/alpha:1b"
    assert ensemble.secondary_model() == ""


def test_user_setting_is_the_primary_when_installed(monkeypatch):
    _inventory(monkeypatch, ["alpha:1b", "beta:2b"])
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", "beta:2b")
    assert ensemble.primary_model() == "ollama/beta:2b"
    assert ensemble.secondary_model() == "ollama/alpha:1b"


def test_an_explicit_ensemble_setting_always_wins(monkeypatch):
    _inventory(monkeypatch, ["alpha:1b"])
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE_PRIMARY", "ollama/whatever:1b")
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE_SECONDARY", "ollama/other:2b")
    assert ensemble.primary_model() == "ollama/whatever:1b"
    assert ensemble.secondary_model() == "ollama/other:2b"
