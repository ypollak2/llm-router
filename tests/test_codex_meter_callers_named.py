"""D-373: no Codex caller can be added without naming itself (a source scan, kept apart from the behavioural tests).

The meter guard defaults to GUARDED: a caller that passes no ``caller=`` is refused like any other, and shows up as ``unspecified`` in
``codex_meter.jsonl``. These scans fail the PR that adds a ``run_codex(...)`` call without a name, or that names itself as the
reserved caller (``agent_route``) anywhere but in the agent-route hook. They walk the source with ``ast``, so the mutation campaign
(``scripts/gf_excluded_tests.py``) excludes this file by rule; the behavioural tests are in ``tests/test_codex_meter.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from llm_router import codex_meter

REPO = Path(__file__).resolve().parents[1]


def _is_caller_unpack(keyword: ast.keyword) -> bool:
    """``**_caller_kwargs(run_codex)``: the agent-route hook passes ``caller`` only to a ``run_codex`` that takes it."""
    value = keyword.value
    return (keyword.arg is None and isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == "_caller_kwargs")


def _run_codex_calls() -> list[tuple[str, int, bool]]:
    """Every ``run_codex(...)`` call in src: (file, line, names its caller)."""
    out = []
    for path in sorted((REPO / "src" / "llm_router").rglob("*.py")):
        if path.name == "codex_agent.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                if name == "run_codex":
                    named = any(k.arg == "caller" or _is_caller_unpack(k) for k in node.keywords)
                    out.append((str(path.relative_to(REPO)), node.lineno, named))
    return out


def test_every_run_codex_call_in_the_package_names_its_caller():
    """A new caller that forgets ``caller=`` is still guarded (the default is guarded), but it shows up as ``unspecified`` in
    ``codex_meter.jsonl``. Naming it is the rule; this fails the PR that forgets."""
    calls = _run_codex_calls()
    assert len(calls) >= 6, calls  # the scan found the callers it is about
    assert [c for c in calls if not c[2]] == []
    assert {c[0] for c in calls} >= {"src/llm_router/tools/codex.py", "src/llm_router/router.py",
                                     "src/llm_router/model_evaluator.py", "src/llm_router/resolver/calibrate.py",
                                     "src/llm_router/session_broker.py", "src/llm_router/hooks/agent-route.py"}


def test_only_agent_route_names_itself_as_the_reserved_caller():
    """No ``caller="agent_route"`` keyword anywhere, and the one place the name is built is the hook's ``_caller_kwargs``."""
    keywords, dicts = {}, {}
    for path in sorted((REPO / "src" / "llm_router").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(REPO))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "caller" and isinstance(node.value, ast.Constant) \
                    and node.value.value in codex_meter.EXEMPT_CALLERS:
                keywords[rel] = node.value.value
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant) and key.value == "caller" and isinstance(value, ast.Constant)
                            and value.value in codex_meter.EXEMPT_CALLERS):
                        dicts[rel] = value.value
    assert keywords == {}
    assert dicts == {"src/llm_router/hooks/agent-route.py": "agent_route"}


def test_a_module_that_builds_its_own_codex_exec_argv_applies_the_guard():
    """``agentic/adapters.py`` starts ``codex exec`` without ``run_codex`` (an injected runner). Any file that does that must
    ask ``codex_meter`` itself, or the reserve has a hole: the one this found was tier 1 of llm_delegate / llm_act."""
    builders = []
    for path in sorted((REPO / "src" / "llm_router").rglob("*.py")):
        if path.name in ("codex_agent.py", "codex_meter.py", "codex_bench.py"):
            continue
        source = path.read_text(encoding="utf-8")
        if '"exec"' in source and '"--ignore-rules"' in source and '"--json"' in source:
            builders.append((str(path.relative_to(REPO)), "codex_meter" in source))
    assert builders, "the scan found no direct `codex exec` builder; it would pass anything"
    assert [b for b in builders if not b[1]] == [], builders
