"""P0.10 (D-17 = A): fail-open never edits ``~/.claude/settings.json`` at runtime.

D-17 chose the shim precisely because it needs no runtime edit of the owner's
settings.json (option C, a watchdog that comments the base URL out, was
rejected). This pins that choice structurally, on the AST (comments and
docstrings cannot satisfy it):

1. The runtime fail-open surfaces (the ``proxy`` package, including the shim,
   and ``proxy_default.py``) never import or call a settings.json writer.
2. In ``commands/proxy_default.py`` the only settings.json writer,
   ``_wire_settings_env``, is called once, from ``install_proxy_default``.
3. ``install_proxy_default`` is called only from ``cmd_proxy_default``, which
   is reached only from the explicit ``llm-router install --proxy-default``
   command (``commands/install.py``).

Scope, stated so it is not over-read: this covers the fail-open path. Other
modules that write settings.json on their own explicit commands (the hook
installer, the pxpipe sync) are outside P0.10 and are not judged here.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "llm_router"

_WRITERS = {"_save_settings", "_wire_settings_env", "settings_path", "_backup_before_overwrite"}
_WRITER_MODULES = {"llm_router.install_hooks", "llm_router.commands.proxy_default"}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _runtime_failopen_files() -> list[Path]:
    files = sorted((SRC / "proxy").glob("*.py")) + [SRC / "proxy_default.py"]
    assert len(files) >= 3 and (SRC / "proxy" / "failopen_shim.py") in files
    return files


def test_runtime_failopen_surfaces_never_touch_a_settings_writer():
    checked = 0
    for path in _runtime_failopen_files():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod not in _WRITER_MODULES, f"{path.name} imports {mod}"
                assert not ({a.name for a in node.names} & _WRITERS), f"{path.name}: {ast.dump(node)}"
            elif isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name not in _WRITER_MODULES, f"{path.name} imports {a.name}"
            elif isinstance(node, ast.Name):
                assert node.id not in _WRITERS, f"{path.name}:{node.lineno} uses {node.id}"
            elif isinstance(node, ast.Attribute):
                assert node.attr not in _WRITERS, f"{path.name}:{node.lineno} uses .{node.attr}"
        checked += 1
    assert checked >= 3


def _calls_by_function(tree: ast.Module, callee: str) -> list[str]:
    """Names of the top-level functions whose bodies call ``callee``."""
    owners = []
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name == callee:
                    owners.append(fn.name)
    return owners


def test_settings_writer_is_called_only_by_the_install_function():
    tree = _tree(SRC / "commands" / "proxy_default.py")
    assert _calls_by_function(tree, "_wire_settings_env") == ["install_proxy_default"]


def test_install_is_reached_only_from_the_explicit_install_command():
    callers: dict[str, list[str]] = {}
    for path in SRC.rglob("*.py"):
        tree = _tree(path)
        for callee in ("install_proxy_default", "cmd_proxy_default"):
            for owner in _calls_by_function(tree, callee):
                callers.setdefault(callee, []).append(f"{path.relative_to(SRC)}:{owner}")
    assert callers["install_proxy_default"] == ["commands/proxy_default.py:cmd_proxy_default"]
    reached_from = callers["cmd_proxy_default"]
    assert reached_from and all(c.startswith("commands/install.py:") for c in reached_from), reached_from
