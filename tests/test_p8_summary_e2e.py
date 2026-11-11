"""P8: the summary path end to end, through the real proxy app with a mocked local backend.

Pins four properties: MR on (P9) serves a reduced body and writes a consistent ledger row; the rolling summary
(context_summary, LLM_ROUTER_ROLLING_SUMMARY) never reaches a proxy request, so the request holds exactly one
set of MR chunk summaries and no chunk is summarised twice; with the chunk store (P10) and the gate (P11) on,
turn two makes 0 map calls and the gate still runs; every flag off leaves the Ollama wire payload untouched.

Why the rolling summary cannot double up: ``context_pack.SUMMARY_DOORS`` has no "proxy" door, and
``proxy.backends._pack_messages`` builds its pack with ``door="proxy"`` and no session id; local-agent mode,
the only caller of MR, refuses every trim but "none", the only trim that is not the pack.

What CAN sit beside the MR summaries is ``proxy.okf_context.attach`` (server.py, ``_build_send``, after MR): a
follow-up prompt may get one prior-turn session block (session_store, <=300 tokens) in the SYSTEM prompt. It is
not the rolling summary. Gates: LLM_ROUTER_CONTEXT_INJECTION (default on), a git cwd in the system prompt, a
reply-shaped newest prompt, recorded session events, and room. With the default 5000-token OKF ceiling, room needs
the MR-reduced send under about 4800 tokens (room = min(1500, ceiling - estimate), at least MIN_ROOM_TOKENS 200).
MR only runs on a body over its cap (25k by default) and leaves it near that cap, so there is no room
(reason "no_room") and no block. The test pins this for one body shape, the p9 session after MR; it does not
measure every body. With LLM_ROUTER_OKF_BY_WINDOW on there is room,
and then exactly one such block and the MR summaries share the request (tests below pin both).
"""
from __future__ import annotations

import copy
import json

import httpx
import pytest

from llm_router import context_pack, context_summary
from llm_router.proxy import backends as pb
from llm_router.proxy import local_mode
from tests.test_p11_summary_gate import _session as _heavy_session
from tests.test_p9_proxy_mr import MRBackend, SUMMARY, _call, _est, _session, _warm
from tests.test_proxy_local_mode import MODEL, _first_call, _overrides_readable, _post, env  # noqa: F401  (fixtures)

ROLLING = "ROLLING-SUMMARY-SENTINEL goal: refactor the widget"
FLAG_VARS = ("LLM_ROUTER_PROXY_MR", "LLM_ROUTER_CHUNK_STORE", "LLM_ROUTER_SUMMARY_GATE", "LLM_ROUTER_COMPACT_BG",
             "LLM_ROUTER_ROLLING_SUMMARY")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_SESSIONS_PATH", str(tmp_path / "sessions.db"))
    for v in FLAG_VARS:
        monkeypatch.delenv(v, raising=False)
    return tmp_path


def _store_rolling(tmp_path, body) -> list[str]:
    """Store the sentinel rolling summary under every session id a request of ``body`` could be keyed by."""
    sids = {"sess-p8", "default"}
    meta = (body.get("metadata") or {}).get("user_id")
    if isinstance(meta, str):
        sids.add(meta)
        try:
            sids.add(json.loads(meta).get("session_id"))
        except Exception:  # noqa: BLE001
            pass
    sids.discard(None)
    for sid in sids:
        context_summary._store(sid, 10, ROLLING, "m", "model")
    return sorted(sids)


class WireBackend:
    """A real OllamaBackend on a mocked Ollama transport: ``sent`` is the exact payload the local model gets."""

    def __init__(self):
        self.sent: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.sent.append(payload)
            is_map = payload["messages"][0]["content"].startswith("You compress one slice")
            msg = ({"role": "assistant", "content": SUMMARY} if is_map else
                   {"role": "assistant", "content": "", "tool_calls": [_call("Bash", {"command": f"ls {len(self.live)}"})]})
            line = json.dumps({"message": msg, "done": True, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 5})
            return httpx.Response(200, content=(line + "\n").encode())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.backend = pb.OllamaBackend(MODEL, client, base_url="http://127.0.0.1:11999", num_ctx=32_768, num_predict=2048)

    @property
    def maps(self):
        return [p for p in self.sent if p["messages"][0]["content"].startswith("You compress one slice")]

    @property
    def live(self):
        return [p for p in self.sent if p not in self.maps]

    async def complete(self, body, timeout_s, priority="interactive"):
        return await self.backend.complete(body, timeout_s)

    def __getattr__(self, name):
        return getattr(self.backend, name)


def _text(payload: dict) -> str:
    return json.dumps(payload["messages"])


# -- 1. MR on ---------------------------------------------------------------------------------------------

async def test_mr_on_backend_gets_the_reduced_body_and_the_row_is_consistent(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    body = _session()
    cap = local_mode.prompt_cap_tokens()
    assert _est(body) > cap
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    row = env.rows()[-1]
    mr, lm = row["mr_summary"], row["local_mode"]
    sent = backend.lives[-1]
    assert env.up.requests == [] and lm["local"] is True
    assert mr["outcome"] == "summarized" and mr["chunks"] == mr["map_calls"] == len(backend.maps) >= 1
    assert mr["tokens_before"] > cap >= mr["tokens_after"]
    assert row["est_prompt_tokens"] <= cap and _est(sent) <= cap  # the ledger sized the reduced body, not the original
    assert row["est_prompt_tokens"] < mr["tokens_before"]
    summaries = [b["text"] for b in sent["messages"][0]["content"] if b.get("type") == "text" and "<chunk-summary" in b["text"]]
    assert len(summaries) == mr["chunks"]
    assert sent["messages"][-1] == body["messages"][-1] and len(sent["messages"]) < len(body["messages"])


# -- 2. no double injection -------------------------------------------------------------------------------

async def test_rolling_summary_never_joins_the_mr_summaries_on_the_wire(env, home, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setenv("LLM_ROUTER_ROLLING_SUMMARY", "1")
    body = _session()
    sids = _store_rolling(home, body)
    for sid in sids:  # the stored row is really readable: the guarantee below is not an empty-store pass
        assert context_summary.read_summary_text(sid) == ROLLING
    wire = WireBackend()
    app = env.app(wire)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    mr = env.rows()[-1]["mr_summary"]
    assert mr["outcome"] == "summarized" and wire.maps and wire.live
    sent = wire.live[-1]
    blob = json.dumps(sent)
    assert "ROLLING-SUMMARY-SENTINEL" not in blob
    assert all("ROLLING-SUMMARY-SENTINEL" not in json.dumps(p) for p in wire.sent)  # nor in a map call
    assert blob.count("<chunk-summary") == mr["chunks"] == len(wire.maps)  # one summary per chunk, none twice
    assert env.up.requests == []


async def test_a_chunk_is_never_summarised_twice_in_one_turn(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    wire = WireBackend()
    app = env.app(wire)
    await _warm(app)
    await _post(app, copy.deepcopy(_session()))
    prompts = [p["messages"][0]["content"] for p in wire.maps]
    assert len(prompts) >= 2 and len(set(prompts)) == len(prompts)


def test_local_agent_cannot_use_the_context_pack_trim(env):  # noqa: F811
    """The only proxy code that builds a context pack is ``--trim fast`` (backends._pack_messages); local-agent
    mode, the only caller of the MR summarizer, refuses every trim but "none"."""
    with pytest.raises(ValueError) as exc:
        env.app(WireBackend(), trim="fast")
    assert type(exc.value) is ValueError
    assert str(exc.value).startswith("--serve local-agent needs") and "--trim none" in str(exc.value)


def test_the_proxy_pack_door_has_no_rolling_summary(home, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ROLLING_SUMMARY", "1")
    context_summary._store("sess-p8", 10, ROLLING, "m", "model")
    assert "proxy" not in context_pack.SUMMARY_DOORS
    assert context_pack._summary("proxy", "sess-p8") is None
    assert context_pack._summary("hook", "sess-p8") == ROLLING  # control: the hook door does read it


def _followup_session(home, monkeypatch, body):
    """``body`` with a git cwd in its system prompt, a recorded prior turn, and a reply-shaped newest prompt."""
    import subprocess

    from llm_router import session_store

    repo = home / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    body["system"] = list(body["system"]) + [{"type": "text", "text": f"Primary working directory: {repo}\n"}]
    sid = json.loads(body["metadata"]["user_id"])["session_id"]
    monkeypatch.chdir(repo)  # the log is written under the project the cwd names
    session_store.record_event(sid, "user_prompt", "PRIOR-TURN-MARKER please refactor the widget parser")
    monkeypatch.chdir(home)
    body["messages"] += [{"role": "assistant", "content": "I changed the parser."}, {"role": "user", "content": "do it"}]
    return body


SESSION_TAG = "[llm_router-session-context]"


async def test_followup_with_mr_default_gets_no_okf_block(env, home, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setenv("LLM_ROUTER_ROLLING_SUMMARY", "1")
    body = _followup_session(home, monkeypatch, _session())
    _store_rolling(home, body)
    wire = WireBackend()
    app = env.app(wire)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    row = env.rows()[-1]
    blob = json.dumps(wire.live[-1])
    assert row["mr_summary"]["outcome"] == "summarized"
    assert row["okf"]["injected"] is False and row["okf"]["reason"] == "no_room" and row["okf"]["room_tokens"] < 0
    assert blob.count("<chunk-summary") == row["mr_summary"]["chunks"]
    assert SESSION_TAG not in blob and "PRIOR-TURN-MARKER" not in blob and "ROLLING-SUMMARY-SENTINEL" not in blob


async def test_followup_with_mr_and_okf_by_window_gets_one_session_block_beside_the_summaries(env, home, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setenv("LLM_ROUTER_OKF_BY_WINDOW", "1")
    monkeypatch.setenv("LLM_ROUTER_ROLLING_SUMMARY", "1")
    body = _followup_session(home, monkeypatch, _session())
    _store_rolling(home, body)
    wire = WireBackend()
    app = env.app(wire)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    row = env.rows()[-1]
    sent = wire.live[-1]
    blob = json.dumps(sent)
    assert row["mr_summary"]["outcome"] == "summarized" and row["okf"]["injected"] is True
    assert blob.count(SESSION_TAG) == 1 and blob.count("PRIOR-TURN-MARKER") == 1  # one recall block, in the system prompt
    assert SESSION_TAG in sent["messages"][0]["content"] and SESSION_TAG not in json.dumps(sent["messages"][1:])
    assert blob.count("<chunk-summary") == row["mr_summary"]["chunks"]  # the MR summaries are all still there, once
    assert "ROLLING-SUMMARY-SENTINEL" not in blob


# -- 3. chunk store + gate --------------------------------------------------------------------------------

async def test_second_turn_hits_the_store_and_the_gate_still_runs(env, monkeypatch):  # noqa: F811
    for v in ("LLM_ROUTER_PROXY_MR", "LLM_ROUTER_CHUNK_STORE", "LLM_ROUTER_SUMMARY_GATE"):
        monkeypatch.setenv(v, "1")
    monkeypatch.setenv("LLM_ROUTER_MR_CLOSE_THRESHOLD", "36000")
    wire = WireBackend()
    app = env.app(wire)
    await _warm(app)
    await _post(app, copy.deepcopy(_heavy_session(20)))
    first = env.rows()[-1]["mr_summary"]
    n = len(wire.maps)
    assert first["outcome"] == "summarized" and n == first["map_calls"] == first["store_writes"] >= 1
    assert first["gate_checked"] == first["chunks"]
    await _post(app, copy.deepcopy(_heavy_session(20)))
    second = env.rows()[-1]["mr_summary"]
    assert second["outcome"] == "summarized" and second["map_calls"] == 0 and len(wire.maps) == n
    assert second["store_hits"] == second["chunks"] == first["chunks"] and second["store_misses"] == 0
    assert second["gate_checked"] == second["chunks"] >= 1  # the gate ran on the stored summaries too
    assert second["gate_failed"] == 0
    assert first["gate_missing_path"] > 0 and second["gate_missing_path"] == first["gate_missing_path"]  # real facts to check
    assert "pinned:" in json.dumps(wire.live[-1])  # the stored summary carries no facts: the gate pinned them on the hit
    assert env.up.requests == []


# -- 4. flags off -----------------------------------------------------------------------------------------

async def _wire_of(env_, monkeypatch, body, *, off_flags: bool, home):
    if off_flags:  # every summary flag explicitly off, and a stored rolling summary that must stay unread
        for v in FLAG_VARS:
            monkeypatch.setenv(v, "0")
        _store_rolling(home, body)
    wire = WireBackend()
    app = env_.app(wire)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    return wire, env_.rows()[-1]


async def _expected_wire(body) -> dict:
    """The Ollama payload for ``body`` with no summary path at all: the real backend called directly."""
    ref = WireBackend()
    await ref.complete(copy.deepcopy(body), 30)
    return ref.sent[-1]


async def test_flags_off_local_wire_payload_equals_the_no_summary_payload(env, home, monkeypatch):  # noqa: F811
    body = _first_call()
    expected = await _expected_wire(body)
    wire, row = await _wire_of(env, monkeypatch, body, off_flags=True, home=home)
    assert wire.maps == [] and "mr_summary" not in row
    served = [p for p in wire.live if p["messages"][-1] == expected["messages"][-1]]
    assert served, "the posted turn never reached the backend"
    assert json.dumps(wire.live[-1], sort_keys=True) == json.dumps(expected, sort_keys=True)


async def test_flags_off_over_cap_request_reaches_claude_byte_identical(env, home, monkeypatch):  # noqa: F811
    body = _session()
    _, base_row = await _wire_of(env, monkeypatch, body, off_flags=False, home=home)
    base_bytes = env.up.requests[-1].content
    env.ledger.unlink(missing_ok=True)
    env.up.requests.clear()
    off, row = await _wire_of(env, monkeypatch, body, off_flags=True, home=home)
    assert off.maps == [] and "mr_summary" not in row and row["reason"] == "prompt_over_cap"
    assert env.up.requests[-1].content == base_bytes
    assert json.loads(base_bytes)["messages"] == body["messages"]
