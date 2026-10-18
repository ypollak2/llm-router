"""AGT A.6-b: every example is wired into CI and documented, and pins its framework.

``examples/<name>/`` must (1) have a ``main.py``, (2) be a row of the matrix in
``.github/workflows/examples.yml``, (3) be linked from ``docs/integration.md``, and (4) pin every
requirement exactly. No framework package may appear in llm-router's own dependencies.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted(p.name for p in (ROOT / "examples").iterdir()
                  if p.is_dir() and not p.name.startswith(("_", ".")))
WORKFLOW = (ROOT / ".github" / "workflows" / "examples.yml").read_text(encoding="utf-8")
DOC = (ROOT / "docs" / "integration.md").read_text(encoding="utf-8")


def test_the_expected_examples_exist():
    # A.6 names these; an empty or short listing must not pass vacuously.
    assert {"python_sdk", "langgraph", "openai_agents", "crewai", "claude_agent_sdk", "pydantic_ai",
            "typescript_openai", "mcp", "proxy"} <= set(EXAMPLES)


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_has_main_and_is_in_the_workflow_matrix_and_the_docs(name):
    assert (ROOT / "examples" / name / "main.py").is_file()
    assert re.search(rf"^\s+- {re.escape(name)}$", WORKFLOW, re.M), f"{name} is not in the examples.yml matrix"
    assert f"(../examples/{name}/)" in DOC, f"docs/integration.md does not link examples/{name}/"


def test_the_workflow_matrix_lists_no_missing_directory():
    listed = re.findall(r"^\s+- (\w+)$", WORKFLOW.split("example:", 1)[1].split("steps:", 1)[0], re.M)
    assert listed and set(listed) == set(EXAMPLES)


@pytest.mark.parametrize("name", EXAMPLES)
def test_requirements_are_pinned_exactly(name):
    path = ROOT / "examples" / name / "requirements.txt"
    if not path.exists():
        pytest.skip("no framework package for this example")
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines
    assert all(re.search(r"==\d", ln) for ln in lines), lines


def test_framework_packages_are_not_llm_router_dependencies():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = " ".join(meta["dependencies"]).lower()
    for package in ("langgraph", "langchain", "crewai", "openai-agents", "claude-agent-sdk", "pydantic-ai"):
        assert package not in declared, package
