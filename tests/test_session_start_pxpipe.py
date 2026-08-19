"""Tests for session-start.py's pxpipe auto-start + ANTHROPIC_BASE_URL sync.

Covers the two new functions added so Claude Code's OWN traffic (not just
LLM Router-routed calls) can route heavy models through a local pxpipe proxy:

  1. _pxpipe_config() — stdlib-only env var parsing (hooks avoid importing
     the full llm_router.config module).
  2. _sync_pxpipe_anthropic_base_url() — writes/removes ANTHROPIC_BASE_URL in
     ~/.claude/settings.json, with the safety rule that a user's own
     unrelated base URL is never touched, and a stale/unreachable pointer
     is always self-healed away rather than left dangling (Claude Code has
     no fallback if the configured base URL doesn't answer).

_ensure_pxpipe_running() (the subprocess-spawning half, mirroring
_ensure_ollama_running) is intentionally not covered here — it shells out
to start-pxpipe.sh, which itself shells out to `npx pxpipe-proxy`; testing
it meaningfully would mean either a real npx install or mocking away
everything worth testing. The reachability-gated sync logic below is the
part with real, testable branching.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "session-start.py"


def _load_hook_module():
    """Import the hyphenated hook file as a module for white-box unit tests.

    Mirrors tests/test_agent_route_hook.py's helper — the hook loads
    ~/.llm-router/.env at import time, which mutates os.environ; snapshot and
    restore it so loading the module doesn't leak vars into other tests.
    """
    spec = importlib.util.spec_from_file_location("session_start_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


class TestPxpipeConfig:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_PXPIPE_ENABLED", raising=False)
        mod = _load_hook_module()
        enabled, url, models = mod._pxpipe_config()
        assert enabled is False
        assert url == "http://127.0.0.1:47821"
        assert models == "claude-fable-5"

    @pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
    def test_truthy_values_enable(self, monkeypatch, value):
        monkeypatch.setenv("LLM_ROUTER_PXPIPE_ENABLED", value)
        mod = _load_hook_module()
        enabled, _url, _models = mod._pxpipe_config()
        assert enabled is True

    def test_custom_url_and_models_respected(self, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_PXPIPE_ENABLED", "true")
        monkeypatch.setenv("LLM_ROUTER_PXPIPE_URL", "http://127.0.0.1:9999/")
        monkeypatch.setenv("LLM_ROUTER_PXPIPE_HEAVY_MODELS", "claude-opus-4-8,gpt-5.6")
        mod = _load_hook_module()
        enabled, url, models = mod._pxpipe_config()
        assert enabled is True
        assert url == "http://127.0.0.1:9999"  # trailing slash stripped
        assert models == "claude-opus-4-8,gpt-5.6"


class TestSyncAnthropicBaseUrl:
    """Exercises _sync_pxpipe_anthropic_base_url() against a real settings.json
    on disk, via a patched Path.home()."""

    def _settings_path(self, tmp_path: Path) -> Path:
        d = tmp_path / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        return d / "settings.json"

    def _run_sync(self, monkeypatch, tmp_path, *, enabled, reachable, url="http://127.0.0.1:47821"):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("LLM_ROUTER_PXPIPE_ENABLED", "true" if enabled else "false")
        monkeypatch.setenv("LLM_ROUTER_PXPIPE_URL", url)
        mod = _load_hook_module()
        monkeypatch.setattr(mod, "_pxpipe_reachable", lambda _url: reachable)
        return mod._sync_pxpipe_anthropic_base_url()

    def test_writes_when_enabled_and_reachable(self, monkeypatch, tmp_path):
        self._run_sync(monkeypatch, tmp_path, enabled=True, reachable=True)
        data = json.loads(self._settings_path(tmp_path).read_text())
        assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:47821"

    def test_noop_when_already_correct(self, monkeypatch, tmp_path):
        path = self._settings_path(tmp_path)
        path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:47821"}}))
        mtime_before = path.stat().st_mtime_ns
        msg = self._run_sync(monkeypatch, tmp_path, enabled=True, reachable=True)
        assert msg == ""
        assert path.stat().st_mtime_ns == mtime_before  # not rewritten

    def test_removes_stale_pointer_when_disabled(self, monkeypatch, tmp_path):
        """Self-heal: was enabled last session (pointer present), now disabled
        — must revert, not leave Claude Code aimed at a proxy nobody wants."""
        path = self._settings_path(tmp_path)
        path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:47821"}}))
        self._run_sync(monkeypatch, tmp_path, enabled=False, reachable=False)
        data = json.loads(path.read_text())
        assert "ANTHROPIC_BASE_URL" not in data.get("env", {})

    def test_removes_stale_pointer_when_unreachable(self, monkeypatch, tmp_path):
        """Enabled, but pxpipe failed to start this session — must revert
        rather than point Claude Code at a dead endpoint (no fallback there)."""
        path = self._settings_path(tmp_path)
        path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:47821"}}))
        self._run_sync(monkeypatch, tmp_path, enabled=True, reachable=False)
        data = json.loads(path.read_text())
        assert "ANTHROPIC_BASE_URL" not in data.get("env", {})

    def test_never_touches_unrelated_custom_url(self, monkeypatch, tmp_path):
        """A user's own corporate-proxy ANTHROPIC_BASE_URL must survive
        untouched, whether pxpipe is enabled, disabled, reachable, or not."""
        path = self._settings_path(tmp_path)
        path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://corp-proxy.internal"}}))
        for enabled in (True, False):
            for reachable in (True, False):
                self._run_sync(monkeypatch, tmp_path, enabled=enabled, reachable=reachable)
                data = json.loads(path.read_text())
                assert data["env"]["ANTHROPIC_BASE_URL"] == "https://corp-proxy.internal"

    def test_preserves_other_env_keys(self, monkeypatch, tmp_path):
        """Writing/removing our key must not clobber unrelated env entries."""
        path = self._settings_path(tmp_path)
        path.write_text(json.dumps({"env": {"SOME_OTHER_VAR": "keep-me"}}))
        self._run_sync(monkeypatch, tmp_path, enabled=True, reachable=True)
        data = json.loads(path.read_text())
        assert data["env"]["SOME_OTHER_VAR"] == "keep-me"
        assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:47821"

    def test_creates_settings_file_if_missing(self, monkeypatch, tmp_path):
        assert not self._settings_path(tmp_path).exists()
        self._run_sync(monkeypatch, tmp_path, enabled=True, reachable=True)
        data = json.loads(self._settings_path(tmp_path).read_text())
        assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:47821"

    def test_malformed_settings_json_is_left_untouched(self, monkeypatch, tmp_path):
        """A corrupt settings.json must not be silently overwritten — that
        risks destroying hooks/mcpServers config unrelated to pxpipe."""
        path = self._settings_path(tmp_path)
        path.write_text("not valid json {{{")
        self._run_sync(monkeypatch, tmp_path, enabled=True, reachable=True)
        assert path.read_text() == "not valid json {{{"

    def test_no_settings_dir_and_disabled_is_noop(self, monkeypatch, tmp_path):
        """Nothing to do, nothing to create — must not fabricate a
        settings.json out of thin air when pxpipe was never enabled."""
        msg = self._run_sync(monkeypatch, tmp_path, enabled=False, reachable=False)
        assert msg == ""
        assert not self._settings_path(tmp_path).exists()
