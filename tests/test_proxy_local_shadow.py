"""Proxy SHADOW mode (llm_router.proxy.local_shadow): Claude serves every step, the
local model answers a copy in parallel, and only reason-code records are kept.

Everything is stubbed: a mocked Anthropic upstream and a fake local backend. Nothing
here reaches a network or an Ollama.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from llm_router.proxy import ledger, local_shadow
from llm_router.proxy import server as ps
from llm_router.proxy.translate import from_ollama

FIXTURE = Path(__file__).parent / "fixtures" / "proxy" / "continuation_request.json"
MODEL = "ollama/fake:1"
SECRET_PROMPT = "ZZ-PRIVATE-PROMPT-TEXT-ZZ"
SECRET_ARG = "/home/zz/private/path.txt"


def _first_call() -> dict:
    body = json.loads(FIXTURE.read_text())
    body["messages"] = body["messages"][:2]
    body["messages"][-1] = {"role": "user", "content": SECRET_PROMPT}
    return body


def _claude(name="Read", args=None) -> bytes:
    return json.dumps({"id": "msg_claude1", "type": "message", "role": "assistant", "model": "claude-x",
                       "stop_reason": "tool_use", "usage": {"input_tokens": 3, "output_tokens": 4},
                       "content": [{"type": "tool_use", "id": "tu1", "name": name,
                                    "input": args if args is not None else {"file_path": SECRET_ARG}}]}).encode()


def _ollama(name="Read", args=None):
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": name,
                                                     "arguments": args if args is not None
                                                     else {"file_path": SECRET_ARG}}}]},
            "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 5}


class Backend:
    """Fake local model. ``gate``: an Event the reply waits for (None = reply at once)."""

    def __init__(self, reply=None, gate: asyncio.Event | None = None):
        self.reply = reply if reply is not None else _ollama()
        self.gate = gate
        self.bodies: list[dict] = []
        self.running = 0
        self.max_running = 0
        self.cancelled = 0

    async def complete(self, body, timeout_s):
        self.bodies.append(body)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if self.gate is not None:
                await self.gate.wait()
            m, err = from_ollama(self.reply, body)
            return m, err, {}
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.running -= 1


class Upstream:
    def __init__(self, payload: bytes | None = None, delay: float = 0.3, status: int = 200):
        self.payload = payload if payload is not None else _claude()
        self.delay, self.status, self.requests = delay, status, []

    async def __call__(self, request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        return httpx.Response(self.status, stream=httpx.ByteStream(self.payload),
                              headers={"content-type": "application/json"})


class Env:
    def __init__(self, tmp_path):
        self.shadow_file = tmp_path / "proxy_local_shadow.jsonl"
        self.ledger = tmp_path / "proxy_calls.jsonl"
        self.kill = tmp_path / "kill"

    def app(self, backend, up, **cfg):
        cfg = dict(dict(model=MODEL, shadow=True, hedge_s=None, warm_up=False, steps=frozenset()), **cfg)
        config = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=self.ledger, kill_switch=self.kill,
                                shadow_path=self.shadow_file, **cfg)
        client = httpx.AsyncClient(transport=httpx.MockTransport(up))
        return ps.build_app(config, client=client, backend_factory=lambda m: backend)

    def records(self):
        return local_shadow.read_records(self.shadow_file)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


async def _post(app, body):
    headers = {"authorization": "Bearer sk-ant-oat01-" + "Zq9" * 20, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        return await c.post("/v1/messages?beta=true", content=json.dumps(body), headers=headers)


async def _step(env, backend, up, body=None, **cfg):
    app = env.app(backend, up, **cfg)
    resp = await _post(app, body or _first_call())
    await app.state.shadow.drain()
    return resp


# ── what a record says ───────────────────────────────────────────────────────

async def test_agreement_is_recorded_and_claude_is_served_untouched(env):
    up, backend = Upstream(), Backend()
    resp = await _step(env, backend, up)
    assert resp.status_code == 200 and resp.content == _claude()          # Claude's bytes, unchanged
    assert len(up.requests) == 1
    (rec,) = env.records()
    assert rec["kind"] == "local_shadow"
    assert rec["agree"] is True and rec["args_equal"] is True
    assert rec["schema_valid"] is True and rec["fallback_reason"] is None
    assert isinstance(rec["local_latency_s"], float) and rec["step_id"] == "msg_claude1"


async def test_same_tool_different_args_agrees_on_name_only(env):
    await _step(env, Backend(_ollama("Read", {"file_path": "/other"})), Upstream())
    (rec,) = env.records()
    assert rec["agree"] is True and rec["args_equal"] is False


async def test_different_tool_disagrees(env):
    await _step(env, Backend(_ollama("Bash", {"command": "ls"})), Upstream())
    (rec,) = env.records()
    assert rec["agree"] is False and rec["args_equal"] is None and rec["schema_valid"] is True


async def test_schema_invalid_local_reply_is_recorded_with_its_reason(env):
    await _step(env, Backend(_ollama("NoSuchTool", {})), Upstream())
    (rec,) = env.records()
    assert rec["schema_valid"] is False and rec["fallback_reason"] == "schema_invalid" and rec["agree"] is None


async def test_backend_crash_is_a_reason_code_and_claude_still_answers(env):
    class Boom(Backend):
        async def complete(self, body, timeout_s):
            raise RuntimeError(SECRET_PROMPT)

    resp = await _step(env, Boom(), Upstream())
    assert resp.status_code == 200
    (rec,) = env.records()
    assert rec["fallback_reason"] == "backend_error" and rec["agree"] is None
    assert SECRET_PROMPT not in json.dumps(rec)


async def test_records_hold_reason_codes_only_never_prompt_or_tool_text(env):
    await _step(env, Backend(), Upstream())
    text = env.shadow_file.read_text()
    for forbidden in (SECRET_PROMPT, SECRET_ARG, "Read", "file_path", "Bearer", "sk-ant"):
        assert forbidden not in text
    assert set(json.loads(text)) == {"kind", "ts", "session_id", "step_id", "agree", "args_equal",
                                     "local_latency_s", "schema_valid", "fallback_reason"}


async def test_non_agent_calls_are_not_shadowed(env):
    body = _first_call()
    body.pop("tools")
    backend = Backend()
    await _step(env, backend, Upstream(), body)
    assert env.records() == [] and backend.bodies == []


async def test_claude_error_is_recorded_not_compared(env):
    await _step(env, Backend(), Upstream(b'{"error":"x"}', status=529))
    (rec,) = env.records()
    assert rec["fallback_reason"] == "claude_not_ok" and rec["agree"] is None


# ── latency isolation ────────────────────────────────────────────────────────

async def test_a_hung_local_job_never_delays_claude_and_is_dropped(env):
    gate = asyncio.Event()                       # never set: local would hang forever
    backend, up = Backend(gate=gate), Upstream()
    app = env.app(backend, up, shadow_budget_s=30.0)
    t0 = time.monotonic()
    resp = await asyncio.wait_for(_post(app, _first_call()), timeout=2.0)
    elapsed = time.monotonic() - t0
    assert resp.status_code == 200 and resp.content == _claude() and elapsed < 1.0
    assert backend.max_running == 1               # local really started, in parallel
    await asyncio.wait_for(app.state.shadow.drain(), timeout=2.0)
    assert backend.cancelled == 1 and backend.running == 0     # dropped, not left running
    (rec,) = env.records()
    assert rec["fallback_reason"] == "dropped_claude_first" and rec["agree"] is None


async def test_local_runs_in_parallel_with_claude_not_after_it(env):
    """Claude takes 0.4 s; local starts during that window (not after it)."""
    seen = {}

    class Probe(Backend):
        async def complete(self, body, timeout_s):
            seen["t"] = time.monotonic()
            return await super().complete(body, timeout_s)

    up = Upstream(delay=0.4)
    t0 = time.monotonic()
    await _step(env, Probe(), up)
    assert seen["t"] - t0 < 0.2
    assert env.records()[0]["agree"] is True


async def test_local_budget_is_capped_when_claude_is_slower(env):
    gate = asyncio.Event()
    backend = Backend(gate=gate)
    await _step(env, backend, Upstream(delay=0.5), shadow_budget_s=0.05)
    (rec,) = env.records()
    assert rec["fallback_reason"] == "budget_exceeded" and backend.cancelled == 1


# ── bounded concurrency ──────────────────────────────────────────────────────

async def test_at_most_one_local_job_at_a_time(env):
    gate = asyncio.Event()
    backend, up = Backend(gate=gate), Upstream(delay=0.3)
    app = env.app(backend, up, shadow_budget_s=30.0)
    r1, r2 = await asyncio.gather(_post(app, _first_call()), _post(app, _first_call()))
    assert r1.status_code == r2.status_code == 200
    await app.state.shadow.drain()
    assert backend.max_running == 1 and len(backend.bodies) == 1
    reasons = sorted(str(r["fallback_reason"]) for r in env.records())
    assert reasons == ["dropped_claude_first", "skipped_busy"]


# ── local never sees or writes the user's tree ───────────────────────────────

async def test_shadow_reads_no_file_and_gets_a_copy(env, monkeypatch):
    from llm_router.proxy import local_mode, okf_context

    def _boom(*a, **k):
        raise AssertionError("shadow must not read the user's tree")

    monkeypatch.setattr(okf_context, "attach", _boom)
    monkeypatch.setattr(local_mode, "annotate_failed_checks", _boom)
    monkeypatch.setattr(local_mode, "check_applied", _boom)

    class Mutator(Backend):
        async def complete(self, body, timeout_s):
            body["messages"].clear()           # a hostile local job
            body["tools"].clear()
            return await super().complete(body, timeout_s)

    body = _first_call()
    up = Upstream()
    await _step(env, Mutator(), up, body)
    sent = json.loads(up.requests[0].content)
    assert sent == body                                      # Claude saw the request unmodified
    assert env.records()[0]["fallback_reason"] in (None, "schema_invalid")


# ── off by default, opt-in flag, registered ──────────────────────────────────

def test_shadow_is_off_by_default_and_flag_comes_from_env(monkeypatch):
    monkeypatch.delenv(local_shadow.ENV_SHADOW, raising=False)
    assert ps.ProxyConfig().shadow is False and ps.ProxyConfig.from_env().shadow is False
    monkeypatch.setenv(local_shadow.ENV_SHADOW, "on")
    assert ps.ProxyConfig.from_env().shadow is True
    monkeypatch.setenv(local_shadow.ENV_SHADOW, "maybe")
    with pytest.raises(ValueError):
        ps.ProxyConfig.from_env()


def test_shadow_flag_is_registered():
    from llm_router.env_registry import ENV_REGISTRY

    assert local_shadow.ENV_SHADOW in ENV_REGISTRY


async def test_off_mode_creates_no_runner_no_backend_call_no_file(env):
    backend, up = Backend(), Upstream()
    app = env.app(backend, up, shadow=False)
    resp = await _post(app, _first_call())
    assert resp.status_code == 200 and app.state.shadow is None
    assert backend.bodies == [] and not env.shadow_file.exists()


def test_shadow_needs_a_local_model_and_cannot_combine_with_serve():
    with pytest.raises(ValueError):
        ps.build_app(ps.ProxyConfig(shadow=True, model="claude-x"))
    with pytest.raises(ValueError):
        ps.build_app(ps.ProxyConfig(shadow=True, model=MODEL, serve="local-agent", trim="none"))


def test_cmd_proxy_refuses_shadow_without_a_local_model(capsys, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_PROXY_MODEL", raising=False)
    assert ps.cmd_proxy(["--shadow", "on"]) == 2
    assert "ollama" in capsys.readouterr().err


# ── reply parsing ────────────────────────────────────────────────────────────

def test_sse_reply_is_parsed_into_calls():
    ev = [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
          {"type": "content_block_start", "index": 1,
           "content_block": {"type": "tool_use", "id": "t", "name": "Bash", "input": {}}},
          {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"command"'}},
          {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ': "ls"}'}}]
    buf = "".join(f"event: x\ndata: {json.dumps(e)}\n\n" for e in ev).encode()
    assert local_shadow.calls_of_reply(buf, "text/event-stream") == [("Bash", {"command": "ls"})]
    assert local_shadow.calls_of_reply(b"not json", "application/json") is None


def test_compare_text_only_replies_agree_and_args_ignore_whitespace_and_key_order():
    assert local_shadow.compare([], []) == (True, True)
    assert local_shadow.compare([("A", {"x": " 1 ", "y": 2})], [("A", {"y": 2, "x": "1"})]) == (True, True)
    assert local_shadow.compare([("A", {})], [("B", {})]) == (False, None)


# ── kpi: a separate informational line that never feeds NS / D1 / D2 ─────────

def _write_records(path, n_agree=3, n_dis=1):
    now = time.time()
    for i in range(n_agree + n_dis):
        local_shadow.write_record({"kind": "local_shadow", "ts": now, "session_id": "s", "step_id": f"m{i}",
                                   "agree": i < n_agree, "args_equal": True if i < n_agree else None,
                                   "local_latency_s": 2.0 + i, "schema_valid": True,
                                   "fallback_reason": None}, path)
    local_shadow.write_record({"kind": "local_shadow", "ts": now, "session_id": "s", "step_id": "mx",
                               "agree": None, "args_equal": None, "local_latency_s": 20.0,
                               "schema_valid": None, "fallback_reason": "budget_exceeded"}, path)


def test_kpi_shows_proxy_shadow_as_its_own_line_and_never_moves_ns_d1_d2(monkeypatch, tmp_path):
    from llm_router import northstar as ns
    from llm_router import session_kind
    from llm_router.commands import kpi

    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    home = tmp_path / "lrhome"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    session_kind._FOUND.clear()
    session_kind.tag_session("s-org", "/Users/someone/Projects/app", env={})
    now = time.time()
    attempted = sorted(ns.ATTEMPTED_KINDS)[0]
    rows = ([{"session_id": "s-org", "kind": attempted, "outcome": ns.OUTCOME_USED, "lever": None, "ts": now}] * 20
            + [{"session_id": "s-org", "kind": "claude_only", "outcome": ns.OUTCOME_NOT_ROUTED, "lever": None,
                "ts": now}] * 60)
    monkeypatch.setattr(ns, "units", lambda days=30, session_id=None, root=None, backfill=False: iter(rows))

    without = kpi.compute_scorecard(days=7, now=1_800_000_000.0)
    text_without = kpi.render_scorecard(without)
    assert without["proxy_local_shadow"]["n"] == 0 and "local shadow (proxy)" not in text_without
    assert without["kpis"]["NS"]["value"] == "25.0% (n=80)"

    _write_records(local_shadow.shadow_path())
    with_rows = kpi.compute_scorecard(days=7, now=1_800_000_000.0)
    text_with = kpi.render_scorecard(with_rows)
    assert with_rows["proxy_local_shadow"]["n"] == 5 and with_rows["proxy_local_shadow"]["n_compared"] == 4

    assert json.dumps(with_rows["kpis"], sort_keys=True) == json.dumps(without["kpis"], sort_keys=True)
    assert json.dumps(with_rows["joins"], sort_keys=True) == json.dumps(without["joins"], sort_keys=True)
    extra = [ln for ln in text_with.splitlines() if ln.startswith("local shadow (proxy)")]
    assert len(extra) == 1 and "n=5" in extra[0] and "75%" in extra[0] and "(3/4)" in extra[0]
    assert "informational, never in NS, D1 or D2" in extra[0]
    assert "\n".join(ln for ln in text_with.splitlines() if not ln.startswith("local shadow (proxy)")) == text_without


def test_shadow_records_are_not_read_by_the_proxy_ledger(tmp_path):
    f = tmp_path / "proxy_local_shadow.jsonl"
    _write_records(f)
    assert ledger.read_rows(f) == [] or all(r.get("kind") == "local_shadow" for r in ledger.read_rows(f))
    assert local_shadow.shadow_path().name != ledger.ledger_path().name
