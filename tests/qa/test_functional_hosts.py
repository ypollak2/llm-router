"""Functional pillar — does each host's integration do what its docs say?

Parametrized over every host in conftest.HOSTS (12 hosts). For hosts with
a Python adapter, the full install/uninstall battery runs. For
rules-only / plugin-only hosts, structural correctness is verified.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.qa.conftest import HostSpec, load_adapter


# ────────────────────────────────────────────────────────────────────────
# Plugin manifest: structural + brand
# ────────────────────────────────────────────────────────────────────────

def test_plugin_manifest_loadable(host: HostSpec):
    if not host.plugin_path:
        pytest.skip(f"{host.id} has no plugin manifest")
    plugin_json = host.plugin_path / "plugin.json"
    assert plugin_json.exists(), f"{host.id}: plugin.json missing"
    data = json.loads(plugin_json.read_text())
    assert isinstance(data, dict)


def test_marketplace_manifest_loadable(host: HostSpec):
    if not host.plugin_path:
        pytest.skip(f"{host.id} has no marketplace manifest")
    mp = host.plugin_path / "marketplace.json"
    assert mp.exists(), f"{host.id}: marketplace.json missing"
    data = json.loads(mp.read_text())
    assert isinstance(data, dict)


def test_plugin_declares_llm_router_name(host: HostSpec):
    if not host.plugin_path:
        pytest.skip(f"{host.id} has no plugin manifest")
    data = json.loads((host.plugin_path / "plugin.json").read_text())
    name = data.get("name", "").lower()
    # Accepts the downstream brand too. This file is SYNCED to llm-routing,
    # where the plugin is named `llm-router` — and the sync rewrites `llm_router`
    # to `llm_router` (the Python package), not to `llm-router` (the CLI and
    # user-facing brand). One upstream name, two downstream ones, and only
    # context distinguishes them; a rewrite rule cannot.
    #
    # So the accepted set is spelled out here rather than patched downstream:
    # a downstream edit to a synced file is reverted by the next sync, which
    # looks like nothing happened.
    _ACCEPTED = ("llm_router", "llm-routing", "llm-router")
    assert any(brand in name for brand in _ACCEPTED), (
        f"{host.id}: plugin name should mention one of {_ACCEPTED}, got {name!r}"
    )


# ────────────────────────────────────────────────────────────────────────
# Rules file: structural + content + tool references
# ────────────────────────────────────────────────────────────────────────

def test_rules_file_loadable(host: HostSpec):
    if not host.rules_path:
        pytest.skip(f"{host.id} ships no rules file")
    assert host.rules_path.exists(), f"{host.id}: rules file missing"
    content = host.rules_path.read_text(encoding="utf-8")
    assert content.strip(), f"{host.id}: rules file is empty"


def test_rules_file_is_valid_utf8(host: HostSpec):
    if not host.rules_path:
        pytest.skip(f"{host.id} ships no rules file")
    # Bytes must round-trip via UTF-8 strictly
    raw = host.rules_path.read_bytes()
    raw.decode("utf-8")  # raises on bad bytes


def test_rules_file_has_markdown_structure(host: HostSpec):
    if not host.rules_path:
        pytest.skip(f"{host.id} ships no rules file")
    content = host.rules_path.read_text()
    assert content.count("#") >= 1, f"{host.id}: rules file has no headers"


# ────────────────────────────────────────────────────────────────────────
# Python adapter happy-path
# ────────────────────────────────────────────────────────────────────────

def test_adapter_writes_valid_config(host: HostSpec, tmp_path: Path):
    cls = load_adapter(host)
    cfg = tmp_path / "config.json"
    adapter = cls(config_path=cfg)
    written = adapter.install(server_command=["llm-router"])

    assert written.exists(), f"{host.id}: install did not create the config"
    data = json.loads(written.read_text())
    assert "mcpServers" in data
    assert "llm_router" in data["mcpServers"]


def test_adapter_install_records_command_args(host: HostSpec, tmp_path: Path):
    cls = load_adapter(host)
    cfg = tmp_path / "config.json"
    adapter = cls(config_path=cfg)
    adapter.install(server_command=["llm-router", "--stdio", "--verbose"])

    data = json.loads(cfg.read_text())
    entry = data["mcpServers"]["llm_router"]
    assert entry["command"] == "llm-router"
    assert entry["args"] == ["--stdio", "--verbose"]


def test_adapter_uninstall_returns_path_when_present(host: HostSpec, tmp_path: Path):
    cls = load_adapter(host)
    cfg = tmp_path / "config.json"
    adapter = cls(config_path=cfg)
    adapter.install(server_command=["llm-router"])
    result = adapter.uninstall()
    assert result is not None, f"{host.id}: uninstall should return the path it cleaned"


def test_adapter_is_installed_after_install(host: HostSpec, tmp_path: Path):
    cls = load_adapter(host)
    cfg = tmp_path / "config.json"
    adapter = cls(config_path=cfg)
    assert not adapter.is_installed()
    adapter.install(server_command=["llm-router"])
    assert adapter.is_installed()


# ────────────────────────────────────────────────────────────────────────
# CLI install host coverage — every host name resolves to an artifact
# ────────────────────────────────────────────────────────────────────────

def test_host_has_at_least_one_artifact(host: HostSpec):
    """Every advertised host must have a plugin dir, rules file, or adapter."""
    has_plugin = bool(host.plugin_path and host.plugin_path.exists())
    has_rules = bool(host.rules_path and host.rules_path.exists())
    has_adapter = bool(host.python_adapter)
    assert has_plugin or has_rules or has_adapter, (
        f"{host.id} has no integration artifact"
    )


# ────────────────────────────────────────────────────────────────────────
# MCP server tool surface — relevant for every host that runs LLM Router
# ────────────────────────────────────────────────────────────────────────

def test_mcp_server_main_importable():
    from llm_router.server import main

    assert callable(main)


def test_mcp_agent_tools_importable():
    from llm_router.tools.agents import (
        register,
        llm_router_agent_check_budget,
        llm_router_agent_complete_session,
        llm_router_agent_lineage,
        llm_router_agent_list,
        llm_router_agent_route,
        llm_router_agent_start_session,
    )

    for tool in (
        register,
        llm_router_agent_check_budget,
        llm_router_agent_complete_session,
        llm_router_agent_lineage,
        llm_router_agent_list,
        llm_router_agent_route,
        llm_router_agent_start_session,
    ):
        assert callable(tool)
