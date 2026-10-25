"""Lane A PR1 (D-285, D-292, D-293): Haiku for classifier-confident CONTINUATION steps at the proxy.

Default OFF. Serving needs the YAML key, ``LLM_ROUTER_PROXY_LANE_A=on`` and ``haiku_rewrite``.
Every continuation ledger row carries ``lane_a_reason`` (closed set) and ``lane_a_conf`` (or null),
served or not. The classifier is the real rules + engine on short synthetic prompts; Anthropic is
a mocked upstream, so no test makes a network call.
"""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import httpx
import pytest
import yaml

from llm_router import engine
from llm_router.proxy import lane_a
from llm_router.proxy import ledger
from llm_router.proxy import server as ps
from llm_router.proxy import tiers as pt
from llm_router.proxy.cache_cost import Stickiness

FIXTURE = Path(__file__).parent / "fixtures" / "proxy" / "continuation_request.json"
OPUS, HAIKU = "claude-opus-5-5", "claude-haiku-4-5"
TEXT = "what does this function do?"  # engine L1 query/simple, confidence 0.667, rules agree
LANE_ON = {"enabled": True, "tau": 0.6, "prev_tool_classes": ["technical_op"]}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(lane_a.ENV_SWITCH, raising=False)


def _raw(lane=LANE_ON, **over) -> dict:
    raw = yaml.safe_load(pt.DEFAULT_POLICY_PATH.read_text())
    raw.update({"haiku_rewrite": True, "pinned_models": [OPUS], **over})
    if lane is None:
        raw.pop("lane_a", None)
    else:
        raw["lane_a"] = lane
    return raw


def _policy(lane=LANE_ON, **over) -> pt.ClaudeTierPolicy:
    return pt.ClaudeTierPolicy.from_dict(_raw(lane, **over))


def _body(text=TEXT, *, prev="Read", model=OPUS, main=True, sid=None, extra_user: list | None = None,
          continuation=True) -> dict:
    """First user turn (+ optional blocks), then, for a continuation, one tool call and its result."""
    body = json.loads(FIXTURE.read_text())
    body["model"] = model
    first = [{"type": "text", "text": text}, *(extra_user or [])]
    msgs = [{"role": "user", "content": first}]
    if continuation:
        inp = {"command": "git status"} if prev == "Bash" else {"file_path": "/work/a.py"}
        msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": prev, "input": inp}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}]
    body["messages"] = msgs
    body["tools"] = [t for t in body["tools"] if t["name"] != "Agent"]
    if main:
        body["tools"].append({"name": "Agent", "description": "d", "input_schema": {"type": "object"}})
    if sid:
        body["metadata"] = {"user_id": json.dumps({"session_id": sid})}
    else:
        body.pop("metadata", None)
    return body


def _rules(task="query", cx="simple"):
    async def fn(text):
        return {"task_type": task, "complexity": cx, "chain_head": [], "model": None}
    return fn


async def _decide(policy, body, classify=None):
    return await policy.decide(body, "sess-lane-a", Stickiness(), classify=classify or _rules())


# ── config: strict, default off ──────────────────────────────────────────────

def test_bundled_policy_ships_lane_a_off():
    raw = yaml.safe_load(pt.DEFAULT_POLICY_PATH.read_text())
    assert raw["lane_a"]["enabled"] is False
    assert pt.ClaudeTierPolicy.load().lane_a.enabled is False
    assert pt.ClaudeTierPolicy.from_dict(_raw(None)).lane_a == lane_a.LaneAConfig()  # absent = off


@pytest.mark.parametrize("bad", [
    {"enabled": "yes"}, {"enabled": 1}, {"tau": 0}, {"tau": 1.5}, {"tau": True}, {"tau": "0.7"},
    {"prev_tool_classes": []}, {"prev_tool_classes": "technical_op"}, {"prev_tool_classes": ["nope"]},
    {"prev_tool_classes": ["edit"]}, {"prev_tool_classes": ["technical_op", "exec"]},
    {"prev_tool_classes": ["agent"]}, {"surprise": 1}, "on", ["x"],
])
def test_parse_config_rejects_bad_values(bad):
    with pytest.raises(ValueError, match="lane_a"):
        pt.ClaudeTierPolicy.from_dict(_raw(bad))


def test_reason_set_is_closed():
    assert len(set(lane_a.REASONS)) == len(lane_a.REASONS)
    for r in lane_a.REASONS:
        assert lane_a.clean_reason(r) == r
    for bad in ("made_up", "", None, 3, ["served"]):
        assert lane_a.clean_reason(bad) is None
    assert lane_a.clean_conf(None) is None and lane_a.clean_conf(1.2) is None
    assert lane_a.clean_conf(True) is None and lane_a.clean_conf("0.9") is None
    assert lane_a.clean_conf(0.5) == 0.5


def test_ensure_fields_only_on_continuation_rows_and_null_when_unknown():
    row = {"step_class": "continuation", "lane_a_reason": "bogus", "lane_a_conf": 7}
    lane_a.ensure_fields(row)
    assert (row["lane_a_reason"], row["lane_a_conf"]) == (None, None)
    row = {"step_class": "continuation"}
    lane_a.ensure_fields(row)
    assert (row["lane_a_reason"], row["lane_a_conf"]) == (lane_a.R_OFF, None)
    row = {"step_class": "turn_first"}
    lane_a.ensure_fields(row)
    assert "lane_a_reason" not in row


# ── the policy branch: every reason code ─────────────────────────────────────

async def test_off_by_every_switch_stays_pinned_and_says_eligible(monkeypatch):
    """YAML off, env unset, env off, haiku_rewrite off: pinned model served, reason eligible, conf stamped."""
    cases = [(LANE_ON | {"enabled": False}, "on", True), (LANE_ON, None, True), (LANE_ON, "off", True),
             (LANE_ON, "on", False)]
    for lane, env, rewrite in cases:
        if env is None:
            monkeypatch.delenv(lane_a.ENV_SWITCH, raising=False)
        else:
            monkeypatch.setenv(lane_a.ENV_SWITCH, env)
        d = await _decide(_policy(lane, haiku_rewrite=rewrite), _body())
        assert (d.served_model, d.reason, d.body_rewrite, d.switched) == (OPUS, pt.REASON_CONFIG_PINNED, None, False)
        assert d.lane_a_reason == lane_a.R_ELIGIBLE, (lane, env, rewrite)
        assert d.lane_a_conf == pytest.approx(0.666667)


async def test_on_and_eligible_serves_the_haiku_tier(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    d = await _decide(_policy(), _body())
    assert (d.served_model, d.tier, d.reason, d.body_rewrite, d.switched) == (
        HAIKU, "haiku", pt.REASON_HAIKU_REWRITE, "haiku", True)
    assert (d.lane_a_reason, d.task_type, d.complexity) == (lane_a.R_SERVED, "query", "simple")
    assert d.arm is None  # lane A rows are not D-31 arm rows


async def test_served_model_comes_from_the_policy_not_the_code(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    raw = _raw()
    for t in raw["tiers"]:
        if t["name"] == "haiku":
            t["model"] = "claude-haiku-9-9"
    d = await _decide(pt.ClaudeTierPolicy.from_dict(raw), _body())
    assert d.served_model == "claude-haiku-9-9"


@pytest.mark.parametrize("name,body,expected", [
    ("turn_first", _body(continuation=False), lane_a.R_PINNED_TURN),
    ("sub_agent", _body(main=False), lane_a.R_NOT_MAIN_THREAD),
    ("edit_step", _body(prev="Edit"), lane_a.R_NOT_ALLOWLISTED),
    ("readonly_bash", _body(prev="Bash"), None),  # a read-only Bash command is technical_op: served
    ("model_cmd", _body(extra_user=[{"type": "text", "text": "<command-name>/model</command-name>"}]),
     lane_a.R_PINNED_BY_USER),
    ("image", _body(extra_user=[{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                              "data": "AAAA"}}]), lane_a.R_HAIKU_BODY),
])
async def test_reason_code_per_branch_with_the_switch_on(monkeypatch, name, body, expected):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    d = await _decide(_policy(), body)
    want = expected or lane_a.R_SERVED
    assert d.lane_a_reason == want, name
    if want != lane_a.R_SERVED:
        assert (d.served_model, d.reason) == (OPUS, pt.REASON_CONFIG_PINNED), name


async def test_side_call_without_client_tools_is_not_a_continuation(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    body = _body()
    body.pop("tools")
    d = await _decide(_policy(), body)
    assert d.lane_a_reason == lane_a.R_NOT_CONTINUATION and d.served_model == OPUS


async def test_non_organic_session_kind_is_not_served(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    from llm_router import session_kind
    monkeypatch.setattr(session_kind, "kind_of", lambda sid: "harness")
    d = await _decide(_policy(), _body())
    assert (d.lane_a_reason, d.served_model) == (lane_a.R_SESSION_KIND, OPUS)


async def test_correction_signal_keeps_the_step_pinned(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    from llm_router.proxy import escalation
    monkeypatch.setattr(escalation, "correction_signal", lambda body: "contradiction")
    d = await _decide(_policy(), _body())
    assert (d.lane_a_reason, d.served_model) == (lane_a.R_CORRECTION, OPUS)


async def test_low_confidence_and_its_value_is_kept(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    d = await _decide(_policy(LANE_ON | {"tau": 0.9}), _body())
    assert (d.lane_a_reason, d.served_model) == (lane_a.R_LOW_CONF, OPUS)
    assert d.lane_a_conf == pytest.approx(0.666667)


async def test_abstain_or_low_signal_is_low_conf(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    d = await _decide(_policy(LANE_ON | {"tau": 0.01}), _body("list the files in the src directory"))
    assert d.lane_a_reason == lane_a.R_LOW_CONF and d.served_model == OPUS


async def test_engine_and_rules_disagree(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    d = await _decide(_policy(), _body(), _rules("analyze", "complex"))
    assert (d.lane_a_reason, d.served_model) == (lane_a.R_ENGINE_DISAGREE, OPUS)
    assert d.lane_a_conf == pytest.approx(0.666667)
    d = await _decide(_policy(), _body(), _rules("query", "moderate"))  # same task, other class
    assert d.lane_a_reason == lane_a.R_ENGINE_DISAGREE


async def test_confidence_is_null_when_the_engine_gives_none(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    real = engine.compute(TEXT)
    import dataclasses
    monkeypatch.setattr(engine, "compute", lambda text, **kw: dataclasses.replace(real, confidence=None))
    d = await _decide(_policy(), _body())
    assert (d.lane_a_reason, d.lane_a_conf, d.served_model) == (lane_a.R_CONF_UNAVAILABLE, None, OPUS)
    monkeypatch.setattr(engine, "compute", lambda text, **kw: dataclasses.replace(real, layer="fallback"))
    d = await _decide(_policy(), _body())
    assert (d.lane_a_reason, d.lane_a_conf) == (lane_a.R_CONF_UNAVAILABLE, None)


async def test_errors_stay_pinned(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")

    def boom(text, **kw):
        raise RuntimeError("engine down")
    monkeypatch.setattr(engine, "compute", boom)
    d = await _decide(_policy(), _body())
    assert (d.lane_a_reason, d.lane_a_conf, d.served_model) == (lane_a.R_ERROR, None, OPUS)


async def test_unpinned_model_is_labelled_and_never_touched(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    d = await _decide(_policy(), _body(model="claude-sonnet-5"))
    assert d.lane_a_reason == lane_a.R_NOT_PINNED


async def test_no_haiku_tier_stays_pinned(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    raw = _raw()
    raw["tiers"] = [t for t in raw["tiers"] if t["name"] != "haiku"]
    raw["route"] = {"default": {k: v for k, v in raw["route"]["default"].items()}}
    for k, v in list(raw["route"]["default"].items()):
        if v == "haiku":
            raw["route"]["default"][k] = "sonnet"
    d = await _decide(pt.ClaudeTierPolicy.from_dict(raw), _body())
    assert d.lane_a_reason == lane_a.R_NO_HAIKU_TIER and d.served_model == OPUS


def test_every_reason_constant_is_in_the_closed_set():
    consts = {v for k, v in vars(lane_a).items() if k.startswith("R_") and isinstance(v, str)}
    assert consts == set(lane_a.REASONS) and len(consts) == 19
    # reached above: all but the reserved breaker code (a later PR) and the not-evaluated default
    # ``off`` (test_row_without_a_tier_decision_is_off)


# ── the proxy: off is byte-identical; the ledger always carries the fields ───

class Upstream:
    def __init__(self, fail_first_haiku=False):
        self.requests: list[httpx.Request] = []
        self.fail = fail_first_haiku

    def __call__(self, request):
        self.requests.append(request)
        model = json.loads(request.content)["model"]
        if self.fail and "haiku" in model:
            return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "no"}})
        sse = ("event: message_start\ndata: " + json.dumps({"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
            "usage": {"input_tokens": 5, "output_tokens": 1}}}) + "\n\n"
               "event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n").encode()
        return httpx.Response(200, stream=httpx.ByteStream(sse), headers={"content-type": "text/event-stream"})


def _app(tmp_path, raw, up):
    pf = tmp_path / "tiers.yaml"
    pf.write_text(yaml.safe_dump(raw))
    cfg = ps.ProxyConfig(steps=frozenset(), upstream="http://127.0.0.1:9", tiers=ps.TIERS_ON,
                         tier_policy=str(pf), ledger_path=tmp_path / "proxy_calls.jsonl")
    return ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(up)))


async def _post(app, body):
    h = {"authorization": "Bearer sk-ant-oat01-" + "Zq9" * 20, "anthropic-version": "2023-06-01",
         "content-type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        return await c.post("/v1/messages?beta=true", content=json.dumps(body), headers=h)


def _sample():
    return [_body(), _body("what is a mutex?", prev="Grep"), _body(prev="Edit"), _body(continuation=False),
            _body(main=False), _body(model="claude-sonnet-5")]


async def _run(tmp_path, raw, bodies):
    up = Upstream()
    app = _app(tmp_path, raw, up)
    for b in bodies:
        assert (await _post(app, copy.deepcopy(b))).status_code == 200
    rows = ledger.read_rows(tmp_path / "proxy_calls.jsonl")
    return [r.content for r in up.requests], rows


async def test_off_serves_byte_identical_requests(tmp_path, monkeypatch):
    """Whatever the switches, with lane A not fully on the upstream sees exactly the bytes it saw
    with no lane_a key at all (the pre-lane-A proxy), for continuation and turn-first requests."""
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base, _ = await _run(base_dir, _raw(None), _sample())
    assert len(base) == 6 and json.loads(base[0])["model"] == OPUS
    variants = [("yaml_off_env_on", LANE_ON | {"enabled": False}, "on", True),
                ("yaml_on_env_unset", LANE_ON, None, True),
                ("yaml_on_env_off", LANE_ON, "off", True),
                ("yaml_on_env_on_no_rewrite", LANE_ON, "on", False)]
    for i, (name, lane, env, rewrite) in enumerate(variants):
        if env is None:
            monkeypatch.delenv(lane_a.ENV_SWITCH, raising=False)
        else:
            monkeypatch.setenv(lane_a.ENV_SWITCH, env)
        d = tmp_path / f"v{i}"
        d.mkdir()
        got, rows = await _run(d, _raw(lane, haiku_rewrite=rewrite), _sample())
        ref_dir = tmp_path / f"r{i}"
        ref_dir.mkdir()
        ref, _ = await _run(ref_dir, _raw(None, haiku_rewrite=rewrite), _sample())
        assert got == ref, name
        assert all(json.loads(g)["model"] in (OPUS, "claude-sonnet-5") for g in got), name
        assert all(r["served_model"] == r["requested_model"] for r in rows), name


async def test_off_still_writes_both_columns_on_every_continuation_row(tmp_path):
    _, rows = await _run(tmp_path, _raw(LANE_ON | {"enabled": False}), _sample())
    conts = [r for r in rows if r["step_class"] == "continuation"]
    assert len(conts) == 5  # sample: 4 pinned/unpinned continuations incl. the sub-agent one, + sonnet
    for r in conts:
        assert "lane_a_reason" in r and "lane_a_conf" in r, r["step_class"]
        assert r["lane_a_reason"] in lane_a.REASONS
    by = [(r["requested_model"], r["prev_tool_class"], r["lane_a_reason"]) for r in conts]
    assert (OPUS, "technical_op", lane_a.R_ELIGIBLE) in by
    assert (OPUS, "edit", lane_a.R_NOT_ALLOWLISTED) in by
    assert ("claude-sonnet-5", "technical_op", lane_a.R_NOT_PINNED) in by
    eligible = next(r for r in conts if r["lane_a_reason"] == lane_a.R_ELIGIBLE)
    assert eligible["lane_a_conf"] == pytest.approx(0.666667)
    skipped = next(r for r in conts if r["lane_a_reason"] == lane_a.R_NOT_ALLOWLISTED)
    assert skipped["lane_a_conf"] is None  # never invented: the engine did not run
    turn_first = next(r for r in rows if r["step_class"] == "turn_first")
    assert (turn_first["lane_a_reason"], turn_first["lane_a_conf"]) == (lane_a.R_PINNED_TURN, None)


async def test_row_without_a_tier_decision_is_off(tmp_path):
    cfg = ps.ProxyConfig(steps=frozenset(), upstream="http://127.0.0.1:9", tiers=ps.TIERS_OFF,
                         ledger_path=tmp_path / "proxy_calls.jsonl")
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(Upstream())))
    assert (await _post(app, _body())).status_code == 200
    (row,) = ledger.read_rows(tmp_path / "proxy_calls.jsonl")
    assert (row["lane_a_reason"], row["lane_a_conf"]) == (lane_a.R_OFF, None)


async def test_on_serves_haiku_for_eligible_and_keeps_turn_first_sub_agent_and_edit_pinned(tmp_path, monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    sent, rows = await _run(tmp_path, _raw(), _sample())
    models = [json.loads(s)["model"] for s in sent]
    assert models == [HAIKU, HAIKU, OPUS, OPUS, OPUS, "claude-sonnet-5"]
    assert "thinking" not in json.loads(sent[0])
    served = [r for r in rows if r.get("lane_a_reason") == lane_a.R_SERVED]
    assert len(served) == 2
    for r in served:
        assert (r["served_model"], r["tier_reason"], r["tier_body_rewrite"]) == (HAIKU, pt.REASON_HAIKU_REWRITE, "haiku")
        assert r["lane_a_conf"] is not None and "tier_arm" not in r
    turn_first = rows[3]
    assert turn_first["step_class"] == "turn_first" and turn_first["served_model"] == OPUS
    assert rows[4]["lane_a_reason"] == lane_a.R_NOT_MAIN_THREAD


async def test_env_off_beats_yaml_on_per_request(tmp_path, monkeypatch):
    up = Upstream()
    app = _app(tmp_path, _raw(), up)
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    await _post(app, _body())
    monkeypatch.setenv(lane_a.ENV_SWITCH, "off")  # no restart: read per request
    await _post(app, _body())
    assert [json.loads(r.content)["model"] for r in up.requests] == [HAIKU, OPUS]


async def test_haiku_refusal_retries_on_the_pinned_model_and_is_labelled(tmp_path, monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    up = Upstream(fail_first_haiku=True)
    app = _app(tmp_path, _raw(), up)
    assert (await _post(app, _body())).status_code == 200
    assert [json.loads(r.content)["model"] for r in up.requests] == [HAIKU, OPUS]
    (row,) = ledger.read_rows(tmp_path / "proxy_calls.jsonl")
    assert (row["served_model"], row["lane_a_reason"]) == (OPUS, lane_a.R_RETRIED)


async def test_advise_authority_never_serves_and_says_eligible(tmp_path, monkeypatch):
    from llm_router import authority
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    monkeypatch.setattr(authority, "mode", lambda: authority.ADVISE)
    up = Upstream()
    app = _app(tmp_path, _raw(), up)
    assert (await _post(app, _body())).status_code == 200
    assert json.loads(up.requests[0].content)["model"] == OPUS
    (row,) = ledger.read_rows(tmp_path / "proxy_calls.jsonl")
    assert (row["lane_a_reason"], row["served_model"]) == (lane_a.R_ELIGIBLE, OPUS)


async def test_ledger_has_no_prompt_text(tmp_path, monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    secret = "zebra secret prompt text, what does this function do?"
    await _run(tmp_path, _raw(), [_body(secret)])
    assert "zebra" not in (tmp_path / "proxy_calls.jsonl").read_text()


# ── ratchet: every proxy ledger write goes through the one stamping writer ───

def test_every_proxy_continuation_ledger_write_sets_both_fields():
    """AST: ``ledger.write_row`` is called exactly once in the proxy package, inside server.py's
    ``write``, after ``lane_a.ensure_fields(row)``; and every ledger write in server.py is ``write``."""
    pkg = Path(ps.__file__).parent
    calls = []

    def walk(node, enclosing, fname):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else enclosing
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "write_row"):
                calls.append((fname, enclosing, child.lineno))
            walk(child, inner, fname)

    for f in sorted(pkg.glob("*.py")):
        walk(ast.parse(f.read_text()), None, f.name)
    assert calls, "no write_row call found: the ratchet checks nothing"
    assert [(f, fn) for f, fn, _ in calls] == [("server.py", "write")], calls
    tree = ast.parse((pkg / "server.py").read_text())
    write = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "write")
    order = [(n.lineno, ast.unparse(n.func)) for n in ast.walk(write) if isinstance(n, ast.Call)]
    stamp = [ln for ln, name in order if name == "lane_a.ensure_fields"]
    out = [ln for ln, name in order if name == "ledger.write_row"]
    assert stamp and out and stamp[0] < out[0]
    # Unconditional: both calls are statements of ``write``'s own body (not under an if/try/with/
    # loop), and the stamp comes first, so no path reaches write_row without it.
    top = [ast.unparse(st.value.func) for st in write.body if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call)]
    assert "lane_a.ensure_fields" in top and "ledger.write_row" in top, top
    assert top.index("lane_a.ensure_fields") < top.index("ledger.write_row")


def test_ensure_fields_stamps_both_keys_on_every_continuation_path():
    """The stamper is total: whatever a row carries, a continuation leaves with both keys."""
    for pre in ({}, {"lane_a_reason": None}, {"lane_a_reason": "served", "lane_a_conf": 0.9},
                {"lane_a_conf": "x"}):
        row = {"step_class": "continuation", **pre}
        lane_a.ensure_fields(row)
        assert set(("lane_a_reason", "lane_a_conf")) <= set(row)


# ── repair round 1: the lane's gates are side-effect free (SHOULD-1, SHOULD-2) ─

import os  # noqa: E402
import socket  # noqa: E402
import sqlite3  # noqa: E402
import time  # noqa: E402

OFF_LANE = LANE_ON | {"enabled": False}


def _state_files() -> dict[str, int]:
    root = Path(os.environ["LLM_ROUTER_HOME"])
    return {str(p.relative_to(root)): p.stat().st_size for p in sorted(root.rglob("*")) if p.is_file()}


def _no_model_calls(monkeypatch) -> list:
    """Any embed / network / sqlite attempt is recorded (and the socket one refused)."""
    from llm_router import classifier_health, complexity_knn

    seen: list = []

    async def knn(text):
        seen.append("complexity_knn.complexity_score")

    def refuse(*a, **k):
        seen.append("socket")
        raise OSError("network refused in test")

    def health(*a, **k):
        seen.append("classifier_health.record_agreement")

    monkeypatch.setattr(complexity_knn, "complexity_score", knn)
    monkeypatch.setattr(classifier_health, "record_agreement", health)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    return seen


@pytest.mark.parametrize("knn", [False, True])
@pytest.mark.parametrize("mode", ["shadow", "decide"])
async def test_lane_off_gates_write_no_state_and_make_no_model_call(monkeypatch, knn, mode):
    """100 pinned continuation steps, lane off, haiku_rewrite on: the gates run (the engine is
    consulted every time) yet the state dir gets no file: no engine_shadow.jsonl row, no
    decide_cache.sqlite, no decision-ledger row, no classifier_health write, no embed."""
    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", mode)
    monkeypatch.delenv("LLM_ROUTER_DECIDE_CACHE", raising=False)  # the sqlite default
    seen = _no_model_calls(monkeypatch)
    computes: list = []
    real = engine.compute
    monkeypatch.setattr(engine, "compute", lambda *a, **k: computes.append(k) or real(*a, **k))
    policy = _policy(OFF_LANE, complexity_knn=knn)
    before, lru = _state_files(), len(engine._lru)
    reasons = set()
    for i in range(100):
        d = await policy.decide(_body("what does function f%d do, how does it fail, and what does it return?" % i),
                                "sess-lane-a", Stickiness())  # the real classifier: no stub
        reasons.add(d.lane_a_reason)
        assert d.served_model == OPUS
    if hasattr(engine, "shadow_drain"):
        engine.shadow_drain(timeout=10)
    assert len(computes) == 100 and all(c.get("cache") is False for c in computes)
    assert reasons & {lane_a.R_ELIGIBLE, lane_a.R_LOW_CONF}, reasons  # the gates really ran
    assert seen == [], seen
    assert _state_files() == before == {}
    assert len(engine._lru) == lru  # not even the in-memory decision cache


async def test_lane_gates_touch_no_sqlite_and_no_loop_blocking_io(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sqlite on the lane path")

    monkeypatch.setattr(sqlite3, "connect", boom)
    policy = _policy(OFF_LANE)
    for i in range(5):
        d = await policy.decide(_body("what is a mutex %d, and how does it differ from a semaphore?" % i),
                                "sess-lane-a", Stickiness())
        assert d.lane_a_reason in (lane_a.R_ELIGIBLE, lane_a.R_LOW_CONF)


@pytest.mark.timing
async def test_lane_gates_do_not_stall_on_a_held_decide_cache_lock():
    """Another process (the hooks) holding the old decide cache's write lock used to stall one
    cold request ~358 ms. The lane no longer touches that file: p95 < 5 ms with the lock held."""
    path = Path(os.environ["LLM_ROUTER_HOME"]) / "decide_cache.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("CREATE TABLE IF NOT EXISTS decisions (k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL NOT NULL)")
    holder.execute("BEGIN IMMEDIATE")
    try:
        policy = _policy(OFF_LANE)
        await policy.decide(_body(), "sess-lane-a", Stickiness())  # warm lazy artifacts
        ms = []
        for i in range(100):
            b = _body("what does function g%d do, how does it fail, and what does it return?" % i)
            t = time.perf_counter()
            await policy.decide(b, "sess-lane-a", Stickiness())
            ms.append((time.perf_counter() - t) * 1000)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert sorted(ms)[94] < 5.0, sorted(ms)[-6:]


def test_compute_writes_no_ledger_row_and_decide_is_compute_plus_one_row(monkeypatch):
    from llm_router import decision_ledger

    rows: list = []
    monkeypatch.setattr(decision_ledger, "record", lambda *a, **k: rows.append((a, k)))
    c = engine.compute("what is a mutex?", cache=False)
    assert rows == []
    d = engine.decide("what is a mutex?", door="proxy")
    assert len(rows) == 1
    same = ("task_type", "tier", "complexity", "confidence", "abstain", "reason", "dims", "policy_version")
    assert all(getattr(c, k) == getattr(d, k) for k in same)


def test_compute_without_cache_leaves_no_cache_entry_or_file(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_DECIDE_CACHE", raising=False)
    n = len(engine._lru)
    engine.compute("a prompt nobody has cached %s" % time.time(), cache=False)
    assert len(engine._lru) == n and "decide_cache.sqlite" not in "".join(_state_files())


# ── repair round 1: the SHIPPED tau (SHOULD-3) ───────────────────────────────

def _shipped_policy() -> pt.ClaudeTierPolicy:
    raw = yaml.safe_load(pt.DEFAULT_POLICY_PATH.read_text())
    raw.update({"haiku_rewrite": True, "pinned_models": [OPUS]})  # lane_a block left exactly as shipped
    return pt.ClaudeTierPolicy.from_dict(raw)


async def test_shipped_tau_separates_score_4_from_score_5_plus(monkeypatch):
    """Engine L1 confidence is quantised: score/(score+2) = 0.600, 0.6667 (4), 0.714 (5), 0.75, 0.778 ...
    The shipped tau (a placeholder the prereg will set) must sit between the score-4 and score-5 bins,
    so a later engine change cannot silently zero the eligible share."""
    shipped = _shipped_policy().lane_a.tau
    assert shipped == lane_a.DEFAULT_TAU == yaml.safe_load(pt.DEFAULT_POLICY_PATH.read_text())["lane_a"]["tau"]
    assert 4 / 6 < shipped <= 5 / 7
    policy = _shipped_policy()
    score4 = ["what does this function do?", "what is a mutex?"]
    score5_plus = ["what does this function do, how does it handle errors, and what does it return when the input is empty?",
                   "what is the difference between a process and a thread, and how does the scheduler treat each of them?",
                   "what does this code do? how does the loop terminate? what is the return type?"]
    for text in score4:
        d = await policy.decide(_body(text), "sess-lane-a", Stickiness())
        assert (d.lane_a_reason, d.lane_a_conf) == (lane_a.R_LOW_CONF, pytest.approx(4 / 6, abs=1e-5)), text
    for text in score5_plus:
        d = await policy.decide(_body(text), "sess-lane-a", Stickiness())
        assert d.lane_a_reason == lane_a.R_ELIGIBLE and d.lane_a_conf >= 5 / 7 - 1e-5, text


# ── repair round 1: tiers on, lane off, byte-identical on every path (SHOULD-4) ──

class PlainUpstream:
    """A non-streaming JSON upstream: 200 message, 200 tool_use message, or an error status."""

    def __init__(self, status=200, tool_use=False):
        self.requests: list[httpx.Request] = []
        self.status, self.tool_use = status, tool_use

    @staticmethod
    def _json(status, obj):  # streamed, as the proxy relays raw upstream bytes
        return httpx.Response(status, stream=httpx.ByteStream(json.dumps(obj).encode()),
                              headers={"content-type": "application/json"})

    def __call__(self, request):
        self.requests.append(request)
        if self.status >= 400:
            return self._json(self.status, {"type": "error", "error": {"type": "api_error", "message": "x"}})
        content = ([{"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {"file_path": "/work/b.py"}}]
                   if self.tool_use else [{"type": "text", "text": "ok"}])
        return self._json(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": OPUS,
                                         "content": content, "stop_reason": "tool_use" if self.tool_use else "end_turn",
                                         "usage": {"input_tokens": 5, "output_tokens": 1}})


@pytest.mark.parametrize("case", ["non_stream", "upstream_4xx", "upstream_5xx", "tool_use_continuation"])
async def test_tiers_on_lane_off_is_byte_identical_on_every_path(tmp_path, monkeypatch, case):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")  # env on, YAML off: still not serving
    status, tool_use = {"upstream_4xx": (400, False), "upstream_5xx": (503, False)}.get(case, (200, case.startswith("tool")))
    body = _body()
    body["stream"] = False
    out = {}
    for name, lane in (("ref", None), ("off", OFF_LANE)):
        up = PlainUpstream(status, tool_use)
        d = tmp_path / name
        d.mkdir()
        resp = await _post(_app(d, _raw(lane), up), copy.deepcopy(body))
        out[name] = ([r.content for r in up.requests], resp.status_code, resp.content)
    assert out["off"] == out["ref"]
    assert out["off"][1] == status
    assert json.loads(out["off"][0][0])["model"] == OPUS  # never rewritten to Haiku
