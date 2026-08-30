"""GH#66 / GH#67 — docs-lint: the shipped docs/skills/rules must describe the
tool surface, hook filenames, CLI name, and package name that actually exist.

GH#66 found `guide/TROUBLESHOOTING.md` and the shipped Claude Code skills still
documenting pre-"North Star 1.0" tool names (`llm_health`, `llm_setup`,
`llm_cache_stats`, `llm_cache_clear`, `llm_quality_report`, `llm_classify`,
`llm_policy`) that the installed 13.0.4 MCP server — running the consolidated
11-door tier by default (``get_config().llm_router_slim == "consolidated"``,
see ``config.py``) — never registers directly. GH#67 found a naming-confusion
sweep: `llm_router` (module/MCP key) used where the real CLI binary is
`llm-router` (`pyproject.toml`'s ``[project.scripts]``), the dead PyPI name
`claude-code-llm-router` instead of `llm-routing`, and hook filenames written
fully-hyphenated (`llm-router-*.py`) instead of the real `llm_router-*.py`
convention (`install_hooks.py`'s ``_HOOK_DEFS``).

WHY THIS TEST DERIVES GROUND TRUTH RATHER THAN FREEZING IT
------------------------------------------------------------
A test that hardcodes today's tool list rots exactly like the docs did: the
next tool rename makes the test pass for the wrong reason (nothing in the
frozen list changed) while new docs drift goes undetected. So each assertion
below is ANNOTATED as either:

  DERIVED   — computed from a live, importable source of truth (tool_surface,
              cli.py's subcommand table, install_hooks.py's hook table,
              env_registry.py, pyproject.toml). Automatically tracks renames.
  HARDCODED — a literal string/pattern with no live source to derive it from
              (the *specific* legacy names GH#66 named, and the dead package
              name GH#67 named). These need a human to update this file if the
              underlying rename ever changes again — there is nothing further
              upstream to point at.

Every hardcoded literal also carries a derived cross-check confirming the
premise still holds (e.g. "this name is genuinely not registered under the
consolidated tier today"), so if the surface changes shape, this file fails
with an explanation instead of silently going stale itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from llm_router import env_registry, tool_surface
from llm_router.cli import _KNOWN_SUBCOMMANDS
from llm_router.install_hooks import _HOOK_DEFS

_REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Scope ────────────────────────────────────────────────────────────────────
# The trees the plan's repro spec (docs/PLAN_ISSUES_59-67.md, PR8) names.
_SCAN_DIRS = ("guide", "skills", "src/llm_router/rules", "docs")

# Excluded as append-only HISTORICAL records that legitimately discuss retired
# names/vars as past-tense fact (the same treatment already given to
# CHANGELOG-ARCHIVE.md, which sits outside these trees and needs no exclusion):
#
#   * docs/PLAN_ISSUES_59-67.md — the diagnostic planning doc for GH#59-67
#     itself. Its own verification table quotes the exact broken strings
#     (`llm_health`, `LLM_ROUTER_SQL_DEBUG`, `llm_router install`, ...) as
#     EVIDENCE of the bugs this test polices. Scanning it would make this test
#     permanently red against a file that is correctly describing history.
#   * docs/releases/ — dated migration-evidence audits (the
#     v11.1.0 migration record and its siblings) that document env vars/flags
#     explicitly as REMOVED or NEVER IMPLEMENTED ("`LLM_ROUTER_QUALITY_FEEDBACK`
#     was never implemented and never will be, by design"). They are already
#     self-aware; rewriting them would falsify the audit trail, not fix a bug.
_EXCLUDED_FILES = {
    _REPO_ROOT / "docs" / "PLAN_ISSUES_59-67.md",
    # Same category: a dated audit that QUOTES the broken strings as evidence
    # (e.g. `llm_router install` from cli.py's --help) in its findings table.
    # Sanitising the quotes would destroy the report's value as a record.
    _REPO_ROOT / "docs" / "AUDIT_2026-08-30.md",
}
_EXCLUDED_DIR_PREFIXES = (_REPO_ROOT / "docs" / "releases",)


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for rel in _SCAN_DIRS:
        base = _REPO_ROOT / rel
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            if p in _EXCLUDED_FILES:
                continue
            if any(str(p).startswith(str(pref)) for pref in _EXCLUDED_DIR_PREFIXES):
                continue
            files.append(p)
    return files


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


_FILES = _scanned_files()


def test_the_scan_finds_something():
    """Guards the guard — an empty file list would make every assertion below
    pass vacuously, exactly the failure mode RED8-10's env_registry tests
    already guard against for a different scanner."""
    assert len(_FILES) > 20, f"docs-lint scanned only {len(_FILES)} files"


# ── 1. Retired tool names (HARDCODED list; DERIVED non-registration check) ──
#
# GH#66's own literal list. No live "list of retired names" exists to derive
# this FROM — these are exactly the strings the issue named as pre-consolidation
# tools still being told to users as things to call.
_RETIRED_TOOL_PATTERN = re.compile(
    r"llm_health|llm_setup|llm_cache_|llm_quality_report|llm_classify\b|llm_policy\b"
)

# DERIVED: every one of the literal names above must actually be unreachable as
# a bare call under the default installed tier. If a future consolidation adds
# a door for one of these, this assertion fails and explains exactly why —
# rather than this file quietly keeping a now-wrong ban in place.
_RETIRED_BARE_NAMES = ("llm_health", "llm_setup", "llm_cache_stats", "llm_cache_clear",
                       "llm_quality_report", "llm_classify", "llm_policy")


def test_retired_names_are_genuinely_not_registered_by_default():
    still_registered = [n for n in _RETIRED_BARE_NAMES if tool_surface.is_registered(n, "consolidated")]
    assert not still_registered, (
        "these legacy names are now reachable under the default 'consolidated' "
        f"tier — the docs ban in this test is stale, not the docs: {still_registered}"
    )


def test_no_retired_tool_names_in_docs():
    hits = []
    for p in _FILES:
        for i, line in enumerate(_read(p).splitlines(), 1):
            if _RETIRED_TOOL_PATTERN.search(line):
                hits.append(f"{p.relative_to(_REPO_ROOT)}:{i}: {line.strip()}")
    assert not hits, "pre-consolidation tool names referenced in docs:\n" + "\n".join(hits)


# ── 2. Dead package name (HARDCODED; DERIVED cross-check against pyproject) ─

_DEAD_PACKAGE_NAME = "claude-code-llm-router"


def test_dead_package_name_is_not_the_real_one():
    """DERIVED: confirms the literal below really is dead, by checking it
    against the live project name in pyproject.toml."""
    import tomllib

    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    real_name = pyproject["project"]["name"]
    assert real_name != _DEAD_PACKAGE_NAME
    assert real_name == "llm-routing"


def test_no_dead_package_name_in_docs():
    hits = []
    for p in _FILES:
        for i, line in enumerate(_read(p).splitlines(), 1):
            if _DEAD_PACKAGE_NAME in line:
                hits.append(f"{p.relative_to(_REPO_ROOT)}:{i}: {line.strip()}")
    assert not hits, "dead package name referenced in docs:\n" + "\n".join(hits)


# ── 3. Old (fully-hyphenated) hook filenames — DERIVED from install_hooks.py ─

_OLD_HOOK_PATTERN = re.compile(r"llm-router-[a-z-]+\.py")
# DERIVED: the real installed hook filenames, straight from the table
# install_hooks.py uses to write them — the single source of truth for what
# an installed hook is actually called.
_REAL_HOOK_NAMES = frozenset(dest for _src, dest, _event, _matcher in _HOOK_DEFS)


def test_real_hook_names_are_underscore_prefixed():
    """DERIVED sanity check: every real hook name uses the current
    `llm_router-*.py` convention, never the old `llm-router-*.py` fork. If this
    ever fails, install_hooks.py itself regressed, not the docs."""
    for name in _REAL_HOOK_NAMES:
        assert name.startswith("llm_router-"), name
        assert not _OLD_HOOK_PATTERN.fullmatch(name), name


def test_no_old_hyphenated_hook_filenames_in_docs():
    hits = []
    for p in _FILES:
        for i, line in enumerate(_read(p).splitlines(), 1):
            for m in _OLD_HOOK_PATTERN.finditer(line):
                hits.append(f"{p.relative_to(_REPO_ROOT)}:{i}: {m.group(0)!r} in: {line.strip()}")
    assert not hits, "old fully-hyphenated hook filenames referenced in docs:\n" + "\n".join(hits)


def test_hook_filenames_mentioned_in_docs_are_real():
    """DERIVED, stronger than the ban above: any CORRECTLY-formed
    `llm_router-*.py` name mentioned in the docs must be one install_hooks.py
    actually installs — catches an invented-but-plausible name, not just the
    wrong hyphenation."""
    correct_pattern = re.compile(r"llm_router-[a-z-]+\.py")
    bad = []
    for p in _FILES:
        for i, line in enumerate(_read(p).splitlines(), 1):
            for m in correct_pattern.finditer(line):
                if m.group(0) not in _REAL_HOOK_NAMES:
                    bad.append(f"{p.relative_to(_REPO_ROOT)}:{i}: {m.group(0)!r} not in install_hooks._HOOK_DEFS")
    assert not bad, "\n".join(bad)


# ── 4. Underscore module name used as a shell command — DERIVED from cli.py ──
#
# `llm_router <subcommand>` (underscore) is never a real command; the CLI
# binary is `llm-router` (pyproject.toml). Subcommand names come straight from
# cli.py's own dispatch table, so a future subcommand rename is picked up
# automatically instead of needing a matching edit here.
_UNDERSCORE_COMMAND_PATTERN = re.compile(
    r"llm_router (" + "|".join(re.escape(n) for n in sorted(_KNOWN_SUBCOMMANDS, key=len, reverse=True)) + r")\b"
)


def test_no_underscore_cli_invocations_in_docs():
    hits = []
    for p in _FILES:
        for i, line in enumerate(_read(p).splitlines(), 1):
            if _UNDERSCORE_COMMAND_PATTERN.search(line):
                hits.append(f"{p.relative_to(_REPO_ROOT)}:{i}: {line.strip()}")
    assert not hits, (
        "`llm_router <subcommand>` used as a shell command (the real CLI binary "
        "is `llm-router`, underscore is the Python module / MCP key):\n" + "\n".join(hits)
    )


# ── 5. Every documented env var is real — DERIVED from a full source-tree scan
#
# Mirrors GH#66's own diagnostic method verbatim ("checked via full-package
# grep, zero matches" — that's how LLM_ROUTER_SQL_DEBUG/LLM_ROUTER_HOOK_DEBUG
# were confirmed dead). Deliberately broader than `env_registry.ENV_REGISTRY`
# alone: several real vars (e.g. LLM_ROUTER_TEAM_ENDPOINT, LLM_ROUTER_WEBHOOK_URL)
# are RouterConfig (pydantic-settings) fields bound to their env var implicitly
# by the framework, with no literal `os.environ.get("...")` call for
# env_registry's own AST scanner to see — so they're legitimately real without
# being (or needing to be) in ENV_REGISTRY. Requiring literal ENV_REGISTRY
# membership here would either produce false positives on those, or require
# adding phantom-looking entries that env_registry's own
# `test_the_registry_has_no_phantom_entries` would then reject. A full
# source-tree token scan sidesteps that split cleanly and is the more honest
# ground truth for "does this env var exist," matching how the issue itself
# checked.
_ENV_TOKEN_PATTERN = re.compile(r"\bLLM_ROUTER_[A-Z0-9_]+\b")


def _env_tokens_in_source() -> frozenset[str]:
    tokens: set[str] = set()
    src = Path(env_registry.__file__).resolve().parent
    for p in src.rglob("*.py"):
        tokens.update(_ENV_TOKEN_PATTERN.findall(_read(p)))
    return frozenset(tokens) | env_registry.registered_names()


def test_every_documented_env_var_exists_in_source():
    real = _env_tokens_in_source()
    bad = []
    for p in _FILES:
        for i, line in enumerate(_read(p).splitlines(), 1):
            for tok in _ENV_TOKEN_PATTERN.findall(line):
                if tok not in real:
                    bad.append(f"{p.relative_to(_REPO_ROOT)}:{i}: {tok} not found anywhere in src/llm_router")
    assert not bad, "documented env vars that don't exist in the codebase:\n" + "\n".join(bad)


# ── 6. plugin.json's mcpServers value, if present, must exist on disk ───────

def test_plugin_json_mcp_servers_path_exists_if_present():
    plugin_json = _REPO_ROOT / ".claude-plugin" / "plugin.json"
    data = json.loads(plugin_json.read_text())
    mcp_servers = data.get("mcpServers")
    if mcp_servers is None:
        pytest.skip("plugin.json has no mcpServers key — nothing to validate")
    if isinstance(mcp_servers, str):
        target = _REPO_ROOT / mcp_servers
        assert target.exists(), f"plugin.json's mcpServers points at {mcp_servers!r}, which does not exist"
    # A dict-shaped mcpServers is an inline config, not a path reference — nothing to check.
