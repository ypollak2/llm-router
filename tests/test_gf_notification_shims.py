"""G-F Group E — the notification shims, under the owner's option (b).

`cost.fire_budget_alert` (50 mutants, ALL no-coverage — nothing tested it at all) and
`router._native_notify` (37 survivors). Ranked #2 and #4 by survivor count.

`gf_phase2_classes.md` §5 recorded, BEFORE any test was written, that killing all of them
would mean asserting the exact argv and kwargs of a subprocess call — a change-detector
test that fails on any refactor and detects no defect a user could observe. The owner chose
option (b): assert PLATFORM DISPATCH and BINARY NAME, and nothing else.

IN SCOPE, because each is a contract a user can feel
    platform dispatch   `sys.platform == "darwin"` → `!=` means every macOS user silently
                        loses budget alerts. Invisible, because the function is
                        best-effort by design and swallows everything.
    binary name         "osascript" → "XXosascriptXX" means the alert never fires. The
                        function's own comment says the point is that a user should not
                        "learn about an overrun from the bill".
    the fail-open code  a swallowed exception with no recorded code is an alert that
                        vanished without trace.

DELIBERATELY LEFT ALIVE — this is not an unfinished file
    timeout=3 → 4, capture_output=True → False, "--urgency=normal", "--app-name=",
    `sound name "Glass"`, the notification body text, and json.dumps separators (which are
    genuinely equivalent — different whitespace, identical parsed data).
Those remain SURVIVORS under doc 20 §4. That is the known, deliberate cost of conservative
scoring, not a gap for a later reader to close by widening this file.

TWO TRAPS, FOUND BY READING THE SOURCE RATHER THAN BY A FAILING RUN
1. `fire_budget_alert` does `import subprocess` and `import sys` INSIDE the function body.
   There is no `llm_router.cost.subprocess` attribute to patch — patching it would silently
   CREATE one, the test would pass, and it would assert nothing. Patch the real module.
2. `_native_notify` runs its body in `threading.Thread(daemon=True)`. Asserting after the
   call races the thread, and `join()` on a daemon thread is still a race under load.
   The thread is replaced with a synchronous stand-in so the assertion is deterministic.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from llm_router import router


@pytest.fixture()
def calls(monkeypatch):
    """Capture subprocess.run argv without executing anything."""
    seen: list[tuple] = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.append((tuple(argv), kw)))
    return seen


@pytest.fixture()
def isolated_failopen(monkeypatch, tmp_path):
    """A proven-isolated fail-open store, reset either side."""
    from llm_router import failopen
    from llm_router.paths import is_isolated

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    assert is_isolated(), "LLM_ROUTER_HOME did not take effect — refusing to write"
    failopen.reset_cache()
    failopen.clear()
    yield failopen
    failopen.reset_cache()


# ─────────────────────────────────────────────────────────────────────────────
# cost.fire_budget_alert — 50 mutants, previously ZERO coverage
# ─────────────────────────────────────────────────────────────────────────────

class TestBudgetAlertPlatformDispatch:
    """Three mutually exclusive branches keyed on `sys.platform`."""

    def test_macos_invokes_osascript(self, calls, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        from llm_router.cost import fire_budget_alert
        fire_budget_alert("Budget", "80% spent")
        assert [c[0][0] for c in calls] == ["osascript"], (
            "exact equality, not `in` — a substring assertion is satisfied by the "
            "surviving mutant 'XXosascriptXX'"
        )

    def test_linux_invokes_notify_send(self, calls, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        from llm_router.cost import fire_budget_alert
        fire_budget_alert("Budget", "80% spent")
        assert [c[0][0] for c in calls] == ["notify-send"]

    def test_the_linux_branch_matches_a_PREFIX_not_equality(self, calls, monkeypatch):
        """`sys.platform.startswith("linux")`. Real values include 'linux2'; a mutant
        switching to `==` drops those hosts silently."""
        monkeypatch.setattr(sys, "platform", "linux2")
        from llm_router.cost import fire_budget_alert
        fire_budget_alert("Budget", "80% spent")
        assert [c[0][0] for c in calls] == ["notify-send"]

    def test_darwin_is_matched_EXACTLY_not_as_a_prefix(self, calls, monkeypatch):
        """The macOS branch is `==`, not `startswith`. Pins the asymmetry between the two
        branches so a mutant cannot quietly make them the same shape."""
        monkeypatch.setattr(sys, "platform", "darwinX")
        from llm_router.cost import fire_budget_alert
        fire_budget_alert("Budget", "80% spent")
        assert calls == []

    def test_an_unknown_platform_invokes_nothing(self, calls, monkeypatch):
        """Neither darwin, nor linux, nor win32. A mutant negating any comparison shows up
        here as a notifier fired on a host that does not have it."""
        monkeypatch.setattr(sys, "platform", "freebsd13")
        from llm_router.cost import fire_budget_alert
        fire_budget_alert("Budget", "80% spent")
        assert calls == []

    def test_exactly_ONE_notifier_runs_per_call(self, calls, monkeypatch):
        """The branches are `elif`. A mutant turning one into `if` fires two notifiers."""
        monkeypatch.setattr(sys, "platform", "darwin")
        from llm_router.cost import fire_budget_alert
        fire_budget_alert("Budget", "80% spent")
        assert len(calls) == 1


class TestBudgetAlertCarriesTheMessage:
    """A notification that fires but says nothing is as useless as one that never fires."""

    def test_the_macos_script_carries_both_title_and_body(self, calls, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        from llm_router.cost import fire_budget_alert
        fire_budget_alert("Budget 80%", "you have spent $40 of $50")
        script = calls[0][0][2]
        assert "Budget 80%" in script and "you have spent $40 of $50" in script

    def test_the_linux_argv_carries_title_and_body_as_SEPARATE_arguments(
        self, calls, monkeypatch
    ):
        """Distinct values on purpose — equal ones would hide a swap of the two."""
        monkeypatch.setattr(sys, "platform", "linux")
        from llm_router.cost import fire_budget_alert
        fire_budget_alert("TITLE-A", "BODY-B")
        argv = calls[0][0]
        assert argv[-2:] == ("TITLE-A", "BODY-B")

    def test_the_macos_invocation_passes_the_script_via_dash_e(self, calls, monkeypatch):
        """`osascript -e <script>`. Without `-e`, osascript reads the argument as a
        FILENAME and the alert silently never appears."""
        monkeypatch.setattr(sys, "platform", "darwin")
        from llm_router.cost import fire_budget_alert
        fire_budget_alert("t", "m")
        assert calls[0][0][1] == "-e"


class TestBudgetAlertWindowsFallback:
    """win32 without win10toast must LOG, not vanish."""

    def test_missing_win10toast_logs_a_warning_naming_the_package(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "win10toast", None)  # → ImportError on import
        from llm_router.cost import fire_budget_alert

        with caplog.at_level("WARNING", logger="llm_router"):
            fire_budget_alert("Budget", "80% spent")

        assert len(caplog.records) == 1
        msg = caplog.records[0].getMessage()
        assert "Budget" in msg and "80% spent" in msg
        assert "win10toast" in msg, "the log must say how to get real notifications"

    # A `None` entry in sys.modules raises ModuleNotFoundError, a SUBCLASS of ImportError,
    # so the source's `except ImportError` catches it. I got this relationship backwards
    # twice during the C1 fail-open work; recorded here so the third time is caught by
    # reading rather than by a failing run.


class TestBudgetAlertRecordsItsFailure:
    def test_a_notifier_crash_is_recorded_and_never_raises(
        self, monkeypatch, isolated_failopen
    ):
        import json as _json

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: (_ for _ in ()).throw(OSError("no osascript")),
        )
        from llm_router.cost import fire_budget_alert

        fire_budget_alert("Budget", "80% spent")  # must not raise

        isolated_failopen.reset_cache()
        assert dict(isolated_failopen.snapshot().by_code) == {
            "CHZ-FO-COST-BUDGET-ALERT": 1
        }
        recorded = [
            _json.loads(ln)
            for ln in isolated_failopen.store_path().read_text().splitlines()
            if ln.strip()
        ]
        assert [r.get("e") for r in recorded] == ["OSError"]


# ─────────────────────────────────────────────────────────────────────────────
# router._native_notify — 37 survivors
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def sync_thread(monkeypatch):
    """Run the notifier body synchronously; see trap 2 in the module docstring."""

    class _Immediate:
        last = None

        def __init__(self, target=None, daemon=None, **kw):
            self._target = target
            self.daemon = daemon
            _Immediate.last = self

        def start(self):
            self._target()

    monkeypatch.setattr(router.threading, "Thread", _Immediate)
    return _Immediate


class TestNativeNotifyPlatformDispatch:
    """`platform.system()` here — returns 'Darwin'/'Linux', NOT sys.platform's 'darwin'."""

    def test_darwin_invokes_osascript(self, calls, sync_thread, monkeypatch):
        monkeypatch.setattr(router.platform, "system", lambda: "Darwin")
        router._native_notify("routing to ollama")
        assert [c[0][0] for c in calls] == ["osascript"]

    def test_linux_invokes_notify_send(self, calls, sync_thread, monkeypatch):
        monkeypatch.setattr(router.platform, "system", lambda: "Linux")
        router._native_notify("routing to ollama")
        assert [c[0][0] for c in calls] == ["notify-send"]

    def test_the_comparison_is_CASE_SENSITIVE(self, calls, sync_thread, monkeypatch):
        """platform.system() returns 'Darwin'. A mutant spelling the literal 'darwin' or
        'DARWIN' stops matching, and the dispatch silently never fires on the only
        platform this repo is developed on."""
        monkeypatch.setattr(router.platform, "system", lambda: "darwin")
        router._native_notify("x")
        assert calls == []

    def test_an_unknown_platform_invokes_nothing(self, calls, sync_thread, monkeypatch):
        monkeypatch.setattr(router.platform, "system", lambda: "Windows")
        router._native_notify("x")
        assert calls == []

    def test_exactly_ONE_notifier_runs_per_call(self, calls, sync_thread, monkeypatch):
        monkeypatch.setattr(router.platform, "system", lambda: "Darwin")
        router._native_notify("x")
        assert len(calls) == 1


class TestNativeNotifyCarriesTheMessage:
    def test_the_macos_script_carries_message_and_title(
        self, calls, sync_thread, monkeypatch
    ):
        monkeypatch.setattr(router.platform, "system", lambda: "Darwin")
        router._native_notify("MSG-A", title="TITLE-B")
        script = calls[0][0][2]
        assert "MSG-A" in script and "TITLE-B" in script

    def test_the_linux_argv_ends_with_the_message(self, calls, sync_thread, monkeypatch):
        monkeypatch.setattr(router.platform, "system", lambda: "Linux")
        router._native_notify("MSG-A", title="TITLE-B")
        argv = calls[0][0]
        assert argv[-1] == "MSG-A"
        assert any("TITLE-B" in a for a in argv), "the title must reach notify-send too"

    def test_the_macos_invocation_passes_the_script_via_dash_e(
        self, calls, sync_thread, monkeypatch
    ):
        monkeypatch.setattr(router.platform, "system", lambda: "Darwin")
        router._native_notify("x")
        assert calls[0][0][1] == "-e"


class TestNativeNotifyIsNonBlocking:
    def test_the_worker_thread_is_a_DAEMON(self, calls, sync_thread, monkeypatch):
        """`daemon=True` is behaviour, not cosmetics: a non-daemon thread keeps the
        interpreter alive at exit, so a hung `osascript` would stall every CLI
        invocation. That is why this is in scope while `timeout=` is not."""
        monkeypatch.setattr(router.platform, "system", lambda: "Darwin")
        router._native_notify("x")
        assert sync_thread.last.daemon is True


class TestNativeNotifyRecordsItsFailure:
    def test_a_notifier_crash_is_recorded_under_its_own_code(
        self, sync_thread, monkeypatch, isolated_failopen
    ):
        monkeypatch.setattr(router.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
        )
        router._native_notify("x")  # must not raise

        isolated_failopen.reset_cache()
        assert dict(isolated_failopen.snapshot().by_code) == {
            "CHZ-FO-ROUTER-DESKTOP-NOTIFY": 1
        }
