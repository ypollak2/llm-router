"""P2.2-A: skips.jsonl. A skip without a reason is refused, so "every skip has a reason"
is structural; the proxy writes one row for each of its two skip sites."""

from __future__ import annotations

import json
import re
import stat
import threading

import pytest

from llm_router import skips


@pytest.fixture(autouse=True)
def _scratch(monkeypatch, tmp_path):
    home = tmp_path / "state"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.delenv("LLM_ROUTER_AUTHORITY", raising=False)
    return home


def _rows(home) -> list[dict]:
    path = home / "skips.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _failopen_codes(home) -> list[str]:
    path = home / "fail_open.jsonl"
    return [json.loads(line)["c"] for line in path.read_text().splitlines()] if path.exists() else []


def test_record_row_shape(_scratch):
    assert skips.record("proxy", "tiers_off", "sess-1") is True
    (row,) = _rows(_scratch)
    assert set(row) == {"ts", "session_id", "door", "reason"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", row["ts"])
    assert (row["session_id"], row["door"], row["reason"]) == ("sess-1", "proxy", "tiers_off")


def test_session_id_is_optional(_scratch):
    skips.record("session-start", "base_url_override")
    assert _rows(_scratch)[0]["session_id"] is None


@pytest.mark.parametrize("door,reason", [("proxy", ""), ("proxy", "   "), ("proxy", None), ("proxy", 7),
                                         ("", "tiers_off"), (None, "tiers_off")])
def test_a_skip_without_a_reason_or_door_is_refused_and_recorded(_scratch, door, reason):
    assert skips.record(door, reason, "s") is False
    assert _rows(_scratch) == []
    assert "CHZ-FO-SKIPS-REASON" in _failopen_codes(_scratch)


def test_file_is_private(_scratch):
    skips.record("proxy", "routing_off")
    assert stat.S_IMODE((_scratch / "skips.jsonl").stat().st_mode) == 0o600


def test_the_log_is_bounded(_scratch, monkeypatch):
    monkeypatch.setattr(skips, "MAX_BYTES", 600)
    for i in range(40):
        skips.record("proxy", "routing_off", f"s{i}")
    assert (_scratch / "skips.jsonl.1").exists(), "past the cap the file rotates"
    assert (_scratch / "skips.jsonl").stat().st_size <= 600 + 200


def test_unwritable_state_dir_is_fail_open(tmp_path, monkeypatch):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(blocker / "state"))
    assert skips.record("proxy", "tiers_off", "s") is False  # no exception


def test_concurrent_writers_do_not_interleave(_scratch):
    def work(tag):
        for i in range(50):
            skips.record("proxy", "routing_off", f"{tag}-{i}")

    threads = [threading.Thread(target=work, args=(t,)) for t in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = _rows(_scratch)  # json.loads on every line: a torn line would raise
    assert len(rows) == 300
    assert len({r["session_id"] for r in rows}) == 300


def test_closed_sets():
    assert skips.REASONS == {"headless", "base_url_override", "tiers_off", "routing_off"}
    assert skips.DOORS == {"agent-route", "session-start", "proxy"}
