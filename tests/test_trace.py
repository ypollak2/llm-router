"""The trace must be off by default, bounded, and unable to break a caller."""
from __future__ import annotations

import json

from llm_router import trace


def _read(path):
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_TRACE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_TRACE_FILE", raising=False)
    assert not trace.enabled()


def test_a_file_path_alone_enables_it(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_TRACE", raising=False)
    monkeypatch.setenv("LLM_ROUTER_TRACE_FILE", str(tmp_path / "t.jsonl"))
    assert trace.enabled()


def test_emit_writes_one_record_per_call(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    monkeypatch.setenv("LLM_ROUTER_TRACE_FILE", str(path))
    trace.emit("a.b", tool="read_file")
    trace.emit("a.c", tool="write_file")
    rows = _read(path)
    assert [r["event"] for r in rows] == ["a.b", "a.c"]
    assert rows[0]["seq"] < rows[1]["seq"], "sequence must be monotonic"
    assert rows[0]["run"] == rows[1]["run"], "same process is one run"


def test_large_fields_are_clipped(tmp_path, monkeypatch):
    """A trace too big to read is not evidence."""
    monkeypatch.setenv("LLM_ROUTER_TRACE_FILE", str(path := tmp_path / "t.jsonl"))
    trace.emit("big", result="x" * 50_000)
    row = _read(path)[0]
    assert len(row["result"]) < 1000
    assert row["result"].endswith(">"), "the clip must say how much was dropped"


def test_emit_never_raises_when_the_path_is_unwritable(tmp_path, monkeypatch):
    """Tracing must never be the reason a routing decision fails."""
    monkeypatch.setenv("LLM_ROUTER_TRACE_FILE", "/proc/nonexistent/nope.jsonl")
    trace.emit("should.not.raise", a=1)   # must not raise


def test_span_records_a_failure_and_reraises(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_TRACE_FILE", str(path := tmp_path / "t.jsonl"))
    try:
        with trace.span("work", tool="x"):
            raise ValueError("boom")
    except ValueError:
        pass
    events = {r["event"]: r for r in _read(path)}
    assert "work.start" in events
    assert events["work.end"]["ok"] is False
    assert "boom" in events["work.end"]["error"]


def test_span_records_success_with_a_duration(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_TRACE_FILE", str(path := tmp_path / "t.jsonl"))
    with trace.span("work"):
        pass
    end = [r for r in _read(path) if r["event"] == "work.end"][0]
    assert end["ok"] is True
    assert isinstance(end["ms"], int)
