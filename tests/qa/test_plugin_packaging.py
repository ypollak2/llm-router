"""Plugin packaging audit — LLM Router must work as a proper plugin for
Claude Code, Cursor, Gemini CLI, and Codex CLI.

Two integration patterns are covered:

    A. **Marketplace plugins** (Claude Code, Codex CLI): the host has a
       plugin marketplace that reads marketplace.json + plugin.json. The
       plugin manifest references .mcp.json which describes how to
       launch the LLM Router MCP server. Hosts in this group:
           - Claude Code (.claude-plugin/)
           - Codex CLI (.codex-plugin/)

    B. **MCP-config plugins** (Cursor, Gemini CLI): the host has no
       plugin marketplace, but its native MCP server config IS the plugin
       mechanism. LLM Router's Python adapters (hosts/cursor.py,
       hosts/gemini_cli.py) write the right config to register the MCP
       server. Hosts in this group:
           - Cursor (~/.cursor/mcp.json)
           - Gemini CLI (~/.gemini/mcp_servers.json)

This suite pins the contract for both patterns so a future refactor
can't silently break plugin install on any of the four.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent.parent


# ════════════════════════════════════════════════════════════════════════
# Pattern A · Marketplace plugins
# ════════════════════════════════════════════════════════════════════════

MARKETPLACE_PLUGINS = [
    (".claude-plugin", "Claude Code"),
    (".codex-plugin", "Codex CLI"),
]


@pytest.mark.parametrize("plugin_dir,host_name", MARKETPLACE_PLUGINS,
                         ids=[h[1] for h in MARKETPLACE_PLUGINS])
def test_marketplace_plugin_has_required_files(plugin_dir: str, host_name: str):
    """plugin.json + marketplace.json + .mcp.json must all ship."""
    d = ROOT / plugin_dir
    for fname in ("plugin.json", "marketplace.json", ".mcp.json"):
        assert (d / fname).exists(), (
            f"{host_name} plugin missing {fname} — install will fail"
        )


@pytest.mark.parametrize("plugin_dir,host_name", MARKETPLACE_PLUGINS,
                         ids=[h[1] for h in MARKETPLACE_PLUGINS])
def test_plugin_manifest_references_mcp_json(plugin_dir: str, host_name: str):
    """plugin.json's mcpServers field must point to a file that exists."""
    plugin = json.loads((ROOT / plugin_dir / "plugin.json").read_text())
    mcp_ref = plugin.get("mcpServers")
    assert mcp_ref, f"{host_name} plugin.json has no mcpServers reference"
    referenced = ROOT / plugin_dir / mcp_ref
    assert referenced.exists(), (
        f"{host_name} plugin.json references {mcp_ref} but the file is missing"
    )


@pytest.mark.parametrize("plugin_dir,host_name", MARKETPLACE_PLUGINS,
                         ids=[h[1] for h in MARKETPLACE_PLUGINS])
def test_mcp_json_is_valid_and_complete(plugin_dir: str, host_name: str):
    """The .mcp.json must register llm_router under a recognizable command."""
    mcp = json.loads((ROOT / plugin_dir / ".mcp.json").read_text())
    assert "mcpServers" in mcp
    assert "llm_router" in mcp["mcpServers"]
    entry = mcp["mcpServers"]["llm_router"]
    assert "command" in entry
    # Command must reference a llm_router launch path (not legacy)
    cmd = entry.get("command", "")
    args = entry.get("args", [])
    full = f"{cmd} {' '.join(args)}"
    assert "llm_router" in full, (
        f"{host_name} .mcp.json command doesn't reference llm_router: {full!r}"
    )
    assert "claude-code-llm_router" not in full, (
        f"{host_name} .mcp.json still references legacy claude-code-llm_router"
    )
    assert "llm-router" not in full.lower() and "llm_router" not in full, (
        f"{host_name} .mcp.json still references legacy llm-router"
    )


@pytest.mark.parametrize("plugin_dir,host_name", MARKETPLACE_PLUGINS,
                         ids=[h[1] for h in MARKETPLACE_PLUGINS])
def test_marketplace_json_has_owner_and_plugins(
    plugin_dir: str, host_name: str
):
    """marketplace.json must follow the host's expected schema."""
    mp = json.loads((ROOT / plugin_dir / "marketplace.json").read_text())
    assert "name" in mp or "owner" in mp, (
        f"{host_name} marketplace.json has no owner identifier"
    )
    assert isinstance(mp.get("plugins"), list), (
        f"{host_name} marketplace.json plugins must be a list"
    )
    assert len(mp["plugins"]) >= 1
    plugin = mp["plugins"][0]
    assert plugin.get("name") == "llm_router"
    assert "source" in plugin, "marketplace plugin must declare source"


@pytest.mark.parametrize("plugin_dir,host_name", MARKETPLACE_PLUGINS,
                         ids=[h[1] for h in MARKETPLACE_PLUGINS])
def test_plugin_interface_has_user_facing_metadata(
    plugin_dir: str, host_name: str
):
    """The interface block must give the plugin store enough metadata to
    render — display name, description, capabilities. Without these the
    plugin shows as nameless in the host's UI."""
    plugin = json.loads((ROOT / plugin_dir / "plugin.json").read_text())
    iface = plugin.get("interface", {})
    assert iface.get("displayName"), (
        f"{host_name} plugin.json interface.displayName is empty"
    )
    assert iface.get("shortDescription"), (
        f"{host_name} plugin.json interface.shortDescription is empty"
    )
    caps = iface.get("capabilities", [])
    assert isinstance(caps, list) and len(caps) > 0, (
        f"{host_name} plugin.json interface.capabilities must be non-empty"
    )


# ════════════════════════════════════════════════════════════════════════
# Pattern B · MCP-config plugins (Cursor + Gemini CLI)
# ════════════════════════════════════════════════════════════════════════

MCP_CONFIG_HOSTS = [
    ("cursor", "llm_router.hosts.cursor", "CursorAdapter", "Cursor"),
    ("gemini-cli", "llm_router.hosts.gemini_cli", "GeminiCliAdapter", "Gemini CLI"),
]


@pytest.mark.parametrize(
    "slug,module,class_name,host_name", MCP_CONFIG_HOSTS,
    ids=[h[3] for h in MCP_CONFIG_HOSTS],
)
def test_mcp_config_adapter_install_produces_runnable_config(
    slug, module, class_name, host_name, tmp_path
):
    """The adapter's install() must produce a config the host can actually
    consume — mcpServers entry with command + args + (optionally) env."""
    import importlib
    mod = importlib.import_module(module)
    cls = getattr(mod, class_name)
    cfg = tmp_path / "mcp.json"
    adapter = cls(config_path=cfg)
    adapter.install(server_command=["llm_router"])

    config = json.loads(cfg.read_text())
    assert "mcpServers" in config
    entry = config["mcpServers"]["llm_router"]
    assert entry["command"] == "llm_router"
    # Args may be empty list, but the key must be present
    assert "args" in entry


@pytest.mark.parametrize(
    "slug,module,class_name,host_name", MCP_CONFIG_HOSTS,
    ids=[h[3] for h in MCP_CONFIG_HOSTS],
)
def test_mcp_config_adapter_install_uses_real_binary(
    slug, module, class_name, host_name, tmp_path
):
    """The command must be a recognizable LLM Router launcher, not legacy."""
    import importlib
    mod = importlib.import_module(module)
    cls = getattr(mod, class_name)
    cfg = tmp_path / "mcp.json"
    cls(config_path=cfg).install(server_command=["llm_router"])

    config = json.loads(cfg.read_text())
    entry = config["mcpServers"]["llm_router"]
    cmd = entry.get("command", "")
    assert "llm_router" in cmd or cmd in ("uvx", "python", "python3"), (
        f"{host_name} adapter wrote unrecognized command: {cmd!r}"
    )


@pytest.mark.parametrize(
    "slug,module,class_name,host_name", MCP_CONFIG_HOSTS,
    ids=[h[3] for h in MCP_CONFIG_HOSTS],
)
def test_mcp_config_adapter_handles_user_override_command(
    slug, module, class_name, host_name, tmp_path
):
    """A power user might run llm_router via `uvx llm-routing` — the adapter
    must record whatever command was passed without rewriting it."""
    import importlib
    mod = importlib.import_module(module)
    cls = getattr(mod, class_name)
    cfg = tmp_path / "mcp.json"
    cls(config_path=cfg).install(server_command=["uvx", "llm-routing"])

    config = json.loads(cfg.read_text())
    entry = config["mcpServers"]["llm_router"]
    assert entry["command"] == "uvx"
    assert entry["args"] == ["llm-routing"]


# ════════════════════════════════════════════════════════════════════════
# Cross-host: version sync across all plugin manifests
# ════════════════════════════════════════════════════════════════════════

def test_marketplace_plugin_versions_match_pyproject():
    """All marketplace plugin versions must match pyproject.toml — drift
    causes the host to show 'llm_router v10.1.3' in the marketplace while
    the runtime is v0.0.2 (confusing the user)."""
    import tomllib
    pyproj = tomllib.loads((ROOT / "pyproject.toml").read_text())
    expected = pyproj["project"]["version"]

    for plugin_dir, host_name in MARKETPLACE_PLUGINS:
        plugin = json.loads((ROOT / plugin_dir / "plugin.json").read_text())
        assert plugin["version"] == expected, (
            f"{host_name} plugin.json version {plugin['version']} "
            f"!= pyproject {expected}"
        )

        mp = json.loads((ROOT / plugin_dir / "marketplace.json").read_text())
        if "version" in mp:
            assert mp["version"] == expected
        for p in mp.get("plugins", []):
            assert p.get("version") == expected, (
                f"{host_name} marketplace.json plugin {p.get('name')} "
                f"version {p.get('version')} != pyproject {expected}"
            )


# ════════════════════════════════════════════════════════════════════════
# Documentation: every supported plugin install path is exercised
# ════════════════════════════════════════════════════════════════════════

def test_four_hosts_have_complete_plugin_coverage():
    """The four explicitly supported plugin hosts (Claude Code, Cursor,
    Gemini CLI, Codex CLI) must each have a working integration path."""
    coverage = {
        "Claude Code": (ROOT / ".claude-plugin" / ".mcp.json").exists(),
        "Codex CLI": (ROOT / ".codex-plugin" / ".mcp.json").exists(),
        "Cursor": True,  # adapter-driven; covered by tests above
        "Gemini CLI": True,  # adapter-driven; covered by tests above
    }
    missing = [h for h, ok in coverage.items() if not ok]
    assert not missing, f"Hosts missing plugin coverage: {missing}"
