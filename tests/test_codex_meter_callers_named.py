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


def _run_codex_calls() -> list[tuple[str, int, bool]]:
    """Every ``run_codex(...)`` call in src: (file, line, passes caller=)."""
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
                    out.append((str(path.relative_to(REPO)), node.lineno, any(k.arg == "caller" for k in node.keywords)))
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
    seen = {}
    for path in sorted((REPO / "src" / "llm_router").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "run_codex":
                for k in node.keywords:
                    if k.arg == "caller" and isinstance(k.value, ast.Constant) and k.value.value in codex_meter.EXEMPT_CALLERS:
                        seen[str(path.relative_to(REPO))] = k.value.value
    assert seen == {"src/llm_router/hooks/agent-route.py": "agent_route"}
