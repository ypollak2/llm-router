"""Regression test for CHZ-AUD-020.

_log_agent_call in agent-route.py persisted raw agent prompts (up to 500
chars) to ~/.llm-router/agent_calls.json at world-readable mode 644, with no
secret scrubbing. This test asserts the file is created 0o600 and that a
pasted API key in the prompt is redacted before it hits disk.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "agent-route.py"


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("agent_route_hook_020", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


@pytest.fixture()
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    yield tmp_path


def test_agent_calls_file_is_owner_only(_isolated_home):
    mod = _load_hook_module()
    mod._log_agent_call("general-purpose", "please read the file", "APPROVE")
    calls_file = _isolated_home / ".llm-router" / "agent_calls.json"
    assert calls_file.exists()
    mode = stat.S_IMODE(calls_file.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_agent_calls_prompt_secret_is_redacted(_isolated_home):
    mod = _load_hook_module()
    secret = "sk-ant-api03-" + "A" * 40
    mod._log_agent_call("general-purpose", f"use this key {secret} now", "APPROVE")
    calls_file = _isolated_home / ".llm-router" / "agent_calls.json"
    data = json.loads(calls_file.read_text())
    stored = data["calls"][-1]["prompt"]
    assert secret not in stored, f"raw secret persisted: {stored!r}"
    assert "REDACTED" in stored
