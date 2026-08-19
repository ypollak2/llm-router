"""Strict zero-Claude routing status and tool-detection regressions."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from llm_router.hooks.chain_builder import get_current_pressure, needs_claude_tools

ROOT = Path(__file__).resolve().parents[1]
SESSION_START_PATH = ROOT / "src" / "llm_router" / "hooks" / "session-start.py"


def _load_session_start():
    spec = importlib.util.spec_from_file_location("session_start_hook_for_test", SESSION_START_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # session-start.py calls _load_dotenv() at module-exec time, which injects
    # keys from the real ~/.llm-router/.env into os.environ. Snapshot/restore so
    # loading it for one test cannot contaminate later tests (provider
    # availability, routing decisions) in the same pytest process.
    env_snapshot = dict(os.environ)
    try:
        spec.loader.exec_module(module)
    finally:
        os.environ.clear()
        os.environ.update(env_snapshot)
    return module


def test_explicit_read_file_prompt_uses_external_agent_path() -> None:
    assert needs_claude_tools("Read notes.txt and report only its marker value.", "query")


def test_one_percent_session_usage_is_not_read_as_one_hundred_percent(
    tmp_path: Path, monkeypatch
) -> None:
    router_dir = tmp_path / ".llm-router"
    router_dir.mkdir()
    (router_dir / "usage.json").write_text(json.dumps({"session_pct": 1.0}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert get_current_pressure() == ("green", 1.0)


def test_zero_claude_banner_overrides_detected_subscription(tmp_path: Path) -> None:
    session_start = _load_session_start()
    state_dir = tmp_path / ".llm-router"
    state_dir.mkdir()
    (state_dir / "routing.yaml").write_text("mode: zero_claude\n", encoding="utf-8")
    session_start.STATE_DIR = str(state_dir)

    banner = session_start._select_banner(is_subscription=True)

    assert "strict zero-Claude routing" in banner
    assert "subscription mode (MCP-tool routing)" not in banner
