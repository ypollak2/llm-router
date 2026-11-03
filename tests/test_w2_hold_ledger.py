"""BIND W2: the text-free hold ledger and its reader ("BLOCKED per 100 prompts").

The coordinator's audit computes the success bar from these rows, so the field names and
the arithmetic are pinned here, and so is the promise that no prompt text ever lands in
the file.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_router import counter_registry, hold_ledger

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    return tmp_path


def _rows(tmp_path) -> list[dict]:
    p = tmp_path / hold_ledger.STORE_FILENAME
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_a_row_carries_the_documented_fields_and_nothing_else(tmp_path):
    hold_ledger.record("probe", "sess-1", tool="llm", kind="door_unrelated", call_words=2,
                       prompt="SECRET PROMPT TEXT", command="rm -rf /", extra={"a": 1}, items=[1])
    (row,) = _rows(tmp_path)
    assert set(row) == {"ts", "event", "session_id", "tool", "kind", "call_words"}
    assert row["event"] == "probe" and row["call_words"] == 2
    assert "SECRET" not in json.dumps(row) and "rm -rf" not in json.dumps(row)


def test_every_field_a_writer_may_pass_is_listed_in_the_module_docstring():
    doc = hold_ledger.__doc__
    for field in hold_ledger._FIELDS - {"session_id"}:
        assert f"``{field}``" in doc, f"{field} is writable but undocumented"
    for event in hold_ledger.EVENTS:
        assert f"``{event}``" in doc


def test_long_values_are_truncated_and_numbers_stay_numbers(tmp_path):
    hold_ledger.record("hold", "s", task="x" * 500, words=7, complexity=None)
    (row,) = _rows(tmp_path)
    assert len(row["task"]) == 64 and row["words"] == 7 and "complexity" not in row


def test_an_unknown_event_writes_nothing_and_is_counted(tmp_path):
    from llm_router import failopen

    failopen.clear()
    hold_ledger.record("not_an_event", "s")
    assert _rows(tmp_path) == []
    assert failopen.snapshot().total >= 1


def test_a_failed_write_is_counted_not_raised(tmp_path, monkeypatch):
    from llm_router import failopen

    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(blocker))   # the state dir cannot be created
    failopen.clear()
    hold_ledger.record("prompt", "s", words=3)             # must not raise
    snap = failopen.snapshot()
    # the fail-open store lives in the same unwritable directory, so the count is held in
    # memory ("unpersisted") and doctor reports it as a store it could not write
    assert snap.total + snap.unpersisted_total >= 1
    assert snap.unpersisted_by_code.get("CHZ-FO-HOLD-LEDGER-WRITE") == 1


def test_summarize_computes_the_rates_the_audit_asks_for(tmp_path):
    for _ in range(200):
        hold_ledger.record("prompt", "a", words=5)
    for _ in range(40):
        hold_ledger.record("hold", "a", task="code", complexity="moderate", mode="suggest")
    for _ in range(3):
        hold_ledger.record("blocked", "a", tool="Bash", task="code", violation=1)
    hold_ledger.record("probe", "a", tool="llm", kind="door_unrelated", call_words=2)
    hold_ledger.record("probe", "a", tool="llm", kind="door_unrelated", call_words=30)
    hold_ledger.record("probe", "a", tool="llm_router_status", kind="non_door", call_words=0)
    hold_ledger.record("exempt", "a", reason="short_prompt", words=3)
    hold_ledger.record("released", "a", how="trap")
    hold_ledger.record("released", "a", how="route")
    s = hold_ledger.summarize(days=30)
    assert s["prompts"] == 200 and s["holds"] == 40 and s["blocked"] == 3
    assert s["blocked_per_100_prompts"] == 1.5
    assert s["holds_per_100_prompts"] == 20.0
    assert s["probes"] == 3 and s["probes_per_100_prompts"] == 1.5
    assert s["probes_short"] == 1          # ack-shaped = a door probe of <= 12 words; a status view is not one
    assert s["probes_by_kind"] == {"door_unrelated": 2, "non_door": 1}
    assert s["exempt_by_reason"] == {"short_prompt": 1}
    assert s["released_by_how"] == {"trap": 1, "route": 1}
    assert s["sessions"] == 1


def test_no_prompt_row_is_unknown_not_zero():
    hold_ledger.record("blocked", "a", tool="Bash")
    s = hold_ledger.summarize(days=30)
    assert s["blocked"] == 1 and s["prompts"] == 0
    assert s["blocked_per_100_prompts"] is None and s["probes_per_100_prompts"] is None


def test_the_window_keeps_only_recent_rows(tmp_path):
    old = {"ts": time.time() - 40 * 86400, "event": "prompt", "session_id": "a", "words": 3}
    (tmp_path / hold_ledger.STORE_FILENAME).write_text(json.dumps(old) + "\n")
    hold_ledger.record("prompt", "a", words=4)
    assert hold_ledger.summarize(days=30)["prompts"] == 1
    assert hold_ledger.summarize(days=None)["prompts"] == 2


def test_the_cli_prints_the_summary_as_json(tmp_path):
    for _ in range(4):
        hold_ledger.record("prompt", "a", words=5)
    hold_ledger.record("blocked", "a", tool="Bash")
    import os

    env = {**os.environ, "LLM_ROUTER_HOME": str(tmp_path), "PYTHONPATH": str(ROOT / "src")}
    out = subprocess.run([sys.executable, "-m", "llm_router.hold_ledger", "--days", "30"],
                         capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=60)
    assert out.returncode == 0, out.stderr
    s = json.loads(out.stdout)
    assert s["prompts"] == 4 and s["blocked"] == 1 and s["blocked_per_100_prompts"] == 25.0


def test_rotation_keeps_two_generations(tmp_path, monkeypatch):
    monkeypatch.setattr(hold_ledger, "_MAX_BYTES", 300)
    for i in range(30):
        hold_ledger.record("prompt", f"s{i}", words=i)
    assert (tmp_path / (hold_ledger.STORE_FILENAME + ".1")).exists()
    assert hold_ledger.summarize(days=None)["prompts"] > 0


# ── the doctor counter ───────────────────────────────────────────────────────

def test_the_doctor_counter_is_unknown_without_prompts():
    r = counter_registry.read_one("blocked_holds")
    assert r.value is None and "no prompt recorded" in r.unknown_reason


def test_the_doctor_counter_alarms_on_the_rate_with_volume():
    for _ in range(150):
        hold_ledger.record("prompt", "a", words=5)
    hold_ledger.record("blocked", "a", tool="Bash")
    r = counter_registry.read_one("blocked_holds")
    assert r.value == 1.0 and not r.alarming            # 0.67 per 100: under the bar
    hold_ledger.record("blocked", "a", tool="Bash")
    r = counter_registry.read_one("blocked_holds")
    assert r.value == 2.0 and r.alarming                # 1.33 per 100: over the bar
    assert any("per 100" in line for line in r.detail)


def test_the_doctor_counter_alarms_on_an_ack_shaped_probe():
    for _ in range(120):
        hold_ledger.record("prompt", "a", words=5)
    hold_ledger.record("probe", "a", tool="llm", kind="door_unrelated", call_words=2)
    r = counter_registry.read_one("blocked_holds")
    assert r.value == 0.0 and r.alarming
    assert any("1 probe" in line for line in r.detail)


def test_a_small_sample_does_not_alarm():
    for _ in range(10):
        hold_ledger.record("prompt", "a", words=5)
    for _ in range(5):
        hold_ledger.record("blocked", "a", tool="Bash")
    assert not counter_registry.read_one("blocked_holds").alarming
