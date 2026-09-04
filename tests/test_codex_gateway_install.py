"""Codex gateway install wiring (opt-in mode).

The gateway does not yet speak Codex's "responses" wire shape, so it is never
the default and must never be forced as Codex's global model provider. Since
2026-09 the default Codex install is the MCP server + hooks + AGENTS.md
(tests/test_codex_install.py); `--mode gateway` layers the provider table on top.
"""
from __future__ import annotations

import contextlib
import io
import pathlib

def _patch_home(monkeypatch, tmp_path):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))


def test_codex_gateway_install_writes_provider_and_keeps_mcp(monkeypatch, tmp_path):
    from llm_router import codex_host
    from llm_router.commands.install import _install_codex_files

    _patch_home(monkeypatch, tmp_path)
    actions = _install_codex_files(mode="gateway")

    config_toml = tmp_path / ".codex" / "config.toml"
    text = config_toml.read_text()
    assert 'model = "auto"' not in text
    assert 'model_provider = "llm_router"' not in text
    assert "[model_providers.llm_router]" in text
    assert 'base_url = "http://127.0.0.1:17900/v1"' in text
    assert 'wire_api = "responses"' in text
    # the MCP server is in config.toml -- the file Codex reads -- not config.yaml
    assert codex_host.read_mcp_server(text) is not None
    assert not (tmp_path / ".codex" / "config.yaml").exists()
    assert any("Registered LLM Router as an available Codex model provider" in a for a in actions)


def test_codex_gateway_install_self_heals_previously_forced_default(monkeypatch, tmp_path):
    from llm_router.commands.install import _install_codex_files

    _patch_home(monkeypatch, tmp_path)
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text('model = "auto"\nmodel_provider = "llm_router"\n')
    actions = _install_codex_files(mode="gateway")
    text = (codex / "config.toml").read_text()
    assert 'model = "auto"' not in text
    assert 'model_provider = "llm_router"' not in text
    assert "[model_providers.llm_router]" in text
    assert any("Reverted Codex's default model_provider" in a for a in actions)


def test_codex_gateway_install_backs_up_existing_config_once(monkeypatch, tmp_path):
    from llm_router.commands.install import _install_codex_files

    _patch_home(monkeypatch, tmp_path)
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text('model = "gpt-5.5"\n')
    _install_codex_files(mode="gateway")
    bak = codex / "config.toml.llm_router-bak"
    assert bak.read_text() == 'model = "gpt-5.5"\n'
    _install_codex_files(mode="gateway")
    assert bak.read_text() == 'model = "gpt-5.5"\n', "second run must not overwrite the backup"


def test_default_mode_does_not_touch_model_provider(monkeypatch, tmp_path):
    from llm_router.commands.install import _install_codex_files

    _patch_home(monkeypatch, tmp_path)
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text('model_provider = "openai"\n')
    _install_codex_files()
    text = (codex / "config.toml").read_text()
    assert 'model_provider = "openai"' in text
    assert "[model_providers.llm_router]" not in text, "gateway is opt-in only"


def test_run_install_host_codex_defaults_to_mcp_and_gateway_is_opt_in(monkeypatch, tmp_path):
    from llm_router.commands import install

    _patch_home(monkeypatch, tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        install._run_install(["--host", "codex"])
    text = (tmp_path / ".codex" / "config.toml").read_text()
    assert "[mcp_servers.llm_router]" in text
    assert "[model_providers.llm_router]" not in text

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        install._run_install(["--host", "codex", "--mode", "gateway"])
    out = buf.getvalue()
    assert "Codex" in out and "model provider" in out
    text = (tmp_path / ".codex" / "config.toml").read_text()
    assert "[model_providers.llm_router]" in text
    assert 'model_provider = "llm_router"' not in text
