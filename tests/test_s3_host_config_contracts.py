"""S3 — the config llm-router writes for each host must match that host's schema
AND be executable as written.

Only four of the seven supported hosts exist on this machine (Claude Code, `codex`,
`opencode`, Cursor), so live end-to-end runs cannot cover the rest. These are the
contract tests that can: they assert the shape of what gets written and, more
usefully, whether the thing written can actually start.

The second half is what the live evidence says matters. Inspecting this machine's
real configs found the schema correct everywhere and the *command* wrong in ways no
schema check would catch:

    ~/.codex/config.toml
        command = "/Users/.../llm-router/.venv/bin/llm-router"   <- absolute, works

    ~/.cursor/mcp.json
        "command": "uvx", "args": ["claude-code-llm-router"]     <- three problems

The Cursor entry (1) names the PUBLISHED package, so Cursor runs a different build
than the checkout being developed here; (2) spawns via `uvx`, which lives at
`~/.local/bin/uvx` and is therefore absent from the PATH a Finder-launched app gets
(`/usr/bin:/bin:/usr/sbin:/sbin`), so the server cannot start at all; and (3) is
keyed `llm-router`, which is NOT a stale spelling of this project — it is the
separate upstream product `tests/qa/test_multi_host_coexistence.py` exists to
coexist with. So the adapter reporting "not installed" is correct: this llm_router
is simply absent from Cursor, and the entry that is there belongs to something else.

A GUI editor is not a login shell. Any host launched from Finder or the Dock needs
an absolute interpreter path, and "it works when I start it from my terminal" is not
evidence that it works.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from llm_router.hosts.cursor import CursorAdapter

# What a macOS app launched from Finder/Dock actually inherits.
GUI_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

SERVER_KEY = "llm_router"


def _resolvable_from_gui(command: str) -> bool:
    """Whether *command* can be spawned by a GUI-launched host."""
    if os.path.isabs(command):
        return os.path.exists(command)
    return shutil.which(command, path=GUI_PATH) is not None


# ── schema: the key each host actually reads ────────────────────────────────

def test_vscode_config_uses_the_servers_key():
    from llm_router.install_hooks import _VSCODE_MCP_CONTENT

    cfg = json.loads(_VSCODE_MCP_CONTENT)
    assert "servers" in cfg, "VS Code reads `servers`, not `mcpServers`"
    entry = cfg["servers"][SERVER_KEY]
    assert entry["type"] == "stdio"


def test_windsurf_config_uses_the_mcpservers_key():
    from llm_router.install_hooks import _WINDSURF_MCP_CONTENT

    cfg = json.loads(_WINDSURF_MCP_CONTENT)
    assert "mcpServers" in cfg, "Windsurf reads `mcpServers`, not `servers`"
    assert SERVER_KEY in cfg["mcpServers"]


def test_cursor_rule_file_carries_valid_mdc_frontmatter():
    from llm_router.install_hooks import _CURSOR_RULE_CONTENT

    assert _CURSOR_RULE_CONTENT.startswith("---\n")
    head = _CURSOR_RULE_CONTENT.split("---", 2)[1]
    for key in ("description:", "globs:", "alwaysApply:"):
        assert key in head, f"Cursor .mdc frontmatter missing {key}"


# ── naming: one server key across every host ────────────────────────────────

def test_every_host_uses_the_same_server_key():
    """One canonical key, so a host config is unambiguous about whose server it is."""
    from llm_router.install_hooks import _VSCODE_MCP_CONTENT, _WINDSURF_MCP_CONTENT

    assert SERVER_KEY in json.loads(_VSCODE_MCP_CONTENT)["servers"]
    assert SERVER_KEY in json.loads(_WINDSURF_MCP_CONTENT)["mcpServers"]

    adapter = CursorAdapter(config_path=Path("/nonexistent/mcp.json"))
    assert adapter.is_installed() is False


def test_cursor_adapter_writes_the_canonical_key(tmp_path):
    cfg = tmp_path / "mcp.json"
    CursorAdapter(config_path=cfg).install(["/abs/path/llm-router"])
    assert SERVER_KEY in json.loads(cfg.read_text())["mcpServers"]


def test_cursor_adapter_preserves_other_servers(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"someone_elses": {"command": "x"}}}))
    CursorAdapter(config_path=cfg).install(["/abs/path/llm-router"])
    servers = json.loads(cfg.read_text())["mcpServers"]
    assert "someone_elses" in servers, "clobbered a user's own MCP server"
    assert SERVER_KEY in servers


def test_a_hyphen_keyed_entry_is_a_DIFFERENT_product_not_a_stale_key(tmp_path):
    """`llm-router` is the upstream this was forked from, not an old spelling.

    Worth pinning from this direction too. Reading `~/.cursor/mcp.json` on this
    machine — keyed `llm-router`, spawning `uvx claude-code-llm-router` — the
    obvious conclusion is a stale entry the adapter has gone blind to, and the
    obvious fix is to teach `is_installed()` both spellings. That is wrong, and
    `tests/qa/test_multi_host_coexistence.py` says why: the two are separate
    products that a user may run side by side during migration, disambiguated by
    the MCP namespace prefix. Claiming the hyphen key would make an install report
    success it did not achieve, and an uninstall delete someone else's server.

    So the adapter is right and the machine simply has the OTHER product
    configured, with this one absent from Cursor entirely.
    """
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"llm-router": {"command": "uvx"}}}))
    assert CursorAdapter(config_path=cfg).is_installed() is False


# ── executability: the half a schema check cannot see ───────────────────────

def test_a_gui_launched_host_can_spawn_what_we_write(tmp_path):
    """An absolute interpreter path is the only thing a Finder-launched app can run."""
    cfg = tmp_path / "mcp.json"
    CursorAdapter(config_path=cfg).install(["/usr/bin/env"])
    command = json.loads(cfg.read_text())["mcpServers"][SERVER_KEY]["command"]
    assert _resolvable_from_gui(command), (
        f"{command!r} is not resolvable from a GUI-launched host "
        f"(PATH={GUI_PATH}) — the MCP server will not start"
    )


def test_a_bare_command_is_recognised_as_unresolvable():
    """The check must actually be able to fail, or it proves nothing."""
    assert _resolvable_from_gui("uvx") is False
    assert _resolvable_from_gui("llm-router") is False
    assert _resolvable_from_gui("/bin/sh") is True


@pytest.mark.parametrize("host_file,key_path", [
    (Path.home() / ".cursor" / "mcp.json", ("mcpServers",)),
])
def test_live_host_config_is_startable(host_file, key_path):
    """Against this machine's real config, when it exists.

    Skips in CI and on any machine without the host installed — this asserts about
    the developer's actual setup, which is where the failure lives.
    """
    if not host_file.exists():
        pytest.skip(f"{host_file} not present")
    cfg = json.loads(host_file.read_text())
    for k in key_path:
        cfg = cfg.get(k, {})
    entries = {k: v for k, v in cfg.items() if "llm" in k.lower() and "router" in k.lower()}
    if not entries:
        pytest.skip("no llm-router entry in this host's config")
    for name, entry in entries.items():
        command = entry.get("command", "")
        assert _resolvable_from_gui(command), (
            f"{host_file}: server {name!r} spawns {command!r}, which a "
            f"GUI-launched host cannot resolve (PATH={GUI_PATH})"
        )
