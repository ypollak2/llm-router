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

import importlib
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
    out: dict[str, str] = {}
    for m in pkgutil.walk_packages(llm_router.__path__, "llm_router."):
        name = m.name
        if ".hooks." in name or name.endswith("__main__"):
            continue
        try:
            importlib.import_module(name)
        except ImportError as e:
            out[name] = str(e)[:120]
        except Exception:
            pass          # a runtime error on import is a different defect
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
