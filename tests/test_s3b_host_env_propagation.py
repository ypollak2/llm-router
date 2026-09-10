"""S3b — a host config must carry the env the router needs, not just the command.

Config is portable across hosts. Environment is not, and nothing was carrying it.

The tuned model selection lives in Claude Code's `~/.claude/settings.json` `env`
block:

    LLM_ROUTER_ENSEMBLE_PRIMARY    ollama/qwen3.8:latest
    LLM_ROUTER_ENSEMBLE_SECONDARY  ollama/qwen3-coder:30b

Cursor, OpenCode, Windsurf and Codex never read that file. Spawned from any of
them, the server falls back to a built-in default that is not installed here, and
says so on the way past:

    ensemble: local classify via ollama/qwen2.5:7b failed:
        OllamaException - {"error":"model 'qwen2.5:7b' not found"}

Tool serving is unaffected, so this degrades rather than breaks — which is why it
went unnoticed. The classifier silently stops being the tuned one, on every host
except the one whose settings file happens to hold the values.

MCP's stdio server spec has an `env` field for exactly this. The adapters simply
were not writing it.

Note on scope: only llm_router's own `LLM_ROUTER_*` keys are propagated. A host
config is not the place to copy a developer's whole environment, and provider API
keys in particular must not be duplicated into editor config files that get synced,
screenshared and committed by accident.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_router.hosts.cursor import CursorAdapter

BIN = "/abs/path/llm-router"


def _entry(cfg_path: Path) -> dict:
    return json.loads(cfg_path.read_text())["mcpServers"]["llm_router"]


def test_env_is_written_into_the_host_config(tmp_path):
    cfg = tmp_path / "mcp.json"
    CursorAdapter(config_path=cfg).install(
        [BIN], env={"LLM_ROUTER_ENSEMBLE_PRIMARY": "ollama/qwen3.8:latest"}
    )
    assert _entry(cfg)["env"]["LLM_ROUTER_ENSEMBLE_PRIMARY"] == "ollama/qwen3.8:latest"


def test_no_env_block_when_there_is_nothing_to_carry(tmp_path):
    """An empty `env: {}` is noise in a file humans read and edit."""
    cfg = tmp_path / "mcp.json"
    CursorAdapter(config_path=cfg).install([BIN])
    assert "env" not in _entry(cfg)
    CursorAdapter(config_path=cfg).install([BIN], env={})
    assert "env" not in _entry(cfg)


def test_the_tuned_ensemble_models_survive_the_trip(tmp_path):
    """The actual failure: without these the classifier falls back to a model
    that is not installed."""
    cfg = tmp_path / "mcp.json"
    CursorAdapter(config_path=cfg).install([BIN], env={
        "LLM_ROUTER_ENSEMBLE_PRIMARY": "ollama/qwen3.8:latest",
        "LLM_ROUTER_ENSEMBLE_SECONDARY": "ollama/qwen3-coder:30b",
    })
    env = _entry(cfg)["env"]
    assert env["LLM_ROUTER_ENSEMBLE_PRIMARY"] == "ollama/qwen3.8:latest"
    assert env["LLM_ROUTER_ENSEMBLE_SECONDARY"] == "ollama/qwen3-coder:30b"


def test_reinstalling_replaces_env_rather_than_accumulating(tmp_path):
    cfg = tmp_path / "mcp.json"
    a = CursorAdapter(config_path=cfg)
    a.install([BIN], env={"LLM_ROUTER_ENSEMBLE_PRIMARY": "old"})
    a.install([BIN], env={"LLM_ROUTER_ENSEMBLE_PRIMARY": "new"})
    assert _entry(cfg)["env"] == {"LLM_ROUTER_ENSEMBLE_PRIMARY": "new"}


def test_other_servers_are_still_untouched(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"someone_elses": {"command": "x"}}}))
    CursorAdapter(config_path=cfg).install([BIN], env={"LLM_ROUTER_ENSEMBLE_PRIMARY": "p"})
    assert "someone_elses" in json.loads(cfg.read_text())["mcpServers"]


# ── what must NOT be copied into an editor config file ──────────────────────

@pytest.mark.parametrize("secret", [
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PERPLEXITY_API_KEY",
    "GEMINI_API_KEY", "AWS_SECRET_ACCESS_KEY",
])
def test_provider_credentials_are_never_propagated(secret, monkeypatch, tmp_path):
    """Editor configs get synced, screenshared and committed by accident.

    Whatever else is true of a host config, it is not a secret store.
    """
    from llm_router.hosts import base

    monkeypatch.setenv(secret, "sk-should-never-appear")
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE_PRIMARY", "ollama/qwen3.8:latest")
    carried = base.routing_env()
    assert secret not in carried
    assert "sk-should-never-appear" not in json.dumps(carried)
    assert carried["LLM_ROUTER_ENSEMBLE_PRIMARY"] == "ollama/qwen3.8:latest"


def test_routing_env_collects_only_llm_router_keys(monkeypatch):
    from llm_router.hosts import base

    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE_PRIMARY", "ollama/qwen3.8:latest")
    monkeypatch.setenv("EDITOR", "vim")
    monkeypatch.setenv("PATH", "/usr/bin")
    carried = base.routing_env()
    assert set(carried) <= {k for k in carried if k.startswith("LLM_ROUTER_")}
    assert "EDITOR" not in carried and "PATH" not in carried


def test_routing_env_is_empty_when_nothing_is_set(monkeypatch):
    from llm_router.hosts import base

    for k in list(__import__("os").environ):
        if k.startswith("LLM_ROUTER_"):
            monkeypatch.delenv(k, raising=False)
    assert base.routing_env() == {}
