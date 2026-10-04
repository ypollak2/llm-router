"""PR 8: real Anthropic spend per call and per session (proxy.cost_accounting).

Claude Code's own ``total_cost_usd`` is phantom on a proxied session (p1p2/
REPORT.txt C4: $21.09 reported over 178 trials, real spend $0). These tests pin
the ledger as the source of truth: what a row records, what the session summary
says, that the reconciliation checks can fail, and that O1 prints reconciled
figures only when the ledger supports them.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import httpx
import pytest

from llm_router.proxy import cost_accounting as ca
from llm_router.proxy import ledger
from tests.test_proxy import (  # noqa: F401  (policy is a fixture)
    FakeBackend, Upstream, _app, _call, _ollama, _post, _req, _rows, _sse_reply, policy,
)

pytestmark = pytest.mark.usefixtures("policy")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_proxy_cost.py"
MODEL = "claude-opus-5-5"


def _usage(inp=10, out=100, read=40_000, w5=0, w1=2_000):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": w5 + w1,
            "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1}}


def _fwd(sid="s1", msg="m1", usage=None, ts=1.0, **kw):
    r = {"ts": ts, "session_id": sid, "msg_id": msg, "decision": "forwarded", "requested_model": MODEL,
         "served_model": MODEL, "upstream_status": 200, "stop_reason": "end_turn", "usage": ledger.normalize_usage(usage or _usage())}
    r.update(kw)
    return r


def _srv(sid="s1", msg="L1", ts=2.0, **kw):
    r = {"ts": ts, "session_id": sid, "msg_id": msg, "decision": "served", "reason": None,
         "requested_model": MODEL, "backend_usage": {"prompt_tokens": 900, "output_tokens": 50}}
    r.update(kw)
    return r


def _price(u, model=MODEL):
    return ledger.anthropic_cost({"served_model": model, "usage": u})


# -- per-call fields -----------------------------------------------------------

def test_forwarded_row_records_real_usage_cache_split_and_price():
    r = ca.annotate(_fwd(usage=_usage(w5=500, w1=1500)))
    assert r["served_by"] == "anthropic"
    assert r["anthropic_usage"]["cache_read_input_tokens"] == 40_000
    assert (r["anthropic_usage"]["cache_creation_5m"], r["anthropic_usage"]["cache_creation_1h"]) == (500, 1500)
    assert r["anthropic_cost_usd"] > 0
    assert r["anthropic_cost_usd"] == pytest.approx(_price(r["anthropic_usage"]), abs=1e-6)


def test_local_row_has_exactly_zero_anthropic_tokens_and_cost_but_a_counterfactual():
    r = ca.annotate(_srv(), prefix_ctx=42_000)
    assert r["served_by"] == "local"
    assert r["anthropic_usage"] == {k: 0 for k in ledger._NORMALIZED_KEYS}
    assert r["anthropic_cost_usd"] == 0.0
    # a cache read of the real prefix plus its own 50-token reply on the requested model
    expected = ledger._price_tokens(MODEL, cache_read_input_tokens=42_000, output_tokens=50)
    assert expected > 0 and r["counterfactual_cost_usd"] == pytest.approx(expected, abs=1e-6)


def test_unknown_usage_is_null_not_zero_but_an_error_status_bills_nothing():
    ok_no_usage = ca.annotate({"decision": "forwarded", "requested_model": MODEL, "upstream_status": 200})
    assert ok_no_usage["anthropic_usage"] is None and ok_no_usage["anthropic_cost_usd"] is None
    unreachable = ca.annotate({"decision": "forwarded", "requested_model": MODEL, "upstream_status": 502})
    assert unreachable["anthropic_cost_usd"] == 0.0 and not any(unreachable["anthropic_usage"].values())


def test_a_truncated_row_is_unknown_even_when_it_carries_placeholder_usage():
    """stop_reason None on a 2xx = the stream was cut: the usage is message_start's
    placeholder (output_tokens=1), so it must not price as a known call."""
    r = ca.annotate(_fwd(stop_reason=None, usage=_usage(out=1)))
    assert r["anthropic_usage"] is None and r["anthropic_cost_usd"] is None
    assert ca.row_complete(r) is False
    assert ca.proxy_session_cost([ca.annotate(_fwd(msg="ok")), r])["reconciled"] is False
    # an error reply with no stop_reason is still a known zero: nothing is billed
    err = ca.annotate(_fwd(stop_reason=None, upstream_status=529, usage=_usage(0, 0, 0, 0, 0)))
    assert err["anthropic_cost_usd"] == 0.0 and ca.row_complete(err)


def test_annotate_failure_leaves_no_partial_fields_and_the_row_is_not_annotated(monkeypatch):
    def boom(row, prefix_ctx):
        raise RuntimeError("counterfactual blew up")

    monkeypatch.setattr(ca, "counterfactual", boom)
    row = _fwd()
    with pytest.raises(RuntimeError):
        ca.annotate(row)
    assert not any(k in row for k in ca.COST_FIELDS)
    assert ca.has_cost_fields(row) is False
    # and a row carrying only some of the four does not count as annotated either
    assert ca.has_cost_fields({"served_by": "anthropic", "anthropic_usage": None}) is False


def test_fallback_after_a_failed_local_attempt_is_anthropic_spend():
    r = ca.annotate(_fwd(decision="fallback", reason="validation", backend_usage={"output_tokens": 9}))
    assert r["served_by"] == "anthropic" and r["anthropic_cost_usd"] > 0


# -- through the live proxy ----------------------------------------------------

async def test_proxy_writes_cost_fields_on_forwarded_fallback_and_served_rows(tmp_path):
    up = Upstream()
    backend = FakeBackend(_ollama("", [_call("Read", {})]))  # invalid -> falls back to Claude
    app = _app(tmp_path, up, backend)
    await _post(app, _req())
    backend.reply = _ollama("Running it.", [_call("Bash", {"command": "python3 tests/test_mod01.py"})])
    await _post(app, _req())  # now served locally, after one real call in this session
    fb, served = _rows(tmp_path)
    assert fb["decision"] == "fallback" and fb["served_by"] == "anthropic"
    assert fb["anthropic_usage"]["cache_read_input_tokens"] == 43_500
    assert fb["anthropic_cost_usd"] > 0
    assert fb["anthropic_cost_usd"] == pytest.approx(ledger.anthropic_cost(fb), abs=1e-6)
    assert served["decision"] == "served" and served["served_by"] == "local"
    assert not any(served["anthropic_usage"].values()) and served["anthropic_cost_usd"] == 0.0
    # priced against the prefix the earlier real call left in this session (43500 + 800)
    assert served["counterfactual_cost_usd"] > ledger._price_tokens(
        served["requested_model"], cache_read_input_tokens=0, output_tokens=2)
    assert len(up.requests) == 1  # the served step never reached Anthropic
    s = ca.proxy_session_cost(_rows(tmp_path))
    assert s["reconciled"] and s["real_anthropic_usd"] == pytest.approx(fb["anthropic_cost_usd"], abs=1e-4)


def _sse(events: list[tuple[str, dict]]) -> bytes:
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


_START = ("message_start", {"type": "message_start", "message": {
    "id": "msg_x", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
    "content": [], "stop_reason": None,
    "usage": {"input_tokens": 3, "cache_read_input_tokens": 43_500, "cache_creation_input_tokens": 800,
              "output_tokens": 1}}})
_START_NO_USAGE = ("message_start", {"type": "message_start", "message": {
    "id": "msg_x", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
    "content": [], "stop_reason": None}})
_BLOCK = ("content_block_start", {"type": "content_block_start", "index": 0,
                                  "content_block": {"type": "text", "text": ""}})
_DELTA_NO_USAGE = ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
_STOP = ("message_stop", {"type": "message_stop"})


class _CutStream(httpx.AsyncByteStream):
    """An upstream body that raises ``httpx.ReadError`` after its first chunk."""

    def __init__(self, first: bytes):
        self.first = first

    async def __aiter__(self):
        yield self.first
        raise httpx.ReadError("connection reset")


def _cut_upstream(first: bytes):
    return lambda request: httpx.Response(200, stream=_CutStream(first),
                                          headers={"content-type": "text/event-stream"})


def _json_upstream(body: dict, status=200):
    return lambda request: httpx.Response(status, stream=httpx.ByteStream(json.dumps(body).encode()),
                                          headers={"content-type": "application/json"})


def _sse_upstream(events):
    return lambda request: httpx.Response(200, stream=httpx.ByteStream(_sse(events)),
                                          headers={"content-type": "text/event-stream"})


_LIVE_UNKNOWN = {
    "sse_message_start_without_usage": (
        _sse_upstream([_START_NO_USAGE, _BLOCK, _DELTA_NO_USAGE, _STOP]), "no_usage"),
    "json_200_without_usage_key": (
        _json_upstream({"id": "msg_j", "type": "message", "model": "claude-sonnet-5",
                        "content": [], "stop_reason": "end_turn"}), "no_usage"),
    "upstream_read_error_after_first_chunk": (_cut_upstream(_sse([_START_NO_USAGE])), "no_usage"),
    "sse_cut_after_message_start": (_sse_upstream([_START, _BLOCK]), "truncated"),
    "read_error_after_message_start": (_cut_upstream(_sse([_START, _BLOCK])), "truncated"),
}


async def _live_forward(tmp_path, upstream, sid):
    app = _app(tmp_path, upstream, steps=frozenset())
    try:
        await _post(app, dict(_req(), metadata={"user_id": json.dumps({"session_id": sid})}))
    except httpx.ReadError:
        pass  # the cut stream propagates to the client; the ledger row is what matters
    return [r for r in _rows(tmp_path) if r.get("session_id") == sid]


@pytest.mark.parametrize("case", sorted(_LIVE_UNKNOWN))
async def test_live_proxy_records_unknown_not_zero_for_a_reply_with_no_usable_usage(tmp_path, case):
    """Through the real proxy (the shape a row actually has on disk): usage that
    never arrived, or a stream cut before the terminal message_delta, is UNKNOWN."""
    upstream, why = _LIVE_UNKNOWN[case]
    rows = await _live_forward(tmp_path, upstream, "live-unk")
    (row,) = rows
    assert row["decision"] == "forwarded" and row["upstream_status"] == 200
    assert row["usage"] is None and row["usage_unknown"] == why
    assert row["served_by"] == "anthropic"
    assert row["anthropic_usage"] is None, "unknown must be null, not zeros"
    assert row["anthropic_cost_usd"] is None
    s = ca.proxy_session_cost(rows)
    assert s["reconciled"] is False and s["unknown_calls"] == 1
    assert ca.check_forwarded(rows, 1.0)["ok"] is False


async def test_live_proxy_complete_reply_is_still_known_and_reconciled(tmp_path):
    rows = await _live_forward(tmp_path, Upstream(), "live-ok")
    (row,) = rows
    assert row["usage"]["cache_read_input_tokens"] == 43_500 and row["stop_reason"] == "end_turn"
    assert row["anthropic_cost_usd"] > 0 and "usage_unknown" not in row
    assert ca.proxy_session_cost(rows)["reconciled"] is True


async def test_live_proxy_error_status_is_a_known_zero_not_unknown(tmp_path):
    rows = await _live_forward(tmp_path, _json_upstream({"type": "error", "error": {"type": "overloaded"}}, 529),
                               "live-err")
    (row,) = rows
    assert row["upstream_status"] == 529 and row["anthropic_cost_usd"] == 0.0
    assert not any(row["anthropic_usage"].values())
    assert ca.proxy_session_cost(rows)["reconciled"] is True


async def test_kpi_o1_does_not_print_reconciled_for_live_rows_with_unknown_usage(tmp_path, kpi_env):
    """60 live-proxy rows whose usage never arrived must not read as
    'reconciled: $0.00 ... real Anthropic spend $0.00'."""
    from llm_router.commands import kpi

    rows = []
    for i in range(60):
        rows += await _live_forward(tmp_path / f"d{i}", _LIVE_UNKNOWN["json_200_without_usage_key"][0], "kpi-live")
    assert len(rows) == 60 and all(r["anthropic_usage"] is None for r in rows)
    for r in rows:
        r["ts"] = time.time()
    _write(rows)
    o1 = kpi.compute_scorecard(days=7)["kpis"]["O1"]
    assert "reconciled:" not in o1["value"] and not o1.get("reconciled")


async def test_a_cost_accounting_bug_never_loses_the_ledger_row(tmp_path, monkeypatch):
    def boom(row, prefix=0):
        raise RuntimeError("boom")

    monkeypatch.setattr(ca, "annotate", boom)
    r = await _post(_app(tmp_path, Upstream(), None, steps=frozenset()), _req())
    assert r.status_code == 200
    (row,) = _rows(tmp_path)
    assert row["decision"] == "forwarded" and "served_by" not in row


# -- per-session summary -------------------------------------------------------

def test_proxy_session_cost_mixed_session_labels_the_phantom_figure_unreliable():
    rows = [ca.annotate(_fwd(msg="a", ts=1)), ca.annotate(_srv(msg="b", ts=2), 42_000),
            ca.annotate(_srv(msg="c", ts=3), 42_000),
            ca.annotate(_fwd(msg="d", ts=4, usage=_usage(w1=0)))]
    s = ca.proxy_session_cost(rows, "s1", claude_code_total_cost_usd=21.09)
    assert s["calls"] == 4 and s["calls_local"] == 2 and s["calls_anthropic"] == 2
    assert s["real_anthropic_usd"] == pytest.approx(
        _price(rows[0]["anthropic_usage"]) + _price(rows[3]["anthropic_usage"]), abs=1e-3)
    assert s["anthropic_tokens"]["cache_read_input_tokens"] == 80_000
    assert s["claude_code_total_cost_usd"] == 21.09 and s["claude_code_total_cost_label"] == "unreliable"
    assert s["est_avoided_label"] == "est." and s["est_avoided_runs"] == 1
    # one served RUN of 2 steps is ONE deferred call: priced once (cache read of the 42k prefix
    # the first call left + one 50-token reply), and the next call wrote no extra cache
    once = ledger._price_tokens(MODEL, cache_read_input_tokens=42_000, output_tokens=50)
    assert s["est_avoided_usd"] == pytest.approx(once, abs=1e-4)
    assert s["reconciled"] is True
    assert "unreliable" in ca.label_claude_code_cost(21.09)


def test_session_with_an_unknown_call_is_not_reconciled():
    rows = [ca.annotate(_fwd()), ca.annotate({"session_id": "s1", "decision": "forwarded",
                                               "requested_model": MODEL, "upstream_status": 200})]
    s = ca.proxy_session_cost(rows)
    assert s["unknown_calls"] == 1 and s["reconciled"] is False


# -- the two reconciliation checks (each must be able to fail) -----------------

def test_forwarded_check_passes_within_tolerance_and_fails_outside_it():
    rows = [ca.annotate(_fwd(msg=f"m{i}", ts=i)) for i in range(5)]
    true = ca.proxy_session_cost(rows)["real_anthropic_usd"]
    assert ca.check_forwarded(rows, true * 1.02)["ok"] is True          # 2% off, tolerance 3%
    bad = ca.check_forwarded(rows, true * 1.10)
    assert bad["ok"] is False and bad["rel_diff"] > ca.DEFAULT_TOLERANCE
    assert ca.check_forwarded(rows + [ca.annotate(_srv())], true)["applicable"] is False


def test_local_check_is_exactly_zero_and_catches_a_stray_anthropic_token():
    clean = [ca.annotate(_srv(msg=f"l{i}")) for i in range(3)]
    assert ca.check_local(clean) == {"applicable": True, "ok": True, "anthropic_tokens": 0}
    # a served row that carries ONE Anthropic token in its raw usage is a ledger bug
    dirty = clean + [ca.annotate(_srv(msg="x", usage={"input_tokens": 1}))]
    res = ca.check_local(dirty)
    assert res["ok"] is False and res["anthropic_tokens"] == 1
    assert ca.proxy_session_cost(dirty)["reconciled"] is False
    assert ca.check_local(clean + [ca.annotate(_fwd())])["applicable"] is False


# -- the script, on fixture ledger + transcripts -------------------------------

def _script():
    spec = importlib.util.spec_from_file_location("reconcile_proxy_cost", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _transcript(path: Path, msgs: dict[str, dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps({"type": "assistant", "message": {
        "id": i, "model": MODEL, "usage": u}}) for i, u in msgs.items()) + "\n")


def _fixture(tmp_path, *, drift=1.0):
    led = tmp_path / "proxy_calls.jsonl"
    proj = tmp_path / "projects"
    fwd = [_fwd(sid="fwd", msg=f"f{i}", ts=i) for i in range(4)]
    loc = [_srv(sid="loc", msg=f"l{i}", ts=i) for i in range(3)]
    for r in fwd + loc:
        ledger.write_row(r, led)
    u = _usage()
    _transcript(proj / "p" / "fwd.jsonl", {f"f{i}": u for i in range(3)})
    # the 4th call lives in a sub-agent transcript, which must be read too
    _transcript(proj / "p" / "fwd" / "subagents" / "a.jsonl",
                {"f3": dict(u, output_tokens=int(u["output_tokens"] * drift))})
    _transcript(proj / "p" / "loc.jsonl", {f"l{i}": {"input_tokens": 900, "output_tokens": 50} for i in range(3)})
    return led, proj


def test_script_matches_a_forwarded_session_and_reports_local_zero(tmp_path):
    mod = _script()
    led, proj = _fixture(tmp_path)
    rep = mod.run(led, proj, set(), None, {}, 0.03)
    by = {s["session_id"]: s for s in rep["sessions"]}
    assert by["fwd"]["check"]["ok"] and by["fwd"]["joined"] == 4  # sub-agent transcript included
    assert by["loc"]["kind"] == "local" and by["loc"]["check"] == {
        "applicable": True, "ok": True, "anthropic_tokens": 0}
    assert rep["checks"] == 2 and rep["failed"] == 0
    assert mod.main(["--ledger", str(led), "--projects-dir", str(proj)]) == 0


def test_script_fails_when_the_transcript_disagrees_and_honours_exclude(tmp_path):
    mod = _script()
    led, proj = _fixture(tmp_path, drift=500.0)  # transcript output tokens x500 on one call
    assert mod.main(["--ledger", str(led), "--projects-dir", str(proj)]) == 1
    rep = mod.run(led, proj, {"fwd"}, None, {}, 0.03)
    assert [s["session_id"] for s in rep["sessions"]] == ["loc"] and rep["failed"] == 0


def test_script_empty_ledger_checks_nothing_rather_than_passing_everything(tmp_path):
    mod = _script()
    rep = mod.run(tmp_path / "none.jsonl", tmp_path, set(), None, {}, 0.03)
    assert rep["checks"] == 0 and rep["sessions"] == []


# -- KPI O1 --------------------------------------------------------------------

def _write(rows):
    p = ledger.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


@pytest.fixture
def kpi_env(monkeypatch, tmp_path):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)


def _session_rows(sid, n, t0):
    rows = []
    for i in range(n):
        r = _fwd(sid=sid, msg=f"{sid}-f{i}", ts=t0 + 2 * i) if i % 2 == 0 else _srv(
            sid=sid, msg=f"{sid}-l{i}", ts=t0 + 2 * i)
        rows.append(ca.annotate(r, 42_000))
    return rows


def test_kpi_o1_prints_reconciled_figures_when_the_ledger_supports_them(kpi_env):
    from llm_router.commands import kpi

    rows = _session_rows("sA", 40, time.time() - 600) + _session_rows("sB", 40, time.time() - 300)
    _write(rows)
    o1 = kpi.compute_scorecard(days=7)["kpis"]["O1"]
    assert o1["measurable"] and o1["value"].startswith("reconciled: $")
    assert "real Anthropic spend" in o1["value"] and "est." not in o1["value"].split("[")[0]
    assert o1["reconciled"] is True and o1["n"] == 80 and o1["real_anthropic_usd"] > 0
    assert "total_cost_usd" not in json.dumps(o1)
    assert o1["newest_ts"] == max(r["ts"] for r in rows)  # the freshness gate reads this


def test_kpi_o1_keeps_est_when_rows_are_legacy_or_unreconciled(kpi_env, monkeypatch):
    from llm_router import dashboard_data as dd
    from llm_router.commands import kpi

    class FakeSummary:
        estimated_n, estimated_usd = 500, 42.0

        def display(self):
            return "est. saved $42.00"

    monkeypatch.setattr(dd, "summary", lambda period: FakeSummary())
    legacy = [dict(r) for r in _session_rows("sL", 100, time.time() - 600)]
    for r in legacy:  # rows written before this PR carry no cost fields
        for k in ("served_by", "anthropic_usage", "anthropic_cost_usd", "counterfactual_cost_usd"):
            r.pop(k)
    _write(legacy)
    o1 = kpi.compute_scorecard(days=7)["kpis"]["O1"]
    assert "est." in o1["value"] and "reconciled:" not in o1["value"] and "not reconciled" in o1["value"]
    # one unknown-usage call poisons its session: still est., never a partial "reconciled"
    rows = _session_rows("sU", 100, time.time() - 600)
    rows[0] = ca.annotate({"session_id": "sU", "ts": rows[0]["ts"], "decision": "forwarded",
                           "requested_model": MODEL, "upstream_status": 200})
    _write(rows)
    assert "reconciled:" not in kpi.compute_scorecard(days=7)["kpis"]["O1"]["value"]
