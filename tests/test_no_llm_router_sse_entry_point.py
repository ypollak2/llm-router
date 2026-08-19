"""SEC-001 regression: llm_router-sse must not be installed as a console script.

The prior `llm_router-sse` entry point invoked `main_sse()` which bound 0.0.0.0
with no auth and exposed the full MCP tool surface (incl. fs + wallet).
It was removed in the SEC-001 remediation. Re-adding it without an auth
wrapper is a Critical regression. This test guards against that.

See: Docs/audit/HIGH_PRIORITY_WORK_PLAN.md F-SEC-001
"""
from __future__ import annotations

from pathlib import Path

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_llm_router_sse_not_in_project_scripts() -> None:
    """[project.scripts] must NOT contain llm_router-sse — SEC-001."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    scripts = data.get("project", {}).get("scripts", {})
    assert "llm_router-sse" not in scripts, (
        "llm_router-sse was re-introduced to [project.scripts] without an auth "
        "wrapper. See SEC-001 in Docs/audit/HIGH_PRIORITY_WORK_PLAN.md before "
        "re-adding."
    )


def test_main_sse_still_importable() -> None:
    """main_sse() must remain importable for future hardened wrappers — SEC-001."""
    from llm_router.server import main_sse
    assert callable(main_sse), "main_sse should remain a callable for future re-use"


def test_main_sse_docstring_carries_security_notice() -> None:
    """main_sse() docstring must warn future maintainers — SEC-001."""
    from llm_router.server import main_sse
    doc = main_sse.__doc__ or ""
    assert "SEC-001" in doc, "main_sse docstring lost its SEC-001 security notice"
    assert "INTENTIONALLY not exposed" in doc, (
        "main_sse docstring lost its 'intentionally not exposed' clause"
    )
