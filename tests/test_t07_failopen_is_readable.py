"""The fail-open counter must be readable, and must survive its own store — T-07.

58 `failopen.record()` call sites in `src/`. **Zero** `snapshot()` readers
outside tests. Every swallowed exception in this codebase was counted into a
file nothing ever opened.

Worse, probed during the audit: with the store unwritable — the condition most
likely to CAUSE a burst of fail-opens — `record()` swallowed its own write and
the only fallback was `structlog.debug` while the effective level was WARNING.
Recorded nowhere, printed nowhere. Three of the 58 sites were added on
2026-09-21 specifically believing they made losses visible.
"""

from __future__ import annotations

import io
import contextlib

import pytest

from llm_router import failopen


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    failopen.clear()
    yield
    failopen.clear()


# ── the second channel ───────────────────────────────────────────────────────

def test_a_loss_survives_an_unwritable_store(tmp_path, monkeypatch):
    """The whole finding. The store is the thing that breaks; the count must not.

    Forced through `_append` rather than by chmod, so the test does not depend
    on running as a user who cannot defeat file permissions (root can).
    """
    def _explode(_payload):
        raise OSError("read-only file system")

    monkeypatch.setattr(failopen, "_append", _explode)
    failopen.record("CHZ-FO-DISK-FULL", OSError("x"))
    failopen.record("CHZ-FO-DISK-FULL", OSError("x"))
    failopen.reset_cache()

    snap = failopen.snapshot()
    assert snap.total == 0, "nothing should have persisted"
    assert snap.unpersisted_total == 2, (
        "the losses vanished — this is the exact defect T-07 describes"
    )
    assert snap.unpersisted_by_code["CHZ-FO-DISK-FULL"] == 2


def test_the_unrecorded_count_is_kept_separate_from_the_recorded_one():
    """Merging them would make a restart look like the failures stopped.

    The in-process counter is not durable; presenting it as if it were would be
    a different kind of dishonesty from the one being fixed.
    """
    snap = failopen.FailOpenCounts(
        by_code={"A": 3}, unpersisted_by_code={"B": 2})
    assert snap.total == 3
    assert snap.unpersisted_total == 2
    # render_total() answers only "what did the store record". The second
    # channel has its own line in render_report(), because "3 recorded" and
    # "2 we could not record" are different claims.
    assert snap.render_total() == "3"
    report = "\n".join(snap.render_report())
    assert "could NOT be recorded: 2" in report
    assert "B *" in report


def test_a_healthy_install_says_zero_not_unknown():
    """Anti-vacuity: the report must distinguish 'nothing failed' from 'cannot tell'."""
    failopen.reset_cache()
    snap = failopen.snapshot()
    assert snap.total == 0
    assert snap.unpersisted_total == 0
    assert "unrecorded" not in snap.render_total()
    assert any("recorded: 0" in ln for ln in snap.render_report())


def test_an_unreadable_store_is_not_reported_as_zero():
    """`None`, not 0 — a store we cannot read is not a period with no failures."""
    snap = failopen.FailOpenCounts(readable=False)
    assert snap.total is None
    assert "UNREADABLE" in "\n".join(snap.render_report())


# ── the escalated log level ──────────────────────────────────────────────────

def test_an_unrecordable_loss_logs_above_debug(monkeypatch):
    """DEBUG is below the effective level in every shipped configuration.

    A fallback nobody can see is not a fallback.
    """
    # R13/A-10. This grepped `record()`'s source, so `.warning(` appearing in
    # a comment satisfied it while the call was gone — and this module's
    # comments discuss WARNING at length. Asserted on the AST now: calls and
    # string values, neither of which a comment can provide.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _ast_assert import calls_in, string_constants

    calls = calls_in(failopen.record)
    # `calls_in` yields whole call expressions, so match on the method being
    # invoked. Still A-10-proof: this is the UNPARSED AST of a real call, and
    # a comment cannot put text there.
    assert any(".warning(" in c for c in calls), (
        f"record() no longer escalates an unrecordable loss above DEBUG: {calls}"
    )
    assert "fail_open_unrecorded" in string_constants(failopen.record), (
        "the unrecordable-loss event name is not a value record() uses"
    )
    # And the ordinary path must be above debug too.
    assert not [c for c in calls if ".debug(" in c], (
        f"the routine fail-open log is still at DEBUG: {calls}"
    )


# ── the reader that did not exist ────────────────────────────────────────────

def test_doctor_reports_the_counters(monkeypatch):
    """T-07's headline: 58 writers, 0 readers. This is the reader."""
    failopen.record("CHZ-FO-PROBE-ALPHA", RuntimeError("x"))
    failopen.record("CHZ-FO-PROBE-ALPHA", RuntimeError("x"))
    failopen.record("CHZ-FO-PROBE-BETA", OSError("y"))
    failopen.reset_cache()

    from llm_router.commands.doctor import _run_doctor

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _run_doctor()
    out = buf.getvalue()

    # R12 moved this from a hand-written doctor block to the counter registry,
    # which doctor renders for every registered counter. The contract T-07 cares
    # about is unchanged and is asserted here, not the section's heading: the
    # total is right, and the SITE is named — a count with no site tells an
    # operator that something degraded and nothing about what.
    assert "fail_open_events: 3 event(s)" in out
    assert "CHZ-FO-PROBE-ALPHA" in out, "doctor does not name the site that degraded"
    assert "fail-open events recorded: 3" in out


def test_doctor_treats_an_unrecordable_loss_as_an_issue(monkeypatch):
    """Not just printed — it must fail the health check.

    A line in a long report that nothing acts on is how 58 call sites became
    invisible in the first place.
    """
    def _explode(_payload):
        raise OSError("read-only file system")

    monkeypatch.setattr(failopen, "_append", _explode)
    failopen.record("CHZ-FO-DISK-FULL", OSError("x"))
    failopen.reset_cache()

    from llm_router.commands.doctor import _run_doctor

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code, issues = _run_doctor()
    assert any("could NOT be recorded" in i for i in issues), (
        f"doctor printed it but did not raise it as an issue: {issues}"
    )
    assert exit_code == 1


def test_status_stays_quiet_on_a_healthy_install(importing_a_submodule):
    """A panel that is always present and always empty is furniture."""
    from llm_router.ui.status_premium import PremiumStatusCommand

    failopen.reset_cache()
    rendered = PremiumStatusCommand().render_degraded_operations()
    assert str(rendered) == "", "status shows a degraded panel with nothing degraded"


def test_status_shows_the_panel_once_something_degrades(importing_a_submodule):
    from llm_router.ui.status_premium import PremiumStatusCommand

    failopen.record("CHZ-FO-PROBE-GAMMA", RuntimeError("x"))
    failopen.reset_cache()
    rendered = str(PremiumStatusCommand().render_degraded_operations())
    assert "Degraded operations" in rendered
    assert "CHZ-FO-PROBE-GAMMA" in rendered


# ── the writers are still there ──────────────────────────────────────────────

def test_the_call_sites_still_outnumber_nothing():
    """Anti-vacuity for the whole file.

    If `record()` stopped being called, every test above would pass against a
    system with no instrumentation at all.
    """
    import pathlib
    import subprocess

    root = pathlib.Path(failopen.__file__).resolve().parent
    out = subprocess.run(
        ["grep", "-rc", "failopen.record(", str(root)],
        capture_output=True, text=True,
    ).stdout
    total = sum(int(ln.rsplit(":", 1)[1]) for ln in out.strip().split("\n") if ":" in ln)
    assert total >= 40, f"only {total} failopen.record() call sites found in src/"
