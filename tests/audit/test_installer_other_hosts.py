"""Audit — Section 8: Installer/doctor self-heal, extended to other hosts.

Context: earlier today, ``_install_codex_gateway_config`` in
``llm_router/commands/install.py`` was found (and fixed) to force Codex CLI's
GLOBAL default ``model_provider`` to "llm_router" in ``~/.codex/config.toml``,
silently routing EVERY Codex call (interactive and LLM Router's own dispatch)
through the local gateway, which didn't speak Codex's exact wire format —
breaking Codex CLI outright. ``tests/test_codex_gateway_install.py`` covers
that fix and its self-heal behavior.

This file extends the same scrutiny to every OTHER host-specific installer
function in ``llm_router/commands/install.py``:

    _install_opencode_files      (OpenCode)
    _install_gemini_cli_files    (Gemini CLI)
    _install_copilot_cli_files   (GitHub Copilot CLI)
    _install_openclaw_files      (OpenClaw)
    _install_trae_files          (Trae IDE)
    _install_factory_files       (Factory Droid)
    _install_vscode_files        (VS Code)
    _install_cursor_files        (Cursor IDE)

For each, the question is exactly the one that burned Codex: does the
installer force any GLOBAL DEFAULT / ACTIVE-PROVIDER scalar key in the
target tool's own config — something that would silently change that
tool's own (non-LLM Router) behavior — or does it only ADD an inert MCP server
registration / hook entry / instructions file that requires the tool (or
user) to explicitly invoke LLM Router before it does anything?

Finding from reading every function in install.py: none of the other
host installers write a TOML config or any "default model / default
provider" scalar at all. They all write to a nested `mcpServers` /
`servers` dict inside a JSON file (merged additively, skipped if already
present), or append prose instructions to a markdown file. Both patterns
are inert until the tool/user explicitly invokes the `llm_router` MCP server
or reads the instructions — structurally different from Codex's
config.toml top-level `model`/`model_provider` scalars, which Codex's
own client reads on every single call with no opt-in step. No other-host
equivalent of the Codex bug was found; see REPORT_B.md.
"""
from __future__ import annotations

import json
import pathlib

import pytest


def _patch_home(monkeypatch, tmp_path):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))


# Keys that would indicate a "forced default / active provider" write —
# the exact shape of the Codex bug (model_provider / model as a top-level
# scalar Codex reads unconditionally on every call). None of the JSON files
# below should ever contain these at the top level.
_DANGEROUS_TOP_LEVEL_KEYS = {
    "model", "model_provider", "defaultModel", "defaultProvider",
    "activeProvider", "activeModel", "provider", "selectedModel",
}


def _assert_json_has_no_forced_default_keys(path: pathlib.Path) -> None:
    assert path.exists(), f"expected config file at {path}"
    data = json.loads(path.read_text())
    found = _DANGEROUS_TOP_LEVEL_KEYS & set(data.keys())
    assert not found, (
        f"{path} contains top-level default/active-provider-style keys "
        f"{found} — this is the exact risk pattern that broke Codex CLI "
        f"(forcing a global default rather than just registering)."
    )


# ── OpenCode ──────────────────────────────────────────────────────────────


def test_opencode_install_only_registers_mcp_server(monkeypatch, tmp_path):
    from llm_router.commands.install import _install_opencode_files

    _patch_home(monkeypatch, tmp_path)
    actions = _install_opencode_files()

    config_json = tmp_path / ".config" / "opencode" / "config.json"
    _assert_json_has_no_forced_default_keys(config_json)
    data = json.loads(config_json.read_text())
    assert set(data.keys()) == {"mcpServers"}
    assert data["mcpServers"]["llm_router"] == {"command": "llm_router", "args": []}
    assert any("llm_router MCP server" in a for a in actions)


def test_opencode_install_is_idempotent_and_additive(monkeypatch, tmp_path):
    """Re-running the installer must not duplicate or overwrite the entry,
    and must not introduce any new top-level key."""
    from llm_router.commands.install import _install_opencode_files

    _patch_home(monkeypatch, tmp_path)
    _install_opencode_files()
    config_json = tmp_path / ".config" / "opencode" / "config.json"
    first = json.loads(config_json.read_text())

    _install_opencode_files()
    second = json.loads(config_json.read_text())
    assert first == second


# ── Gemini CLI ────────────────────────────────────────────────────────────


def test_gemini_cli_install_settings_json_only_registers_mcp_server(monkeypatch, tmp_path):
    from llm_router.commands.install import _install_gemini_cli_files

    _patch_home(monkeypatch, tmp_path)
    _install_gemini_cli_files()

    settings_json = tmp_path / ".gemini" / "settings.json"
    _assert_json_has_no_forced_default_keys(settings_json)
    data = json.loads(settings_json.read_text())
    assert set(data.keys()) == {"mcpServers"}


def test_gemini_cli_install_extension_manifest_has_no_default_keys(monkeypatch, tmp_path):
    """The extension manifest carries name/version/description/mcpServers —
    never a scalar that would change Gemini CLI's own default model."""
    from llm_router.commands.install import _install_gemini_cli_files

    _patch_home(monkeypatch, tmp_path)
    _install_gemini_cli_files()

    manifest = tmp_path / ".gemini" / "extensions" / "llm_router" / "gemini-extension.json"
    assert manifest.exists()
    data = json.loads(manifest.read_text())
    found = _DANGEROUS_TOP_LEVEL_KEYS & set(data.keys())
    assert not found, f"gemini-extension.json has forced-default keys: {found}"
    assert set(data.keys()) == {"name", "version", "description", "mcpServers"}


def test_gemini_cli_install_hooks_json_only_adds_hook_entries(monkeypatch, tmp_path):
    """hooks.json entries are lifecycle-event registrations (SessionStart,
    PostToolUse, UserPromptSubmit, SessionEnd) — these fire on Gemini CLI's
    own lifecycle events regardless of LLM Router, which is expected (that's
    what a hook IS), but critically they do NOT change what MODEL Gemini
    CLI uses by default. Confirm the hooks payload carries no model/provider
    scalar."""
    from llm_router.commands.install import _install_gemini_cli_files

    _patch_home(monkeypatch, tmp_path)
    _install_gemini_cli_files()

    hooks_json = tmp_path / ".gemini" / "extensions" / "llm_router" / "hooks" / "hooks.json"
    assert hooks_json.exists()
    text = hooks_json.read_text()
    for key in ("model_provider", '"model"', "defaultModel", "activeProvider"):
        assert key not in text, f"hooks.json unexpectedly contains {key!r}"


# ── Copilot CLI ───────────────────────────────────────────────────────────


def test_copilot_cli_install_only_registers_mcp_server(monkeypatch, tmp_path):
    from llm_router.commands.install import _install_copilot_cli_files

    _patch_home(monkeypatch, tmp_path)
    _install_copilot_cli_files()

    mcp_json = tmp_path / ".config" / "gh" / "copilot" / "mcp.json"
    _assert_json_has_no_forced_default_keys(mcp_json)
    data = json.loads(mcp_json.read_text())
    assert set(data.keys()) == {"mcpServers"}


# ── OpenClaw ──────────────────────────────────────────────────────────────


def test_openclaw_install_only_registers_mcp_server(monkeypatch, tmp_path):
    from llm_router.commands.install import _install_openclaw_files

    _patch_home(monkeypatch, tmp_path)
    _install_openclaw_files()

    mcp_json = tmp_path / ".openclaw" / "mcp.json"
    _assert_json_has_no_forced_default_keys(mcp_json)
    data = json.loads(mcp_json.read_text())
    assert set(data.keys()) == {"mcpServers"}


# ── Trae IDE ──────────────────────────────────────────────────────────────


def test_trae_install_only_registers_mcp_server(monkeypatch, tmp_path):
    from llm_router.commands.install import _install_trae_files

    _patch_home(monkeypatch, tmp_path)
    # Run from a scratch cwd so the ".rules" file this installer writes to
    # the CURRENT DIRECTORY doesn't land in the real repo checkout.
    monkeypatch.chdir(tmp_path)
    _install_trae_files()

    import sys
    if sys.platform == "darwin":
        mcp_json = tmp_path / "Library" / "Application Support" / "Trae" / "mcp.json"
    elif sys.platform == "win32":
        mcp_json = tmp_path / "AppData" / "Roaming" / "Trae" / "mcp.json"
    else:
        mcp_json = tmp_path / ".config" / "Trae" / "mcp.json"

    _assert_json_has_no_forced_default_keys(mcp_json)
    data = json.loads(mcp_json.read_text())
    assert set(data.keys()) == {"mcpServers"}


# ── Factory Droid ─────────────────────────────────────────────────────────


def test_factory_install_writes_no_config_files_at_all(monkeypatch, tmp_path):
    """Factory Droid's installer only prints guidance — it must not write
    anything under the patched HOME (nothing to force a default with)."""
    from llm_router.commands.install import _install_factory_files

    _patch_home(monkeypatch, tmp_path)
    actions = _install_factory_files()

    written = list(tmp_path.rglob("*"))
    assert written == [], f"Factory installer unexpectedly wrote files: {written}"
    assert isinstance(actions, list) and actions


# ── VS Code ───────────────────────────────────────────────────────────────


def test_vscode_install_only_registers_mcp_server_under_servers_key(monkeypatch, tmp_path):
    """VS Code's mcp.json uses root_key='servers' (not 'mcpServers') — confirm
    that's the ONLY top-level key written, no editor-default scalar."""
    from llm_router.commands.install import _install_vscode_files

    _patch_home(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    _install_vscode_files()

    import sys
    if sys.platform == "darwin":
        mcp_json = tmp_path / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
    elif sys.platform == "win32":
        mcp_json = tmp_path / "AppData" / "Roaming" / "Code" / "User" / "mcp.json"
    else:
        mcp_json = tmp_path / ".config" / "Code" / "User" / "mcp.json"

    assert mcp_json.exists()
    data = json.loads(mcp_json.read_text())
    found = _DANGEROUS_TOP_LEVEL_KEYS & set(data.keys())
    assert not found, f"VS Code mcp.json has forced-default keys: {found}"
    assert set(data.keys()) == {"servers"}
    assert data["servers"]["llm_router"] == {"command": "llm_router", "args": []}


# ── Cursor IDE ────────────────────────────────────────────────────────────


def test_cursor_install_only_registers_mcp_server(monkeypatch, tmp_path):
    from llm_router.commands.install import _install_cursor_files

    _patch_home(monkeypatch, tmp_path)
    _install_cursor_files()

    mcp_json = tmp_path / ".cursor" / "mcp.json"
    _assert_json_has_no_forced_default_keys(mcp_json)
    data = json.loads(mcp_json.read_text())
    assert set(data.keys()) == {"mcpServers"}

    rules_md = tmp_path / ".cursor" / "rules" / "llm_router.md"
    assert rules_md.exists()
    # Rules are prose instructions, not enforced config — confirm it's
    # plain markdown text, not a machine-parsed default-provider setting.
    text = rules_md.read_text()
    assert not text.strip().startswith("{")  # not JSON/TOML-shaped


# ── Cross-host generic invariant ─────────────────────────────────────────


@pytest.mark.parametrize(
    "installer_name,relative_json_paths",
    [
        ("_install_opencode_files", [(".config", "opencode", "config.json")]),
        ("_install_copilot_cli_files", [(".config", "gh", "copilot", "mcp.json")]),
        ("_install_openclaw_files", [(".openclaw", "mcp.json")]),
        ("_install_cursor_files", [(".cursor", "mcp.json")]),
    ],
)
def test_no_other_host_writes_a_forced_default_scalar(
    monkeypatch, tmp_path, installer_name, relative_json_paths
):
    """Generic sweep: for every simple (non-platform-branching) other-host
    installer, every JSON file it writes contains ONLY registration-style
    top-level keys (mcpServers/servers) — the Codex-class risk pattern
    (top-level model/model_provider/default* scalar) is absent everywhere
    outside the already-fixed Codex path."""
    import llm_router.commands.install as install_mod

    _patch_home(monkeypatch, tmp_path)
    installer = getattr(install_mod, installer_name)
    installer()

    for parts in relative_json_paths:
        path = tmp_path.joinpath(*parts)
        _assert_json_has_no_forced_default_keys(path)


def test_no_other_host_writes_any_toml_config(monkeypatch, tmp_path):
    """The Codex bug was specifically possible because config.toml has
    unconditional top-level scalars Codex reads on every call. Confirm no
    OTHER host installer writes any .toml file at all under HOME — if one
    started doing so, it would need the same self-heal scrutiny as Codex."""
    from llm_router.commands.install import (
        _install_copilot_cli_files,
        _install_cursor_files,
        _install_gemini_cli_files,
        _install_openclaw_files,
        _install_opencode_files,
        _install_trae_files,
        _install_vscode_files,
    )

    _patch_home(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    for fn in (
        _install_opencode_files,
        _install_gemini_cli_files,
        _install_copilot_cli_files,
        _install_openclaw_files,
        _install_trae_files,
        _install_vscode_files,
        _install_cursor_files,
    ):
        fn()

    toml_files = list(tmp_path.rglob("*.toml"))
    assert toml_files == [], f"Unexpected TOML config written by a non-Codex installer: {toml_files}"
