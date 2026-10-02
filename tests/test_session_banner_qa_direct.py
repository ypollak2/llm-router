"""The SessionStart banner must not teach the model to route Q&A (owner decision 2026-10-01).

The banner is injected into the model's context for the whole session. While it
said "simple -> llm_query, moderate -> llm_analyze, research -> llm_research" it
contradicted the UserPromptSubmit hook, which no longer routes Q&A. Default: the
banner says Q&A is answered directly. ``LLM_ROUTER_QA_ROUTING=on`` restores the
routing table.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "session-start.py"

_ROUTING_TABLE_WORDS = ("simple", "moderate", "research", "llm_query", "llm_analyze",
                        "llm_code", "llm_research", "llm(task=")


def _load():
    spec = importlib.util.spec_from_file_location("ss_banner_qa", HOOK)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _banner(monkeypatch, *, subscription: bool, cloud_key: bool, qa_routing: str | None):
    monkeypatch.delenv("LLM_ROUTER_ZERO_CLAUDE", raising=False)
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE", "off")
    if qa_routing is None:
        monkeypatch.delenv("LLM_ROUTER_QA_ROUTING", raising=False)
    else:
        monkeypatch.setenv("LLM_ROUTER_QA_ROUTING", qa_routing)
    m = _load()
    monkeypatch.setattr(m, "_CC_MODE", False, raising=False)
    monkeypatch.setattr(m, "_any_cloud_key", lambda: cloud_key)
    return m._select_banner(subscription)


_MODES = [
    pytest.param(True, False, id="subscription"),
    pytest.param(False, True, id="api-keys"),
    pytest.param(False, False, id="local"),
]


@pytest.mark.parametrize("subscription,cloud_key", _MODES)
@pytest.mark.parametrize("qa_routing", [None, "", "off", "0"])
def test_default_banner_says_qa_is_answered_directly(monkeypatch, subscription, cloud_key, qa_routing):
    banner = _banner(monkeypatch, subscription=subscription, cloud_key=cloud_key, qa_routing=qa_routing)
    assert "answer them directly" in banner, banner
    assert "bounded edits" in banner and "tiering" in banner, banner
    leaked = [w for w in _ROUTING_TABLE_WORDS if w in banner]
    assert leaked == [], (leaked, banner)


@pytest.mark.parametrize("subscription,cloud_key", _MODES)
@pytest.mark.parametrize("qa_routing", ["on", "1", "true", "yes"])
def test_qa_routing_on_restores_the_routing_table(monkeypatch, subscription, cloud_key, qa_routing):
    banner = _banner(monkeypatch, subscription=subscription, cloud_key=cloud_key, qa_routing=qa_routing)
    assert "answer them directly" not in banner
    if subscription or cloud_key:
        assert "simple" in banner and "research" in banner, banner
    else:
        assert "local routing" in banner, banner


@pytest.mark.parametrize("subscription,cloud_key", _MODES)
def test_default_banner_box_is_square_and_names_the_mode(monkeypatch, subscription, cloud_key):
    banner = _banner(monkeypatch, subscription=subscription, cloud_key=cloud_key, qa_routing=None)
    lines = banner.splitlines()
    assert len({len(line) for line in lines}) == 1, [len(line) for line in lines]
    label = "subscription" if subscription else ("API-key" if cloud_key else "local")
    assert label in banner
