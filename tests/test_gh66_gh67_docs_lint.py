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

import ast
import io
import json
import re
import tokenize
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


# ── 7. Underscore CLI invocations inside SOURCE user-facing strings (GH#72) ──
#
# Section 4 above catches `llm_router <subcommand>` in docs/skills/rules, but
# does NOT scan `src/` — which is exactly where GH#72 found it: install.py's
# `--help` text and the Dockerfile snippet it prints, and doctor.py's own
# `fix="llm_router ..."` remediation hints (including the bold `llm_router
# doctor` heading the command prints about itself).
#
# SCOPE: GH#72 lint only `commands/install.py` and `commands/doctor.py` — the
# two files it named. GH#82 is the named follow-up: it widens the same lint,
# file-by-file, to the rest of `src/llm_router` rather than flipping to a
# whole-tree scan in one step, so each batch stays reviewable. This batch
# adds every file a fresh raw scan over `src/llm_router` turned up with a
# genuine (non-docstring, non-comment) violation as of GH#82 — 44 files, 97
# real hits, all now fixed to `llm-router <subcommand>`. Files the same raw
# scan touched but which had ZERO real violations (the raw hit was entirely
# inside a docstring/comment already) are intentionally NOT added here —
# nothing to guard yet, and a full-tree flip is exactly what this issue asks
# to avoid doing in one step. Widening further as those files pick up real
# violations (or just because full coverage is wanted) is the natural
# follow-up.
#
# THE HARD PART, mechanically: telling a PRINTED shell command apart from a
# legitimate `import llm_router`, an MCP server dict key, or a docstring
# narrating past behavior — without a lint so blunt everyone has to suppress
# it. The rule has two layers:
#
#   1. Reuse `_UNDERSCORE_COMMAND_PATTERN` (section 4): it only fires on
#      `llm_router` + a space + one of cli.py's real subcommand words. That
#      alone already rejects `import llm_router` (nothing follows), a bare
#      `"llm_router"` MCP/dict key (nothing follows), `llm_router.md` /
#      `llm_router-auto-route.py` (a `.` or `-` follows, not a space), and
#      `model_provider=llm_router` (an `=` follows, and it's a config VALUE,
#      not an invocation). Only "llm_router install/doctor/status/..." shaped
#      exactly like a shell command survives this filter.
#   2. A survivor is excluded only if its line falls inside (a) a genuine
#      docstring, INCLUDING a "trailing docstring" (GH#82 widened this from
#      GH#72's `body[0]`-only check — see `_docstring_line_span`'s own
#      docstring for why matching on the bare `Expr(Constant(str))` node
#      shape directly is a strict, lossless generalization) — or (b) a `#`
#      comment, found with `tokenize` (comments are invisible to `ast`
#      entirely, so tokenize is the only way to see them). Both are prose
#      ABOUT behavior, past or present; neither is ever text the CLI itself
#      prints. Everything else that survives layer 1 — a `print()` argument,
#      an f-string assigned then printed, a `fix=`/`actions.append(...)`
#      remediation message — reaches a real terminal.
#
# KNOWN RULE GAP (GH#82, not fixed mechanically — see the two source edits
# instead): `llm_router` immediately followed by a subcommand WORD that is
# also just an ordinary English noun/verb (`routing`, `status`, ...) used in
# prose rather than as an invocation — e.g. a banner reading "--- llm_router
# routing (inherited) ---" or a progress message "llm_router routing
# {task}/{complexity}..." — is indistinguishable from a real invocation by
# any local, mechanical rule: both are literally "llm_router" + space +
# subcommand-shaped word. There is no live signal (unlike layer 2's ast/
# tokenize spans) to derive the exclusion from. Rather than adding a
# suppression list for this, the two affected strings
# (`hooks/agent-route.py`'s `_SPAWN_ROUTING_NOTE` banner + its paired
# containment check, and `tools/text.py`'s `_announce_routing` progress
# message) were reworded to `llm_router: routing ...` — same meaning, and it
# no longer collides with the pattern's required bare space. Both files are
# genuinely clean now, not suppressed.
#
# `test_docstring_and_comment_detection_tells_command_from_reference` below
# proves this split actually works on a minimal fixture before the real
# scan trusts it on install.py/doctor.py.

_CLI_LINT_FILES = tuple(
    _REPO_ROOT / "src" / "llm_router" / rel
    for rel in (
        # GH#72
        "commands/install.py",
        "commands/doctor.py",
        # GH#82
        "auto_profile.py",
        "benchmark/regression.py",
        "cli_init_memory.py",
        "commands/audit.py",
        "commands/benchmark.py",
        "commands/budget.py",
        "commands/config.py",
        "commands/cp.py",
        "commands/demo.py",
        "commands/gc.py",
        "commands/invoice.py",
        "commands/migrate.py",
        "commands/okf.py",
        "commands/onboard.py",
        "commands/policy.py",
        "commands/probe.py",
        "commands/profile.py",
        "commands/routing.py",
        "commands/serve.py",
        "commands/set_enforce.py",
        "commands/setup.py",
        "commands/share.py",
        "commands/soak.py",
        "commands/team.py",
        "commands/team_sync.py",
        "commands/test.py",
        "commands/update.py",
        "cost.py",
        "hooks/agent-route.py",
        "hooks/auto-route.py",
        "hooks/enforce-route.py",
        "hooks/session-end.py",
        "hooks/session-start.py",
        "install_hooks.py",
        "observability/summary.py",
        "onboard.py",
        "quickstart.py",
        "router.py",
        "server.py",
        "test_delta.py",
        "tools/admin.py",
        "tools/setup.py",
        "tools/text.py",
        "ui/status_premium.py",
    )
)


def _docstring_line_span(tree: ast.AST) -> set[int]:
    """Every line number that belongs to a real docstring OR a "trailing
    docstring" — GH#82: this codebase also uses bare string-literal
    statements that are NOT the first statement of their block (e.g.
    `cost.py`'s narrative blurbs describing a constant/table defined just
    above them) purely as documentation. A bare `ast.Expr(ast.Constant(str))`
    is a no-op at runtime by construction — Python evaluates it and throws
    the value away — so it is categorically impossible for it to be
    something a user ever sees printed; only a `Call` (`print(...)`), an
    f-string interpolation, an `Assign`, or similar could ever reach a
    terminal. Matching on the node shape directly (rather than restricting
    to `body[0]` of a Module/FunctionDef/AsyncFunctionDef/ClassDef, as the
    original GH#72 version did) is therefore a strict generalization with no
    loss of precision: every leading docstring is *also* a bare
    `Expr(Constant(str))`, so this still catches everything the narrower
    check did, plus the orphan/trailing case it missed. Comments are not
    visible to `ast` at all, so this can never accidentally swallow one."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            end = node.value.end_lineno or node.value.lineno
            lines.update(range(node.value.lineno, end + 1))
    return lines


def _comment_line_span(src: str) -> set[int]:
    """Every line number carrying a `#` comment token. `ast` drops comments
    entirely, so `tokenize` — the stdlib's own source of truth for comment
    tokens — is the only mechanical way to see them."""
    lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                lines.add(tok.start[0])
    except (tokenize.TokenizeError, IndentationError, SyntaxError):
        pass
    return lines


def _underscore_cli_hits_in_source(path: Path) -> list[str]:
    """Lines in `path` where `llm_router <subcommand>` appears as a live,
    user-facing string — a `print()` argument, an f-string, a `fix=`/
    `actions.append(...)` remediation message, a `--help` constant — and NOT
    inside a docstring or `#` comment (which narrate past/other behavior
    rather than instructing a current action)."""
    src = _read(path)
    if not src:
        return []
    tree = ast.parse(src, filename=str(path))
    excluded = _docstring_line_span(tree) | _comment_line_span(src)
    try:
        label = str(path.relative_to(_REPO_ROOT))
    except ValueError:
        label = str(path)
    hits = []
    for i, line in enumerate(src.splitlines(), 1):
        if i in excluded:
            continue
        if _UNDERSCORE_COMMAND_PATTERN.search(line):
            hits.append(f"{label}:{i}: {line.strip()}")
    return hits


def test_cli_lint_files_exist_and_are_nonempty():
    """Guards the guard, same purpose as test_the_scan_finds_something above:
    an empty/missing file would make the real lint below pass vacuously.

    GH#72's two files (`install.py`/`doctor.py`) are both large, so that
    fix used `> 100` lines as its "not truncated" signal. GH#82 widens
    `_CLI_LINT_FILES` to genuinely short, complete files too (e.g.
    `commands/probe.py` is 36 lines) — a flat `> 100` would make this guard
    fail on files that are correct, not truncated. `> 10` still catches an
    accidentally-emptied/truncated file while accepting every real one."""
    for p in _CLI_LINT_FILES:
        assert p.is_file(), p
        assert len(_read(p).splitlines()) > 10, f"{p} looks truncated"


def test_docstring_and_comment_detection_tells_command_from_reference(tmp_path):
    """Unit-level proof that the AST/tokenize split actually distinguishes a
    PRINTED shell command from a module reference / historical narration —
    GH#72's own warning that a lint which cannot tell them apart is worse
    than none. All four lines below contain the literal same offending text
    (`llm_router install`); only the `print()` one may ever surface as a hit."""
    sample = '''"""Module docstring: `llm_router install` is what an old release told you to run."""

import llm_router  # not a command — must never match on its own


def f():
    """`llm_router install` here too — still just prose about the past."""
    # `llm_router install` — also just a comment, not printed
    print("Run `llm_router install` to fix this")
'''
    sample_path = tmp_path / "gh72_sample.py"
    sample_path.write_text(sample)

    hits = _underscore_cli_hits_in_source(sample_path)

    assert len(hits) == 1, f"expected exactly the print() line to match, got: {hits}"
    assert 'print("Run `llm_router install` to fix this")' in hits[0]


def test_trailing_docstring_is_excluded_like_a_leading_one(tmp_path):
    """GH#82 unit-level proof for the widened `_docstring_line_span`: a bare
    string statement that is NOT `body[0]` (e.g. narrating the constant
    defined just above it, `cost.py`'s idiom) must be excluded exactly like
    a real leading docstring — it is a no-op statement, never printed."""
    sample = '''X = 1
"""Trailing docs: `llm_router install` narrates past behavior here too, not
`body[0]` of anything."""


def f():
    y = 2
    """Also trailing, inside a function body, still not body[0]: `llm_router
    install` again."""
    print("Run `llm_router install` to fix this")
    return y
'''
    sample_path = tmp_path / "gh82_trailing_sample.py"
    sample_path.write_text(sample)

    hits = _underscore_cli_hits_in_source(sample_path)

    assert len(hits) == 1, f"expected exactly the print() line to match, got: {hits}"
    assert 'print("Run `llm_router install` to fix this")' in hits[0]


def test_no_underscore_cli_invocations_in_cli_lint_files():
    """GH#72 covered `install.py`/`doctor.py`; GH#82 widens `_CLI_LINT_FILES`
    to 44 more files across `src/llm_router` with a real (non-docstring,
    non-comment) `llm_router <subcommand>` violation, all now fixed to
    `llm-router <subcommand>`. See the `_CLI_LINT_FILES` comment above for
    which files this batch does and does not cover."""
    hits: list[str] = []
    for p in _CLI_LINT_FILES:
        hits.extend(_underscore_cli_hits_in_source(p))
    assert not hits, (
        "`llm_router <subcommand>` used as a printed/user-facing CLI "
        "instruction in source (the real CLI binary is `llm-router`; "
        "underscore is the Python module / MCP key):\n" + "\n".join(hits)
    )
