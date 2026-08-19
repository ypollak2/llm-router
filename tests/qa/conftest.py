"""Shared fixtures + host-coverage matrix for the QA suite.

The HOSTS list is the single source of truth for which hosts the QA
suite exercises. Adding a new host = appending one HostSpec; every
parametrized test picks it up automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class HostSpec:
    """One host the QA suite exercises.

    Attributes:
        id: short identifier (matches CLI --host name when applicable)
        name: human-readable
        plugin_dir: relative path to .{name}-plugin dir (or None)
        rules_file: filename of *-rules.md under src/llm_router/rules/ (or None)
        python_adapter: module path under llm_router.hosts (or None)
        adapter_class: class name to import from python_adapter
    """

    id: str
    name: str
    plugin_dir: str | None = None
    rules_file: str | None = None
    python_adapter: str | None = None
    adapter_class: str | None = None

    @property
    def plugin_path(self) -> Path | None:
        return ROOT / self.plugin_dir if self.plugin_dir else None

    @property
    def rules_path(self) -> Path | None:
        if not self.rules_file:
            return None
        return ROOT / "src" / "llm_router" / "rules" / self.rules_file


# OpenClaw + OpenCode deferred per user request.
HOSTS: list[HostSpec] = [
    HostSpec("claude-code",   "Claude Code",
             plugin_dir=".claude-plugin", rules_file="llm_router.md"),
    HostSpec("claude-desktop", "Claude Desktop",
             rules_file="desktop-rules.md"),
    HostSpec("cursor",        "Cursor",
             rules_file="cursor-rules.md",
             python_adapter="llm_router.hosts.cursor", adapter_class="CursorAdapter"),
    HostSpec("codex-cli",     "Codex CLI",
             plugin_dir=".codex-plugin", rules_file="codex-rules.md"),
    HostSpec("vscode",        "Codex / VS Code",
             rules_file="vscode-rules.md"),
    HostSpec("gemini-cli",    "Gemini CLI",
             rules_file="gemini-cli-rules.md",
             python_adapter="llm_router.hosts.gemini_cli", adapter_class="GeminiCliAdapter"),
    HostSpec("gemini",        "Gemini",
             rules_file="gemini-rules.md"),
    HostSpec("copilot",       "GitHub Copilot",
             rules_file="copilot-rules.md"),
    HostSpec("copilot-cli",   "Copilot CLI",
             rules_file="copilot-cli-rules.md"),
    HostSpec("factory",       "Factory IDE",
             plugin_dir=".factory-plugin"),
    HostSpec("trae",          "Trae IDE",
             rules_file="trae-rules.md"),
    HostSpec("pi",            "PI",
             rules_file="pi-rules.md"),
]


@pytest.fixture(params=HOSTS, ids=lambda h: h.id)
def host(request) -> HostSpec:
    """Inject each host once per parametrized test."""
    return request.param


@pytest.fixture(scope="session")
def hosts_with_adapter() -> list[HostSpec]:
    """Hosts that expose a Python install adapter — those get the full
    install/uninstall test battery."""
    return [h for h in HOSTS if h.python_adapter]


@pytest.fixture(scope="session")
def hosts_with_plugin() -> list[HostSpec]:
    return [h for h in HOSTS if h.plugin_dir]


@pytest.fixture(scope="session")
def hosts_with_rules() -> list[HostSpec]:
    return [h for h in HOSTS if h.rules_file]


@pytest.fixture
def fresh_temp(tmp_path: Path) -> Path:
    """A guaranteed-empty subdirectory; some adapters fail on shared dirs."""
    sub = tmp_path / "fresh"
    sub.mkdir()
    return sub


def load_adapter(spec: HostSpec):
    """Import and instantiate the Python adapter for spec; skip if none."""
    if not spec.python_adapter or not spec.adapter_class:
        pytest.skip(f"{spec.id} has no Python adapter")
    import importlib

    module = importlib.import_module(spec.python_adapter)
    return getattr(module, spec.adapter_class)
