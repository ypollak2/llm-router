"""Regression: RED2-8-01 — uninstall must remove home-scoped `--host` MCP
registrations (codex/cursor/gemini/vscode/…), leaving other servers intact."""
import json
import pathlib

from llm_router.commands.install import (
    uninstall_host_integrations,
    _remove_toml_table_block,
)


def test_removes_llm_router_from_all_json_hosts(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    gem = tmp_path / ".gemini" / "settings.json"
    gem.parent.mkdir(parents=True)
    gem.write_text(json.dumps({"mcpServers": {"llm_router": {"command": "llm_router"}, "keep": {"x": 1}}}))
    (tmp_path / ".gemini" / "extensions" / "llm_router").mkdir(parents=True)
    cur = tmp_path / ".cursor" / "mcp.json"
    cur.parent.mkdir(parents=True)
    cur.write_text(json.dumps({"mcpServers": {"llm_router": {"command": "llm_router"}}}))
    codex = tmp_path / ".codex" / "config.toml"
    codex.parent.mkdir(parents=True)
    codex.write_text('[model_providers.llm_router]\nname = "LLM Router"\nbase_url = "x"\n\n[other]\nk = 1\n')

    uninstall_host_integrations()

    assert "llm_router" not in json.loads(gem.read_text())["mcpServers"], "gemini llm_router not removed"
    assert "keep" in json.loads(gem.read_text())["mcpServers"], "other server dropped"
    assert not (tmp_path / ".gemini" / "extensions" / "llm_router").exists(), "gemini ext dir not removed"
    assert "llm_router" not in json.loads(cur.read_text()).get("mcpServers", {}), "cursor llm_router not removed"
    toml_after = codex.read_text()
    assert "[model_providers.llm_router]" not in toml_after, "codex TOML block not removed"
    assert "[other]" in toml_after, "codex unrelated table dropped"


def test_noop_when_nothing_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    actions = uninstall_host_integrations()  # must not raise
    assert isinstance(actions, list)


def test_toml_block_removal_precise():
    txt = '[a]\nx = 1\n\n[model_providers.llm_router]\nname = "C"\nurl = "u"\n\n[b]\ny = 2\n'
    out = _remove_toml_table_block(txt, "model_providers.llm_router")
    assert "[model_providers.llm_router]" not in out
    assert "[a]" in out and "[b]" in out and "y = 2" in out
