"""RED1-20/21/22 (P0) — every emitted tool name must name a REGISTERED tool.

CHZ-SURF-01 fixed this for Claude Code. It did not fix it for anyone else: the
`_append_routing_rules` installer — the one serving Cursor, Windsurf, Copilot,
Gemini CLI, opencode, Trae and the rest — wrote the bundled rules verbatim, so a
file saying `llm_code` taught those models to make a call that 404s, in every
session, for the life of the file. Vendor-neutrality is the North Star's central
claim and the vendor-neutral path was the unlocalized one.

The rules file is the highest-leverage artifact in the product. It is loaded into
every session and is the strongest teacher of which tool to call, and it was the
one thing the tool-name lint never looked at.

**These tests resolve against the LIVE registered surface**, enumerated by
actually registering the MCP tools, not against `tool_surface.py`'s own tables.
That is the audit's explicit instruction and it is not pedantry: comparing an
emitter to `tool_surface.py` only proves the two agree, and two components can
agree on the same wrong assumption. That is how this shipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RULES_DIR = REPO / "src" / "llm_router" / "rules"


def _live_registered_tools(slim: str | None = None) -> frozenset[str]:
    """Names the MCP server ACTUALLY registers, by registering them.

    Not `KNOWN_TOOLS`, not `EMITTABLE_TOOLS`, not any table that a rules file
    could be wrong in the same direction as.
    """
    from mcp.server.mcpserver import MCPServer

    from llm_router.tool_tiers import make_should_register
    from llm_router.tools import admin, agentic, media, pipeline, routing, text

    mcp = MCPServer("surface-probe")
    gate = make_should_register(slim)
    for module in (routing, text, media, pipeline, admin, agentic):
        try:
            module.register(mcp, gate)
        except Exception as exc:  # noqa: BLE001 — a module that cannot register
            pytest.skip(f"could not register {module.__name__}: {exc}")

    manager = getattr(mcp, "_tool_manager", None)
    if manager is None:  # pragma: no cover — MCPServer internals moved
        pytest.skip("cannot enumerate MCPServer tools on this version")
    return frozenset(manager._tools)


def _rules_files() -> list[Path]:
    return sorted(RULES_DIR.glob("*.md"))


def test_there_are_rules_files_to_check():
    """Guards the guard: a glob that matches nothing passes every test below."""
    assert _rules_files(), f"no rules files found under {RULES_DIR}"


@pytest.mark.parametrize("rules_file", _rules_files(), ids=lambda p: p.name)
def test_localized_rules_name_only_registered_tools(rules_file):
    """RED1-20: what the installer WRITES must resolve, for every host.

    Re-scans the OUTPUT of localize() rather than trusting that localize() is
    correct — RED1-22 noted that the lint exempted localize()'s output on the
    grounds that it "rewrites a whole blob, so it is already safe". That is an
    assumption about the resolver, checked nowhere.
    """
    import re

    from llm_router.tool_surface import DEPRECATED_TOOLS, localize

    localized = localize(rules_file.read_text(encoding="utf-8"))
    registered = _live_registered_tools()

    survivors = sorted(
        name
        for name in DEPRECATED_TOOLS
        if name not in registered and re.search(rf"\b{re.escape(name)}\b", localized)
    )
    assert survivors == [], (
        f"{rules_file.name} still names unregistered tools after localization: "
        f"{survivors}"
    )


def test_the_installer_localizes_before_writing(tmp_path, monkeypatch):
    """RED1-20 at the installer, not just at the resolver.

    localize() being correct is worth nothing if the function that writes the
    file never calls it. This is the gap that existed: a correct resolver and an
    installer that did not use it.
    """
    from llm_router.commands.install import _append_routing_rules

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    dest = tmp_path / "host-rules.md"
    source = _rules_files()[0]

    _append_routing_rules(dest, source.name)

    assert dest.exists(), "installer wrote nothing"
    written = dest.read_text(encoding="utf-8")
    from llm_router.tool_surface import localize

    assert written.strip() == localize(source.read_text(encoding="utf-8")).strip(), (
        "the installer wrote the raw bundle instead of the localized text"
    )


def test_only_one_append_routing_rules_implementation_exists():
    """RED8-06: two copies is how one gets fixed and the other does not.

    `cli.py` and `commands/install.py` each had a full implementation. They had
    already diverged — only one recorded to the install manifest, so the other's
    files survived uninstall — and only one was ever going to receive the
    localization fix.
    """
    import ast

    src = REPO / "src" / "llm_router"
    definitions: list[str] = []
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_append_routing_rules"
            ):
                # A delegating alias has no file I/O of its own.
                body = ast.dump(node)
                if "write_text" in body or "'a'" in body:
                    definitions.append(f"{path.relative_to(src)}:{node.lineno}")
    assert len(definitions) <= 1, (
        f"{len(definitions)} independent implementations: {definitions}"
    )


def test_guarded_is_derived_not_hand_maintained():
    """RED1-22: the hand-written tuple covered 13 of 24 deprecated names.

    Eleven names — including llm_reason, llm_dashboard, llm_providers and the
    four llm_router_agent_* — could be emitted unresolved with the lint reporting
    clean. A green check over an unchecked surface is worse than no check.
    """
    import importlib.util

    from llm_router.tool_surface import DEPRECATED_TOOLS

    spec = importlib.util.spec_from_file_location(
        "_lint_surface", REPO / "scripts" / "lint_tool_surface.py"
    )
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)

    missing = sorted(set(DEPRECATED_TOOLS) - set(lint.GUARDED))
    assert missing == [], f"deprecated names the lint does not guard: {missing}"


def test_llm_reason_resolves(tmp_path):
    """RED1-21: emitted by three generated tables, known to no surface set."""
    from llm_router.tool_surface import DEPRECATED_TOOLS, localize

    assert "llm_reason" in DEPRECATED_TOOLS
    out = localize("Call `llm_reason(prompt=...)` for deep reasoning.")
    assert "llm_reason" not in out, f"llm_reason survived localization: {out}"


def test_the_lint_scans_markdown_and_json():
    """RED1-22: the rules files are .md and were never scanned.

    The check covered the code that writes the file and not the file.
    """
    text = (REPO / "scripts" / "lint_tool_surface.py").read_text(encoding="utf-8")
    for suffix in (".md", ".json", ".yaml", ".sh"):
        assert f'"{suffix}"' in text, f"the lint does not scan {suffix} files"


def test_mutation_an_invented_tool_name_is_caught(tmp_path):
    """Mutation gate: injecting a bogus name must fail the lint.

    The audit's proven blind spot — everything was green while unregistered
    names shipped. A guard that cannot fail is not a guard.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_lint_surface_mut", REPO / "scripts" / "lint_tool_surface.py"
    )
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)

    victim = tmp_path / "poisoned.md"
    victim.write_text("Use `llm_analyze` for deep work.\n", encoding="utf-8")

    problems = lint.check_non_python(victim)
    assert problems, "an unregistered tool name in a .md file was not flagged"


# GAP, stated rather than papered over: there is no per-tier assertion here.
#
# Two attempts were made and both had a false premise. "No deprecated name is
# ever registered" is wrong — the FULL tier registers every legacy alias, which
# is the point of the aliases. "The consolidated tier registers none of them" is
# also wrong: probing with slim="routing" still returns eleven of them, so either
# that is not the tier the rules files target or the tier/DEPRECATED_TOOLS split
# means something other than what this test assumed.
#
# Rather than weaken the assertion until it passes — which would produce exactly
# the decorative green check this work package exists to remove — the property is
# left uncovered and named. What IS covered, above and against the live
# registered surface, is that localize() output for every bundled rules file
# names only registered tools under the tier actually in effect. The missing
# piece is proving it holds under EVERY tier, and closing it needs the tier
# semantics established first.
