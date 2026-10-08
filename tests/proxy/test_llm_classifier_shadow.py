"""M1.6: the proxy's classifier shadow seam (``proxy/llm_shadow.py``, wired in ``decide_tier``).

Rules under test: with ``LLM_ROUTER_LOCAL_CLASSIFIER=shadow`` the local verdict is
logged next to the rules' and NEVER applied; the rules' decision, the forwarded
bytes and the ledger row are what they are with the mode ``off``; a turn-first
call costs at most 30 ms (p95) however slow the classifier is; a continuation
never schedules and never waits; one text is one call; the log holds hashes,
tiers and numbers, never prompt text.

Anthropic is a mocked upstream (httpx.MockTransport). The classifier is either a
fake ``classify_async`` (timing, queue and golden tests) or the real one against a
loopback Ollama (cache and privacy tests), so no test needs Ollama.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from pathlib import Path

import pytest

from llm_router import local_classifier as lc
from llm_router.proxy import backends as pb
from llm_router.proxy.cls_input import assemble
from llm_router.proxy import llm_shadow as ls
from llm_router.proxy import tiers as pt
from tests.proxy.test_tier_decisions_golden import _shapes
from tests.test_local_classifier import MARKER, FakeOllama, _reply
from tests.test_proxy_tiers import OPUS, SONNET, Upstream, _app, _first, _post, _req, _rows

SID = "11111111-2222-3333-4444-555555555555"
LOG = "classifier_shadow.jsonl"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("LLM_ROUTER_LOCAL_CLASSIFIER", "LLM_ROUTER_CLASSIFIER_MODEL",
              "LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS", "LLM_ROUTER_OLLAMA_URL"):
        monkeypatch.delenv(k, raising=False)
    # No test may reach a real Ollama: an unreachable port, so a call that escapes the fakes fails fast.
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", "http://127.0.0.1:9")
    lc._reset_state()
    yield
    lc._reset_state()


@pytest.fixture
def simple(monkeypatch):
    """The rules classifier says moderate/code (the policy then proposes Sonnet)."""
    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "code", "complexity": "moderate", "chain_head": [], "model": None}
    monkeypatch.setattr(pb, "choose_model", choose)


class FakeClassifier:
    """Stands in for ``classify_async``. ``delay`` is how long a call takes; ``gate``
    (an Event) holds every call until set."""

    def __init__(self, monkeypatch, *, delay: float = 0.0, gate: asyncio.Event | None = None,
                 tier: str = "haiku"):
        self.calls: list[dict] = []
        self.delay, self.gate, self.tier = delay, gate, tier
        monkeypatch.setattr(lc, "classify_async", self)

    async def __call__(self, assembled, *, session_id, text_sha, **kw):
        self.calls.append({"session_id": session_id, "text_sha": text_sha, "assembled": assembled})
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        return lc.parse_verdict(_reply(8, tier=self.tier, local_eligible=True, task_type="code"), ms=12.5)


def _turn(text: str, *, sid: str = SID) -> dict:
    """A turn-first call: the conversation's first request, tools attached."""
    body = _first()
    body["messages"] = [{"role": "user", "content": [{"type": "text", "text": text}]}]
    body["metadata"] = {"user_id": json.dumps({"session_id": sid})}
    return body


def _records(tmp_path: Path, kind: str = ls.KIND) -> list[dict]:
    path = tmp_path / LOG
    if not path.exists():
        return []
    return [r for r in (json.loads(x) for x in path.read_text().splitlines()) if r["kind"] == kind]


def _shadow_app(tmp_path, up=None, **kw):
    return _app(tmp_path, up or Upstream(), classifier_shadow_path=tmp_path / LOG, **kw)


# --- 1: shadow changes nothing the rules decided ------------------------------------------

_DECISION = ("served_model", "tier", "tier_reason", "tier_proposed", "tier_switch", "tier_task_type",
             "tier_complexity", "tier_detail", "tier_body_rewrite", "tier_retry", "decision", "reason",
             "cls_source", "cls_ms", "cls_arm", "cls_applied", "text_sha", "step_class")


@pytest.mark.parametrize("rewrite", [True, False])
async def test_decisions_are_identical_with_mode_off_and_shadow_on_50_fixtures(
        tmp_path, monkeypatch, simple, rewrite):
    shapes = _shapes()
    assert len(shapes) == 25  # x2 for haiku_rewrite = 50
    fake = FakeClassifier(monkeypatch, delay=0.005)
    scheduled = 0
    for name, body in shapes:
        out = {}
        for mode in ("off", "shadow"):
            monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", mode)
            sub = tmp_path / f"{name}-{mode}"
            sub.mkdir()
            up = Upstream()
            app = _shadow_app(sub, up, policy_overrides={"haiku_rewrite": rewrite})
            assert (await _post(app, copy.deepcopy(body))).status_code == 200
            await app.state.cls_shadow.drain()
            (row,) = _rows(sub)
            assert row["cls_applied"] is False, (name, mode)
            out[mode] = ({k: row.get(k) for k in _DECISION}, [q.content for q in up.requests])
        assert out["off"] == out["shadow"], name
        scheduled += len(_records(tmp_path / f"{name}-shadow"))
    # the comparison is not vacuous: the classifier ran on the turn-first shapes
    assert len(fake.calls) == scheduled >= 8


def _mid_conversation_turn_first() -> dict:
    """A turn-first call in the middle of a conversation: earlier turns in the history,
    the last message a human's text (not a tool_result), the client's tools attached,
    Opus requested."""
    body = _first()
    body["messages"] = [
        {"role": "user", "content": [{"type": "text", "text": "first question"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "first answer"}]},
        {"role": "user", "content": [{"type": "text", "text": "now a second, different question"}]},
    ]
    body["metadata"] = {"user_id": json.dumps({"session_id": SID})}
    return body


async def test_golden_the_bytes_sent_upstream_on_a_switched_turn_first_call_are_mode_independent(
        tmp_path, monkeypatch, simple):
    """The switched path (server.py ``sent = dict(body, model=...)``): Opus requested,
    the rules policy serves Sonnet. The upstream bytes with the mode ``shadow`` must
    equal those with ``off`` AND equal the client's own body with only ``model``
    changed, so a shadow-only edit of the body (or an edit in both modes) shows."""
    fake = FakeClassifier(monkeypatch, delay=0.005)
    sent: dict[str, list[bytes]] = {}
    for mode in ("off", "shadow"):
        monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", mode)
        sub = tmp_path / mode
        sub.mkdir()
        up = Upstream()
        app = _shadow_app(sub, up)
        body = _mid_conversation_turn_first()
        warm = dict(body, messages=body["messages"][:1])  # the conversation's first call: served as requested
        assert (await _post(app, warm)).status_code == 200
        assert (await _post(app, copy.deepcopy(body))).status_code == 200
        await app.state.cls_shadow.drain()
        _, second = _rows(sub)
        # the path under test: a switch by the rules policy, at a turn-first step
        assert (second["requested_model"], second["served_model"]) == (OPUS, SONNET)
        assert second["tier_switch"] is True and second["tier_reason"] == "policy"
        assert second["step_class"] != "continuation" and second["cls_applied"] is False
        sent[mode] = [q.content for q in up.requests]
        assert sent[mode][1] == json.dumps(dict(body, model=SONNET)).encode(), mode
    # not vacuous: the shadow classified the switched call, and only in shadow mode
    shadow_rows = _rows(tmp_path / "shadow")
    assert [c["text_sha"] for c in fake.calls].count(shadow_rows[1]["text_sha"]) == 1
    assert len(_records(tmp_path / "shadow")) == 2 and not (tmp_path / "off" / LOG).exists()
    assert sent["off"] == sent["shadow"]


# --- 2: only turn-first calls schedule; a continuation never waits ------------------------


async def test_a_continuation_and_a_side_call_never_schedule(tmp_path, monkeypatch, simple):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    fake = FakeClassifier(monkeypatch)
    app = _shadow_app(tmp_path)
    assert (await _post(app, _req())).status_code == 200                     # continuation
    assert (await _post(app, dict(_req(), tools=[]))).status_code == 200     # side call
    await app.state.cls_shadow.drain()
    rows = _rows(tmp_path)
    assert [r["step_class"] for r in rows] == ["continuation", "side_call"]
    assert fake.calls == [] and _records(tmp_path) == [] and app.state.cls_shadow.drops == 0
    sched = app.state.cls_shadow
    assert sched.maybe_schedule(_req(), {"step_class": "continuation", "text_sha": "x"}) == ls.SKIPPED_CONTINUATION
    assert sched.maybe_schedule(dict(_req(), tools=[]), {"step_class": None, "text_sha": "x"}) == ls.SKIPPED_SIDE_CALL
    assert sched.maybe_schedule(_turn("hi"), {"step_class": None, "text_sha": None}) == ls.SKIPPED_NO_KEY


async def test_a_continuation_is_not_delayed_by_a_pending_classification(tmp_path, monkeypatch, simple):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    gate = asyncio.Event()
    fake = FakeClassifier(monkeypatch, gate=gate)
    app = _shadow_app(tmp_path)
    assert (await _post(app, _turn("rename the helper in util.py"))).status_code == 200
    for _ in range(300):         # the detached task is running (stuck at the gate)
        await asyncio.sleep(0.01)
        if fake.calls:
            break
    assert len(fake.calls) == 1
    t0 = time.perf_counter()
    assert (await _post(app, _req())).status_code == 200
    assert time.perf_counter() - t0 < 0.25
    assert not gate.is_set() and len(app.state.cls_shadow._tasks) == 1   # still pending: nobody waited for it
    gate.set()
    await app.state.cls_shadow.drain()
    assert len(_records(tmp_path)) == 1


# --- 3: one text, one call; and the record -------------------------------------------------


async def test_the_same_text_twice_makes_one_ollama_call_and_one_record(tmp_path, monkeypatch, simple):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    async with FakeOllama(monkeypatch, reply=_reply(9, tier="haiku", local_eligible=True)) as ollama:
        app = _shadow_app(tmp_path)
        body = _turn("rename the helper in util.py")
        assert (await _post(app, body)).status_code == 200
        await app.state.cls_shadow.drain()
        assert (await _post(app, body)).status_code == 200      # cache hit: not even scheduled
        assert (await _post(app, body)).status_code == 200
        await app.state.cls_shadow.drain()
        assert len(ollama.chat) == 1
        (rec,) = _records(tmp_path)
        assert app.state.cls_shadow.maybe_schedule(body, _rows(tmp_path)[0]) == ls.SKIPPED_KNOWN
        await app.state.cls_shadow.aclose()
    rows = _rows(tmp_path)
    assert len(rows) == 3 and all(r["cls_applied"] is False for r in rows)
    # the record carries exactly the plan's fields, joined to the ledger row by (session_id, text_sha)
    first = rows[0]
    assert rec["session_id"] == SID and rec["text_sha"] == first["text_sha"]
    assert rec["ts"] == first["ts"] and rec["step_class"] == first["step_class"]
    assert rec["rules"] == {"task_type": first["tier_task_type"], "complexity": first["tier_complexity"],
                            "tier": first["tier_proposed"]}
    assert rec["llm"] == {"tier": "local", "task_type": "code", "margin": None, "qa": False,
                          "needs_repo_context": True, "local_eligible": True, "derivation": "direct"}
    assert rec["source"] == "llm" and isinstance(rec["ms"], float)
    assert (rec["tier_reason_live"], rec["tier_live"], rec["requested_tier"], rec["policy_version"]) == (
        first["tier_reason"], first["tier"], first["requested_model"], first["tier_policy_version"])
    assert rec["session_kind"] == first["session_kind"]
    assert (rec["model"], rec["prompt_version"]) == (lc.DEFAULT_MODEL, lc.PROMPT_VERSION)


async def test_the_same_text_pending_twice_is_one_task(tmp_path, monkeypatch, simple):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    gate = asyncio.Event()
    fake = FakeClassifier(monkeypatch, gate=gate)
    app = _shadow_app(tmp_path)
    body = _turn("rename the helper in util.py")
    for _ in range(3):
        assert (await _post(app, body)).status_code == 200
    assert len(app.state.cls_shadow._tasks) == 1 and app.state.cls_shadow.drops == 0
    gate.set()
    await app.state.cls_shadow.drain()
    assert len(fake.calls) == 1 and len(_records(tmp_path)) == 1


# --- 4: a full queue drops, counts, and raises nothing -------------------------------------


async def test_a_full_queue_counts_drops_and_raises_nothing(tmp_path, monkeypatch, simple):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    gate = asyncio.Event()
    FakeClassifier(monkeypatch, gate=gate)
    app = _shadow_app(tmp_path)
    for i in range(7):
        assert (await _post(app, _turn(f"task number {i} in module{i}.py"))).status_code == 200
    sched = app.state.cls_shadow
    assert len(sched._tasks) == ls.MAX_PENDING == 4 and sched.drops == 3
    drops = _records(tmp_path, ls.KIND_DROP)
    assert len(drops) == 3 and all(d["text_sha"] and "llm" not in d for d in drops)
    assert all(r["cls_applied"] is False and r["tier_reason"] for r in _rows(tmp_path))
    gate.set()
    await sched.drain()
    assert len(_records(tmp_path)) == 4 and sched.drops == 3
    # after the queue drained a new turn is accepted again
    assert (await _post(app, _turn("a later task in later.py"))).status_code == 200
    await sched.drain()
    assert len(_records(tmp_path)) == 5


# --- 5: the decision stays under 30 ms (p95) however slow the classifier ---------------------


async def test_decision_p95_stays_under_30ms_with_a_2s_classifier(tmp_path, monkeypatch, simple):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    gate = asyncio.Event()                  # a classifier that never answers while the 200 calls are posted:
    fake = FakeClassifier(monkeypatch, gate=gate)   # a 2 s sleep let a slow runner finish the first call mid-loop
    app = _shadow_app(tmp_path)                     # (195 drops, a 5th task) because 200 posts took over 2 s
    for i in range(200):
        assert (await _post(app, _turn(f"task {i}: change line {i} of file{i}.py"))).status_code == 200
    sched = app.state.cls_shadow
    rows = _rows(tmp_path)
    secs = sorted(r["tier_decision_s"] for r in rows)
    assert len(secs) == 200 and all(isinstance(s, float) for s in secs)
    p95 = secs[int(0.95 * (len(secs) - 1))]
    assert p95 <= 0.030, f"p95 {p95 * 1000:.1f} ms"
    # the measurement is not vacuous: the 2 s classifier really was running, the rest were dropped
    assert len(sched._tasks) == 4 and sched.drops == 196 and len(fake.calls) == 1
    await sched.aclose()


async def test_a_long_history_is_assembled_off_the_request_path(tmp_path, monkeypatch, simple):
    """``assemble`` walks the whole history (45 ms on 600 messages; the fixture is 900): that must not be paid by the call."""
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    fake = FakeClassifier(monkeypatch)
    app = _shadow_app(tmp_path)
    turns = []
    for i in range(5):
        body = _turn("x")
        msgs = []
        for j in range(450):
            msgs.append({"role": "user", "content": [{"type": "text", "text": "<system-reminder>" + "r" * 3000
                                                      + "</system-reminder>\n" + "hello world " * 300}]})
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": "ok " * 1000}]})
        msgs.append({"role": "user", "content": [{"type": "text", "text": f"final prompt number {i}"}]})
        body["messages"] = msgs
        turns.append(body)
    assemble_ms = []
    for b in turns:
        t = time.perf_counter()
        assemble(b)
        assemble_ms.append((time.perf_counter() - t) * 1000)
    assert min(assemble_ms) > 30, "fixture too small to show the effect"
    for b in turns:
        assert (await _post(app, b)).status_code == 200
        await app.state.cls_shadow.drain()     # one at a time: a slow runner must not hit the pending cap
    secs = sorted(r["tier_decision_s"] for r in _rows(tmp_path))
    assert secs[len(secs) // 2] < 0.030        # the median call
    await app.state.cls_shadow.drain()
    assert len(fake.calls) == 5 and len(_records(tmp_path)) == 5
    assert all(c["assembled"].prompt.startswith("final prompt number") for c in fake.calls)


async def test_assemble_never_holds_the_event_loop(tmp_path, monkeypatch, simple):
    """The test above reads only the posting call's own decision time and drains after each post, so ``assemble``
    run inline in the task (45+ ms of walking a long history on the loop) would pass it while stalling every
    concurrent call. Here ``assemble`` is made to BLOCK until the test releases it, and the test releases it only
    after a continuation posted while it is blocked has come back. Two facts follow, neither read off a clock
    (a wall-clock gap or a ``slow_callback_duration`` also counts every moment a loaded CI runner deschedules the
    process, which failed this test on a healthy build): ``assemble`` ran on a worker thread, and the loop was
    free to serve a request while it was in flight. Inline, the loop would be blocked here, nobody could release
    it, and the wait below would time out with ``released`` False."""
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    FakeClassifier(monkeypatch)
    app = _shadow_app(tmp_path)
    loop_thread = threading.get_ident()
    in_flight, release = threading.Event(), threading.Event()
    seen: list[dict] = []

    def blocking_assemble(body):
        rec = {"thread": threading.get_ident()}
        seen.append(rec)
        in_flight.set()
        rec["released"] = release.wait(3.0)         # held until the continuation below has been served
        return assemble(body)
    monkeypatch.setattr(ls, "assemble", blocking_assemble)

    for i in range(3):
        in_flight.clear()
        release.clear()
        assert (await _post(app, _turn(f"long history turn {i}", sid=f"{i}" * 8 + SID[8:]))).status_code == 200
        assert await asyncio.to_thread(in_flight.wait, 3.0), "the assemble never started"
        assert (await _post(app, _req())).status_code == 200      # a continuation, served while it is blocked
        release.set()
        await app.state.cls_shadow.drain()
    assert len(seen) == 3
    assert all(r["thread"] != loop_thread for r in seen), "assemble ran on the event loop thread"
    assert all(r["released"] for r in seen), "the loop could not serve a request while assemble was in flight"
    assert len(_records(tmp_path)) == 3


async def test_the_scheduling_cost_is_inside_the_ledgered_decision_time(tmp_path, monkeypatch, simple):
    """G1_proxy reads ``tier_decision_s``: a slow seam must show up there, not hide after the stamp."""
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    FakeClassifier(monkeypatch)
    app = _shadow_app(tmp_path)
    real = app.state.cls_shadow._maybe_schedule

    def slow(body, row):
        time.sleep(0.05)
        return real(body, row)
    monkeypatch.setattr(app.state.cls_shadow, "_maybe_schedule", slow)
    assert (await _post(app, _turn("rename the helper in util.py"))).status_code == 200
    assert _rows(tmp_path)[0]["tier_decision_s"] >= 0.05
    await app.state.cls_shadow.drain()


# --- 6: the long-first-prompt floor still floors, and is still shadowed --------------------


async def test_the_long_floor_path_returns_the_floor_decision_and_schedules_one_call(
        tmp_path, monkeypatch, simple):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    fake = FakeClassifier(monkeypatch)
    app = _shadow_app(tmp_path)
    body = _turn("1. do this\n2. do that\n3. then this\n" + "word " * 700)
    assert (await _post(app, body)).status_code == 200
    await app.state.cls_shadow.drain()
    (row,) = _rows(tmp_path)
    assert row["tier_reason"] == pt.REASON_LONG_FIRST_PROMPT and row["cls_applied"] is False
    assert row["served_model"] == row["requested_model"]
    assert len(fake.calls) == 1
    (rec,) = _records(tmp_path)
    assert rec["tier_reason_live"] == pt.REASON_LONG_FIRST_PROMPT and rec["rules"]["tier"] == row["tier_proposed"]


# --- 7: no prompt text in the log ----------------------------------------------------------


async def test_a_prompt_marker_never_reaches_the_shadow_log(tmp_path, monkeypatch, simple):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    async with FakeOllama(monkeypatch, reply=_reply(9, reason=f"echoes {MARKER}")) as ollama:
        app = _shadow_app(tmp_path)
        body = _turn(f"fix the bug in parser.py {MARKER}")
        body["system"] = f"Primary working directory: /tmp/{MARKER}"
        assert (await _post(app, body)).status_code == 200
        await app.state.cls_shadow.drain()
        await app.state.cls_shadow.aclose()
        # the marker did reach the model (so the test can fail) ...
        assert MARKER in json.dumps(ollama.chat[0])
    # ... and nowhere in the log, even though the model echoed it in its own `reason`
    text = (tmp_path / LOG).read_text()
    assert MARKER not in text and "parser.py" not in text and '"reason"' not in text
    assert json.loads(text)["llm"]["tier"] == "sonnet"


async def test_every_record_field_is_a_hash_a_number_a_tier_or_a_code(tmp_path, monkeypatch, simple):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    FakeClassifier(monkeypatch)
    app = _shadow_app(tmp_path)
    assert (await _post(app, _turn(f"some text {MARKER}"))).status_code == 200
    await app.state.cls_shadow.drain()
    (rec,) = _records(tmp_path)

    def leaves(o):
        if isinstance(o, dict):
            for v in o.values():
                yield from leaves(v)
        else:
            yield o
    assert {v for v in leaves(rec) if isinstance(v, str) and len(v) > 40} <= {rec["policy_version"]}
    assert not any(MARKER in str(v) for v in leaves(rec))


# --- 8: nothing else moved -----------------------------------------------------------------


def test_tiers_py_and_the_classify_contract_do_not_know_the_shadow():
    src = Path(pt.__file__).read_text()
    for name in ("llm_shadow", "local_classifier", "cls_input", "classifier_shadow"):
        assert name not in src
    assert "choice = await classify(tier_text(body))" in src      # the contract M1.6 leaves alone


async def test_nothing_typed_and_cache_answers_are_not_logged(tmp_path, monkeypatch, simple):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    fake = FakeClassifier(monkeypatch)
    app = _shadow_app(tmp_path)
    reminder_only = _turn("<system-reminder>hook context only</system-reminder>")
    assert (await _post(app, reminder_only)).status_code == 200
    await app.state.cls_shadow.drain()
    assert fake.calls == [] and _records(tmp_path) == []          # no prompt: nothing to classify

    async def from_cache(assembled, **kw):                          # another caller's verdict, already logged
        v = lc.parse_verdict(_reply(8), ms=1.0)
        return lc.replace(v, source="cache", ms=0.0)
    monkeypatch.setattr(lc, "classify_async", from_cache)
    assert (await _post(app, _turn("rename the helper in util.py"))).status_code == 200
    await app.state.cls_shadow.drain()
    assert _records(tmp_path) == []


# --- modes ---------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "off", "OFF", "banana", "1", "true", ""])
async def test_off_and_unknown_values_make_zero_ollama_calls_and_write_nothing(
        tmp_path, monkeypatch, simple, value):
    async with FakeOllama(monkeypatch) as ollama:     # (it sets shadow on entry: set the value after)
        if value is None:
            monkeypatch.delenv("LLM_ROUTER_LOCAL_CLASSIFIER")
        else:
            monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", value)
        app = _shadow_app(tmp_path)
        assert (await _post(app, _turn("rename the helper in util.py"))).status_code == 200
        assert app.state.cls_shadow.maybe_schedule(_turn("x"), {"text_sha": "a", "step_class": None}) == ls.OFF
        await app.state.cls_shadow.drain()
        assert ollama.requests == 0
    assert not (tmp_path / LOG).exists() and app.state.cls_shadow._tasks == set()
    (row,) = _rows(tmp_path)
    assert row["cls_applied"] is False


async def test_mode_on_is_still_shadow_only_in_m1(tmp_path, monkeypatch, simple):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    FakeClassifier(monkeypatch, tier="opus")
    app = _shadow_app(tmp_path)
    assert (await _post(app, _turn("rename the helper in util.py"))).status_code == 200
    await app.state.cls_shadow.drain()
    (row,) = _rows(tmp_path)
    assert row["cls_applied"] is False and row["cls_source"] == "rules"
    assert row["served_model"] == row["requested_model"] or row["tier_reason"]
    (rec,) = _records(tmp_path)
    assert rec["llm"]["tier"] == "opus"


# --- failures are logged as fallbacks, never as errors -------------------------------------


async def test_a_cold_model_is_a_fallback_record_and_the_call_goes_through(tmp_path, monkeypatch, simple):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    async with FakeOllama(monkeypatch, loaded=False):
        app = _shadow_app(tmp_path)
        assert (await _post(app, _turn("rename the helper in util.py"))).status_code == 200
        await app.state.cls_shadow.drain()
        await app.state.cls_shadow.aclose()
    (rec,) = _records(tmp_path)
    assert rec["source"] == "cold" and rec["llm"]["tier"] is None


async def test_an_unreachable_ollama_is_a_timeout_record_then_a_cooldown(tmp_path, monkeypatch, simple):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS", "300")
    app = _shadow_app(tmp_path)         # port 9: refused
    for text in ("one task in a.py", "two task in b.py"):
        assert (await _post(app, _turn(text))).status_code == 200
        await app.state.cls_shadow.drain()
    recs = _records(tmp_path)
    assert [r["source"] for r in recs] == ["timeout", "timeout"]
    assert recs[1]["ms"] == 0.0        # the second one never tried: the 30 s cooldown
    await app.state.cls_shadow.aclose()


async def test_a_failing_write_never_reaches_the_request(tmp_path, monkeypatch, simple):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    FakeClassifier(monkeypatch)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    app = _app(tmp_path, Upstream(), classifier_shadow_path=blocker / LOG)
    assert (await _post(app, _turn("rename the helper in util.py"))).status_code == 200
    await app.state.cls_shadow.drain()
    assert _rows(tmp_path)[0]["cls_applied"] is False


# --- the reader ----------------------------------------------------------------------------


def test_read_records_keeps_both_kinds_and_skips_junk_and_old_rows(tmp_path):
    now = time.time()
    path = tmp_path / LOG
    path.write_text("\n".join([
        json.dumps({"kind": ls.KIND, "ts": now, "n": 1}),
        json.dumps({"kind": ls.KIND_DROP, "ts": now, "n": 2}),
        json.dumps({"kind": "other", "ts": now}),
        json.dumps({"kind": ls.KIND, "ts": now - 40 * 86400}),
        json.dumps({"kind": ls.KIND}),
        "not json", json.dumps([1, 2]),
    ]) + "\n")
    assert len(ls.read_records(path)) == 4                 # two good rows, the 40-day-old one, the one without ts
    assert [r["kind"] for r in ls.read_records(path)][:2] == [ls.KIND, ls.KIND_DROP]
    assert [r["n"] for r in ls.read_records(path, days=7)] == [1, 2]
    assert ls.read_records(tmp_path / "missing.jsonl") == []


def test_is_cached_reads_without_asking(monkeypatch):
    lc._reset_state()
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    assert lc.is_cached("s", "sha") is False
    lc._cache_put(lc._key("s", "sha", lc.DEFAULT_MODEL, "direct", lc.T_H, lc.T_S),
                  lc.parse_verdict(_reply(8)))
    assert lc.is_cached("s", "sha") is True and lc.is_cached("s", "other") is False
    assert lc.is_cached("s", "sha", model="another-model") is False
