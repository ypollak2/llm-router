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
        return json.dumps({"candidates": [{"content": {"parts": [{"text": "GEMINI_REPLY"}]}}]}).encode()


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


def _run_main(hook, monkeypatch, capsys, snap):
    import io

    payload = {"hook_event_name": "PostToolUse", "tool_name": "mcp__plugin_playwright__browser_snapshot",
               "tool_response": {"text": snap}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(hook, "_try_ollama", lambda s: None)
    hook.main()
    return json.loads(capsys.readouterr().out)["hookSpecificOutput"]["contextForAgent"]


def test_main_with_secret_snapshot_uses_rule_based_not_gemini(hook, calls, monkeypatch, capsys):
    key = fx.real_shaped("github_token", 12)
    ctx = _run_main(hook, monkeypatch, capsys, _snapshot(f'- text "token" [ref=e98]: {key}'))
    assert calls == []
    assert "GEMINI_REPLY" not in ctx and "DOM snapshot compressed" in ctx


def test_main_with_clean_snapshot_uses_gemini(hook, calls, monkeypatch, capsys):
    ctx = _run_main(hook, monkeypatch, capsys, _snapshot())
    assert len(calls) == 1 and "GEMINI_REPLY" in ctx


def test_secret_past_the_truncation_boundary_is_not_sent_so_not_blocked(hook, calls):
    key = fx.real_shaped("openai_key", 13)
    filler = "x" * (hook.MAX_SNAPSHOT_CHARS + 5)
    assert hook._try_gemini(_snapshot(filler + "\n" + key)) == "GEMINI_REPLY"
    assert len(calls) == 1
    # and a secret just inside the boundary is blocked
    head = "- row\n" * 10
    inside = head + key
    assert len(inside) < hook.MAX_SNAPSHOT_CHARS
    assert hook._try_gemini(inside) is None
    assert len(calls) == 1


@pytest.fixture
def ollama_calls(hook, monkeypatch):
    seen: list[str] = []

    class R(_Resp):
        def read(self):
            return json.dumps({"response": "LOCAL"}).encode()

    def fake_open(req, base_url, timeout):
        seen.append(req.full_url)
        return R()

    monkeypatch.setattr(hook, "_ollama_open", fake_open)
    return seen


def test_remote_ollama_with_secret_gets_no_request(hook, ollama_calls, monkeypatch):
    monkeypatch.setattr(hook, "OLLAMA_BASE", "http://ollama.remote.invalid:11434")
    key = fx.real_shaped("openai_key", 14)
    assert hook._try_ollama(_snapshot(f'- text "k" [ref=e1]: {key}')) is None
    assert ollama_calls == []


def test_remote_ollama_clean_snapshot_still_sent(hook, ollama_calls, monkeypatch):
    monkeypatch.setattr(hook, "OLLAMA_BASE", "http://ollama.remote.invalid:11434")
    assert hook._try_ollama(_snapshot()) == "LOCAL"
    assert len(ollama_calls) == 1


def test_loopback_ollama_with_secret_stays_local(hook, ollama_calls, monkeypatch):
    monkeypatch.setattr(hook, "OLLAMA_BASE", "http://localhost:11434")
    key = fx.real_shaped("openai_key", 15)
    assert hook._try_ollama(_snapshot(f'- text "k" [ref=e1]: {key}')) == "LOCAL"
    assert len(ollama_calls) == 1


def test_clean_snapshot_still_goes_to_gemini(hook, calls):
    assert hook._try_gemini(_snapshot()) == "GEMINI_REPLY"
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
