"""Regression: RED2-8-03 — the SessionStart banner must reflect ACTUAL provider
availability, not claim "API-key routing in effect" when no cloud keys are set."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "src" / "llm_router" / "hooks" / "session-start.py"


def _load():
    spec = importlib.util.spec_from_file_location("ss_banner", HOOK)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_no_cloud_keys_shows_local_banner(monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "_CC_MODE", False, raising=False)
    monkeypatch.setattr(m, "_any_cloud_key", lambda: False)
    assert "local routing" in m._resolve_banner(False)
    assert "API-key routing" not in m._resolve_banner(False)


def test_cloud_key_shows_api_banner(monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "_CC_MODE", False, raising=False)
    monkeypatch.setattr(m, "_any_cloud_key", lambda: True)
    assert "API-key routing" in m._resolve_banner(False)


def test_subscription_shows_subscription_banner(monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "_any_cloud_key", lambda: False)
    assert "subscription mode" in m._resolve_banner(True)
