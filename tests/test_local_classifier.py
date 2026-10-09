"""The local classifier module (M1.1-M1.4): verdict v6, async-only calls, the Ollama contract.

Rules under test: the p_eval rubric strings are verbatim (md5-pinned); a verdict is
the rubric's tier plus four extra fields and a margin; an answer is valid only if
every key and every value is in range, else it is discarded; the proxy path never
blocks the event loop, asks once per (session, prompt) and backs off after a
failure; ``off`` makes zero Ollama calls; no function raises.

Ollama is a real HTTP server on a loopback port (aiohttp), so the request that
leaves the process is the request under test. Nothing here needs Ollama.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import subprocess
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from llm_router import local_classifier as lc

MARKER = "ZQX-SECRET-PROMPT-MARKER-7731"
ALIAS_TAGGED = "llmr-classifier:latest"


def _dims(total: int) -> dict[str, int]:
    """Six rubric scores (each 1-5) that sum to ``total`` (6..30)."""
    extra, out = total - 6, {}
    for d in lc.DIMS:
        add = min(4, extra)
        out[d] = 1 + add
        extra -= add
    assert sum(out.values()) == total
    return out


def _reply(total: int = 12, **over) -> str:
    data = {**_dims(total), "needs_tools": True, "tier": "sonnet", "task_type": "code", "qa": False,
            "needs_repo_context": True, "local_eligible": False, "reason": "one file, clear spec"}
    data.update(over)
    return json.dumps(data)


def _assembled(prompt: str = f"fix the bug in parser.py {MARKER}") -> lc.Assembled:
    ctx = "Working directory: /tmp/x\n\n(This is the FIRST prompt of the session: no prior context.)"
    return lc.Assembled(ctx, prompt)


class FakeOllama:
    """A loopback Ollama: /api/chat answers ``reply`` after ``delay``; /api/ps lists
    the alias when ``loaded``. A chat with no messages is a warm-up and is counted apart."""

    def __init__(self, monkeypatch, *, reply=None, delay=0.0, loaded=True, status=200,
                 ps_delay=0.0, ctx=None):
        self.mp, self.reply, self.delay = monkeypatch, reply or _reply(), delay
        self.ctx = ctx                                   # context_length /api/ps reports; None = omitted
        self.ps_delay = ps_delay
        self.loaded, self.status = loaded, status
        self.chat: list[dict] = []
        self.warm: list[dict] = []
        self.ps_calls = 0

    async def __aenter__(self) -> FakeOllama:
        async def chat(request):
            body = await request.json()
            if body.get("messages") == []:
                self.warm.append(body)
                return web.json_response({"done": True})
            self.chat.append(body)
            await asyncio.sleep(self.delay)
            return web.json_response({"message": {"role": "assistant", "content": self.reply}},
                                     status=self.status)

        async def ps(request):
            self.ps_calls += 1
            await asyncio.sleep(self.ps_delay)
            entry = {"name": ALIAS_TAGGED}
            if self.ctx is not None:
                entry["context_length"] = self.ctx
            return web.json_response({"models": [entry] if self.loaded else []})

        app = web.Application()
        app.router.add_post("/api/chat", chat)
        app.router.add_get("/api/ps", ps)
        self.server = TestServer(app)
        await self.server.start_server()
        self.mp.setenv("LLM_ROUTER_OLLAMA_URL", str(self.server.make_url("")).rstrip("/"))
        self.mp.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
        lc._reset_state()
        return self

    async def __aexit__(self, *exc) -> None:
        await lc.aclose()
        await self.server.close()
        lc._reset_state()

    @property
    def requests(self) -> int:
        return len(self.chat) + len(self.warm) + self.ps_calls


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("LLM_ROUTER_LOCAL_CLASSIFIER", "LLM_ROUTER_CLASSIFIER_MODEL",
              "LLM_ROUTER_CLASSIFIER_KEEP_ALIVE", "LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS"):
        monkeypatch.delenv(k, raising=False)
    lc._reset_state()


@pytest.fixture
def clock(monkeypatch):
    now = SimpleNamespace(t=1000.0)
    monkeypatch.setattr(lc, "_now", lambda: now.t)
    return now


async def _ask(text_sha: str = "s1", *, sid: str = "sess", **kw) -> lc.Verdict:
    return await lc.classify_async(_assembled(), session_id=sid, text_sha=text_sha, **kw)


# --- M1.2: the p_eval strings are verbatim -------------------------------------------------


@pytest.mark.parametrize("name,md5", [
    ("RUBRIC", "0bc9f3af7aa57e56d4936eebc2b70d91"),
    ("ITEM_TEMPLATE", "c55ab55028610f2e422e45350c2ff969"),
    ("SINGLE_TAIL", "889e536fc2d86861eebb19210ea1b811"),
])
def test_p_eval_strings_are_pinned(name, md5):
    # md5 of the STRING, not of rubric.py (1bcb3f44...): that file lives outside the repo
    assert hashlib.md5(getattr(lc, name).encode()).hexdigest() == md5


def test_message_layout_is_the_p_eval_one_plus_v6_extra():
    payload = lc._payload("m", _assembled("do the thing"))
    system, user = payload["messages"]
    assert system == {"role": "system", "content": lc.RUBRIC}
    expected = (lc.ITEM_TEMPLATE.format(id="turn", context=_assembled().context, prompt="do the thing")
                + "\n\n" + lc.SINGLE_TAIL + "\n\n" + lc.V6_EXTRA)
    assert user == {"role": "user", "content": expected}
    for key in ("task_type", "qa", "needs_repo_context", "local_eligible"):
        assert key in lc.V6_EXTRA


def test_plain_string_input_is_taken_as_the_prompt():
    user = lc._payload("m", "just a string")["messages"][1]["content"]
    assert "(no context)" in user and "just a string" in user


def test_braces_in_a_prompt_survive_formatting():
    user = lc._payload("m", _assembled("keep {not_a_field} as is"))["messages"][1]["content"]
    assert "{not_a_field}" in user


def test_payload_keeps_the_last_2000_chars_of_the_prompt():
    text = "".join(f"{i:03d}|" for i in range(625))          # 2500 chars, every 4-char block differs
    user = lc._payload("m", lc.Assembled("ctx", text))["messages"][1]["content"]
    assert lc.MAX_PROMPT_CHARS == 2000
    assert text[-2000:] in user and text[:500] not in user   # the tail, never the head


def test_schema_is_strict_with_enums():
    s = lc.SCHEMA
    assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
    assert all(s["properties"][d] == {"type": "integer", "minimum": 1, "maximum": 5} for d in lc.DIMS)
    assert s["properties"]["tier"]["enum"] == ["haiku", "sonnet", "opus"]
    assert s["properties"]["task_type"]["enum"] == list(lc.TASK_TYPES)


# --- M1.2: parsing, derivations, margin, local ------------------------------------------


def test_valid_answer_becomes_a_verdict():
    v = lc.parse_verdict(_reply(12), model="m", ms=5.0)
    assert (v.source, v.ok, v.tier, v.task_type, v.qa, v.needs_repo_context, v.local_eligible) == \
        ("llm", True, "sonnet", "code", False, True, False)
    assert v.dims == _dims(12) and v.margin is None and v.derivation == "direct"
    assert (v.model, v.prompt_version, v.ms, v.complexity) == ("m", "v6", 5.0, "moderate")


@pytest.mark.parametrize("content", [
    "", "not json", "[]", "null", "{}",
    json.dumps({**json.loads(_reply()), "extra": 1}),
    json.dumps({k: v for k, v in json.loads(_reply()).items() if k != "risk"}),
    _reply(tier="local"),            # the rubric cannot say local: that is derived
    _reply(tier="gpt"),
    _reply(task_type="chat"),
    _reply(qa="yes"),
    _reply(needs_tools=1),
    _reply(reason=7),
    _reply(scope=0), _reply(scope=6), _reply(scope=2.5), _reply(scope="3"), _reply(scope=True),
])
def test_invalid_answers_are_parse_errors_with_no_decision(content):
    v = lc.parse_verdict(content)
    assert v.source == "parse_error" and not v.ok
    assert (v.tier, v.task_type, v.dims, v.margin, v.qa) == (None, None, None, None, None)


def test_none_content_does_not_raise():
    assert lc.parse_verdict(None).source == "parse_error"  # type: ignore[arg-type]


def test_direct_derivation_keeps_the_models_tier_and_has_no_margin():
    v = lc.parse_verdict(_reply(30, tier="haiku"), derivation="direct")
    assert (v.tier, v.margin) == ("haiku", None)


@pytest.mark.parametrize("total,tier,margin", [
    (6, "haiku", 8), (13, "haiku", 1),     # below T_h=14
    (14, "sonnet", 0), (15, "sonnet", 1),  # [T_h, T_s)
    (16, "opus", 0), (17, "opus", 1), (30, "opus", 14),
])
def test_rule_derivation_thresholds_and_margin(total, tier, margin):
    v = lc.parse_verdict(_reply(total, tier="sonnet"), derivation="rule")
    assert (v.tier, v.margin, v.derivation) == (tier, margin, "rule")


def test_rule_derivation_thresholds_are_parameters():
    v = lc.parse_verdict(_reply(10), derivation="rule", t_h=10, t_s=12)
    assert (v.tier, v.margin) == ("sonnet", 0)


@pytest.mark.parametrize("deriv,over,tier", [
    ("direct", dict(tier="haiku", local_eligible=True, task_type="code"), "local"),
    ("direct", dict(tier="haiku", local_eligible=True, task_type="query"), "haiku"),
    ("direct", dict(tier="haiku", local_eligible=False, task_type="code"), "haiku"),
    ("direct", dict(tier="sonnet", local_eligible=True, task_type="code"), "sonnet"),
    ("rule", dict(tier="opus", local_eligible=True, task_type="code"), "local"),  # rule says haiku (sum 8)
])
def test_local_needs_haiku_and_eligible_and_code(deriv, over, tier):
    total = 8 if deriv == "rule" else 12
    assert lc.parse_verdict(_reply(total, **over), derivation=deriv).tier == tier


async def test_local_survives_the_cache_and_the_shadow_record(monkeypatch):
    reply = _reply(8, tier="haiku", local_eligible=True, task_type="code")
    async with FakeOllama(monkeypatch, reply=reply):
        first, again = await _ask(), await _ask()
    assert (first.source, first.tier) == ("llm", "local")
    assert (again.source, again.tier, again.local_eligible) == ("cache", "local", True)
    assert again.as_log() == {"tier": "local", "task_type": "code", "margin": None, "qa": False,
                              "needs_repo_context": True, "local_eligible": True, "derivation": "direct"}
    assert MARKER not in json.dumps(again.as_log())


def test_verdict_is_frozen():
    v = lc.parse_verdict(_reply())
    with pytest.raises(Exception):
        v.tier = "opus"  # type: ignore[misc]


# --- M1.4: configuration and the call contract ---------------------------------------------


def test_mode_reads_anything_but_shadow_and_on_as_off(monkeypatch):
    assert lc.mode() == "off"
    for raw, want in [("shadow", "shadow"), (" ON ", "on"), ("true", "off"), ("1", "off"), ("", "off")]:
        monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", raw)
        assert lc.mode() == want


def test_defaults_and_overrides(monkeypatch):
    assert (lc._model(), lc._keep_alive(), lc._timeout_s()) == ("llmr-classifier", "30m", 2.0)
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_MODEL", "other")
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_KEEP_ALIVE", "5m")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS", "99999")
    assert (lc._model(), lc._keep_alive(), lc._timeout_s()) == ("other", "5m", 10.0)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS", "abc")
    assert lc._timeout_s() == 2.0


async def test_request_contract(monkeypatch):
    async with FakeOllama(monkeypatch) as o:
        v = await _ask()
    assert v.source == "llm" and v.model == "llmr-classifier"
    (body,) = o.chat
    assert body["model"] == "llmr-classifier"
    assert body["format"] == lc.SCHEMA
    assert body["think"] is False and body["stream"] is False
    assert body["keep_alive"] == "30m"
    assert body["options"] == {"temperature": 0, "num_predict": 160, "num_ctx": 4096}
    assert body["messages"][0]["content"] == lc.RUBRIC


@pytest.mark.timing
async def test_model_and_keep_alive_come_from_the_environment(monkeypatch):
    async with FakeOllama(monkeypatch, loaded=False) as o:
        monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_MODEL", "llmr-classifier-38")
        monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_KEEP_ALIVE", "10m")
        v = await _ask()
        assert v.source == "cold" and v.model == "llmr-classifier-38"
        await asyncio.sleep(0.05)
    assert o.warm[0]["model"] == "llmr-classifier-38" and o.warm[0]["keep_alive"] == "10m"


@pytest.mark.timing
async def test_timeout_then_cooldown_then_recovery(monkeypatch, clock):
    async with FakeOllama(monkeypatch, delay=1.0) as o:
        slow = await _ask("a", timeout_s=0.2)
        assert (slow.source, slow.ok, slow.tier) == ("timeout", False, None) and slow.ms < 600
        o.delay = 0.0
        skipped = await _ask("b")                       # inside the 30 s cooldown: no request at all
        assert skipped.source == "timeout" and len(o.chat) == 1
        clock.t += 31
        assert (await _ask("c")).source == "llm" and len(o.chat) == 2


@pytest.mark.timing
async def test_the_budget_covers_ps_and_chat_together(monkeypatch):
    """/api/ps (0.9 s) then /api/chat (0.9 s) each fit a 1.0 s budget alone; together they
    must not: the overall budget, not a per-request one, bounds the call."""
    async with FakeOllama(monkeypatch, ps_delay=0.9, delay=0.9) as o:
        t0 = time.perf_counter()
        v = await _ask("a", timeout_s=1.0)
        elapsed = time.perf_counter() - t0
        assert o.ps_calls == 1                           # the probe ran, so the slow ps was really hit
        assert v.source == "timeout" and not v.ok
        assert elapsed <= 1.0 + 0.25, elapsed            # an unbounded total would take ~1.8 s


async def test_http_error_is_a_timeout_source(monkeypatch):
    async with FakeOllama(monkeypatch, status=500):
        assert (await _ask()).source == "timeout"


async def test_unreachable_server_is_a_timeout_source(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", "http://127.0.0.1:1")  # nothing listens
    try:
        assert (await _ask(timeout_s=0.5)).source == "timeout"
    finally:
        await lc.aclose()


@pytest.mark.parametrize("reply", ["{not json", "[]", json.dumps({"tier": "opus"}), _reply(tier="local"),
                                   _reply(scope=9)])
async def test_bad_answers_are_parse_errors_and_not_cached(monkeypatch, reply):
    async with FakeOllama(monkeypatch, reply=reply) as o:
        a, b = await _ask(), await _ask()
    assert (a.source, b.source) == ("parse_error", "parse_error") and len(o.chat) == 2


async def test_empty_message_content_is_a_parse_error(monkeypatch):
    async with FakeOllama(monkeypatch) as o:
        o.reply = None  # {"message": {"content": null}}
        v = await _ask()
    assert v.source == "parse_error"


@pytest.mark.timing
async def test_cold_model_is_reported_and_warmed_once_per_30_seconds(monkeypatch, clock):
    async with FakeOllama(monkeypatch, loaded=False) as o:
        first = await _ask("a")
        assert (first.source, first.ok) == ("cold", False)
        await asyncio.sleep(0.05)
        assert len(o.warm) == 1 and o.chat == []        # warm-up is a chat with no messages
        assert o.warm[0]["model"] == "llmr-classifier"
        # Ollama 0.32 loads a no-num_ctx request at the server default (32768), and the real
        # call's 4096 then reloads the runner: the warm-up must ask for the same context.
        assert o.warm[0]["options"]["num_ctx"] == lc.NUM_CTX == 4096
        assert (await _ask("b")).source == "cold"
        await asyncio.sleep(0.05)
        assert len(o.warm) == 1                          # same 30 s window
        clock.t += 31
        assert (await _ask("c")).source == "cold"
        await asyncio.sleep(0.05)
        assert len(o.warm) == 2


@pytest.mark.timing
async def test_resident_at_the_wrong_context_is_cold_not_a_reload_inside_the_budget(monkeypatch):
    """/api/ps shows the alias at 32768: the real call asks for 4096 and would reload (~5 s)
    under a 2 s budget. Report cold and warm it at 4096 instead."""
    async with FakeOllama(monkeypatch, ctx=32768) as o:
        v = await _ask("a")
        await asyncio.sleep(0.05)
        assert (v.source, o.chat) == ("cold", [])
        assert len(o.warm) == 1 and o.warm[0]["options"]["num_ctx"] == 4096
    lc._reset_state()
    async with FakeOllama(monkeypatch, ctx=4096) as o:
        assert (await _ask("a")).source == "llm"
    lc._reset_state()
    async with FakeOllama(monkeypatch) as o:             # server that omits context_length
        assert (await _ask("a")).source == "llm"


def test_untagged_alias_matches_latest():
    assert lc._same_model("llmr-classifier", "llmr-classifier:latest")
    assert lc._same_model("llmr-classifier:latest", "llmr-classifier")
    assert not lc._same_model("llmr-classifier", "qwen3.5:latest")


async def test_off_makes_zero_ollama_calls(monkeypatch):
    async with FakeOllama(monkeypatch) as o:
        monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "bogus")
        v = await _ask()
    assert (v.source, v.ok) == ("off", False) and o.requests == 0


async def test_residency_is_checked_once_then_remembered(monkeypatch, clock):
    async with FakeOllama(monkeypatch) as o:
        await _ask("a")
        await _ask("b")
        assert o.ps_calls == 1                           # a good answer proves the model resident
        clock.t += 21
        await _ask("c")
        assert o.ps_calls == 2


# --- M1.3: async only, never block the loop ------------------------------------------------


@pytest.mark.timing
async def test_ten_concurrent_classifications_overlap(monkeypatch):
    async with FakeOllama(monkeypatch, delay=1.0) as o:
        t0 = time.perf_counter()
        out = await asyncio.gather(*[_ask(f"s{i}") for i in range(10)])
        elapsed = time.perf_counter() - t0
    assert all(v.source == "llm" for v in out) and len(o.chat) == 10
    assert elapsed < 1.3, f"10 concurrent calls took {elapsed:.2f}s: something serialized or blocked"


@pytest.mark.timing
async def test_a_pending_classification_does_not_delay_other_work(monkeypatch):
    """Plan test 2. A ticker that wakes every 10 ms is started BEFORE a 1 s classification and
    measures how long the loop was held while it runs: a blocking call anywhere in ``_classify``
    shows up as one big gap. (The continuation decide itself arrives in M1.6, which re-asserts
    this on the real path.)"""
    async with FakeOllama(monkeypatch, delay=1.0):
        gaps, stop = [], False

        async def ticker():
            last = time.perf_counter()
            while not stop:
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        tick = asyncio.ensure_future(ticker())
        await asyncio.sleep(0.05)
        pending = asyncio.ensure_future(_ask())
        await asyncio.sleep(0.1)                         # the request is in flight
        lc.mode()                                        # what a continuation decide does here: no wait
        await asyncio.sleep(0.2)
        assert not pending.done()
        assert (await pending).source == "llm"
        stop = True
        await tick
        assert len(gaps) > 50 and max(gaps) < 0.05, (
            f"event loop was held for {max(gaps) * 1000:.0f} ms ({len(gaps)} ticks)")


class _LoopGuard:
    """Records every call to a blocking primitive made on the event-loop thread.

    Asserting on callback duration (asyncio debug ``slow_callback_duration``) measures the
    runner: a callback preempted by a busy CI box looks as slow as a blocking one (0.137 s on
    py3.11, 0.068 s on another PR, none of them blocking). What makes a path block the loop is
    *which calls it makes*, so the guard watches those instead of the clock: ``time.sleep``,
    joining a thread (``classify_local`` joins a worker; ``Thread.start`` itself is fine,
    executors call it from the loop), ``urlopen``, a socket in
    blocking mode, and starting a subprocess. Non-blocking sockets (asyncio's own) pass.

    It does NOT catch other blocking primitives: file IO / ``open``, ``sqlite3``,
    ``socket.getaddrinfo`` (DNS), lock and ``Event`` waits, ``os.system``, or an executor
    future's ``.result()``. A path that blocks the loop through one of those passes this guard."""

    def __init__(self, monkeypatch):
        self.thread = threading.get_ident()
        self.calls: list[str] = []
        mp = monkeypatch

        def wrap(owner, name, label=None, only_blocking_socket=False):
            orig = getattr(owner, name)

            def guarded(*a, **k):
                if threading.get_ident() == self.thread and not (
                        only_blocking_socket and a[0].gettimeout() == 0.0):
                    self.calls.append(label or name)
                    raise RuntimeError(f"blocking call {label or name} on the event-loop thread")
                return orig(*a, **k)

            mp.setattr(owner, name, guarded)

        wrap(time, "sleep")
        wrap(threading.Thread, "join", "Thread.join")
        wrap(urllib.request, "urlopen")
        wrap(subprocess.Popen, "__init__", "subprocess.Popen")
        for name in ("connect", "send", "sendall", "recv", "recv_into", "accept"):
            wrap(socket.socket, name, f"socket.{name}", only_blocking_socket=True)


async def test_no_blocking_call_on_the_async_path_and_the_guard_works(monkeypatch):
    """A sleeping async fake would pass a blocking implementation. So: install a guard that
    records blocking primitives called on the loop thread, prove it fires for the real sync
    path (``classify_local`` from the loop) and for an injected ``time.sleep``, then prove it
    stays silent for the whole ``classify_async`` path (ps probe + chat). No clock involved."""
    async with FakeOllama(monkeypatch, delay=0.3) as o:
        await _ask("warm-up")                            # connection set-up is not under test
        # That answer proved the model resident, so the next call would skip /api/ps and never
        # reach _is_loaded: a blocking call there would go unseen. Forget it, so the call under
        # test runs the whole path (ps probe + chat).
        lc._resident_until = 0.0
        ps_before = o.ps_calls
        with monkeypatch.context() as m:
            guard = _LoopGuard(m)
            m.setattr(lc, "_post", lambda model, assembled, timeout: _reply())
            with pytest.raises(RuntimeError, match="Thread.join"):
                lc.classify_local(_assembled(), timeout_s=2.0)       # WRONG on a loop: joins a thread
            assert "Thread.join" in guard.calls, "the guard missed a sync classify on the loop"
            guard.calls.clear()
            with pytest.raises(RuntimeError, match="blocking call sleep"):
                time.sleep(0.01)                         # what a sync call in the proxy would do
            assert guard.calls == ["sleep"]
            guard.calls.clear()
            verdict = await _ask("under-test")
            assert guard.calls == [], f"blocking call(s) on the event loop: {guard.calls}"
            assert verdict.source == "llm"
        assert o.ps_calls == ps_before + 1, "the call under test skipped _is_loaded"


async def test_nine_calls_with_one_text_make_one_http_call(monkeypatch):
    async with FakeOllama(monkeypatch, delay=0.2) as o:
        together = await asyncio.gather(*[_ask() for _ in range(6)])    # in-flight dedupe
        later = [await _ask() for _ in range(3)]                        # cache
    assert len(o.chat) == 1
    assert sorted(v.source for v in together + later) == ["cache"] * 8 + ["llm"]
    assert len({(v.tier, v.task_type) for v in together + later}) == 1


async def test_cancelling_a_waiter_does_not_cancel_the_shared_request(monkeypatch):
    async with FakeOllama(monkeypatch, delay=0.3) as o:
        leader = asyncio.ensure_future(_ask())
        await asyncio.sleep(0.05)
        follower = asyncio.ensure_future(_ask())
        await asyncio.sleep(0.05)
        follower.cancel()
        assert (await leader).source == "llm"
        assert (await _ask()).source == "cache" and len(o.chat) == 1


async def test_cache_is_keyed_by_session_and_text(monkeypatch):
    async with FakeOllama(monkeypatch) as o:
        await _ask("a", sid="s1")
        await _ask("a", sid="s2")
        await _ask("b", sid="s1")
        assert len(o.chat) == 3
        assert (await _ask("a", sid="s1")).source == "cache"
        assert (await _ask("a", sid="s1", derivation="rule")).source == "llm"   # another question


async def test_cache_ttl_and_lru(monkeypatch, clock):
    monkeypatch.setattr(lc, "CACHE_MAX", 2)
    async with FakeOllama(monkeypatch) as o:
        for sha in ("a", "b", "c"):
            await _ask(sha)
        assert len(lc._cache) == 2 and len(o.chat) == 3
        assert (await _ask("c")).source == "cache"
        assert (await _ask("a")).source == "llm"          # evicted
        assert len(o.chat) == 4
        clock.t += lc.CACHE_TTL_S + 1
        assert (await _ask("a")).source == "llm"          # expired
        assert len(o.chat) == 5


@pytest.mark.timing
async def test_a_failed_classification_is_shared_by_concurrent_callers(monkeypatch):
    async with FakeOllama(monkeypatch, delay=1.0) as o:
        out = await asyncio.gather(*[_ask(timeout_s=0.2) for _ in range(4)])
    assert [v.source for v in out] == ["timeout"] * 4 and len(o.chat) == 1


# --- the synchronous path, for the eval harness only --------------------------------------


def test_classify_local_returns_a_verdict_and_never_raises(monkeypatch):
    monkeypatch.setattr(lc, "_post", lambda model, assembled, timeout: _reply(14, tier="opus"))
    v = lc.classify_local(_assembled(), derivation="rule")
    assert (v.source, v.tier, v.margin) == ("llm", "sonnet", 0)

    def boom(model, assembled, timeout):
        raise OSError("down")

    monkeypatch.setattr(lc, "_post", boom)
    assert lc.classify_local(_assembled()).source == "timeout"
    assert lc.classify_local("   ").source == "parse_error"


@pytest.mark.timing
def test_classify_local_is_bounded_by_its_budget(monkeypatch):
    monkeypatch.setattr(lc, "_post", lambda model, assembled, timeout: time.sleep(2) or _reply())
    t0 = time.perf_counter()
    assert lc.classify_local(_assembled(), timeout_s=0.1).source == "timeout"
    assert time.perf_counter() - t0 < 1.0


async def test_the_async_path_never_uses_the_sync_one(monkeypatch):
    """classify_local blocks its thread for up to the budget: it is for the eval harness."""
    def forbidden(*a, **k):
        raise AssertionError("classify_local called from the async path")

    monkeypatch.setattr(lc, "classify_local", forbidden)
    monkeypatch.setattr(lc, "_post", forbidden)
    async with FakeOllama(monkeypatch):
        assert (await _ask()).source == "llm"
