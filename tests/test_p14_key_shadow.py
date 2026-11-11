"""P14 key shadow: LLM_ROUTER_PROXY_LAYOUT_SHADOW (default off) logs hashes and counts of the conversation key."""
from __future__ import annotations

import copy
import json
import re

from llm_router import failopen
from llm_router.proxy import history_layout, prefix_hash
from tests import test_proxy_off_golden as g

MARKER = "ZQXMARKER7731"
HEX16 = re.compile(r"^[0-9a-f]{16}$")


def _body():
    b = g._req()
    b["messages"][0] = {"role": "user", "content": f"please do the thing {MARKER}"}
    b["system"] = [{"type": "text", "text": f"system {MARKER}"}]
    return b


async def _run(tmp_path, monkeypatch, shadow, bodies=None, name="continuation_served_sse"):
    g.CASES["_shadow"] = (*g.CASES[name][:4], [("POST", "/v1/messages?beta=true", b) for b in (bodies or [_body()])])

    clean = g._clean_env

    def clean_then_set(mp):
        clean(mp)
        if shadow:
            mp.setenv("LLM_ROUTER_PROXY_LAYOUT_SHADOW", "1")

    monkeypatch.setitem(g.__dict__, "_clean_env", clean_then_set)
    # The prefix tracker is process-wide: a second run of the same body would see the first as its previous turn
    # (appended_only True instead of None), so every run starts fresh, as in test_proxy_off_golden.
    prefix_hash.TRACKER._last.clear()
    try:
        return await g._run_case(tmp_path, monkeypatch, "_shadow", None)
    finally:
        g.CASES.pop("_shadow", None)


async def test_flag_off_adds_no_field(tmp_path, monkeypatch):
    rec = await _run(tmp_path, monkeypatch, False)
    assert rec["ledger"] and all("layout_shadow" not in r for r in rec["ledger"])


async def test_flag_on_shape_and_no_text(tmp_path, monkeypatch):
    rec = await _run(tmp_path, monkeypatch, True)
    assert len(rec["ledger"]) == 1
    row = rec["ledger"][0]
    s = row["layout_shadow"]
    body = _body()
    assert set(s) == {"key", "system", "tools", "first_human", "first_reply", "n_messages", "n_tools", "main_thread"}
    assert s["key"] == history_layout.conv_key(body)
    for k in ("key", "system", "tools", "first_human", "first_reply"):
        assert HEX16.match(s[k]), k
    assert s["n_messages"] == len([m for m in body["messages"] if m["role"] != "system"]) and s["n_tools"] == len(body["tools"])
    assert MARKER not in json.dumps(row)


async def test_upstream_and_local_requests_identical_with_and_without_flag(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    off = await _run(tmp_path / "a", monkeypatch, False)
    on = await _run(tmp_path / "b", monkeypatch, True)
    assert off["sent"] and off["sent"] == on["sent"]
    assert off["responses"] == on["responses"]
    strip = lambda rows: [{k: v for k, v in r.items() if k != "layout_shadow"} for r in rows]  # noqa: E731
    assert strip(off["ledger"]) == strip(on["ledger"])


async def test_forwarded_request_identical_too(tmp_path, monkeypatch):
    bodies = [g._first_call()]
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    off = await _run(tmp_path / "a", monkeypatch, False, bodies, "first_call_forwarded")
    on = await _run(tmp_path / "b", monkeypatch, True, bodies, "first_call_forwarded")
    assert any(s["to"] == "anthropic" for s in off["sent"]) and off["sent"] == on["sent"]
    assert "layout_shadow" in on["ledger"][0]


async def test_shadow_failure_still_serves_and_writes_the_row(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **k: seen.append(code))

    def boom(body):
        raise ValueError("key broke")

    monkeypatch.setattr(history_layout, "conv_key", boom)
    rec = await _run(tmp_path, monkeypatch, True)
    assert rec["responses"][0]["status"] == 200
    assert len(rec["ledger"]) == 1 and "layout_shadow" not in rec["ledger"][0]
    assert "LR-FO-PROXY-LAYOUT-SHADOW" in seen


def test_shadow_is_skipped_when_layout_is_on(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT_SHADOW", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_LAYOUT", "1")
    assert history_layout.shadow_enabled() and history_layout.enabled()


def test_components_attribute_a_key_change():
    a = _body()
    base = history_layout.shadow_fields(a)
    b = copy.deepcopy(a)
    b["tools"] = b["tools"] + [{"name": "ToolSearchLoaded"}]
    c = copy.deepcopy(a)
    c["system"] = [{"type": "text", "text": "other"}]
    fb, fc = history_layout.shadow_fields(b), history_layout.shadow_fields(c)
    assert fb["key"] != base["key"] and fb["tools"] != base["tools"] and fb["system"] == base["system"]
    assert fc["key"] != base["key"] and fc["system"] != base["system"] and fc["tools"] == base["tools"]


def test_stability_script_on_a_synthetic_ledger(tmp_path, capsys):
    from scripts import p14_key_stability as sc

    def row(sid, key, n, system="s", tools="t", fh="h", fr="r", ts=0.0, main=True):
        return {"session_id": sid, "ts": ts, "layout_shadow": {"key": key, "system": system, "tools": tools,
                "first_human": fh, "first_reply": fr, "n_messages": n, "n_tools": 3, "main_thread": main}}

    # A's rows are written out of request order (ts), a sub-agent row with its own first message sits between
    # them, and a session-less row is present: none of these may count as a key change.
    rows = [row("A", "k1", 5, ts=2), row("A", "k1", 3, ts=1), row("A", "sub", 1, fh="h2", ts=2.5, main=False),
            row("A", "k2", 7, tools="t2", ts=3), row("A", "k2", 7, ts=4),
            row("B", "k9", 1, ts=1), row("B", "k8", 3, system="s2", ts=2), row(None, "kx", 9, ts=1),
            {"session_id": "C"}, {"session_id": "A"}]
    p = tmp_path / "l.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    r = sc.analyse(p.read_text().splitlines())
    assert r["sessions"] == 2 and r["rows"] == 6
    assert r["per_session"] == [(2, 2), (4, 2)]
    assert (r["growing_pairs"], r["key_kept"], r["key_changed"]) == (3, 1, 2)
    assert r["changed_component"] == {"system": 1, "tools": 1, "first_human": 0, "first_reply": 0,
                                      "none_identified": 0}
    assert (r["excluded_no_session"], r["excluded_not_main_thread"]) == (1, 1)
    assert (r["sessions_with_pairs"], r["key_kept_ratio"]) == (2, 0.3333)
    assert sc.analyse([])["key_kept_ratio"] is None and sc.analyse([])["sessions_with_pairs"] == 0
    assert sc.main(["x", str(p)]) == 0
    out = capsys.readouterr().out
    assert "key_changed: 2" in out and "k1" not in out
    assert "sessions_with_pairs: 2" in out and "key_kept_ratio: 0.3333" in out
