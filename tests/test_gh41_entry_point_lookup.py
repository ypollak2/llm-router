"""Regression: GH#41 — MCP registration must find the real console script.

`[project.scripts]` declares only the HYPHENATED `llm-router`; there is no
`llm_router` console script on any install type. Every registration site used
`shutil.which("llm_router")`, which therefore always returned None:

  * install_hooks.py:723  (Claude Desktop) fell back to the literal string
    "llm_router" — a command that can never resolve.
  * install_hooks.py:877  (main MCP entry) fell back to
    `uv run --directory <site-packages>` — valid only for a source checkout.
    Against a pipx venv this is what produced CONNECTION_CLOSED.
  * install_hooks.py:1234 (claw-code) fell back to the literal string too.

Reported against a clean-slate `pipx install "llm-routing[cli]"` of 13.0.2,
where `doctor` reported 0 issues while `claude mcp list` showed
llm_router as CONNECTION_CLOSED.
"""
from __future__ import annotations

import json

import llm_router.install_hooks as ih


def _pipx_which(bin_path: str):
    """Simulate a pipx install: only the hyphenated entry point exists."""
    def _which(name: str):
        return bin_path if name == "llm-router" else None
    return _which


def test_router_bin_prefers_hyphenated_entry_point(monkeypatch):
    monkeypatch.setattr(ih.shutil, "which", _pipx_which("/opt/pipx/venvs/llm-routing/bin/llm-router"))
    assert ih._router_bin() == "/opt/pipx/venvs/llm-routing/bin/llm-router"


def test_router_bin_still_finds_underscore_alias(monkeypatch):
    """A dev checkout that exposes the legacy underscore name must still work."""
    monkeypatch.setattr(ih.shutil, "which", lambda n: "/dev/bin/llm_router" if n == "llm_router" else None)
    assert ih._router_bin() == "/dev/bin/llm_router"


def test_router_bin_returns_none_when_nothing_resolves(monkeypatch):
    monkeypatch.setattr(ih.shutil, "which", lambda n: None)
    assert ih._router_bin() is None


def test_mcp_entry_on_pipx_uses_binary_not_uv_run(monkeypatch):
    """The bug: pipx install produced a `uv run --directory <site-packages>` entry."""
    monkeypatch.setattr(ih.shutil, "which", _pipx_which("/opt/pipx/venvs/llm-routing/bin/llm-router"))
    entry, actions = ih._build_mcp_entry()
    assert entry is not None
    assert entry["command"] == "/opt/pipx/venvs/llm-routing/bin/llm-router"
    assert entry["args"] == []
    assert "uv" not in json.dumps(entry)


def test_mcp_entry_falls_back_to_uv_run_only_for_source_checkout(monkeypatch, tmp_path):
    """Dev checkouts keep the uv run path — but only when a pyproject.toml is really there."""
    pkg = tmp_path / "src" / "llm_router"
    pkg.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='llm-routing'\n")
    monkeypatch.setattr(ih, "_PACKAGE_DIR", pkg)
    monkeypatch.setattr(ih.shutil, "which", lambda n: "/usr/bin/uv" if n == "uv" else None)

    entry, actions = ih._build_mcp_entry()
    assert entry is not None
    assert entry["command"] == "/usr/bin/uv"
    assert entry["args"] == ["run", "--directory", str(tmp_path), "llm-router"]


def test_mcp_entry_warns_instead_of_writing_dead_command(monkeypatch, tmp_path):
    """No entry point and no checkout: warn loudly, never write an unresolvable command."""
    pkg = tmp_path / "site-packages" / "llm_router"
    pkg.mkdir(parents=True)
    monkeypatch.setattr(ih, "_PACKAGE_DIR", pkg)
    monkeypatch.setattr(ih.shutil, "which", lambda n: None)

    entry, actions = ih._build_mcp_entry()
    assert entry is None
    assert any("WARN" in a for a in actions), actions


def test_claude_desktop_registers_resolved_binary(monkeypatch, tmp_path):
    """GH#41: the desktop entry was the bare literal "llm_router"."""
    cfg = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(ih, "claude_desktop_config_path", lambda: cfg)
    monkeypatch.setattr(ih.shutil, "which", _pipx_which("/opt/pipx/venvs/llm-routing/bin/llm-router"))

    ih._install_claude_desktop()
    written = json.loads(cfg.read_text())["mcpServers"]["llm_router"]
    assert written["command"] == "/opt/pipx/venvs/llm-routing/bin/llm-router"


def test_claude_desktop_skips_when_binary_unresolvable(monkeypatch, tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(ih, "claude_desktop_config_path", lambda: cfg)
    monkeypatch.setattr(ih, "_PACKAGE_DIR", tmp_path / "site-packages" / "llm_router")
    monkeypatch.setattr(ih.shutil, "which", lambda n: None)

    actions = ih._install_claude_desktop()
    assert any("WARN" in a for a in actions), actions
    assert not cfg.exists(), "must not write a command that cannot resolve"
