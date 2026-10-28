"""UN-04c: a Playwright snapshot holding a secret is never sent to Gemini.

`_try_gemini` used to send every snapshot to generativelanguage.googleapis.com whenever GEMINI_API_KEY was set.
These tests trace urllib in-process (no network, no real HOME): a secret-bearing snapshot makes zero calls and the
hook falls back to the local rule-based summary; a clean snapshot still reaches Gemini; a door that raises fails
closed and is recorded.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from tests import secret_fixtures as fx

ROOT = Path(__file__).resolve().parents[1]


def _load_hook(rel):
    spec = importlib.util.spec_from_file_location("pw_compress_un04c", ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=["hooks/playwright-compress.py", "src/llm_router/hooks/playwright-compress.py"])
def hook(request, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    return _load_hook(request.param)


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps({"candidates": [{"content": {"parts": [{"text": "SUMMARY"}]}}]}).encode()


@pytest.fixture
def calls(hook, monkeypatch):
    seen: list[str] = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return _Resp()

    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_urlopen)
    return seen


def _snapshot(extra: str = "") -> str:
    return "\n".join(f'- button "Item {i}" [ref=e{i}]' for i in range(60)) + "\n" + extra


def test_secret_snapshot_makes_no_gemini_call(hook, calls):
    key = fx.real_shaped("openai_key", 11)
    snap = _snapshot(f'- textbox "API key" [ref=e99]: {key}')
    assert hook._try_gemini(snap) is None
    assert calls == []


def test_secret_snapshot_falls_back_to_rule_based_in_main(hook, calls, monkeypatch, capsys):
    key = fx.real_shaped("github_token", 12)
    snap = _snapshot(f'- text "token" [ref=e98]: {key}')
    monkeypatch.setattr(hook, "_try_ollama", lambda s: None)
    out = hook._try_gemini(snap) or hook._rule_based(snap)
    assert calls == []
    assert out and "SUMMARY" not in out


def test_clean_snapshot_still_goes_to_gemini(hook, calls):
    assert hook._try_gemini(_snapshot()) == "SUMMARY"
    assert len(calls) == 1 and "generativelanguage.googleapis.com" in calls[0]


def test_no_key_still_no_call(hook, calls, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    assert hook._try_gemini(_snapshot()) is None
    assert calls == []


def test_door_exception_fails_closed_and_is_recorded(hook, calls, monkeypatch):
    from llm_router import failopen, local_privacy

    def boom(*a, **k):
        raise RuntimeError("door down")

    recorded: list[str] = []
    monkeypatch.setattr(local_privacy, "scan_secrets", boom)
    monkeypatch.setattr(failopen, "record", lambda code, *a, **k: recorded.append(code))
    assert hook._try_gemini(_snapshot()) is None
    assert calls == []
    assert recorded == ["UN-04C-PW-DOOR"]


def test_door_unimportable_fails_closed(hook, calls, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_lp(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "llm_router" and "local_privacy" in (fromlist or ()):
            raise ImportError("gone")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", no_lp)
    assert hook._try_gemini(_snapshot()) is None
    assert calls == []
