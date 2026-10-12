"""technical_ops.jsonl writer: schema, privacy, mode, cap (PLAN v16 P2.1 task 3)."""
from __future__ import annotations

import stat

from llm_router import technical_ops as t

KEYS = {"ts", "session_id", "task_id", "tool", "op_class", "bytes_in", "bytes_out",
        "served_by", "skip_reason", "ms"}


def _rec(path, **kw):
    args = dict(session_id="s1", task_id="t1", tool="Grep", op_class=t.OpClass.SEARCH,
                bytes_in=9000, bytes_out=1200, served_by="local_filter", ms=12, path=path)
    args.update(kw)
    return t.record_op(**args)


def test_row_schema_and_mode(tmp_path):
    p = tmp_path / "technical_ops.jsonl"
    assert _rec(p) is True
    rows = t.read_ops(p)
    assert len(rows) == 1 and set(rows[0]) == KEYS
    assert rows[0]["op_class"] == "search" and rows[0]["served_by"] == "local_filter"
    assert rows[0]["bytes_in"] == 9000 and rows[0]["skip_reason"] is None
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_all_served_by_values_accepted(tmp_path):
    p = tmp_path / "o.jsonl"
    for sb in t.SERVED_BY:
        assert _rec(p, served_by=sb) is True
    assert [r["served_by"] for r in t.read_ops(p)] == list(t.SERVED_BY)


def test_invalid_served_by_writes_nothing(tmp_path):
    p = tmp_path / "o.jsonl"
    assert _rec(p, served_by="cloud") is False
    assert not p.exists()


def test_no_text_leaks_through_skip_reason(tmp_path):
    p = tmp_path / "o.jsonl"
    assert _rec(p, skip_reason="/Users/x/secret dir/file.py rm -rf", session_id="/a/b/sess") is True
    row = t.read_ops(p)[0]
    assert "/" not in row["skip_reason"] and " " not in row["skip_reason"]
    assert row["skip_reason"] == "file.py_rm_-rf"
    assert row["session_id"] == "sess"


def test_default_path_follows_llm_router_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    assert _rec(None) is True
    assert (tmp_path / "technical_ops.jsonl").exists()
    assert len(t.read_ops()) == 1


def test_cap_rotates(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_TECH_OPS_MAX_BYTES", "300")
    p = tmp_path / "o.jsonl"
    for _ in range(10):
        _rec(p)
    assert (tmp_path / "o.jsonl.1").exists()
    assert not p.exists() or p.stat().st_size <= 300 + 400
    assert len(t.read_ops(p)) >= 2


def test_unwritable_path_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert _rec(blocker / "sub" / "o.jsonl") is False
