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


def test_without_handwritten_rows_adoption_is_n_a_not_a_hundred_percent():
    """A hook registered with an `if` filter never sees a command without the placeholder, so there are no
    hand-written rows. 3 of 3 would be an artefact of the filter, not an adoption rate."""
    rows = [row("bound", latency_ms=10, out_chars=400), row("denied", outcome="rejected", reason="generic_subject"),
            row("bound", job="pr_description", latency_ms=20, out_chars=800)]
    s = ledger.summarize(rows)
    assert s["adoption"] is None and s["denominator_known"] is False and s["n_placeholder"] == 3
    text = ledger.render(s)
    assert "adoption: n/a" in text and "`if` filter" in text and "--transcripts" in text and "100%" not in text
    assert s["adoption_unavailable_reason"] and s["adoption_source"] is None
    assert "still spent on hand-written" not in text
    assert ledger.summarize(rows + [row("handwritten", cmd_chars=100)])["adoption"] == pytest.approx(0.75)


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
    assert "2 placeholder commands" in r.stdout and "units:" in r.stdout and "window:" in r.stdout


# ---- F14: window and units, latency of every outcome, shape versus model, adoption from transcripts -----------

def test_the_report_prints_its_window_and_its_units():
    rows = [row("bound", latency_ms=10, out_chars=4, ts=1_700_000_000.0), row("bound", latency_ms=20, out_chars=4, ts=1_700_086_400.0)]
    s = ledger.summarize(rows)
    s["window_days"] = 7.0
    text = ledger.render(s)
    assert "window: 2023-11-14T22:13:20Z to 2023-11-15T22:13:20Z (the last 7 days requested)" in text
    assert "units: latency in milliseconds" in text and "UTC" in text


def test_every_outcome_gets_a_latency_summary_so_the_budget_and_cooldown_can_be_tuned():
    rows = ([row("bound", latency_ms=ms) for ms in (4000, 6000, 8000)]
            + [row("denied", outcome="timeout", reason="budget", latency_ms=100_000)] * 2
            + [row("denied", outcome="fallback", reason="no_lane", latency_ms=1500)]
            + [row("denied", outcome="busy", reason="lock_held", latency_ms=10_050)]
            + [row("denied", outcome="cooldown", reason="after_timeout", latency_ms=300)]
            + [row("denied", outcome="rejected", reason="generic_subject", latency_ms=9000)])
    s = ledger.summarize(rows)
    lat = s["latency_by_outcome"]
    assert set(lat) == {"bound:accepted", "denied:timeout", "denied:fallback", "denied:busy", "denied:cooldown", "denied:rejected"}
    assert lat["bound:accepted"] == {"n": 3, "n_timed": 3, "p50": 6000, "p95": 8000, "max": 8000}
    assert lat["denied:timeout"]["n"] == 2 and lat["denied:timeout"]["max"] == 100_000
    text = ledger.render(s)
    for key in lat:
        assert f"{key}: n=" in text
    assert "denied:timeout: n=2 p50 100,000 p95 100,000 max 100,000" in text
    # a row without a latency is counted but not timed, and the report says so
    assert "(timed 1)" in ledger.render(ledger.summarize([row("denied", outcome="error", latency_ms=5), row("denied", outcome="error")]))


def test_command_shape_refusals_are_separate_from_model_failures():
    rows = [row("denied", outcome="unsupported", reason="mutating_predecessor")] * 3 + [
        row("denied", outcome="refused", reason="nothing_staged"),
        row("denied", outcome="busy", reason="lock_held"), row("denied", outcome="cooldown", reason="after_timeout"),
        row("denied", outcome="rejected", reason="generic_subject"), row("denied", outcome="timeout", reason="budget"),
        row("bound", latency_ms=1), row("bound", latency_ms=1)]
    s = ledger.summarize(rows)
    assert (s["n_denied_shape"], s["n_denied_capacity"], s["n_denied_model"]) == (4, 2, 2)
    # the model ran for 2 bound + 2 failed jobs; the shape and capacity refusals say nothing about it
    assert s["n_reached_model"] == 4 and s["bound_rate_when_model_ran"] == pytest.approx(0.5)
    assert s["bound_rate"] == pytest.approx(2 / 10)
    text = ledger.render(s)
    assert "2 by the model or its lane" in text and "2 for capacity" in text and "4 for the command's shape" in text


def _transcript(path, entries):
    with open(path, "w", encoding="utf-8") as fh:
        for i, (cmd, ts) in enumerate(entries):
            fh.write(json.dumps({"type": "assistant", "timestamp": ts, "message": {"content": [
                {"type": "text", "text": "git commit -m 'in prose, not a tool call'"},
                {"type": "tool_use", "id": f"toolu_{path.name}_{i}", "name": "Bash", "input": {"command": cmd}}]}}) + "\n")


def test_adoption_comes_from_transcripts_or_is_n_a_with_its_reason(tmp_path):
    root = tmp_path / "projects"
    (root / "s1" / "subagents").mkdir(parents=True)
    T = '"${LLM_TEXT:?}"'
    _transcript(root / "s1.jsonl", [
        (f"git commit -m {T}", "2026-11-03T10:00:00Z"), ("git commit -m 'by hand'", "2026-11-03T10:01:00Z"),
        ("ls -la", "2026-11-03T10:02:00Z"), (f"git commit -m {T}", "2026-10-01T10:00:00Z")])  # last: outside the window
    _transcript(root / "s1" / "subagents" / "agent-a.jsonl", [
        (f"gh pr create --title t --body {T}", "2026-11-03T11:00:00Z"), ("gh pr create --title t --body 'x'", "2026-11-03T11:05:00Z"),
        ("git -C /r commit -am 'hand'", "2026-11-03T11:06:00Z")])
    since = 1_793_000_000.0  # 2026-10-26
    t = ledger.scan_transcripts([root], since)
    assert (t["n_commit_pr"], t["n_placeholder"], t["files"]) == (5, 2, 2)
    s = ledger.summarize([row("bound", latency_ms=1, ts=1_793_600_000.0)], transcripts=t)
    assert s["adoption"] == pytest.approx(0.4) and s["adoption_source"] == "transcripts" and s["denominator_known"]
    text = ledger.render(s)
    assert "adoption: 2 of 5 git commit / gh pr create commands in 2 transcript files used the placeholder (40%)" in text
    # nothing to count: n/a and why, never a percentage over nothing
    empty = ledger.summarize([row("bound", latency_ms=1)], transcripts=ledger.scan_transcripts([tmp_path / "nowhere"], since))
    assert empty["adoption"] is None and "no git commit or gh pr create" in empty["adoption_unavailable_reason"]
    assert "adoption: n/a - the transcripts in the window hold no" in ledger.render(empty)


def test_the_report_command_scans_transcripts_on_request(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    import time as _t

    now = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
    _transcript(root / "s.jsonl", [('git commit -m "${LLM_TEXT:?}"', now), ("git commit -m hand", now)])
    ledger.record_row({"kind": "bound", "job": "commit_message", "outcome": "accepted", "latency_ms": 5, "out_chars": 40})
    env = {**os.environ, "LLM_ROUTER_HOME": str(tmp_path / "router_home")}
    r = subprocess.run([sys.executable, "-m", "llm_router.text_job_bind", "report", "--json", "--transcripts", str(root)],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    s = json.loads(r.stdout)
    assert s["adoption"] == pytest.approx(0.5) and s["transcripts"]["n_commit_pr"] == 2
