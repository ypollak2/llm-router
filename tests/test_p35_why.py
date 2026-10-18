"""P3.5-f: ``llm-router why`` / ``llm_router_status(view="why")`` print five elements per decision.

Seeded HOME: one proxy, one hook and one MCP decision. The join probe records what is true today:
a decision record in decisions.jsonl cannot be joined to a routing_decisions row (trace_id null).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re

import pytest

from llm_router import call_identity, cost, engine, why
from llm_router import decision_ledger as DL
from llm_router.commands import why as why_cmd
from llm_router.proxy import ledger as proxy_ledger

CANARY = "PROMPT-CANARY-quartz-7731 please summarise my secret plan"
NA = "n/a (not recorded for"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "usage.db"))
    monkeypatch.setenv("LLM_ROUTER_DECIDE_CACHE", "memory")
    monkeypatch.setenv("LLM_ROUTER_ALLOW_STUBS", "1")
    import llm_router.config as config_module
    config_module._config = None
    engine.clear_cache(persistent=False)
    yield tmp_path
    config_module._config = None


def _log(**kw):
    base = dict(prompt=CANARY, task_type="code", profile="balanced", classifier_type="heuristic",
                classifier_model=None, classifier_confidence=0.8, classifier_latency_ms=1.0,
                complexity="moderate", recommended_model="ollama/a", base_model="ollama/a",
                was_downshifted=False, budget_pct_used=0.0, quality_mode="balanced",
                final_model="ollama/served", final_provider="ollama", success=True, input_tokens=10,
                output_tokens=5, cost_usd=0.01, latency_ms=5.0, response=CANARY)
    base.update(kw)
    asyncio.run(cost.log_routing_decision(**base))


def _log_hook(**kw):
    """A row the way hooks/savings_logger.py:455-481 writes it: classifier_confidence/latency/budget/
    quality NULL on purpose (P0.8), reason_code "direct", no layer/confidence/abstain/skipped."""
    base = dict(classifier_type="heuristic", classifier_model=None, classifier_confidence=None,
                classifier_latency_ms=None, was_downshifted=None, budget_pct_used=None, quality_mode=None,
                reason_code="direct")
    base.update(kw)
    _log(**base)


def _log_mcp(**kw):
    """A row the way router.py:2757-2790 writes it: classifier_confidence/classifier_type/classifier_model
    set from classification_data (tools/routing.py:383-396), engine layer/confidence never passed."""
    base = dict(classifier_type="heuristic", classifier_model="engine", classifier_confidence=0.82)
    base.update(kw)
    _log(**base)


def _seed(home):
    proxy_ledger.write_row({
        "ts": 2_000_000_003.0, "trace_id": "ptrace", "served_model": "claude-haiku-x", "tier": "haiku",
        "tier_reason": "policy_table", "tier_task_type": "query", "tier_complexity": "simple",
        "tier_proposed": "sonnet", "tier_quota_state": "ok", "cls_layer": "L1", "cls_confidence": 0.91,
        "prompt": CANARY}, home / proxy_ledger.LEDGER_NAME)
    _log_hook(final_model="ollama/hook", trace_id="htrace")
    _log_mcp(final_model="ollama/mcp", reason_code="mcp_route",
             skipped=[{"model": "codex/x", "reason": "bench", "until": 1.5}], trace_id="mtrace")


def _by_door(recs):
    return {r["door"]: r for r in recs}


def test_three_doors_print_header_and_five_elements(home):
    _seed(home)
    recs, notes = why.last_decisions(10)
    assert notes == [] and set(_by_door(recs)) == {"proxy", "hook", "mcp"}
    d = _by_door(recs)
    p = why.render(d["proxy"])
    assert p.splitlines()[0] == "proxy | claude-haiku-x | tier_reason: policy_table"
    assert "query/simple" in p and "0.91" in p and "L1" in p and "ok" in p and "sonnet" in p
    h = why.render(d["hook"])
    assert h.splitlines()[0] == "hook | ollama/hook | tier_reason: direct"
    assert "code/moderate" in h
    assert "2 confidence:   n/a (not recorded for hook)" in h
    assert "3 layer:        n/a (engine layer not recorded for hook; classifier: heuristic)" in h
    assert f"4 quota state:  {NA} hook)" in h and f"5 alternatives: {NA} hook)" in h
    m = why.render(d["mcp"])
    assert "codex/x: bench" in m
    assert "2 confidence:   0.82 (classifier)" in m
    assert "3 layer:        n/a (engine layer not recorded for mcp; classifier: heuristic)" in m
    assert f"4 quota state:  {NA} mcp)" in m


def test_join_probe(home):
    """A hook/MCP routing_decisions row can NOT be joined to its decision record today."""
    with call_identity.scope("s-probe") as (_task, trace):
        engine.decide("probe prompt about sorting a list", door="hook", session_id="s-probe")
        _log_hook(trace_id=trace, final_model="ollama/hook")
    rows = DL.read_ledger(home)
    assert rows, "the probe found no decision record to check"
    joined = [r for r in rows if r.get("trace_id") == trace]
    assert joined == [], "decide now stamps trace_id: wire the join test and drop the n/a note"
    recs, _ = why.last_decisions(5)
    assert recs and recs[0]["trace_id"] == trace and recs[0]["decision_id"] is None


def test_join_fills_missing_layer_when_trace_matches(home):
    _log_hook(final_model="ollama/hook", trace_id="jt")  # no layer/confidence
    DL._append_ledger({"decision_id": "d1", "ts": 1.0, "door": "hook", "trace_id": "jt",
                       "layer": "L1", "confidence": 0.55, "abstain": False})
    (rec,), _ = why.last_decisions(1)
    assert rec["layer"] == "L1" and rec["confidence"] == 0.55 and rec["decision_id"] == "d1"


def test_unknown_is_na_never_blank_or_zero(home):
    _log_hook(classifier_type="", final_model="ollama/hook", task_type=None, complexity="",
              reason_code=None)  # confidence NULL = unknown
    (rec,), _ = why.last_decisions(1)
    out = why.render(rec)
    assert out.count(rec["door"] and "n/a (not recorded for") == 6  # tier_reason + the five elements
    # Assert on the rendered FIELD lines only. The old whole-output `": 0" not in out` matched the
    # random `trace: 0<hex>` line (F90: CI flake on 3.11, main run 39162562245; the "0.00" in pytest's
    # message was the diff of the same `and` expression, not a real match).
    header, *rest = out.splitlines()
    assert header.split("| tier_reason: ", 1)[1].startswith(NA)
    fields = {m.group(1): m.group(2) for m in
              (re.match(r"\s+\d (\w[\w ]*?):\s+(.*)$", ln) for ln in rest) if m}
    assert list(fields) == ["class", "confidence", "layer", "quota state", "alternatives"]
    for label, value in fields.items():
        assert value.startswith(NA), (label, value)
        assert not re.fullmatch(r"\s*|0(\.0+)?", value), (label, value)
    for k in why.ELEMENTS:
        assert rec[k] == f"n/a (not recorded for {rec['door']})"


def test_classifier_confidence_zero_is_a_real_value(home):
    _log_mcp(classifier_confidence=0.0, final_model="ollama/mcp")
    (rec,), _ = why.last_decisions(1)
    assert "2 confidence:   0.00 (classifier)" in why.render(rec)


@pytest.mark.parametrize("kw,door", [
    (dict(classifier_type="hook"), "hook"),
    (dict(classifier_type="heuristic", reason_code="direct"), "hook"),  # savings_logger.py:481
    (dict(classifier_type="sdk", reason_code="direct"), "sdk"),  # sdk.py:373
    (dict(classifier_type="llm"), "mcp"),
    (dict(classifier_type="unhinted"), "mcp"),
    (dict(classifier_type="weird-new"), "router (weird-new)"),
])
def test_door_label_from_classifier_type(home, kw, door):
    _log(final_model="ollama/x", **kw)
    (rec,), _ = why.last_decisions(1)
    assert rec["door"] == door


def test_n_is_clamped(home, capsys):
    _seed(home)
    assert why.clamp_n("9" * 5000) == why.MAX_N and why.clamp_n(10**30) == why.MAX_N
    assert why.clamp_n("-4") == 1 and why.clamp_n(0) == 1 and why.clamp_n("007") == 7
    assert why_cmd.main(["--last", "9" * 5000, "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["decisions"]) == 3
    assert len(json.loads(asyncio.run(_mcp("why", "9" * 5000)))["decisions"]) == 3


def test_empty_home_prints_message_and_json_shape(home, capsys):
    assert why_cmd.main([]) == 0
    assert "No routing decisions" in capsys.readouterr().out
    _seed(home)
    assert why_cmd.main(["--last", "2", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"decisions", "notes"} and len(data["decisions"]) == 2
    need = {"door", "model", "tier_reason", "ts", "trace_id", *why.ELEMENTS}
    assert all(need <= set(r) for r in data["decisions"])
    assert data["decisions"][0]["door"] == "proxy"  # newest first


def test_no_prompt_text_anywhere(home, capsys):
    _seed(home)
    engine.decide(CANARY, door="mcp")
    why_cmd.main(["--last", "10"])
    text = capsys.readouterr().out
    why_cmd.main(["--last", "10", "--json"])
    text += capsys.readouterr().out
    text += asyncio.run(_mcp("why", "10"))
    assert "CANARY" not in text and "secret plan" not in text and text.strip()


async def _mcp(view, period="today"):
    from llm_router.tools.consolidated import llm_router_status
    return await llm_router_status(view=view, period=period)


def test_mcp_view_returns_same_records(home):
    _seed(home)
    recs, notes = why.last_decisions(5)
    got = json.loads(asyncio.run(_mcp("why", "5")))
    assert got == json.loads(json.dumps({"decisions": recs, "notes": notes}))


def test_command_writes_nothing(home):
    _seed(home)
    def snap():  # -shm/-wal are SQLite's reader sidecars (WAL mode), not data; compare content
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in home.rglob("*")
                if p.is_file() and not p.name.endswith(("-shm", "-wal"))}
    before = snap()
    why_cmd.main(["--last", "5", "--json"])
    asyncio.run(_mcp("why", "5"))
    assert snap() == before and before
