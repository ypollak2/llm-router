"""Every execution path must attach repo knowledge through the same function.

OKF reached 2 of 7 execution paths — `router.py` and
`direct_executor.execute_chain`. The tool loop, the Codex agent, the Claude
agent, the Gemini agent and `llm_local_task` all ran blind. Nobody decided that;
it is what happens when injection is something each new path has to REMEMBER.

Measured cost on one audited day: 101 drafts injected into a session, zero used,
a third of them asserting statuses the model had no way to observe — because it
was answering questions about a repository it could not see.

This test is the thing that makes forgetting loud. A new execution path that
does not cross `llm_router.context_injection` fails here, at the commit that
adds it, rather than weeks later in a log audit.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "llm_router"

# Every module that sends a prompt to a model on the user's behalf.
EXECUTION_PATHS = [
    "hooks/agent_loop.py",
    "hooks/direct_executor.py",
    "codex_agent.py",
    "claude_agent.py",
    "gemini_cli_agent.py",
    "tools/local_task.py",
    "proxy/okf_context.py",
]


def _calls_build_pack(src: str) -> bool:
    """A real `build_pack(...)` Call node; an import or a comment does not count."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id == "build_pack") or (
                isinstance(f, ast.Attribute) and f.attr == "build_pack"
            ):
                return True
    return False


def _crosses_choke_point(src: str, pack_src: str) -> bool:
    """Directly, or by calling `context_pack.build_pack`, whose project layer
    calls `context_injection.inject` (P1.1 doors; behaviour pinned below)."""
    if "context_injection" in src:
        return True
    return _calls_build_pack(src) and "context_injection" in pack_src


@pytest.mark.parametrize("rel", EXECUTION_PATHS)
def test_execution_path_crosses_the_choke_point(rel):
    src = (SRC / rel).read_text()
    assert _crosses_choke_point(src, (SRC / "context_pack.py").read_text()), (
        f"{rel} sends prompts to a model but never imports "
        f"llm_router.context_injection (or llm_router.context_pack, which "
        f"calls it), so it runs with no repo knowledge. "
        f"Call inject() or inject_system_prompt() — do not add a third private "
        f"copy of the retrieval logic."
    )


def test_a_module_bypassing_both_fails_the_choke_point_check():
    pack_src = (SRC / "context_pack.py").read_text()
    assert "context_injection" in pack_src  # the premise of the indirect route
    assert not _crosses_choke_point("import subprocess\nrun(prompt)\n", pack_src)
    real = "from llm_router.context_pack import build_pack\nbuild_pack(p, door='x', target_window=1)\n"
    assert _crosses_choke_point(real, pack_src)
    assert not _crosses_choke_point(real, "no seam")


@pytest.mark.parametrize("mutant", [
    "from llm_router.context_pack import build_pack\nrun(prompt)\n",  # import only
    "# uses context_pack.build_pack(...) someday\nrun(prompt)\n",       # comment
    '"""calls build_pack(x)"""\nrun(prompt)\n',                         # docstring
    "from llm_router import context_pack\nrun(prompt)\n",               # module import only
])
def test_import_or_mention_of_context_pack_is_not_crossing(mutant):
    pack_src = (SRC / "context_pack.py").read_text()
    assert not _crosses_choke_point(mutant, pack_src)


def test_attribute_call_to_build_pack_counts():
    pack_src = (SRC / "context_pack.py").read_text()
    src = "from llm_router import context_pack\ncontext_pack.build_pack(p, door='x', target_window=1)\n"
    assert _crosses_choke_point(src, pack_src)


def test_build_pack_really_calls_inject_unless_project_is_supplied(tmp_path, monkeypatch):
    """Behaviour behind the indirect route: the textual claim is only true if
    build_pack(project_root=...) reaches context_injection.inject, and the
    #498 `project=` override deliberately skips it."""
    from llm_router import context_injection, context_pack

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("# Rules\n- always run the tests\n")
    calls = []

    def spy(request, *a, **kw):
        calls.append(kw.get("root"))
        return request

    monkeypatch.setattr(context_injection, "inject", spy)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    context_pack.build_pack("fix the bug", project_root=str(repo), target_window=100000, door="codex")
    assert calls == [str(repo)]

    calls.clear()
    context_pack.build_pack("fix the bug", project_root=str(repo), target_window=100000,
                            door="mcp", project="already injected")
    assert calls == []


def test_only_the_choke_point_implements_injection():
    """No module may call `okf.inject_context` itself.

    The rule is about INJECTION, not retrieval. `auto-route` legitimately calls
    `find_relevant` to ask "is there knowledge for this prompt?" as a gating
    decision — it needs the concept list, not an enriched prompt, and that is a
    different question. What must not be duplicated is the attach step: two
    copies is how one of them lost its scope argument and how five execution
    paths ended up with neither.
    """
    skip_prefixes = ("#", '"""', "'''", "*")
    offenders = []
    for path in SRC.rglob("*.py"):
        # router.py was exempted here from the day this test was written: it
        # predated the choke point and injected OKF with its own
        # find_relevant + inject_context pair. It now crosses inject() like
        # every other path, so the rule has no exceptions left. The swap was
        # measured before it was made — see tests/test_router_context_seam.py.
        if path.name in ("context_injection.py", "okf.py"):
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip().startswith(skip_prefixes):
                continue
            if ".inject_context(" in line:
                offenders.append(f"{path.relative_to(SRC)}:{i}")
    assert not offenders, (
        "these attach OKF context themselves instead of calling "
        "context_injection.inject(): " + ", ".join(offenders)
    )


def test_injection_is_fail_open():
    """A broken knowledge store must never break a routed call."""
    from llm_router.context_injection import inject
    assert inject("hello", root="/nonexistent/path/xyz") == "hello"
    assert inject("") == ""


def test_a_single_switch_disables_it_everywhere(monkeypatch):
    from llm_router.context_injection import enabled, inject
    monkeypatch.setenv("LLM_ROUTER_CONTEXT_INJECTION", "off")
    assert not enabled()
    assert inject("anything", root=str(SRC.parent.parent)) == "anything"
