"""WP-13 — a swallowed exception must still leave a trace.

RED8-09. Fail-open is often the RIGHT behaviour on these paths: a hook that
raises kills the user's turn, and a telemetry write that raises turns
observability into an outage. The defect was never the catch — it was the
SILENCE. A handler ending in bare `pass` is indistinguishable from the happy
path in every surface we have.

These pin the accounting itself. The lint (`scripts/lint_fail_open.py`) pins that
call sites exist; this pins that the mechanism they call actually records, and —
more importantly — that it cannot make things worse than the silence it replaces.
"""

from __future__ import annotations

import pytest

from llm_router import failopen


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Part 0 rule 4: assert the resolved path is inside the tmpdir.

    LLM_ROUTER_HOME did not isolate cost._get_db() and that gap destroyed real data
    once during this audit. The assertion is the point, not the setenv.
    """
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    failopen.reset_cache()
    resolved = failopen.store_path()
    assert str(resolved).startswith(str(tmp_path)), (
        f"fail-open store escaped the tmpdir: {resolved}"
    )
    yield
    failopen.reset_cache()


def test_records_are_counted_by_code():
    failopen.record("CHZ-FO-TEST-ALPHA", ValueError("x"))
    failopen.record("CHZ-FO-TEST-ALPHA", ValueError("y"))
    failopen.record("CHZ-FO-TEST-BETA")
    failopen.reset_cache()

    snap = failopen.snapshot()
    assert snap.by_code["CHZ-FO-TEST-ALPHA"] == 2
    assert snap.by_code["CHZ-FO-TEST-BETA"] == 1
    assert snap.total == 3


def test_recording_never_raises_even_when_the_store_is_broken(monkeypatch):
    """THE critical property. Every caller is already inside a handler that
    exists because propagating was unacceptable — an accounting call that can
    throw converts a fail-open into a crash, strictly worse than the silence."""
    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(failopen, "_append", _boom)
    failopen.record("CHZ-FO-TEST-BROKEN", RuntimeError("inner"))  # must not raise


def test_recording_never_raises_on_a_weird_exception():
    class _Awkward(Exception):
        def __str__(self):  # noqa: D105
            raise RuntimeError("even my repr explodes")

    failopen.record("CHZ-FO-TEST-AWKWARD", _Awkward())  # must not raise


def test_empty_store_is_zero_not_unknown():
    """No events recorded IS a real zero — distinct from an unreadable store."""
    snap = failopen.snapshot()
    assert snap.readable is True
    assert snap.total == 0
    assert snap.render_total() == "0"


def test_unreadable_store_is_unknown_not_zero():
    """A store we cannot parse is not a period without failures. Reporting it as
    one is the RED2-02 shape that this whole audit keeps finding."""
    failopen.store_path().write_text("{ not json at all\n")
    failopen.reset_cache()

    snap = failopen.snapshot()
    assert snap.readable is False
    assert snap.total is None
    assert snap.render_total() == "Unknown"


def test_partial_corruption_still_reports_what_it_can():
    """A partial count beats no count, as long as it is not silently understated
    to zero — which is why total is None only when NOTHING parsed."""
    failopen.record("CHZ-FO-TEST-GOOD")
    with failopen.store_path().open("a") as fh:
        fh.write("{ garbage\n")
    failopen.reset_cache()

    snap = failopen.snapshot()
    assert snap.readable is True
    assert snap.by_code["CHZ-FO-TEST-GOOD"] == 1


def test_detail_is_bounded():
    """Detail comes from exception text, which is unbounded input."""
    failopen.record("CHZ-FO-TEST-LONG", ValueError("x"), detail="y" * 5000)
    failopen.reset_cache()

    raw = failopen.store_path().read_text()
    assert len(raw) < 1000, f"one event wrote {len(raw)} chars"


def test_the_protected_modules_actually_call_it():
    """Guards the guard: the lint checks for a traced handler, but a call site
    that imports the module and never calls record() would satisfy neither the
    spirit nor the operator. Assert real call sites exist."""
    from pathlib import Path

    src = Path(failopen.__file__).resolve().parent
    total = 0
    for name in ("cost.py", "router.py", "summary.py",
                 "execution_ledger.py", "dashboard_data.py"):
        total += (src / name).read_text().count("failopen.record(")
    assert total >= 30, f"only {total} failopen.record call sites across the protected set"
