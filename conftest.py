"""Repo-root conftest — downstream-only test behaviour.

WHY THIS FILE IS AT THE ROOT AND NOT IN tests/
==============================================

It was in ``tests/conftest.py`` first. The next upstream sync overwrote it,
because ``tests/conftest.py`` exists upstream too and the sync owns every file
it carries. The hook vanished silently and 45 tests went back to failing.

So: anything downstream-specific must live somewhere the sync does not write.
The repo root is outside both synced trees (``src/`` and ``tests/``), and pytest
picks up a root ``conftest.py`` automatically. Same effect, and it survives.

That is a general rule for this repository now, not a one-off: **do not put
downstream-only code in a file the sync carries.** It will be silently
reverted, and the revert looks like nothing happened.

WHAT IT DOES
============

llm-routing deliberately does not ship ``enterprise``, ``admin_api``,
``invoice_reconciliation``, ``tenant_policy_sidecar`` or the agoragentic tools
(see the upstream sync tooling's exclusion set, and the downstream sync plan §1).

The sync skips test FILES whose module-level imports name one of those. It
cannot skip a test whose import is DEFERRED — inside a fixture or a function
body — because the file imports fine and only the individual test raises.

A failure is the wrong signal there: nothing is broken. The capability is
absent by design, so the test is not applicable to this package, and
``skipped`` is the status that says exactly that. Marking it passed would be a
lie; leaving it failed trains people to ignore a red suite.

Deliberately NARROW: only the two exception shapes an absent submodule
produces, and only for these exact module roots. A generic ``ImportError``,
or one naming anything else, still fails — "the module is absent by design"
and "the module is broken" must never look the same.
"""

from __future__ import annotations

import re

import pathlib

import pytest

_EXCLUDED_MODULE_ROOTS = (
    "llm_router.enterprise",
    "llm_router.admin_api",
    "llm_router.commands.admin_api",
    "llm_router.invoice_reconciliation",
    "llm_router.tenant_policy_sidecar",
    "llm_router.tools.agoragentic",
    "llm_router.control_plane.audit",
    # Not an excluded CAPABILITY — a repo-root helper package (the RouterArena
    # benchmark harness) that lives outside every synced tree. Same treatment
    # for the same reason: absent by design, so not applicable rather than
    # broken.
    "bench",
)


_CANNOT_IMPORT_NAME = re.compile(
    r"cannot import name '(?P<leaf>[^']+)' from '(?P<pkg>[^']+)'"
)


def _matches_excluded(dotted: str) -> str | None:
    for root in _EXCLUDED_MODULE_ROOTS:
        if dotted == root or dotted.startswith(root + "."):
            return root
    return None


def _missing_excluded_module(exc: BaseException | None) -> str | None:
    """The excluded module root this exception is about, or None.

    TWO FORMS, because Python raises different exceptions for them:

        import llm_router.invoice_reconciliation
            -> ModuleNotFoundError, .name == "llm_router.invoice_reconciliation"

        from llm_router import invoice_reconciliation
            -> plain ImportError, .name == "llm_router" (the PACKAGE, which
               exists), and the missing part appears only in the message:
               "cannot import name 'invoice_reconciliation' from 'llm_router'"

    The first version handled only ModuleNotFoundError and missed every
    `from … import <submodule>` — which is the common form in tests, so a
    third of the excluded-capability failures stayed red while the hook looked
    like it was working.

    Still narrow on purpose. The message is parsed for the exact
    ``pkg.leaf`` pair, and that pair must be an excluded root. A generic
    ImportError, or one naming anything else, still fails — "absent by design"
    and "broken" must never look the same.

    Walks the ``__cause__`` / ``__context__`` chain: a fixture that wraps the
    import in its own error still has the real one underneath.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ModuleNotFoundError):
            hit = _matches_excluded(getattr(exc, "name", None) or "")
            if hit:
                return hit
        elif isinstance(exc, ImportError):
            match = _CANNOT_IMPORT_NAME.search(str(exc))
            if match:
                hit = _matches_excluded(
                    f"{match.group('pkg')}.{match.group('leaf')}"
                )
                if hit:
                    return hit
        exc = exc.__cause__ or exc.__context__
    return None


#: Repository artifacts that live upstream and are deliberately NOT synced.
#:
#: The sync carries src/, tests/, scripts/ and config/ — the package and the
#: tooling that guards it. It does not carry deployment manifests, planning
#: docs, benchmark corpora or the upstream audit tree, because those describe
#: the upstream REPOSITORY rather than this package. A helm chart named for
#: another project is not something this redistribution should ship.
#:
#: Upstream tests that read them are asserting a property of that repository.
#: Not applicable here, and `skipped` says so; leaving them red trains people
#: to ignore the suite.
#:
#: Path prefixes, matched against the missing file. Narrow on purpose — a
#: FileNotFoundError for anything else is a real failure and still fails.
_UNSYNCED_REPO_PATHS = (
    "Dockerfile",
    "deploy/",
    "Docs/",
    "bench/",
    "zero-tolerance-audit/",
    # The two module paths below are absent for the reasons recorded elsewhere:
    # commands/admin_api.py is an excluded capability, and summary.py is
    # relocated into observability/ (a module cannot sit beside a package of
    # the same name). Tests reading them BY PATH rather than importing them
    # bypass every import-based check.
    "src/llm_router/commands/admin_api.py",
    "src/llm_router/summary.py",
)


def _missing_unsynced_artifact(exc: BaseException | None) -> str | None:
    """The unsynced repo path this FileNotFoundError is about, or None."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, FileNotFoundError):
            path = str(getattr(exc, "filename", "") or "")
            for marker in _UNSYNCED_REPO_PATHS:
                if marker in path:
                    return marker
        exc = exc.__cause__ or exc.__context__
    return None


def _shipped_module_in_traceback(excinfo) -> str | None:
    """A SHIPPED `llm_router` file on the failing import's traceback, if any.

    T-11 (audit 2026-09-22). The excluded-module skip below is right for a test
    that imports an excluded capability directly — that test is not applicable
    to this distribution. It is WRONG when the thing reaching for the excluded
    module is shipped code, because then the ImportError is not a property of
    the test, it is a defect in the wheel.

    That is not hypothetical. `budget_lineage_reconciliation` shipped a public
    function whose body did `from llm_router.control_plane import audit`, so it
    raised on every call any installed user could make — and the four tests that
    would have caught it were rewritten from FAIL to SKIP right here, reading
    "not applicable here" for months.

    So: walk the traceback. If any frame belongs to a shipped `llm_router`
    module (one not itself excluded from the wheel), refuse to skip and let the
    failure stand.
    """
    try:
        import llm_router as _pkg
        roots = [str(pathlib.Path(pth).resolve()) for pth in _pkg.__path__]
    except Exception:  # noqa: BLE001
        return None

    tb = getattr(excinfo, "tb", None)
    while tb is not None:
        fname = tb.tb_frame.f_code.co_filename
        try:
            resolved = str(pathlib.Path(fname).resolve())
        except Exception:  # noqa: BLE001
            resolved = fname
        if any(resolved.startswith(r) for r in roots):
            rel = resolved
            for r in roots:
                if resolved.startswith(r):
                    rel = "src/llm_router" + resolved[len(r):]
                    break
            if rel not in _WHEEL_EXCLUDED_PATHS:
                return rel
        tb = tb.tb_next
    return None


def _wheel_excluded_paths() -> frozenset[str]:
    """Paths pyproject excludes from the wheel, read from pyproject itself.

    Read rather than restated, so this cannot drift away from what is actually
    packaged — the drift being the whole subject of the audit this came from.
    """
    try:
        import tomllib
        cfg = tomllib.loads(
            (pathlib.Path(__file__).parent / "pyproject.toml").read_text(encoding="utf-8")
        )
        return frozenset(
            cfg.get("tool", {}).get("hatch", {}).get("build", {})
            .get("targets", {}).get("wheel", {}).get("exclude", [])
        )
    except Exception:  # noqa: BLE001
        return frozenset()


_WHEEL_EXCLUDED_PATHS = _wheel_excluded_paths()


def _skip(report, reason: str) -> None:
    report.outcome = "skipped"
    report.longrepr = (__file__, 0, reason)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if call.excinfo is None:
        return

    root = _missing_excluded_module(call.excinfo.value)
    if root is not None:
        # T-11: never convert a SHIPPED module's failure into a skip.
        culprit = _shipped_module_in_traceback(call.excinfo)
        if culprit is not None:
            report.longrepr = (
                f"{report.longrepr}\n\n"
                f"[conftest] NOT skipped: {culprit} is shipped in the wheel and "
                f"reaches for the excluded module {root!r}. That is a packaging "
                f"defect, not an inapplicable test (T-11)."
            )
            return
        _skip(
            report,
            f"Skipped: {root} is deliberately not shipped in llm-routing "
            f"(the downstream sync plan §1). This test exercises an excluded "
            f"capability, so it is not applicable here rather than failing.",
        )
        return

    artifact = _missing_unsynced_artifact(call.excinfo.value)
    if artifact is not None:
        _skip(
            report,
            f"Skipped: {artifact!r} is an upstream repository artifact that "
            f"this package deliberately does not carry. The test asserts a "
            f"property of that repository, not of llm-routing.",
        )
