"""W7 text-job-bind: the ledger is text-free, bounded, and its report says what it counted and how.

The point of the ledger is to measure adoption (placeholder commands of all commit and PR
commands) and savings (characters the model did not write). A figure with no N or no definition
is not a figure, so the report prints both.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest

from llm_router.text_job_bind import ledger


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))


def row(kind, job="commit_message", outcome="accepted", reason="", **kw):
    r = {"kind": kind, "job": job, "outcome": outcome, "reason": reason, "ts": kw.pop("ts", 1_000.0)}
    r.update(kw)
    return r


def test_a_row_is_appended_privately_with_a_timestamp(tmp_path):
    ledger.record_row({"kind": "bound", "job": "commit_message"})
    p = ledger.ledger_path()
    assert p == tmp_path / "router_home" / "text_job_binds.jsonl"
    (r,) = [json.loads(ln) for ln in p.read_text().splitlines()]
    assert r["kind"] == "bound" and isinstance(r["ts"], float)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_a_write_failure_never_raises(tmp_path, monkeypatch):
    (tmp_path / "router_home").write_text("a file, not a directory")
    ledger.record_row({"kind": "bound"})  # must not raise


def test_the_ledger_is_capped_to_two_generations(monkeypatch):
    monkeypatch.setattr(ledger, "MAX_LEDGER_BYTES", 400)
    for i in range(40):
        ledger.record_row({"kind": "handwritten", "i": i})
    p = ledger.ledger_path()
    older = p.with_name(p.name + ".1")
    assert older.exists()  # rotated at least once (the live file may be absent right after a rotation)
    assert sum(f.stat().st_size for f in (p, older) if f.exists()) < 1500
    rows = ledger.read_rows()
    assert rows and rows[-1]["i"] == 39 and len(rows) < 40  # the oldest generation was dropped


def test_read_rows_windows_and_skips_junk():
    p = ledger.ledger_path()
    p.parent.mkdir(parents=True)
    p.write_text('{"ts": 5, "kind": "a"}\nnot json\n[1]\n{"ts": 50, "kind": "b"}\n{"kind": "no ts"}\n')
    assert [r["kind"] for r in ledger.read_rows()] == ["a", "b", "no ts"]
    assert [r["kind"] for r in ledger.read_rows(since=10)] == ["b"]


def test_summary_arithmetic_with_exact_numbers():
    rows = [
        row("bound", latency_ms=1000, out_chars=400), row("bound", latency_ms=3000, out_chars=800),
        row("bound", job="pr_description", latency_ms=5000, out_chars=1200),
        row("denied", outcome="rejected", reason="generic_subject"),
        row("denied", outcome="unsupported", reason="mutating_predecessor"),
        row("denied", outcome="unsupported", reason="mutating_predecessor"),
        row("handwritten", cmd_chars=240), row("handwritten", cmd_chars=160), row("handwritten", job="pr_description", cmd_chars=400),
        row("handwritten", cmd_chars=0, ts=2_000.0),
    ]
    s = ledger.summarize(rows)
    assert s["n_commit_pr_commands"] == 10 and s["n_placeholder"] == 6 and s["n_handwritten"] == 4
    assert s["n_bound"] == 3 and s["n_denied"] == 3
    assert s["adoption"] == pytest.approx(0.6) and s["bound_rate"] == pytest.approx(0.5)
    assert s["bound_latency_ms_p50"] == 3000 and s["bound_latency_ms_p95"] == 5000
    assert s["bound_out_chars"] == 2400 and s["est_output_tokens_avoided"] == 600
    assert s["handwritten_cmd_chars"] == 800 and s["est_output_tokens_still_handwritten"] == 200
    assert s["denied_by_reason"] == {"unsupported:mutating_predecessor": 2, "rejected:generic_subject": 1}
    assert s["by_job"]["pr_description"] == {"bound": 1, "denied": 0, "handwritten": 1}
    assert (s["since"], s["until"]) == (1000.0, 2000.0)


def test_an_empty_ledger_reports_n_a_not_zero_percent():
    s = ledger.summarize([])
    assert s["adoption"] is None and s["bound_rate"] is None and s["bound_latency_ms_p50"] is None
    text = ledger.render(s)
    assert "n/a" in text and "0%" not in text


def test_the_rendered_report_carries_n_and_labels_estimates():
    text = ledger.render(ledger.summarize([row("bound", latency_ms=10, out_chars=400), row("handwritten", cmd_chars=100)]))
    assert "1 of 2" in text and "n=1" in text
    assert text.count("estimate") >= 2 and "/ 4" in text


def test_the_report_command_prints_json_over_a_window(tmp_path):
    import time

    ledger.record_row({"kind": "bound", "job": "commit_message", "outcome": "accepted", "out_chars": 80, "latency_ms": 5})
    ledger.record_row({"kind": "bound", "job": "commit_message", "ts": time.time() - 20 * 86400, "out_chars": 9999})
    env = {**os.environ, "LLM_ROUTER_HOME": str(tmp_path / "router_home")}
    r = subprocess.run([sys.executable, "-m", "llm_router.text_job_bind", "report", "--json", "--days", "7"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    s = json.loads(r.stdout)
    assert s["n_bound"] == 1 and s["bound_out_chars"] == 80 and s["window_days"] == 7.0
    r = subprocess.run([sys.executable, "-m", "llm_router.text_job_bind", "report", "--days", "30"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert "2 placeholder uses" in r.stdout.replace("of 2 ", "2 ") or "bound (local text in the command): 2" in r.stdout
