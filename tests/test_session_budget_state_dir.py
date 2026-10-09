"""agent-route.py wrote ~/.llm-router/session_budget.json without creating ~/.llm-router.

Seen by a reviewer's harness running the hook with an empty HOME: FileNotFoundError
from write_text. The state dir convention (statusline_tick, shadow_text) is mode 0700.
"""
from __future__ import annotations

import json
import stat

from tests.test_agent_route_hook import _load_hook_module


def test_budget_init_creates_the_state_dir_0700_under_an_empty_home(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess")
    mod = _load_hook_module()
    home = tmp_path / ".llm-router"
    assert not home.exists()
    initial = mod._initialize_session_budget()
    assert initial >= 5.0
    data = json.loads((home / "session_budget.json").read_text())
    assert data["session_id"] == "sess" and data["initial"] == initial
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_provisional_decrement_also_works_under_an_empty_home(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess")
    mod = _load_hook_module()
    mod._decrement_budget_provisional(1.0)
    data = json.loads((tmp_path / ".llm-router" / "session_budget.json").read_text())
    assert data["provisional_spend"] == 1.0
