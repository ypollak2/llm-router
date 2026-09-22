"""Every shipped module must import, or be a declared optional extra.

Found 2026-09-15 while finishing the quarantine triage. `test_cp_sse_policy_events`
was quarantined because `llm_router.control_plane.api` "no longer imports", and
that was read as a test problem. It is not: the module is shipped and CANNOT be
imported, because it does `from llm_router.control_plane import audit`
unconditionally and `audit.py` is the enterprise module deliberately excluded from
this distribution.

Quarantining the test is what hid it. Nothing else in `src/` imports
`control_plane.api`, so no other test would ever have noticed.

This is a ratchet, not a fix. Whether to ship `audit.py` or drop these two modules
from the package is a packaging decision, and guessing it here would be worse than
recording it: stubbing the import would silently disable audit logging in a control
plane, which is the opposite of what an audit module is for.
"""
from __future__ import annotations

import contextlib
import importlib
import pathlib
import pkgutil

import pytest

import llm_router

# Optional extras. Absent on a default install BY DESIGN, and each is declared in
# pyproject's optional-dependencies.
OPTIONAL = {
    "llm_router.dashboard.tui": "textual",
    "llm_router.tui": "textual",
    "llm_router.integrations.agno": "agno",
}

# Shipped but un-importable, with the reason. Lower this list; never extend it
# without recording why the module ships in a state where it cannot be used.
# M-10, resolved 2026-09-22: both are now EXCLUDED FROM THE WHEEL
# (`[tool.hatch.build.targets.wheel] exclude`), so they are no longer shipped in
# a state where they cannot be imported. They remain in the source tree for
# whoever writes `control_plane/audit.py`, and nothing in `src/` imports them.
#
# They still fail to import from a source checkout, which is correct and is not
# what this test is for: the invariant is "every SHIPPED module imports".
KNOWN_BROKEN: dict[str, str] = {}

#: Present in src/ but deliberately not packaged. Each needs a reason.
NOT_SHIPPED = {
    "llm_router.control_plane.api":
        "M-10 — needs control_plane/audit.py, which has never existed",
    "llm_router.control_plane.reconciliation":
        "M-10 — same",
}


def _import_failures() -> dict[str, str]:
    """Import every shipped module and report the ones that raise ImportError.

    Restores the package attributes `pkgutil.walk_packages` perturbs. Walking a
    package imports it, which binds `llm_router.hooks` as a namespace module
    with `__file__ = None` — a fileless stub left on the package object that
    then answers for the real module in every test that runs after this one.
    That is the module-cache contamination shape from T-01, caught here by the
    `_no_module_state_leak` guard in tests/conftest.py rather than by anyone
    noticing the eight downstream failures it can cause.
    """
    import sys as _sys

    before = {n: dict(vars(mod)) for n, mod in list(_sys.modules.items())
              if n == "llm_router" or n.startswith("llm_router.")}
    out: dict[str, str] = {}
    try:
        for m in pkgutil.walk_packages(llm_router.__path__, "llm_router."):
            name = m.name
            if ".hooks." in name or name.endswith("__main__"):
                continue
            try:
                importlib.import_module(name)
            except ImportError as e:
                out[name] = str(e)[:120]
            except Exception:
                pass      # a runtime error on import is a different defect
    finally:
        for pkg_name, snapshot in before.items():
            mod = _sys.modules.get(pkg_name)
            if mod is None:
                continue
            for attr in [a for a in vars(mod) if a not in snapshot]:
                sub = getattr(mod, attr, None)
                if getattr(sub, "__file__", "sentinel") is None:
                    try:
                        delattr(mod, attr)
                    except Exception:  # noqa: BLE001
                        pass
    return out


def test_no_new_module_fails_to_import():
    failures = _import_failures()
    # NOT_SHIPPED modules are excluded from the wheel, so they are outside this
    # invariant by construction: "every SHIPPED module imports". They still fail
    # from a source checkout, which is correct and is the reason they are not
    # packaged. `test_unshipped_modules_are_actually_excluded_from_the_wheel`
    # keeps that claim honest.
    unexpected = {
        n: e for n, e in failures.items()
        if n not in KNOWN_BROKEN and n not in NOT_SHIPPED and not any(
            dep in e for dep in OPTIONAL.values())
    }
    assert not unexpected, (
        f"{len(unexpected)} shipped module(s) newly fail to import: {unexpected}. "
        "A module that ships in a state where it cannot be imported is a defect "
        "no other test will notice, because nothing imports it."
    )


def test_the_known_broken_list_has_not_grown():
    failures = _import_failures()
    still_broken = {n for n in KNOWN_BROKEN if n in failures}
    assert len(still_broken) <= len(KNOWN_BROKEN)
    fixed = set(KNOWN_BROKEN) - still_broken
    if fixed:
        pytest.fail(
            f"{sorted(fixed)} now import — good. Remove them from KNOWN_BROKEN so "
            "the ratchet keeps its teeth."
        )


@pytest.mark.parametrize("mod,dep", sorted(OPTIONAL.items()))
def test_optional_extras_fail_only_on_their_own_dependency(mod, dep):
    """An extra may be absent. It may not be absent for the WRONG reason."""
    try:
        importlib.import_module(mod)
    except ImportError as e:
        assert dep in str(e), (
            f"{mod} failed for a reason other than its declared extra {dep!r}: {e}"
        )
    except Exception:
        pass


def test_unshipped_modules_are_actually_excluded_from_the_wheel():
    """A module listed as not-shipped must really be excluded.

    Otherwise this list becomes a comfortable fiction: the modules keep going
    out in the wheel, and the record says they do not.
    """
    import pathlib as _pl
    import tomllib

    root = _pl.Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = set(
        cfg.get("tool", {}).get("hatch", {}).get("build", {})
        .get("targets", {}).get("wheel", {}).get("exclude", [])
    )
    missing = []
    for dotted in NOT_SHIPPED:
        rel = "src/" + dotted.replace(".", "/") + ".py"
        if rel not in excluded:
            missing.append(rel)
    assert not missing, (
        f"listed as not shipped but still packaged: {missing}"
    )


# ── Importing is not enough (T-11, audit 2026-09-22) ─────────────────────────


def _shipped_source_files() -> list[pathlib.Path]:
    """Every .py file that goes into the wheel."""
    root = pathlib.Path(llm_router.__file__).resolve().parent
    repo = root.parents[1]
    out = []
    for f in sorted(root.rglob("*.py")):
        rel = "src/llm_router" + str(f)[len(str(root)):]
        if rel in _wheel_excluded():
            continue
        if "/hooks/" in str(f):
            continue  # hooks run as standalone scripts, not as package imports
        out.append(f)
    _ = repo
    return out


def _wheel_excluded() -> set[str]:
    import tomllib
    root = pathlib.Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return set(
        cfg.get("tool", {}).get("hatch", {}).get("build", {})
        .get("targets", {}).get("wheel", {}).get("exclude", [])
    )


def _function_level_llm_router_imports() -> list[tuple[str, int, str]]:
    """`from llm_router... import X` statements INSIDE a function body.

    A module-level import of a missing module makes the whole module fail to
    import, and `test_no_new_module_fails_to_import` catches that. A
    function-level one does not: the module imports fine, ships fine, and raises
    only when someone calls it. That is T-11 exactly, and nothing caught it
    because no test called the function.
    """
    import ast

    found: list[tuple[str, int, str]] = []
    for f in _shipped_source_files():
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom) and (sub.module or "").startswith("llm_router"):
                    for alias in sub.names:
                        found.append((str(f), sub.lineno,
                                      f"{sub.module}.{alias.name}"))
                elif isinstance(sub, ast.Import):
                    for alias in sub.names:
                        if alias.name.startswith("llm_router"):
                            found.append((str(f), sub.lineno, alias.name))
    return found


@contextlib.contextmanager
def _no_package_attr_leak():
    """Undo the fileless package attributes an import walk binds.

    Importing `llm_router.x.y` binds `y` on `llm_router.x`. For a namespace
    package that attribute is a module with `__file__ = None`, and it then
    answers for the real module in every test that runs afterwards — the T-01
    contamination shape, which `tests/conftest.py::_no_module_state_leak`
    catches at teardown. Any helper here that imports broadly wraps itself in
    this.
    """
    import sys as _sys

    before = {n: set(vars(m)) for n, m in list(_sys.modules.items())
              if n == "llm_router" or n.startswith("llm_router.")}
    try:
        yield
    finally:
        for pkg_name, snapshot in before.items():
            mod = _sys.modules.get(pkg_name)
            if mod is None:
                continue
            for attr in [a for a in vars(mod) if a not in snapshot]:
                sub = getattr(mod, attr, None)
                if getattr(sub, "__file__", "sentinel") is None:
                    try:
                        delattr(mod, attr)
                    except Exception:  # noqa: BLE001
                        pass


#: Function-level imports that cannot resolve, each with a reason.
#:
#: This is a RATCHET, like KNOWN_BROKEN above. Lower it; never extend it without
#: recording why a shipped function is allowed to raise when called. Two
#: categories live here and they are not the same thing:
#:
#:   * NOT SHIPPED DOWNSTREAM — the target is an upstream/enterprise capability
#:     this distribution deliberately does not carry, and the call site guards
#:     the import. Acceptable.
#:   * KNOWN DEFECT — the target simply does not exist. The function raises on
#:     every call. Each of these carries its finding id and must be removed when
#:     that finding is fixed, not when someone tires of the list.
UNRESOLVABLE_ALLOWED: dict[str, str] = {
    # ── not shipped downstream (guarded at the call site) ──
    "llm_router.admin_api.create_app":
        "admin API is an excluded capability (see conftest _UNSYNCED_REPO_PATHS)",
    "llm_router.chronicle.chronicle_log_decision":
        "upstream-only module, deliberately not synced",
    "llm_router.invoice_reconciliation.build_reconciliation_report":
        "upstream-only module, deliberately not synced",
    "llm_router.invoice_reconciliation.compute_diff": "same",
    "llm_router.invoice_reconciliation.format_report": "same",
    "llm_router.enterprise.identity.IdentityStore": "enterprise, not shipped downstream",
    "llm_router.enterprise.identity.InvalidToken": "enterprise, not shipped downstream",
    "llm_router.enterprise.oidc.OidcConfig": "enterprise, not shipped downstream",
    "llm_router.enterprise.oidc.OidcError": "enterprise, not shipped downstream",
    "llm_router.enterprise.oidc.OidcValidator": "enterprise, not shipped downstream",
    "llm_router.enterprise.quotas.QuotaExceeded": "enterprise, not shipped downstream",
    "llm_router.enterprise.quotas.QuotaTracker": "enterprise, not shipped downstream",
    "llm_router.enterprise.rbac.Permission": "enterprise, not shipped downstream",
    "llm_router.enterprise.rbac.permissions_for_role": "enterprise, not shipped downstream",

    # ── optional extras (declared in pyproject optional-dependencies) ──
    "llm_router.dashboard.tui.run": "optional extra: textual",
    "llm_router.tui.LLMRouterDashboard": "optional extra: textual",
    "llm_router.tui.run_dashboard": "optional extra: textual",

    # ── KNOWN DEFECTS — these raise on every call. Remove when fixed. ──
    "llm_router.auto_profile.PROFILE_PATH":
        "T-18 — `llm-router profile` raises ImportError; the symbol does not "
        "exist in auto_profile.py. Rediscovered here independently of the audit.",
    "llm_router.claude_usage.refresh_claude_usage":
        "NEW (found by this test, not by the audit) — QuotaTracker.force_refresh "
        "raises ImportError on every call; claude_usage.py has no such function. "
        "The call site calls itself 'a placeholder'. Writing it is feature work.",
}


def _unresolvable(module: str, name: str) -> bool:
    """True when `from <module> import <name>` cannot succeed on an install.

    Resolved statically against the shipped tree rather than by importing the
    target, so a module with import side effects is not executed here.

    The original version of this test asked whether the target was in the
    wheel's exclude list, and that was the wrong question: `control_plane/
    audit.py` was never excluded, it has simply never existed. Checking
    "excluded" instead of "resolvable" let the F21 gate pass on the reverted
    T-11 function — caught by running the gate, which is the point of running it.
    """
    try:
        pkg = importlib.import_module(module)
    except Exception:  # noqa: BLE001
        return True   # the module itself does not import — already a defect
    if hasattr(pkg, name):
        return False
    for path in getattr(pkg, "__path__", []):
        base = pathlib.Path(path)
        if (base / f"{name}.py").exists() or (base / name / "__init__.py").exists():
            # Present in the source tree — but is it in the wheel?
            rel = "src/llm_router" + str(base / f"{name}.py")[
                len(str(pathlib.Path(llm_router.__file__).resolve().parent)):]
            return rel in _wheel_excluded()
    return True


def test_no_shipped_function_imports_a_module_that_cannot_be_resolved():
    """The T-11 shape: a shipped function that raises the moment it is called.

    `reconcile_budget_lineage_audited` did `from llm_router.control_plane import
    audit` in its body. `audit.py` has never existed. The module imported
    cleanly, shipped in the wheel, and raised ImportError for every installed
    user on every call — invisible to a suite that only imports modules, and
    rewritten from FAIL to SKIP by the root conftest for the four tests that
    did call it.
    """
    with _no_package_attr_leak():
        offenders = []
        for path, lineno, target in _function_level_llm_router_imports():
            module, _, name = target.rpartition(".")
            if target in UNRESOLVABLE_ALLOWED:
                continue
            if module and _unresolvable(module, name):
                offenders.append(f"{path}:{lineno} — from {module} import {name}")
        assert not offenders, (
            "shipped function(s) import something that cannot be resolved on an "
            "install. Each raises ImportError on every call:\n  "
            + "\n  ".join(sorted(offenders))
        )


def test_the_function_import_scan_is_not_vacuous():
    """A scan that finds nothing proves nothing.

    `llm_router` uses deferred imports heavily, so this number is large. If it
    ever drops to zero the AST walk has stopped matching and the test above is
    passing on an empty set.
    """
    found = _function_level_llm_router_imports()
    assert len(found) >= 50, (
        f"the function-level import scan found only {len(found)} statements — "
        "it is no longer reading the source tree it is meant to protect"
    )


def test_the_allowlist_has_not_gone_stale():
    """An entry that now resolves must be removed, or the ratchet loses its teeth.

    Same shape as `test_the_known_broken_list_has_not_grown`: a list of accepted
    breakage is only honest while every entry is still broken.
    """
    with _no_package_attr_leak():
        fixed = []
        for target in UNRESOLVABLE_ALLOWED:
            module, _, name = target.rpartition(".")
            if module and not _unresolvable(module, name):
                fixed.append(target)
        assert not fixed, (
            f"{sorted(fixed)} now resolve — remove them from UNRESOLVABLE_ALLOWED."
        )
