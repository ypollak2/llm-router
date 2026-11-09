"""compute_scorecard reads ledger rows older than its window slim (LEDGERMEM-1).

The refresher read all of ``proxy_calls.jsonl`` into dicts (112 MB on disk) every minute. History older
than the window is now cut to the few keys that anything reads from it. The scorecard must be identical
to the one built from the full read: ``reference`` below is the old code path (``read_rows`` that ignores
the slim arguments)."""
from __future__ import annotations

import json

import pytest

from llm_router import session_kind
from llm_router.commands import kpi
from llm_router.proxy import ledger as pl

from tests import _o3_fixture as fx
from tests._o3_fixture import NOW, proxy_row

DAY = 86400.0


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "usage.db"))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    from llm_router import failopen

    failopen.reset_unpersisted()
    failopen.reset_cache()
    session_kind._FOUND.clear()
    session_kind.tag_session("s-org", "/Users/someone/Projects/app", env={})
    yield
    session_kind._FOUND.clear()
    failopen.reset_unpersisted()
    failopen.reset_cache()


def _synthetic_ledger() -> list[dict]:
    rows: list[dict] = []
    i = 0
    # History (older than 7 d): carries what only history can tell. "s-span" is stamped harness (an excluded kind) ONLY
    # here and its window rows are unstamped; policy v-new and the late keys first appear here.
    for k in range(40):
        rows.append(proxy_row(i, sid="s-span", tier="haiku", kind="harness", ts=NOW - 20 * DAY + k,
                              pv="v-new", text_sha="abc", req_bytes=123, big_blob="x" * 500))
        i += 1
    for k in range(30):
        rows.append(proxy_row(i, sid="s-old", tier="opus", kind="organic", ts=NOW - 9 * DAY + k, pv="v-old"))
        i += 1
    # Boundary: just before and just after the 7 d cutoff.
    rows.append(proxy_row(i, sid="s-span", tier="sonnet", kind="harness", ts=NOW - 7 * DAY - 5))
    i += 1
    rows.append(proxy_row(i, sid="s-span", tier="sonnet", kind=None, ts=NOW - 7 * DAY + 5))
    i += 1
    # Window rows.
    for k in range(120):
        rows.append(proxy_row(i, sid="s-span" if k % 3 == 0 else "s-org", tier="opus" if k % 5 == 0 else "sonnet",
                              kind=None if k % 3 == 0 else "organic", ts=NOW - 3 * DAY + k * 60,
                              step="continuation" if k % 2 else "turn_first",
                              pv="v-new", **({"text_sha": "d", "req_bytes": 9} if k >= 30 else {})))
        i += 1
    # The first 20 window rows lack tier_proposed (a schema key that history already carried): G3 counts
    # them only because history shows the key existed.
    for r in rows[-120:-100]:
        r.pop("tier_proposed")
    # Undated rows are kept whole whatever the window.
    rows.append({"session_id": "s-org", "tier": "opus"})
    rows.append({"ts": "yesterday", "session_id": "s-org", "tier": "opus"})
    return rows


def _write(rows):
    fx.write_jsonl(pl.ledger_path(), rows)
    with pl.ledger_path().open("a", encoding="utf-8") as fh:
        fh.write("not json\n\n[1, 2]\n")


def _canon(card) -> str:
    """The card as text, minus kpis.G2: it stamps the wall clock of the fail-open store, so two runs of
    the SAME code differ there (test_reference_is_deterministic). G2 reads only in-window proxy ts."""
    kpis = {k: v for k, v in card.get("kpis", {}).items() if k != "G2"}
    return json.dumps({**card, "kpis": kpis}, sort_keys=True, default=str)


def _reference(monkeypatch, **kw) -> str:
    """The scorecard from the OLD path: every row read in full."""
    real = pl.read_rows
    monkeypatch.setattr(pl, "read_rows", lambda path=None, days=None, **_ignored: real(path, days))
    try:
        return _canon(kpi.compute_scorecard(**kw))
    finally:
        monkeypatch.setattr(pl, "read_rows", real)


@pytest.mark.parametrize("kw", [
    {"days": 7, "now": NOW},
    {"days": 0, "now": NOW},
    {"days": 1, "now": NOW},
    {"days": 7, "now": NOW, "since_policy": "v-new"},
    {"days": 3, "now": NOW, "schema_since": 1.0},
    {"since": NOW - 4 * DAY, "until": NOW - 1 * DAY},
    {"since": NOW - 30 * DAY, "until": NOW},
])
def test_scorecard_is_identical_to_the_full_read(monkeypatch, kw):
    _write(_synthetic_ledger())
    slim_calls = []
    real = pl.read_rows

    def spy(path=None, days=None, **k):
        slim_calls.append(k)
        return real(path, days, **k)
    monkeypatch.setattr(pl, "read_rows", spy)
    got = _canon(kpi.compute_scorecard(**kw))
    monkeypatch.setattr(pl, "read_rows", real)
    no_cutoff = kw.get("days") == 0 and "since" not in kw              # --days 0: readers take every row, nothing slimmed
    assert slim_calls and (slim_calls[0].get("slim_before") is None) == no_cutoff   # the slim path ran iff it may
    assert got == _reference(monkeypatch, **kw)


def test_reference_is_deterministic(monkeypatch):
    _write(_synthetic_ledger())
    kw = {"days": 7, "now": NOW}
    a, b = _reference(monkeypatch, **kw), _reference(monkeypatch, **kw)
    assert a == b


def test_the_history_is_really_slimmed():
    _write(_synthetic_ledger())
    full = pl.read_rows()
    slim = pl.read_rows(slim_before=NOW - 7 * DAY, slim_keep=kpi._SCORECARD_OLD_KEYS)
    assert len(slim) == len(full) == 194                                   # no row dropped, n=len(full)
    assert [r.get("ts") for r in slim] == [r.get("ts") for r in full]
    old = [r for r in slim if isinstance(r.get("ts"), float) and r["ts"] < NOW - 7 * DAY]
    assert len(old) == 71 and sum("text_sha" in r for r in old) == 40   # n=71 older rows, 40 carry the key
    assert not any("big_blob" in r for r in old)
    assert [r for r in slim if not isinstance(r.get("ts"), float)] == [r for r in full
                                                                       if not isinstance(r.get("ts"), float)]


def test_read_rows_without_arguments_is_unchanged():
    rows = [proxy_row(i, ts=NOW - i) for i in range(5)]
    _write(rows)
    assert pl.read_rows() == rows
    assert [r["ts"] for r in pl.read_rows(days=1e-5)] == []
    pl.ledger_path().unlink()
    assert pl.read_rows() == []


def test_router_tool_scan_matches_the_whole_file_check(tmp_path):
    from llm_router import usage_outcome as uo

    cases = {"a.jsonl": '{"x": 1}\n{"name": "mcp__llm_router__llm"}\n', "b.jsonl": '{"x": 1}\n' * 50,
             "c.jsonl": "router_\n_router__", "d.jsonl": "", "e.jsonl": "router__"}
    for name, text in cases.items():
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        assert uo._mentions_router_tool(p) == ("router__" in text), name
    (tmp_path / "bad.jsonl").write_bytes(b'\xff\xfe router__ \xff')
    assert uo._mentions_router_tool(tmp_path / "bad.jsonl") is True
    with pytest.raises(OSError):
        uo._mentions_router_tool(tmp_path / "missing.jsonl")


def _old_jsonl_stat(path):
    """ledger_view._jsonl_stat as it was before LEDGERMEM-1 (whole file as one string)."""
    lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    newest = None
    for ln in reversed(lines):
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        ts = row.get("ts", row.get("timestamp")) if isinstance(row, dict) else None
        if ts is not None:
            newest = ts
            break
    return {"present": True, "rows": len(lines), "newest_ts": newest}


@pytest.mark.parametrize("name,text", [
    ("empty", ""),
    ("normal", '{"ts": 1}\n{"ts": 2}\n\n{"timestamp": 3}\ntorn {\n'),
    ("no ts anywhere", '{"a": 1}\n' * 200),
    ("ts only early (past the tail)", '{"ts": 7}\n' + '{"a": 1}\n' * 200 + "junk\n"),
    ("last parseable has no ts key but earlier has", '{"ts": 5}\n' + '{"a": 1}\n' * 3),
    ("no trailing newline", '{"ts": 1}\n{"ts": 9}'),
])
def test_jsonl_stat_matches_the_whole_file_version(tmp_path, name, text):
    from llm_router import ledger_view as lv

    p = tmp_path / "x.jsonl"
    p.write_text(text, encoding="utf-8")
    if name == "empty":
        assert lv._jsonl_stat(p) == {"present": True, "rows": 0, "newest_ts": None}
    else:
        assert lv._jsonl_stat(p) == _old_jsonl_stat(p)
    assert lv._jsonl_stat(tmp_path / "missing.jsonl") == {"present": False, "rows": None, "newest_ts": None}


def test_ledger_view_proxy_rows_keeps_only_the_window():
    from llm_router import ledger_view as lv

    _write(_synthetic_ledger())
    b = lv.Bounds("t", "t", NOW - 4 * DAY, NOW)
    got = lv._proxy_rows(b, pl.ledger_path())
    want = [r for r in pl.read_rows() if isinstance(r.get("ts"), float) and b.covers(r["ts"])]
    assert got == want and 0 < len(got) < 194
