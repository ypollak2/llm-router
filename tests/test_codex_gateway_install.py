"""Codex gateway install wiring.

Codex automatic routing depends on routing the host model provider through the
LLM Router OpenAI-compatible gateway, not only installing MCP pull-routing tools.
"""
from __future__ import annotations

import contextlib
import io
import pathlib


def _patch_home(monkeypatch, tmp_path):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))


def test_codex_gateway_install_writes_provider_and_keeps_mcp(monkeypatch, tmp_path):
    """The llm_router provider is registered (available for opt-in use) but must
    NOT be forced as Codex's global default — the gateway doesn't yet speak
    Codex's exact "responses" wire shape, so forcing it as default breaks
    every Codex call (interactive and LLM Router's own routed dispatch alike)."""
    from llm_router.commands.install import _install_codex_files

    _patch_home(monkeypatch, tmp_path)
    actions = _install_codex_files(mode="gateway")

    config_toml = tmp_path / ".codex" / "config.toml"
    assert config_toml.exists()
    text = config_toml.read_text()
    assert 'model = "auto"' not in text
    assert 'model_provider = "llm_router"' not in text
    assert "[model_providers.llm_router]" in text
    assert 'base_url = "http://127.0.0.1:17900/v1"' in text
    assert 'wire_api = "responses"' in text

    config_yaml = tmp_path / ".codex" / "config.yaml"
    assert "llm_router" in config_yaml.read_text()
    assert any("Registered LLM Router as an available Codex model provider" in a for a in actions)


def test_codex_gateway_install_self_heals_previously_forced_default(monkeypatch, tmp_path):
    """A config left over from an earlier llm_router install that forced
    model_provider=llm_router/model=auto must be reverted on the next install run
    — this is what lets existing broken installs self-heal via a reinstall."""
    from llm_router.commands.install import _install_codex_files

    _patch_home(monkeypatch, tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config_toml = codex_dir / "config.toml"
    config_toml.write_text('model = "auto"\nmodel_provider = "llm_router"\n')

    actions = _install_codex_files(mode="gateway")
    text = config_toml.read_text()
    assert 'model = "auto"' not in text
    assert 'model_provider = "llm_router"' not in text
    assert "[model_providers.llm_router]" in text
    assert any("Reverted Codex's default model_provider" in a for a in actions)


def test_codex_gateway_install_backs_up_existing_config_once(monkeypatch, tmp_path):
    from llm_router.commands.install import _install_codex_files

    _patch_home(monkeypatch, tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config_toml = codex_dir / "config.toml"
    config_toml.write_text('model = "gpt-5.5"\nmodel_provider = "openai"\n')

    _install_codex_files(mode="gateway")
    backup = codex_dir / "config.toml.llm_router-bak"
    assert backup.exists()
    assert 'model_provider = "openai"' in backup.read_text()

    backup.write_text("sentinel\n")
    _install_codex_files(mode="gateway")
    assert backup.read_text() == "sentinel\n"


def test_codex_companion_mode_does_not_change_model_provider(monkeypatch, tmp_path):
    from llm_router.commands.install import _install_codex_files

    _patch_home(monkeypatch, tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config_toml = codex_dir / "config.toml"
    config_toml.write_text('model = "gpt-5.5"\nmodel_provider = "openai"\n')

    _install_codex_files(mode="companion")
    text = config_toml.read_text()
    assert 'model = "gpt-5.5"' in text
    assert 'model_provider = "openai"' in text
    assert "[model_providers.llm_router]" not in text


def test_run_install_host_codex_gateway_mode_uses_temp_home(monkeypatch, tmp_path):
    from llm_router.commands import install

    _patch_home(monkeypatch, tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        install._run_install(["--host", "codex", "--mode", "gateway"])

    out = buf.getvalue()
    assert "Codex" in out
    assert "model provider" in out
    assert 'model_provider = "llm_router"' not in (tmp_path / ".codex" / "config.toml").read_text()

