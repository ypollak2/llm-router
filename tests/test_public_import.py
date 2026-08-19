"""Regression: the public distribution must import without ``llm_router.enterprise``.

The wheel/sdist intentionally exclude ``src/llm_router/enterprise/`` (pyproject
``[tool.hatch.build.targets.sdist] exclude``). Public modules therefore MUST
guard their ``from llm_router.enterprise import ...`` statements with
``try/except ImportError`` — otherwise importing ``llm_router.server`` (the MCP
routing entrypoint) crashes for everyone who ``pip install``s the package.

This test runs in a subprocess with ``llm_router.enterprise`` forced absent to
simulate the public wheel, and asserts the core modules still import.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


def test_public_modules_import_without_enterprise():
    code = textwrap.dedent(
        """
        import sys
        # Simulate the public distribution: enterprise/ is not shipped.
        sys.modules["llm_router.enterprise"] = None
        # These are the modules that import llm_router.enterprise at top level and
        # sit on the core routing / CLI / API import paths.
        import llm_router.audit_routing   # noqa: F401  (imported by llm_router.router)
        import llm_router.router          # noqa: F401
        import llm_router.server          # noqa: F401  (MCP entrypoint — `llm_router` CLI)
        import llm_router.rbac_routing     # noqa: F401
        import llm_router.admin_api        # noqa: F401
        import llm_router.scim_api         # noqa: F401
        import llm_router.commands.audit   # noqa: F401
        print("PUBLIC_IMPORT_OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert "PUBLIC_IMPORT_OK" in result.stdout, (
        "Public import failed without llm_router.enterprise — an unguarded "
        f"enterprise import regressed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


def test_critical_module_check_boots_without_enterprise():
    """The MCP server's _critical_modules_or_die() must not require llm_router.enterprise
    in the public (non-enterprise) profile — otherwise the published MCP server
    refuses to boot ('No module named llm_router.enterprise')."""
    code = textwrap.dedent(
        """
        import sys
        sys.modules["llm_router.enterprise"] = None  # simulate the public wheel
        from llm_router.server import _critical_modules_or_die
        _critical_modules_or_die()  # must NOT sys.exit / raise in non-enterprise profile
        print("CRITICAL_CHECK_OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert "CRITICAL_CHECK_OK" in result.stdout, (
        "MCP critical-module check died without llm_router.enterprise — the public "
        f"MCP server would refuse to boot.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
