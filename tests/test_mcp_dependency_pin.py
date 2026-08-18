"""The `mcp` dependency must stay within the major version this code imports against.

WHY THIS EXISTS
===============

`pyproject.toml` declared `mcp>=1.0.0` with no upper bound. `mcp 2.0.0` — the
current latest on PyPI — removed `mcp.server.fastmcp` entirely; its server
submodules are `auth`, `lowlevel`, `mcpserver`, with `MCPServer` the closest
analogue to `FastMCP`. Seven modules here do `from mcp.server.fastmcp import …`.

So every fresh `pip install` / `pipx install` resolved 2.0.0 and died on import
before registering a single tool. The client reports `CONNECTION_CLOSED`, which
looks like a provider or network fault and is neither — the process was already
dead. A user has no way to tell those apart from the outside.

TWO CHECKS, DELIBERATELY
========================

Pinning alone is not the fix asked for. The requirement was a check that catches
a FUTURE major bump, not only this one, so there are two:

1. `test_pin_has_an_upper_bound` — the declared spec must exclude the next major.
   Catches the pin being loosened back to unbounded, which is how this happened.
   It reads pyproject rather than the installed environment, so it fails in CI on
   the commit that introduces the problem rather than months later on someone's
   fresh install.

2. `test_every_mcp_import_in_src_resolves` — discovers `from mcp…` imports by
   parsing the source tree and checks each one actually resolves. Catches the
   real failure (an API the code needs having been removed) under ANY version,
   including a future 2.x port where the pin is legitimately raised and one
   module is missed.

Check 2 is the one that would have caught the original bug. Check 1 is what
stops it recurring. Neither subsumes the other: (1) passes fine against a broken
installed environment, and (2) passes fine against a dangerously loose pin that
simply happens to resolve to a working version today.
"""

from __future__ import annotations

import ast
import importlib
import sys
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
_PYPROJECT = _ROOT / "pyproject.toml"

#: The major version whose API this source tree is written against.
_SUPPORTED_MAJOR = 1


def _mcp_requirement() -> str:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    for dep in data["project"]["dependencies"]:
        name = dep.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip()
        if name == "mcp":
            return dep
    pytest.fail("no `mcp` entry in [project.dependencies] — did it get renamed?")


def test_pin_has_an_upper_bound():
    """An unbounded `mcp` pin is the defect, not a style preference."""
    req = _mcp_requirement()
    assert "<" in req, (
        f"`mcp` is pinned as {req!r} with no upper bound.\n"
        f"mcp 2.0.0 removed mcp.server.fastmcp, which this code imports in "
        f"{len(_mcp_import_sites())} modules, so an unbounded pin means a fresh "
        f"install resolves a version that cannot start. Pin "
        f"`mcp>=1.0.0,<{_SUPPORTED_MAJOR + 1}.0.0`, or port the imports and "
        f"raise both this bound and _SUPPORTED_MAJOR together."
    )
    assert f"<{_SUPPORTED_MAJOR + 1}" in req, (
        f"`mcp` pin {req!r} does not exclude "
        f"{_SUPPORTED_MAJOR + 1}.x, but this source imports the "
        f"{_SUPPORTED_MAJOR}.x API. If the port has been done, raise "
        f"_SUPPORTED_MAJOR in this file in the same commit."
    )


def _mcp_import_sites() -> list[tuple[Path, str, tuple[str, ...]]]:
    """Every `from mcp… import …` in src/, found by parsing rather than grepping.

    Parsed so a commented-out or string-literal mention is not mistaken for a
    real import, and so new modules are covered without editing this test.
    """
    out: list[tuple[Path, str, tuple[str, ...]]] = []
    for path in sorted(_SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "mcp" or node.module.startswith("mcp."):
                    out.append(
                        (path, node.module, tuple(a.name for a in node.names))
                    )
    return out


def test_source_actually_imports_mcp():
    """Guards the guard: if nothing imports mcp, the test below is vacuous."""
    sites = _mcp_import_sites()
    assert sites, (
        "no `from mcp…` imports found in src/ — either the dependency is no "
        "longer used (drop it and this test) or this detector is broken. Both "
        "need a human; a silently vacuous check is worse than none."
    )


@pytest.mark.parametrize(
    "module,names,where",
    [
        pytest.param(m, n, str(p.relative_to(_ROOT)), id=f"{p.stem}:{m}")
        for p, m, n in _mcp_import_sites()
    ],
)
def test_every_mcp_import_in_src_resolves(module: str, names: tuple[str, ...], where: str):
    """The failure the pin exists to prevent, asserted directly.

    This is what actually breaks: not the version number, but a symbol the code
    needs having been removed. It fails identically whether the cause is a loose
    pin, a manual upgrade, or a partial port.
    """
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - the failure path is the point
        pytest.fail(
            f"{where} imports `{module}`, which does not exist in the installed "
            f"mcp ({_installed_mcp_version()}).\n"
            f"mcp 2.0.0 removed mcp.server.fastmcp — its server submodules are "
            f"auth/lowlevel/mcpserver. Either the pin was loosened past "
            f"{_SUPPORTED_MAJOR}.x, or the port is incomplete.\n"
            f"Original error: {exc}"
        )
    for name in names:
        assert hasattr(mod, name), (
            f"{where} imports `{name}` from `{module}`, which exists but no "
            f"longer exports it (installed mcp {_installed_mcp_version()}). "
            f"This is the shape a partial API port leaves behind."
        )


def _installed_mcp_version() -> str:
    try:
        from importlib.metadata import version

        return version("mcp")
    except Exception:  # pragma: no cover
        return "unknown"


def test_the_server_entrypoint_imports():
    """End-to-end: the module whose import failure produced CONNECTION_CLOSED.

    The parametrised test above covers this symbol already; this one exists so a
    failure names the user-visible symptom rather than only the offending line.
    """
    sys.path.insert(0, str(_SRC))
    try:
        importlib.import_module("llm_router.server")
    except ImportError as exc:
        pytest.fail(
            f"llm_router.server does not import, so the MCP server exits before "
            f"registering any tool and the client reports CONNECTION_CLOSED — "
            f"indistinguishable from a network fault. Installed mcp: "
            f"{_installed_mcp_version()}. Error: {exc}"
        )
