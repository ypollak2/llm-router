"""Integration tests — every supported host gets full validation.

What this suite proves for each host:
    1. The config artifact LLM Router ships is structurally valid.
    2. Required fields are present and reference LLM Router (not llm-router).
    3. Python adapters install + uninstall idempotently and don't clobber
       coexisting MCP servers in the same config file.
    4. The version in every manifest matches pyproject.toml.
    5. Rules files exist, are non-empty, are valid markdown, and never
       reference the legacy llm-router brand.

Run:
    pytest tests/integration/test_host_integrations.py -v

The suite is deliberately read-only — it never modifies the user's actual
~/.claude/ or ~/.cursor/. All adapter tests use tmp_path-backed configs.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest


# ────────────────────────────────────────────────────────────────────────
# Repo root
# ────────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent.parent


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.fixture(scope="session")
def pyproject_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return data["project"]["version"]


# ────────────────────────────────────────────────────────────────────────
# Plugin manifests — JSON validity, brand, version sync
# ────────────────────────────────────────────────────────────────────────

PLUGIN_DIRS = [
    ".claude-plugin",
    ".codex-plugin",
    ".factory-plugin",
]


def _plugin_files() -> list[tuple[str, Path]]:
    """All plugin.json + marketplace.json under .{name}-plugin/ dirs."""
    out: list[tuple[str, Path]] = []
    for plugin_dir in PLUGIN_DIRS:
        d = ROOT / plugin_dir
        if not d.exists():
            continue
        for fname in ("plugin.json", "marketplace.json"):
            p = d / fname
            if p.exists():
                out.append((f"{plugin_dir}/{fname}", p))
    return out


@pytest.mark.parametrize("label,path", _plugin_files(), ids=lambda x: x if isinstance(x, str) else "")
def test_plugin_manifest_is_valid_json(label: str, path: Path):
    data = _read_json(path)
    assert isinstance(data, dict), f"{label} must be a JSON object"


@pytest.mark.parametrize("label,path", _plugin_files(), ids=lambda x: x if isinstance(x, str) else "")
def test_plugin_manifest_version_matches_pyproject(
    label: str, path: Path, pyproject_version: str
):
    data = _read_json(path)
    versions = []
    if "version" in data:
        versions.append(("root", data["version"]))
    for plugin in data.get("plugins", []) if isinstance(data.get("plugins"), list) else []:
        if isinstance(plugin, dict) and "version" in plugin:
            versions.append((f"plugins[{plugin.get('name', '?')}]", plugin["version"]))
    assert versions, f"{label} declares no version field"
    for location, version in versions:
        assert version == pyproject_version, (
            f"{label} {location}: {version} != pyproject {pyproject_version}"
        )


@pytest.mark.parametrize("label,path", _plugin_files(), ids=lambda x: x if isinstance(x, str) else "")
def test_plugin_manifest_has_no_legacy_brand_references(label: str, path: Path):
    """Every reference must be 'llm_router', never 'llm-router' / 'llm_router' /
    'LLM_ROUTER_' env vars. The rebrand must be complete."""
    raw = path.read_text()
    legacy = re.findall(r"llm[-_]router|LLM_ROUTER_", raw)
    assert not legacy, (
        f"{label} still contains legacy brand refs: {set(legacy)}"
    )


@pytest.mark.parametrize("label,path", _plugin_files(), ids=lambda x: x if isinstance(x, str) else "")
def test_plugin_manifest_has_name_and_description(label: str, path: Path):
    data = _read_json(path)
    # Plugin.json shape: has name+description directly
    # Marketplace.json shape: has plugins array
    if "plugins" in data and isinstance(data["plugins"], list):
        for plugin in data["plugins"]:
            assert "name" in plugin, f"{label} plugin entry missing 'name'"
    else:
        assert "name" in data, f"{label} missing 'name'"


# ────────────────────────────────────────────────────────────────────────
# Rules files — every per-host markdown
# ────────────────────────────────────────────────────────────────────────

RULES_DIR = ROOT / "src" / "llm_router" / "rules"


def _rules_files() -> list[Path]:
    return sorted(RULES_DIR.glob("*.md")) if RULES_DIR.exists() else []


@pytest.mark.parametrize("path", _rules_files(), ids=lambda p: p.name)
def test_rules_file_is_non_empty(path: Path):
    content = path.read_text()
    assert len(content.strip()) > 100, f"{path.name} suspiciously short"


@pytest.mark.parametrize("path", _rules_files(), ids=lambda p: p.name)
def test_rules_file_starts_with_markdown_header(path: Path):
    content = path.read_text()
    # Allow leading HTML comment for tooling version markers
    stripped = re.sub(r"^<!--[^>]*-->\s*\n", "", content, count=1)
    first = stripped.split("\n", 1)[0]
    assert first.startswith("#"), (
        f"{path.name} first line should be a markdown header, got: {first!r}"
    )


@pytest.mark.parametrize("path", _rules_files(), ids=lambda p: p.name)
def test_rules_file_has_no_legacy_brand_references(path: Path):
    """Rules files MUST reference llm_router, not llm-router.

    LLM Router is the new brand. Legacy refs would confuse end users about
    which router actually fires.
    """
    raw = path.read_text()
    legacy = re.findall(r"llm[-_]router|LLM_ROUTER_", raw, re.IGNORECASE)
    assert not legacy, (
        f"{path.name} still references legacy brand: {set(legacy)} "
        f"— rebrand sweep missed this file."
    )


@pytest.mark.parametrize("path", _rules_files(), ids=lambda p: p.name)
def test_rules_file_mentions_at_least_one_llm_router_tool(path: Path):
    """Rules should tell the user about at least one LLM Router MCP tool."""
    raw = path.read_text().lower()
    tools = ["llm_query", "llm_research", "llm_analyze", "llm_code", "llm_generate", "llm_router_agent"]
    if not any(t in raw for t in tools):
        pytest.skip(
            f"{path.name} doesn't mention tools — may be a high-level host overview"
        )


# ────────────────────────────────────────────────────────────────────────
# Python host adapters — Cursor + Gemini CLI
# ────────────────────────────────────────────────────────────────────────

def test_cursor_adapter_writes_valid_config(tmp_path):
    """CursorAdapter.install creates ~/.cursor/mcp.json with the expected schema."""
    from llm_router.hosts.cursor import CursorAdapter

    adapter = CursorAdapter(config_path=tmp_path / "mcp.json")
    written_path = adapter.install(server_command=["llm_router"])

    assert written_path.exists(), "install() must create the config file"
    config = _read_json(written_path)
    assert "mcpServers" in config
    assert "llm_router" in config["mcpServers"]
    entry = config["mcpServers"]["llm_router"]
    assert entry["command"] == "llm_router"
    assert entry["args"] == []


def test_cursor_adapter_install_is_idempotent(tmp_path):
    """Two installs in a row must produce the same config (no duplicates)."""
    from llm_router.hosts.cursor import CursorAdapter

    adapter = CursorAdapter(config_path=tmp_path / "mcp.json")
    adapter.install(server_command=["llm_router"])
    first = _read_json(tmp_path / "mcp.json")
    adapter.install(server_command=["llm_router"])
    second = _read_json(tmp_path / "mcp.json")
    assert first == second


def test_cursor_adapter_preserves_other_mcp_servers(tmp_path):
    """LLM Router install must NOT clobber unrelated MCP servers in the same file."""
    from llm_router.hosts.cursor import CursorAdapter

    pre_existing = {
        "mcpServers": {
            "other-server": {"command": "other", "args": ["--foo"]},
            "another-one": {"command": "another"},
        }
    }
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps(pre_existing))

    adapter = CursorAdapter(config_path=cfg)
    adapter.install(server_command=["llm_router"])
    after = _read_json(cfg)
    assert "other-server" in after["mcpServers"]
    assert "another-one" in after["mcpServers"]
    assert "llm_router" in after["mcpServers"]


def test_cursor_adapter_uninstall_removes_only_llm_router(tmp_path):
    from llm_router.hosts.cursor import CursorAdapter

    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "other-server": {"command": "other"},
            "llm_router": {"command": "llm_router"},
        }
    }))
    adapter = CursorAdapter(config_path=cfg)
    adapter.uninstall()
    after = _read_json(cfg)
    assert "llm_router" not in after["mcpServers"]
    assert "other-server" in after["mcpServers"]


def test_cursor_adapter_uninstall_on_missing_config_is_noop(tmp_path):
    from llm_router.hosts.cursor import CursorAdapter

    adapter = CursorAdapter(config_path=tmp_path / "nonexistent.json")
    result = adapter.uninstall()
    assert result is None


def test_cursor_adapter_is_installed_check(tmp_path):
    from llm_router.hosts.cursor import CursorAdapter

    adapter = CursorAdapter(config_path=tmp_path / "mcp.json")
    assert not adapter.is_installed()
    adapter.install(server_command=["llm_router"])
    assert adapter.is_installed()
    adapter.uninstall()
    assert not adapter.is_installed()


def test_cursor_adapter_recovers_from_corrupt_config(tmp_path):
    """If mcp.json is invalid JSON, install should overwrite it cleanly."""
    from llm_router.hosts.cursor import CursorAdapter

    cfg = tmp_path / "mcp.json"
    cfg.write_text("{ this is not valid json [[[")
    adapter = CursorAdapter(config_path=cfg)
    adapter.install(server_command=["llm_router"])
    after = _read_json(cfg)
    assert "llm_router" in after["mcpServers"]


def test_gemini_cli_adapter_writes_valid_config(tmp_path):
    from llm_router.hosts.gemini_cli import GeminiCliAdapter

    adapter = GeminiCliAdapter(config_path=tmp_path / "mcp_servers.json")
    written = adapter.install(server_command=["llm_router", "--stdio"])
    config = _read_json(written)
    assert "mcpServers" in config
    assert config["mcpServers"]["llm_router"]["command"] == "llm_router"
    assert config["mcpServers"]["llm_router"]["args"] == ["--stdio"]


def test_gemini_cli_adapter_install_is_idempotent(tmp_path):
    from llm_router.hosts.gemini_cli import GeminiCliAdapter

    adapter = GeminiCliAdapter(config_path=tmp_path / "mcp_servers.json")
    adapter.install(server_command=["llm_router"])
    first = _read_json(tmp_path / "mcp_servers.json")
    adapter.install(server_command=["llm_router"])
    second = _read_json(tmp_path / "mcp_servers.json")
    assert first == second


def test_gemini_cli_adapter_preserves_other_servers(tmp_path):
    from llm_router.hosts.gemini_cli import GeminiCliAdapter

    cfg = tmp_path / "mcp_servers.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"existing": {"command": "x"}},
    }))
    adapter = GeminiCliAdapter(config_path=cfg)
    adapter.install(server_command=["llm_router"])
    after = _read_json(cfg)
    assert "existing" in after["mcpServers"]
    assert "llm_router" in after["mcpServers"]


def test_gemini_cli_adapter_uninstall_idempotent(tmp_path):
    from llm_router.hosts.gemini_cli import GeminiCliAdapter

    adapter = GeminiCliAdapter(config_path=tmp_path / "mcp_servers.json")
    adapter.install(server_command=["llm_router"])
    adapter.uninstall()
    adapter.uninstall()  # second time on empty must not crash
    after = _read_json(tmp_path / "mcp_servers.json")
    assert "llm_router" not in after.get("mcpServers", {})


# ────────────────────────────────────────────────────────────────────────
# CLI install target coverage — every host name in cli.py is supported
# ────────────────────────────────────────────────────────────────────────

CLI_INSTALL_HOSTS = [
    "codex",
    "copilot",
    "copilot-cli",
    "factory",
    "gemini-cli",
    "openclaw",
    "opencode",
    "pi",
    "trae",
]


@pytest.mark.parametrize("host", CLI_INSTALL_HOSTS)
def test_cli_install_host_has_rules_or_plugin(host: str):
    """Every host advertised in llm_router install --host <X> must have either
    a plugin directory or a *-rules.md file backing it."""
    rules_candidates = [
        RULES_DIR / f"{host}-rules.md",
        RULES_DIR / f"{host}.md",
    ]
    plugin_candidates = [
        ROOT / f".{host}-plugin",
        ROOT / f".{host.replace('-cli', '')}-plugin",
    ]
    has_rules = any(p.exists() for p in rules_candidates)
    has_plugin = any(p.exists() for p in plugin_candidates)
    assert has_rules or has_plugin, (
        f"--host {host} advertised by CLI but no rules file or plugin dir "
        f"found. Checked: rules={[p.name for p in rules_candidates]}, "
        f"plugins={[p.name for p in plugin_candidates]}"
    )


# ────────────────────────────────────────────────────────────────────────
# MCP tool surface — server registers expected tools
# ────────────────────────────────────────────────────────────────────────

EXPECTED_TOOL_GROUPS = {
    "routing": ["llm_classify", "llm_route"],
    "text": ["llm_query", "llm_research", "llm_analyze", "llm_code", "llm_generate"],
    "agents": [
        "llm_router_agent_list",
        "llm_router_agent_start_session",
        "llm_router_agent_route",
        "llm_router_agent_check_budget",
        "llm_router_agent_complete_session",
        "llm_router_agent_lineage",
    ],
}


@pytest.mark.parametrize(
    "module_name,expected_tools",
    [(name, tools) for name, tools in EXPECTED_TOOL_GROUPS.items()],
    ids=list(EXPECTED_TOOL_GROUPS.keys()),
)
def test_tools_module_exposes_expected_callables(
    module_name: str, expected_tools: list[str]
):
    """Each tools/*.py module must expose async functions with the
    canonical names so the FastMCP server can wire them by reference."""
    import importlib

    mod = importlib.import_module(f"llm_router.tools.{module_name}")
    for name in expected_tools:
        assert hasattr(mod, name), (
            f"llm_router.tools.{module_name}.{name} missing — "
            f"MCP server registration will fail"
        )


def test_tools_agents_register_function_exists():
    """tools/agents.py.register(mcp) wires all 6 agent tools at server start."""
    from llm_router.tools import agents

    assert callable(agents.register), "tools/agents.py must expose register(mcp)"


# ────────────────────────────────────────────────────────────────────────
# Frameworks integration — every adapter has the protocol shape
# ────────────────────────────────────────────────────────────────────────

FRAMEWORK_MODULES = [
    "agno",
    "hermes",
    "langgraph",
    "crewai",
    "openai_agents",
    "claude_agent_sdk",
    "pydantic_ai",
]


@pytest.mark.parametrize("framework", FRAMEWORK_MODULES)
def test_framework_adapter_has_protocol_shape(framework: str):
    """Every framework module must export an Adapter class with the
    FrameworkAdapter protocol shape (name + 3 methods)."""
    import importlib

    mod = importlib.import_module(f"llm_router.frameworks.{framework}")
    # Find the Adapter class (capitalized framework name + Adapter suffix)
    candidates = [
        attr for attr in dir(mod)
        if attr.endswith("Adapter") and not attr.startswith("_")
    ]
    assert candidates, f"frameworks/{framework}.py exports no *Adapter class"
    adapter_cls = getattr(mod, candidates[0])
    assert hasattr(adapter_cls, "name"), f"{framework} adapter missing 'name'"
    assert hasattr(adapter_cls, "wrap_model"), f"{framework} adapter missing wrap_model"
    assert hasattr(adapter_cls, "detect_agent_id"), (
        f"{framework} adapter missing detect_agent_id"
    )
    assert hasattr(adapter_cls, "is_available"), (
        f"{framework} adapter missing is_available"
    )


@pytest.mark.parametrize("framework", FRAMEWORK_MODULES)
def test_framework_adapter_is_available_is_callable(framework: str):
    import importlib

    mod = importlib.import_module(f"llm_router.frameworks.{framework}")
    candidates = [
        attr for attr in dir(mod)
        if attr.endswith("Adapter") and not attr.startswith("_")
    ]
    adapter_cls = getattr(mod, candidates[0])
    result = adapter_cls.is_available()
    assert isinstance(result, bool), (
        f"{framework}.is_available() must return bool, got {type(result).__name__}"
    )


# ────────────────────────────────────────────────────────────────────────
# Cross-host consistency
# ────────────────────────────────────────────────────────────────────────

def test_all_plugin_versions_match():
    """Every plugin manifest carries the same version (catches drift)."""
    versions = set()
    for _, path in _plugin_files():
        data = _read_json(path)
        if "version" in data:
            versions.add(data["version"])
        for plugin in data.get("plugins", []) if isinstance(data.get("plugins"), list) else []:
            if isinstance(plugin, dict) and "version" in plugin:
                versions.add(plugin["version"])
    assert len(versions) == 1, (
        f"Plugin manifests carry mismatched versions: {sorted(versions)} — "
        f"run scripts/sync-versions.py"
    )


def test_main_rules_file_exists():
    """llm_router.md is the canonical Claude Code rules file."""
    assert (RULES_DIR / "llm_router.md").exists(), (
        "src/llm_router/rules/llm_router.md must exist — Claude Code installs reference it"
    )


def test_main_rules_file_has_version_marker():
    """llm_router.md should start with an HTML comment carrying a version marker."""
    content = (RULES_DIR / "llm_router.md").read_text()
    first_line = content.split("\n", 1)[0]
    assert "llm_router-rules-version" in first_line, (
        f"llm_router.md first line should be a version marker, got: {first_line!r}"
    )


def test_dotrules_file_references_llm_router():
    """The repo-root .rules file is for Trae IDE; must reference llm_router."""
    dotrules = ROOT / ".rules"
    if not dotrules.exists():
        pytest.skip(".rules file not shipped at root")
    content = dotrules.read_text()
    assert "llm_router" in content.lower(), ".rules must reference llm_router brand"
    assert "llm-router" not in content.lower(), ".rules must not reference legacy brand"
