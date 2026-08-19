"""An optional tool group must not be a load-bearing import of the server.

WHY THIS EXISTS
===============

``server.py`` imported ``llm_router.tools.agoragentic`` at module level, in the same
statement as every mandatory tool module. That contradicted two things the
codebase already said about it:

* **SEC-003**: it is OFF by default — ``register()`` no-ops without
  ``LLM_ROUTER_AGORAGENTIC=on``.
* It is excluded from redistributions that do not want marketplace/wallet tools
  (36_DOWNSTREAM_SYNC_PLAN.md §1 lists it in the exclusion set).

So a build that dropped it got a server that could not import AT ALL — failing
at startup over a feature nobody had enabled. Not a gradual degradation: a
``ModuleNotFoundError`` before a single tool registered, which surfaces to the
client as ``CONNECTION_CLOSED`` and looks exactly like a network fault. That is
the same symptom, and the same diagnostic dead end, as the mcp 2.0 breakage
reported downstream as issue #37.

HOW IT WAS FOUND
================

Not by reading the file. The downstream sync's availability closure marked
``llm_router.server`` unreachable, because it walks module-level imports and one of
them was excluded. The consequence was that the sync could not carry the server
module at all — which read as a tooling limitation until the actual question
got asked: *why is an optional, default-off tool group a hard dependency of the
server?*

The gate was already correct. The import was not. A default-off feature whose
absence is fatal at import time is not really optional, whatever its gate says.

WHAT IS ASSERTED
================

The structural property, not the current file contents: every tool module the
server treats as optional must be imported in a way that survives its absence,
and must be guarded at its registration site. A future optional tool group added
to the mandatory import list fails here.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

#: Tool modules that may legitimately be absent from a build. Keep in sync with
#: the sync tooling's exclusion set — an entry here is a promise that the server
#: still starts without it.
OPTIONAL_TOOL_MODULES = ("agoragentic",)

_SERVER = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "server.py"


def _server_tree() -> ast.Module:
    return ast.parse(_SERVER.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", OPTIONAL_TOOL_MODULES)
def test_optional_group_is_not_in_the_mandatory_import(name: str):
    """It must not ride along in the unguarded `from llm_router.tools import …`."""
    for node in _server_tree().body:
        if isinstance(node, ast.ImportFrom) and node.module == "llm_router.tools":
            imported = {a.name for a in node.names}
            assert name not in imported, (
                f"{name!r} is in the mandatory `from llm_router.tools import …` "
                f"statement. It is optional and excludable, so a build without "
                f"it gets a server that cannot import — the client sees "
                f"CONNECTION_CLOSED and cannot tell it from a network fault."
            )


@pytest.mark.parametrize("name", OPTIONAL_TOOL_MODULES)
def test_optional_group_is_imported_defensively(name: str):
    """Its import must sit inside a try/except that tolerates absence."""
    found = False
    for node in ast.walk(_server_tree()):
        if not isinstance(node, ast.Try) or not node.handlers:
            continue
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.ImportFrom) and stmt.module == "llm_router.tools":
                if name in {a.name for a in stmt.names}:
                    found = True
    assert found, (
        f"{name!r} is not imported inside a try/except. Without one its absence "
        f"is fatal at import time, which is not what 'optional' means."
    )


@pytest.mark.parametrize("name", OPTIONAL_TOOL_MODULES)
def test_registration_is_guarded(name: str):
    """`register()` must not be called on a module that may be None.

    The defensive import alone is not enough — binding the name to ``None`` and
    then calling ``.register()`` on it moves the failure a few lines later and
    turns a clear ImportError into an AttributeError.
    """
    src = _SERVER.read_text(encoding="utf-8")
    assert f"if {name} is not None:" in src, (
        f"{name}.register(...) is called without a None guard, so a build that "
        f"excludes it fails with AttributeError instead of starting"
    )


def test_the_server_still_imports_with_the_group_absent(monkeypatch):
    """The behaviour, not just the shape: simulate the excluded build.

    Blocked with a ``sys.meta_path`` finder rather than by patching
    ``builtins.__import__``. The first version patched ``__import__`` and
    checked for the name ``llm_router.tools.agoragentic`` — which never appears,
    because ``from llm_router.tools import agoragentic`` calls
    ``__import__("llm_router.tools", …, fromlist=["agoragentic"])``. The blocker
    matched nothing and the test passed against the hard import too, i.e. it
    was not a test. A finder sees the real submodule name.

    IN A SUBPROCESS, because importing the server registers tools on a module
    level ``mcp`` object. The in-process version re-imported ``llm_router.server``
    with a blocker installed and left a second registry behind;
    ``test_policy_digest_community`` then failed on a tool it could no longer
    see. It passed in isolation and failed in the suite — pollution, not a
    defect, and the kind that gets "fixed" by reordering tests until it hides.
    A subprocess cannot pollute anything.
    """
    import subprocess
    import sys
    import textwrap

    program = textwrap.dedent(
        """
        import sys

        class Block:
            def find_spec(self, name, path=None, target=None):
                if name == "llm_router.tools.agoragentic":
                    raise ImportError("simulated: excluded from this build")
                return None

        sys.meta_path.insert(0, Block())
        import llm_router.server  # noqa: F401
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0 and "OK" in result.stdout, (
        "the server could not import without agoragentic — that is the "
        "excluded-build configuration, and it must start.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr[-1500:]}"
    )


def test_every_optional_group_is_actually_excludable():
    """Guards the guard: this list must match the sync's exclusion set.

    If the two drift, this file asserts a property about modules nobody
    excludes, and stays green while the real optional group goes unchecked.
    """
    sync = Path(__file__).resolve().parents[1] / "scripts" / "sync_downstream.py"
    if not sync.exists():  # pragma: no cover
        pytest.skip("sync tooling not present")
    text = sync.read_text(encoding="utf-8")
    for name in OPTIONAL_TOOL_MODULES:
        assert f"tools/{name}.py" in text or f"tools.{name}" in text, (
            f"{name!r} is treated as optional here but is not in the sync's "
            f"exclusion set — one of the two is wrong"
        )


def test_mandatory_groups_are_still_mandatory():
    """The fix must not quietly make everything optional.

    A defensive import around every tool module would pass the tests above and
    turn a missing core tool group into a silently smaller tool list.
    """
    src = inspect.getsource(__import__("llm_router.server", fromlist=["x"]))
    for mandatory in ("routing", "admin", "consolidated"):
        assert f"{mandatory}.register(mcp" in src, (
            f"{mandatory} is no longer registered unconditionally"
        )
