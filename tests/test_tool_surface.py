"""CHZ-SURF-01 — a routing hint must never name a tool that isn't registered.

Regression suite for the defect where ``hooks/auto-route.py`` emitted the legacy
completion tool names while ``LLM_ROUTER_SLIM`` defaulted to ``consolidated``, under
which none of them are registered. The caller received "Error: No such tool
available", silently did the work on the expensive model, and no metric could
distinguish that from a decision not to route.

The invariant under test: for EVERY tool name any emitter can produce, and EVERY
slim tier, ``resolve()`` returns a tool that is registered on that tier.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from llm_router.tool_surface import (
    CONSOLIDATED_TOOLS,
    CORE_TOOLS,
    DEPRECATED_TOOLS,
    EMITTABLE_TOOLS,
    ROUTING_TOOLS,
    active_slim,
    call_parts,
    localize,
    registered_tools,
    resolve,
    route_call,
    route_call_with_complexity,
    route_tool,
    unregistered,
)

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "src" / "llm_router" / "hooks"
ALL_TIERS = ("off", "routing", "core", "consolidated")


# ── The core invariant ───────────────────────────────────────────────────────

@pytest.mark.parametrize("tier", ALL_TIERS)
def test_every_emittable_tool_resolves_to_a_registered_tool(tier):
    """The whole point of the module. If this fails, hints 404 again."""
    assert unregistered(slim=tier) == [], (
        f"tier {tier!r} cannot serve these emittable tools; a hint naming one "
        f"would fail with 'No such tool available'"
    )


@pytest.mark.parametrize("tier", ALL_TIERS)
@pytest.mark.parametrize("logical", sorted(EMITTABLE_TOOLS))
def test_resolution_is_total_and_registered(tier, logical):
    call = resolve(logical, tier)
    reg = registered_tools(tier)
    assert call.name, "resolve() must always yield a name"
    if reg is not None:
        assert call.name in reg, f"{logical} -> {call.name} is not registered on {tier}"


def test_the_original_bug_is_fixed():
    """The exact failing case from the report: consolidated + legacy names."""
    for legacy in ("llm_query", "llm_analyze", "llm_code", "llm_research", "llm_generate"):
        assert legacy not in CONSOLIDATED_TOOLS  # premise: genuinely unregistered
        call = resolve(legacy, "consolidated")
        assert call.name == "llm"
        # The specialization must survive the collapse — a bare `llm` loses the
        # routing decision the classifier just made.
        assert call.pinned == (("task", legacy.removeprefix("llm_")),)
    assert resolve("llm_delegate", "consolidated").name == "llm_act"


def test_consolidated_is_the_default_when_env_is_unset(monkeypatch):
    """Assuming `off` by default is what made the emitters wrong."""
    monkeypatch.delenv("LLM_ROUTER_SLIM", raising=False)
    assert active_slim() == "consolidated"
    assert route_tool("llm_code") == 'llm(task="code")'


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_breakage_was_not_consolidated_only(tier):
    """core hid 4/7 route targets and routing hid 1/7 — a consolidated-only
    special case would have left those broken."""
    legacy_map_targets = [
        "llm_research", "llm_generate", "llm_analyze",
        "llm_code", "llm_query", "llm_image", "llm_route",
    ]
    reg = registered_tools(tier)
    if reg is None:
        return
    for t in legacy_map_targets:
        assert resolve(t, tier).name in reg


# ── Call-form correctness ────────────────────────────────────────────────────

def test_render_never_produces_a_double_call():
    """`f"{route_tool(x)}(prompt=…)"` is the trap; route_call is the fix."""
    disp = route_tool("llm_code", )
    assert disp == 'llm(task="code")'
    broken = f"{disp}(prompt=…)"
    assert broken == 'llm(task="code")(prompt=…)'  # documents WHY route_call exists
    good = route_call("llm_code", "prompt=…")
    assert good == 'llm(task="code", prompt=…)'
    assert good.count("(") == 1


def test_call_parts_gives_head_and_args_separately():
    head, pinned = call_parts("llm_analyze", "consolidated")
    assert head == "llm"
    assert pinned == ['task="analyze"']
    head_off, pinned_off = call_parts("llm_analyze", "off")
    assert (head_off, pinned_off) == ("llm_analyze", [])


def test_complexity_is_translated_to_the_arg_the_target_accepts():
    """`llm` takes tier=fast|balanced|best; the legacy tools take complexity=.
    Renaming the tool without translating the argument only swaps 'no such tool'
    for 'unexpected keyword argument'."""
    assert route_call_with_complexity("llm_code", "moderate", slim="consolidated") == \
        'llm(task="code", tier="balanced")'
    assert route_call_with_complexity("llm_code", "simple", slim="consolidated") == \
        'llm(task="code", tier="fast")'
    assert route_call_with_complexity("llm_code", "complex", slim="consolidated") == \
        'llm(task="code", tier="best")'
    assert route_call_with_complexity("llm_code", "moderate", slim="off") == \
        "llm_code(complexity='moderate')"


def test_localize_rewrites_a_whole_template():
    text = "Use llm_code for code and llm_research for research."
    out = localize(text, "consolidated")
    assert "llm_code" not in out and "llm_research" not in out
    assert 'llm(task="code")' in out and 'llm(task="research")' in out
    assert localize(text, "off") == text


def test_localize_is_not_confused_by_name_prefixes():
    """llm_check_usage must not be rewritten as llm_check + usage, etc."""
    out = localize("run llm_check_usage now", "consolidated")
    assert out == "run llm_router_status now"


# ── Single source of truth ───────────────────────────────────────────────────

def test_consolidated_module_reexports_the_same_map():
    from llm_router.tools.consolidated import DEPRECATED_TOOLS as reexported
    assert reexported is DEPRECATED_TOOLS


def test_tool_tiers_reexports_the_same_sets():
    from llm_router import tool_tiers
    assert tool_tiers.CORE_TOOLS is CORE_TOOLS
    assert tool_tiers.ROUTING_TOOLS is ROUTING_TOOLS
    assert tool_tiers.CONSOLIDATED_TOOLS is CONSOLIDATED_TOOLS


def test_enforce_route_no_longer_keeps_a_private_door_map():
    """The private copy in enforce-route.py is how the knowledge failed to reach
    auto-route.py. It must not come back."""
    src = (HOOKS / "enforce-route.py").read_text()
    assert "_OLD_TOOL_TO_DOOR = {" not in src


def test_tool_surface_has_no_llm_router_imports():
    """It must stay loadable by path from a hook whose interpreter has no llm_router."""
    tree = ast.parse((REPO / "src" / "llm_router" / "tool_surface.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("llm_router"):
            pytest.fail(f"tool_surface imports {node.module} — breaks standalone load")
        if isinstance(node, ast.Import):
            for a in node.names:
                assert not a.name.startswith("llm_router"), f"imports {a.name}"


def test_tool_surface_loads_standalone_by_path(tmp_path):
    """Simulates a hook running under an interpreter without llm_router installed."""
    spec = importlib.util.spec_from_file_location(
        "standalone_tool_surface", REPO / "src" / "llm_router" / "tool_surface.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["standalone_tool_surface"] = mod
    spec.loader.exec_module(mod)
    try:
        assert mod.resolve("llm_code", "consolidated").display == 'llm(task="code")'
        assert mod.unregistered(slim="consolidated") == []
    finally:
        sys.modules.pop("standalone_tool_surface", None)


# ── The lint is itself part of the guarantee ─────────────────────────────────

def test_source_lint_passes():
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "lint_tool_surface.py")],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"CHZ-SURF-01 lint failed:\n{r.stdout}\n{r.stderr}"


def test_lint_actually_catches_a_regression(tmp_path):
    """A guard that cannot fail is not a guard."""
    bad = tmp_path / "bad_hook.py"
    bad.write_text('print("  • llm_code: for code tasks")\n')
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "lint_tool_surface.py"), str(bad)],
        capture_output=True, text=True,
    )
    assert r.returncode == 1
    assert "llm_code" in r.stdout


def test_lint_catches_the_double_call_form(tmp_path):
    bad = tmp_path / "bad_call.py"
    bad.write_text('x = f"{route_tool(\'llm_code\')}(prompt=1)"\n')
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "lint_tool_surface.py"), str(bad)],
        capture_output=True, text=True,
    )
    assert r.returncode == 1
    assert "double call" in r.stdout


def test_lint_allows_bare_logical_names(tmp_path):
    ok = tmp_path / "ok.py"
    ok.write_text('TOOL_MAP = {"code": "llm_code"}\ntool = "llm_query"\n')
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "lint_tool_surface.py"), str(ok)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout


# ── Installer ships the standalone copy ──────────────────────────────────────

def test_installer_ships_tool_surface_beside_the_hooks():
    from llm_router.install_hooks import _HOOK_SUPPORT_FILES
    assert ("tool_surface.py", "llm_router_tool_surface.py") in _HOOK_SUPPORT_FILES


# ── Accounting must see the doors too (CHZ-SURF-01, measurement side) ────────

def test_usage_refresh_counts_the_consolidated_door(tmp_path):
    """Under the default tier the completion door is named exactly `llm`, which
    fails a startswith("llm_") test. That dropped every routed call before it
    reached the savings log — an UNDERcount indistinguishable from "not routed"."""
    import json as _json
    import os as _os

    hook = REPO / "src" / "llm_router" / "hooks" / "usage-refresh.py"
    env = {**_os.environ, "HOME": str(tmp_path), "LLM_ROUTER_SLIM": "consolidated"}
    for tool in ("mcp__llm_router__llm", "llm_query"):
        subprocess.run([sys.executable, str(hook)], input=_json.dumps({"toolName": tool}),
                       capture_output=True, text=True, env=env)

    log = tmp_path / ".llm-router" / "savings_log.jsonl"
    assert log.exists(), "no savings log written — routed calls went uncounted"
    rows = [_json.loads(line) for line in log.read_text().splitlines()]
    tools = {r["tool"] for r in rows}
    assert "mcp__llm_router__llm" in tools, "the consolidated door was not counted"
    assert "llm_query" in tools


def test_usage_refresh_skips_observability_doors(tmp_path):
    """llm_router_status is pure observability. Counting it as a routed call would
    mean checking your savings increases your savings."""
    import json as _json
    import os as _os

    hook = REPO / "src" / "llm_router" / "hooks" / "usage-refresh.py"
    env = {**_os.environ, "HOME": str(tmp_path), "LLM_ROUTER_SLIM": "consolidated"}
    subprocess.run([sys.executable, str(hook)],
                   input=_json.dumps({"toolName": "mcp__llm_router__llm_router_status"}),
                   capture_output=True, text=True, env=env)
    log = tmp_path / ".llm-router" / "savings_log.jsonl"
    rows = [_json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert not any("llm_router_status" in r["tool"] for r in rows)


# ── Rules files: the strongest teacher of which tool to call ─────────────────

@pytest.mark.parametrize("tier", ALL_TIERS)
def test_installed_rules_never_name_an_unregistered_tool(tier):
    """The rules file is loaded into EVERY session. Shipping it with the legacy
    names while the default tier registers only the doors trained the model, in
    every session, to make a call that fails."""
    import re as _re

    rules = sorted((REPO / "src" / "llm_router" / "rules").glob("*.md"))
    assert rules, "no rules files found"
    reg = registered_tools(tier)
    for f in rules:
        out = localize(f.read_text(), tier)
        if reg is None:
            continue
        for name in _re.findall(r"\b(llm_(?:query|code|analyze|research|generate|delegate))\b", out):
            assert name in reg, f"{f.name} still names {name}, unregistered on {tier}"


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_localize_never_emits_a_double_call(tier):
    """A name-only rewrite turns `llm_code(complexity="complex")` into the
    uncallable `llm(task="code")(complexity="complex")` — and it carries an
    argument the door does not accept. localize must rewrite the whole call."""
    import re as _re

    targets = sorted((REPO / "src" / "llm_router" / "rules").glob("*.md")) + \
        sorted((REPO / "skills").rglob("SKILL.md"))
    for f in targets:
        out = localize(f.read_text(), tier)
        bad = [ln for ln in out.splitlines() if _re.search(r"\)\(", ln)]
        assert not bad, f"{f.name} produced a double call on {tier}: {bad[:1]}"


def test_localize_translates_complexity_to_tier_inside_a_call():
    assert localize('llm_code(complexity="complex")', "consolidated") == \
        'llm(task="code", tier="best")'
    # Other arguments are preserved, not dropped.
    out = localize('llm_code(prompt="...", complexity="complex")', "consolidated")
    assert out == 'llm(task="code", prompt="...", tier="best")'


def test_localize_is_a_true_noop_on_legacy_tiers():
    """Cosmetic rewrites (quote style) would make every install report drift."""
    for f in sorted((REPO / "src" / "llm_router" / "rules").glob("*.md")):
        src = f.read_text()
        assert localize(src, "off") == src, f"{f.name} changed on tier off"


def test_rules_installer_writes_the_localized_text(tmp_path, monkeypatch):
    from llm_router import install_hooks as ih

    monkeypatch.setenv("LLM_ROUTER_SLIM", "consolidated")
    src = ih._RULES_SRC / "llm_router.md"
    text = ih._localized_rules_text(src)
    assert 'llm(task="query")' in text
    assert "llm_query" not in text


# ── The lint must reach outside Python too ───────────────────────────────────

def test_lint_scans_workflows_and_shell_by_default():
    """Default run must include .github/workflows and scripts/*.sh."""
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "lint_tool_surface.py")],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout
    # The count in the summary proves non-.py files were included.
    import re as _re
    m = _re.search(r"clean \((\d+) files checked\)", r.stdout)
    assert m, r.stdout
    py_only = len(list((REPO / "src" / "llm_router").rglob("*.py")))
    assert int(m.group(1)) > py_only, (
        f"only {m.group(1)} files checked vs {py_only} python files — "
        "workflows/shell scripts are not being scanned"
    )


def test_lint_result_does_not_depend_on_the_python_version(tmp_path):
    """The lint IS the guarantee, so it must not be version-sensitive.

    PEP 701 (3.12) gave f-string literal parts their real line numbers, where 3.11
    had them inherit the enclosing node's. A pragma check anchored to line
    PROXIMITY therefore passed on 3.11 and failed on 3.12+ — the lint looked clean
    locally and broke CI. Pragmas are now anchored to the enclosing statement,
    whose position is stable. This test pins the property on the running
    interpreter; CI runs it on 3.11/3.12/3.13/3.14.
    """
    src = REPO / "src" / "llm_router" / "commands" / "doctor.py"
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "lint_tool_surface.py"), str(src)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, (
        f"pragma resolution is version-sensitive on {sys.version_info[:2]}:\n{r.stdout}"
    )


def test_pragma_is_honoured_inside_a_multiline_statement(tmp_path):
    """A multi-line call puts the offending argument several lines in; the natural
    place to justify it is right there, not above the statement."""
    f = tmp_path / "m.py"
    f.write_text(
        "print(\n"
        "    'a',\n"
        "    # chz-surface-ok: internal log record\n"
        "    'calling llm_code now',\n"
        ")\n"
    )
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "lint_tool_surface.py"), str(f)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout
