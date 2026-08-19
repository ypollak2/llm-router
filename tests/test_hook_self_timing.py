"""The hook records its own evidence when it is slow, and says nothing when it is not.

Audit #58. A ~36-second invocation was measured once — Claude Code's budget is 30s, so the
routing directive was discarded — and it never reproduced. Nine hypotheses were refuted by
measurement (gateway lock, gateway existence, WAL size, checkpoint starvation, pytest
load, Ollama model, Ollama generally, import cost, external classifier), and a 2x2
controlled reproduction across gateway-up/down x idle/loaded found nothing.

By the time anyone investigates, the conditions have passed. So the hook now captures the
breakdown itself, under `CHZ-FO-HOOK-SLOW`, and the next occurrence answers "which phase"
without anyone needing to be watching.

TWO PROPERTIES, AND THE SECOND IS THE ONE THAT KEEPS IT USEFUL:
  1. a slow invocation is recorded WITH the phase timings;
  2. a healthy invocation records NOTHING — instrumentation that fires on the fast path
     becomes noise, and noise trains people to ignore the signal. The measured healthy
     path is 0.2s against a 5s threshold.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


def _hook_module():
    """Load the hook as a module. It is a script with a dashed filename, so importlib
    has to be told explicitly rather than going through the normal import system."""
    spec = importlib.util.spec_from_file_location("auto_route_hook", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSlowInvocationsRecordTheirBreakdown:
    def test_a_breach_is_recorded_with_per_phase_timings(self, monkeypatch, tmp_path):
        from llm_router import failopen
        from llm_router.paths import is_isolated

        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        assert is_isolated(), "LLM_ROUTER_HOME did not take effect — refusing to write"
        failopen.reset_cache()
        failopen.clear()

        h = _hook_module()
        monkeypatch.setattr(h, "_SLOW_HOOK_SECONDS", 1.0)
        # Synthetic marks: 4s total, with the ledger write holding 3.5s of it — the shape
        # the original profile showed (31.19s of a 36.9s run inside record_event).
        monkeypatch.setattr(h, "_MARKS", [
            ("start", 100.0),
            ("classify_and_build_directive", 100.4),
            ("emit_stdout", 100.5),
            ("ledger_write", 104.0),
        ])
        h._report_if_slow()

        failopen.reset_cache()
        assert dict(failopen.snapshot().by_code) == {"CHZ-FO-HOOK-SLOW": 1}

        import json as _json
        rec = [
            _json.loads(ln)
            for ln in failopen.store_path().read_text().splitlines()
            if ln.strip()
        ]
        detail = " ".join(str(r) for r in rec)
        assert "ledger_write=3.50s" in detail, (
            "the breakdown must name the phase that consumed the time — a bare 'it was "
            f"slow' would leave the next investigator exactly where this one started: {detail}"
        )
        assert "total=4.00s" in detail, "the total belongs in the record too"
        assert detail.index("ledger_write") < detail.index("classify_and_build"), (
            "phases must be ordered slowest-first — detail is truncated at 200 chars, so "
            "the phase that consumed the time must not be the one that gets cut"
        )
        failopen.reset_cache()


class TestHealthyInvocationsAreSilent:
    def test_a_fast_run_records_nothing(self, monkeypatch, tmp_path):
        """Instrumentation that fires on the fast path is noise, and noise gets ignored."""
        from llm_router import failopen
        from llm_router.paths import is_isolated

        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        assert is_isolated()
        failopen.reset_cache()
        failopen.clear()

        h = _hook_module()
        monkeypatch.setattr(h, "_SLOW_HOOK_SECONDS", 5.0)
        monkeypatch.setattr(h, "_MARKS", [
            ("start", 100.0),
            ("classify_and_build_directive", 100.15),
            ("emit_stdout", 100.2),
            ("ledger_write", 100.22),
        ])
        h._report_if_slow()

        failopen.reset_cache()
        assert dict(failopen.snapshot().by_code) == {}, (
            "a 0.22s invocation must record nothing at a 5s threshold"
        )
        failopen.reset_cache()

    def test_too_few_marks_is_not_an_error(self, monkeypatch, tmp_path):
        """An invocation that exits early (malformed stdin, empty prompt) never reaches
        the later marks. That must be silent, not a crash and not a false report."""
        from llm_router import failopen
        from llm_router.paths import is_isolated

        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        assert is_isolated()
        failopen.reset_cache()
        failopen.clear()

        h = _hook_module()
        monkeypatch.setattr(h, "_MARKS", [("start", 100.0)])
        h._report_if_slow()          # must not raise

        failopen.reset_cache()
        assert dict(failopen.snapshot().by_code) == {}
        failopen.reset_cache()


class TestTheDiagnosticCannotBreakTheTurn:
    def test_a_broken_failopen_store_does_not_propagate(self, monkeypatch, tmp_path):
        """A UserPromptSubmit hook must fail open. A diagnostic that raises would turn a
        slow turn into a broken one — strictly worse than the problem it reports."""
        h = _hook_module()
        monkeypatch.setattr(h, "_SLOW_HOOK_SECONDS", 0.1)
        monkeypatch.setattr(h, "_MARKS", [("start", 100.0), ("emit_stdout", 105.0)])

        import llm_router.failopen as fo
        monkeypatch.setattr(
            fo, "record",
            lambda *a, **k: (_ for _ in ()).throw(OSError("store unwritable")),
        )
        h._report_if_slow()   # must not raise

    def test_mark_survives_a_hostile_clock(self, monkeypatch):
        """_mark is called on every invocation; it must never be the thing that fails."""
        h = _hook_module()
        monkeypatch.setattr(
            h.time, "monotonic",
            lambda: (_ for _ in ()).throw(RuntimeError("no clock")),
        )
        h._mark("x")   # must not raise
