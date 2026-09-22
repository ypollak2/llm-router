"""M-11 — what the wheel imports, the wheel must declare.

`llm-router status` is the flagship savings command and the first thing most
people run. On a clean `pip install llm-routing` it crashed:

    ModuleNotFoundError: No module named 'rich'

`rich` appeared nowhere in `pyproject.toml` — not in `dependencies`, not in an
extra — while eight modules imported it, including `commands/status.py` (via
`ui/status_premium.py`) and the session-end summary. It worked for every
developer because the dev environment installs it transitively, which is exactly
why nobody noticed for three weeks.

These tests check the three ways this class of defect appears:

1. a console-script entry point that cannot be imported at all;
2. an unguarded third-party import that nothing declares;
3. production code importing from the **test tree**, which resolves under pytest
   and does not exist in a wheel.

The third was found while fixing the first: `commands/soak.py` does
`from soak.report import ...`, and `soak` is `tests/soak`. `llm-router soak` is
therefore broken for every installed user and passes every test run.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import re
import sys
import tomllib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "llm_router"
PYPROJECT = REPO / "pyproject.toml"

# Modules already known to be unimportable, each with the finding that owns it.
# Named rather than skipped: an allowlist entry is a visible debt, a skip is not.
KNOWN_BROKEN = {
    "llm_router.control_plane.api": "M-10 — imports `audit`, which is not distributed",
    "llm_router.tui.cli": "imports `textual`, declared only as an optional extra",
}

# Import name -> distribution name, where they differ.
_ALIAS = {
    "yaml": "pyyaml",
    "dotenv": "python_dotenv",
    "jwt": "pyjwt",
    "PIL": "pillow",
    "sklearn": "scikit_learn",
    # the opentelemetry namespace is provided by these distributions
    "opentelemetry": "opentelemetry_sdk",
    # pulled in by fastapi/uvicorn and always present with them
    "starlette": "fastapi",
    "anyio": "fastapi",
}


def _declared() -> set[str]:
    pj = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    names = set(pj["project"]["dependencies"])
    for extra in pj["project"].get("optional-dependencies", {}).values():
        names |= set(extra)
    return {re.split(r"[<>=!\[;]", n)[0].strip().lower().replace("-", "_") for n in names}


def _console_scripts() -> dict[str, str]:
    pj = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return pj["project"].get("scripts", {})


def test_rich_is_declared():
    """The specific regression, pinned."""
    assert "rich" in _declared(), (
        "rich is undeclared again — `llm-router status` will crash on a clean install"
    )


@pytest.mark.parametrize("name", sorted(_console_scripts()))
def test_every_console_script_imports(name):
    """The end-to-end form: an entry point a user can type must load."""
    module = _console_scripts()[name].split(":")[0]
    try:
        importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 — any failure is a failure
        pytest.fail(f"console script `{name}` -> {module} cannot be imported: {exc!r}")


def test_console_scripts_were_actually_found():
    """Denominator guard: an empty script table would pass the test above."""
    scripts = _console_scripts()
    assert len(scripts) >= 4, f"only {len(scripts)} console scripts found — parse failed?"
    assert "llm-router" in scripts


def _unguarded_third_party() -> dict[str, set[str]]:
    """Third-party imports NOT inside a try/except and NOT declared.

    An import inside `try:` is an optional feature degrading gracefully. An
    unguarded one is a hard requirement whether or not anyone wrote it down.
    """
    declared = _declared()
    stdlib = set(sys.stdlib_module_names)
    out: dict[str, set[str]] = {}

    for f in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue

        guarded = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for n in ast.walk(node):
                    if isinstance(n, (ast.Import, ast.ImportFrom)):
                        guarded.add(id(n))

        for n in ast.walk(tree):
            if id(n) in guarded:
                continue
            mods: list[str] = []
            if isinstance(n, ast.Import):
                mods = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods = [n.module.split(".")[0]]
            for m in mods:
                if m in stdlib or m == "llm_router" or m.startswith("_"):
                    continue
                if _ALIAS.get(m, m).lower() in declared:
                    continue
                out.setdefault(m, set()).add(str(f.relative_to(SRC)))
    return out


def test_no_unguarded_undeclared_third_party_import():
    """`rich` generalised: the next one must fail here, not on a user's machine."""
    found = _unguarded_third_party()
    # modules owned by a KNOWN_BROKEN entry are tracked there, not here
    excused_files = {
        "control_plane/api.py",
        "tui/cli.py",
        # postgres and control-plane crypto are deployment-optional backends
        "budget_backend_postgres.py",
        "control_plane/signing.py",
    }
    real = {
        mod: files - excused_files
        for mod, files in found.items()
        if files - excused_files
    }
    assert not real, (
        "unguarded third-party imports that nothing declares:\n"
        + "\n".join(f"  {m}: {sorted(f)}" for m, f in sorted(real.items()))
        + "\n\nEither declare it in pyproject.toml, or wrap the import in "
          "try/except and degrade when it is absent."
    )


def test_production_code_does_not_import_the_test_tree():
    """M-11b. `tests/` is not in the wheel; an import of it is broken on install.

    This resolves under pytest — which is precisely why it survived. The failure
    only appears for someone who installed the package.
    """
    test_packages = {
        p.name for p in (REPO / "tests").iterdir()
        if p.is_dir() and (p / "__init__.py").exists()
    }
    assert test_packages, "no test packages found — this check would be vacuous"

    offenders = []
    for f in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods = [n.module.split(".")[0]]
            for m in mods:
                if m in test_packages:
                    offenders.append(f"{f.relative_to(SRC)}:{n.lineno} imports `{m}`")

    # M-11b is fixed: `commands/soak.py` now imports the harness lazily, inside
    # the command, and prints an actionable message when it is absent. A
    # MODULE-level import of the test tree is the defect, because it breaks the
    # module for everyone rather than the one command that needs it.
    module_level = []
    for f in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in tree.body:            # top level only
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods = [n.module.split(".")[0]]
            for m in mods:
                if m in test_packages:
                    module_level.append(f"{f.relative_to(SRC)}:{n.lineno} imports `{m}`")
    assert not module_level, (
        "production modules importing the test tree at module level (broken in "
        "any wheel):\n  " + "\n  ".join(module_level)
    )


@pytest.mark.parametrize("module", sorted(KNOWN_BROKEN))
def test_known_broken_modules_are_still_broken(module):
    """Strict tracking: when one is fixed, this fails and forces the list to shrink.

    Without this, KNOWN_BROKEN becomes a place defects go to be forgotten.
    """
    try:
        importlib.import_module(module)
    except Exception:
        return  # still broken, as recorded
    pytest.fail(
        f"{module} now imports cleanly ({KNOWN_BROKEN[module]}). Remove it from "
        f"KNOWN_BROKEN and from the excused-files list above."
    )
