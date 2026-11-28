"""A lifecycle row (``event`` key, e.g. ``backend_recycle``) in the proxy ledger is not a Claude call.

Bug: ``server.write`` annotated every row, and ``cost_accounting.served_by`` is "anthropic" for any row whose
decision is not "served", so a ``backend_recycle`` row got ``served_by="anthropic"`` and
``anthropic_cost_usd=None`` and every counting reader counted it as a call of unknown cost.
Source of the live count: 6 such rows, all 2026-11-25, home proxy_calls.jsonl (coordinator, 2026-11-26).
"""
import json

import pytest

from llm_router import proxy_liveness
from llm_router.proxy import cost_accounting as ca
from llm_router.proxy import ledger
from tests.test_proxy import _post, _req
from tests.test_proxy_backend_health import Clock, _app, policy  # noqa: F401
from tests.test_proxy_selfheal import StubBackend, command  # noqa: F401

OLD_EVENT = {"ts": 1.0, "event": "backend_recycle", "backend_health": {"recycle": "ok"},
             "served_by": "anthropic", "anthropic_usage": None, "anthropic_cost_usd": None,
             "counterfactual_cost_usd": None}


@pytest.mark.usefixtures("policy")
async def test_recycle_event_row_is_not_a_call(tmp_path, monkeypatch, command):  # noqa: F811
    cmd, marker = command
    monkeypatch.setenv("LLM_ROUTER_BACKEND_RECYCLE_CMD", cmd)
    clock = Clock()
    app = _app(tmp_path, StubBackend(clock, marker), clock, loop_max_consecutive=100, loop_repeat_window=0)
    await _post(app, _req())
    await app.state.recycler.task
    path = tmp_path / "proxy_calls.jsonl"
    raw = [json.loads(x) for x in path.read_text().splitlines()]
    events = [r for r in raw if r.get("event")]
    calls = [r for r in raw if not r.get("event")]
    assert len(events) == 1 and len(calls) == 1          # the check found both kinds of row
    # (a) the written event row does not look like a served call
    assert events[0].get("served_by") != "anthropic"
    assert not any(k in events[0] for k in ca.COST_FIELDS)
    # an OLD-format ledger row (event + served_by=anthropic) is appended too
    with path.open("a") as fh:
        fh.write(json.dumps(OLD_EVENT) + "\n")
    # (b) every counting reader sees 1 call, not 2 or 3
    rows = ledger.read_rows(path)
    assert len(rows) == 1 and not rows[0].get("event")
    assert len(ledger.read_rows(path, include_events=True)) == 3
    assert ledger.stats(rows)["calls"] == 1
    cost = ca.proxy_session_cost(rows)
    assert cost["calls"] == 1 and cost["unknown_calls"] <= 1
    assert cost["calls_anthropic"] + cost["calls_local"] == 1
    live = proxy_liveness.liveness(proxy_rows=rows, now=rows[0]["ts"] + 1.0)
    assert live["proxy_rows_24h"] == 1
