"""P1 #3: drafts are free-tier-only + per-session paid-spend cap.

A draft must never hit a paid API (the $0.10 gpt-4o draft that made routing
net-negative), and once cumulative paid spend crosses the cap the hook tells the
caller to stop routing to paid tiers.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


def _load():
    cached = sys.modules.get("auto_route_free_tier")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("auto_route_free_tier", HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_free_tier"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ar():
    return _load()


# ── free-tier draft chain ────────────────────────────────────────────────────
def test_free_tier_filter_drops_paid_providers(ar):
    chain = [
        SimpleNamespace(provider="ollama", model="hermes3"),
        SimpleNamespace(provider="openai", model="gpt-4o-mini"),   # paid
        SimpleNamespace(provider="gemini", model="gemini-2.5-flash"),  # paid
        SimpleNamespace(provider="codex", model="gpt-5.4"),
        SimpleNamespace(provider="claude", model="opus"),          # subscription, not a draft tier
    ]
    out = ar._free_tier_draft_chain(chain)
    assert [m.provider for m in out] == ["ollama", "codex"]


def test_free_tier_filter_empty_when_all_paid(ar):
    chain = [
        SimpleNamespace(provider="openai", model="gpt-4o"),
        SimpleNamespace(provider="gemini", model="gemini-2.0-pro"),
    ]
    assert ar._free_tier_draft_chain(chain) == []


def test_free_draft_providers_are_only_free(ar):
    assert ar._FREE_DRAFT_PROVIDERS == frozenset({"ollama", "codex", "gemini_cli"})
    assert "openai" not in ar._FREE_DRAFT_PROVIDERS
    assert "gemini" not in ar._FREE_DRAFT_PROVIDERS  # paid API ≠ free gemini_cli


# ── session paid spend + cap ─────────────────────────────────────────────────
def test_session_paid_spend_reads_total_usd(ar, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".llm-router").mkdir(parents=True)
    (tmp_path / ".llm-router" / "session_spend.json").write_text(json.dumps({"total_usd": 0.1132}))
    # Path.home() honors $HOME on POSIX.
    assert ar._session_paid_spend() == pytest.approx(0.1132)


def test_session_paid_spend_zero_when_missing(ar, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ar._session_paid_spend() == 0.0


def test_paid_spend_cap_default_and_override(ar, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_SESSION_PAID_CAP", raising=False)
    assert ar._paid_spend_cap() == 0.50
    monkeypatch.setenv("LLM_ROUTER_SESSION_PAID_CAP", "2.00")
    assert ar._paid_spend_cap() == 2.00
    monkeypatch.setenv("LLM_ROUTER_SESSION_PAID_CAP", "garbage")
    assert ar._paid_spend_cap() == 0.50


# ── end-to-end: cap note appears once spend crosses the cap ───────────────────
def _run_hook(prompt: str, home: Path, spend: float | None) -> str:
    (home / ".llm-router").mkdir(parents=True, exist_ok=True)
    if spend is not None:
        (home / ".llm-router" / "session_spend.json").write_text(json.dumps({"total_usd": spend}))
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(home)
    env["LLM_ROUTER_ENFORCE"] = "suggest"
    env["LLM_ROUTER_DIRECT_EXECUTION"] = "off"   # deterministic directive JSON
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": prompt, "session_id": "cap"}),
        capture_output=True, text=True, env=env,
    )
    out = result.stdout.strip()
    if not out:
        return ""
    d = json.loads(out)
    hso = d.get("hookSpecificOutput", {})
    return hso.get("additionalContext") or hso.get("contextForAgent") or d.get("reason", "")


def test_cap_note_present_when_over_cap(tmp_path):
    ctx = _run_hook("what is the capital of France", tmp_path, spend=0.75)  # > 0.50 default
    assert "SESSION PAID-API CAP REACHED" in ctx
    assert "$0.75" in ctx


def test_cap_note_absent_when_under_cap(tmp_path):
    ctx = _run_hook("what is the capital of France", tmp_path, spend=0.05)  # < 0.50
    assert "SESSION PAID-API CAP REACHED" not in ctx
