"""PR-7 cls_embed_authority: the embedding classifier may turn a rules "Claude" into "local" on ONE soft refusal
(a plain conversation pin), behind its own flag (default off) and its own kill file. Hard limits always win.

A fake HTTP embed server only (``_Fake``: vec (1, 0) = ``local``, (0, 1) = ``claude``); Anthropic is a mocked
upstream and the serving model a fake backend. No real Ollama is contacted."""
# ruff: noqa: F811  (fixtures are imported and used as arguments)
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from llm_router.paths import state_path
from llm_router.proxy import cls_decide, cls_embed, cls_embed_authority, cls_embed_shadow, ledger, local_mode
from tests.test_cls_embed import _artifact, _Fake
from tests.test_proxy_cls_authority import pinned_env, second
from tests.test_proxy_cls_decide import Env, turn_body
from tests.test_proxy_local_mode import Breaker, _first_call, _overrides_readable, _post, _with_image  # noqa: F401

NEW_FIELDS = ("cls_opened", "cls_opened_by")


@pytest.fixture
def fx(tmp_path, monkeypatch):
    """Embed lane on (``shadow``: the backend flag), a fake embed server answering ``local``, authority OFF."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    from llm_router import session_kind  # the home moved after the autouse fixture wrote its empty overrides file
    monkeypatch.setattr(session_kind, "_OVERRIDES_CACHE", {})
    monkeypatch.setattr(session_kind, "_OVERRIDES_PROBLEM", {})
    session_kind.overrides_path().parent.mkdir(parents=True, exist_ok=True)
    session_kind.overrides_path().write_text("{}")
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED", "shadow")
    monkeypatch.delenv("LLM_ROUTER_CLS_EMBED_AUTHORITY", raising=False)
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL_URL", raising=False)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", "http://127.0.0.1:11434")
    art = tmp_path / "head.json"
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_ARTIFACT", str(art))
    _artifact(art)
    cls_embed.reset_state()
    cls_embed_shadow.reset()
    cls_embed_authority.reset()
    fake = _Fake()
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_URL", fake.url)
    for k in ("LLM_ROUTER_CLASSIFIER_AUTHORITY", "LLM_ROUTER_CLASSIFIER_ELIGIBLE_SHADOW"):
        monkeypatch.delenv(k, raising=False)
    yield SimpleNamespace(home=tmp_path, fake=fake, mp=monkeypatch)
    fake.close()
    cls_embed.reset_state()
    cls_embed_shadow.reset()
    cls_embed_authority.reset()


def on(fx):
    fx.mp.setenv("LLM_ROUTER_CLS_EMBED_AUTHORITY", "decide")


def no_embed_post(fx):
    return not any(p.startswith("POST") for p in fx.fake.paths)


# -- authority off: identical decisions ----------------------------------------------------------------


def _scenario(tmp_path):
    """Turn 1 pinned by the kill switch; turn 2 is the call under test. Returns the second row."""
    return second(pinned_env(tmp_path))


def _shape(row):
    lm = row["local_mode"]
    return (row["decision"], lm["local"], lm["reason"], row["cls_applied"], row["cls_source"])


@pytest.mark.parametrize("value", [None, "", "off", "positive", "true", "1"])
def test_flag_off_or_not_decide_is_identical_to_no_embed_at_all(fx, tmp_path, value):
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    fx.mp.delenv("LLM_ROUTER_CLS_EMBED")  # reference: the whole embed family absent
    ref = _scenario(base_dir)
    fx.mp.setenv("LLM_ROUTER_CLS_EMBED", "shadow")
    cls_embed_shadow.reset()
    if value is not None:
        fx.mp.setenv("LLM_ROUTER_CLS_EMBED_AUTHORITY", value)
    d = tmp_path / "x"
    d.mkdir()
    row = _scenario(d)
    assert _shape(row) == _shape(ref) == ("forwarded", False, "pinned_claude", False, "rules")
    assert not any(k in row for k in NEW_FIELDS) and "cls_authority" not in row["local_mode"]
    assert not any(k.startswith("cls_emb") for k in row)


def test_authority_flag_without_the_embed_backend_is_off(fx, tmp_path):
    on(fx)
    fx.mp.delenv("LLM_ROUTER_CLS_EMBED")
    assert cls_embed_authority.active() is False
    row = _scenario(tmp_path)
    assert row["local_mode"]["reason"] == "pinned_claude" and not any(k in row for k in NEW_FIELDS)
    assert no_embed_post(fx)


# -- on: abstain / timeout -> rules --------------------------------------------------------------------


def test_on_with_a_local_verdict_on_a_soft_pin_serves_local(fx, tmp_path):
    on(fx)
    e = pinned_env(tmp_path)
    row = second(e)
    assert row["decision"] == ledger.DECISION_SERVED and row["local_mode"]["local"] is True
    assert row["local_mode"]["cls_authority"] == "pin_waived"
    assert row["cls_applied"] is True and row["cls_source"] == "embed"
    assert row["cls_opened"] is True and row["cls_opened_by"] == "embed"
    assert row["cls_emb_tier"] == "local" and row["cls_emb_source"] == "embed"
    assert len(e.up.requests) == 1 and len(e.backend.calls) == 1
    assert any(p.startswith("POST") for p in fx.fake.paths)  # the embed server did receive the call


def test_on_a_claude_verdict_leaves_the_pin_and_marks_not_opened(fx, tmp_path):
    on(fx)
    fx.fake.vec = [0.0, 1.0]
    row = second(pinned_env(tmp_path))
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_applied"] is False
    assert row["cls_opened"] is False and "cls_opened_by" not in row and row["cls_emb_tier"] == "claude"


def test_on_below_floor_abstains_to_the_rules(fx, tmp_path):
    on(fx)
    fx.fake.vec = [1.0, 1.0]
    row = second(pinned_env(tmp_path))
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_opened"] is False
    assert row["cls_emb_source"] == "below_floor" and row["cls_emb_tier"] is None


@pytest.mark.timing
def test_on_a_slow_embed_times_out_and_the_rules_decide_within_the_budget(fx, tmp_path):
    on(fx)
    fx.fake.delay = 1.5
    fx.mp.setenv("LLM_ROUTER_CLS_EMBED_BUDGET_MS", "150")
    e = pinned_env(tmp_path)
    t0 = time.monotonic()
    row = second(e)
    assert time.monotonic() - t0 < 1.0  # the call did not wait the 1.5 s embed
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_opened"] is False
    assert row["cls_emb_source"] == "timeout" and row["cls_emb_tier"] is None and e.backend.calls == []


def test_on_no_artifact_abstains_to_the_rules(fx, tmp_path):
    on(fx)
    fx.mp.setenv("LLM_ROUTER_CLS_EMBED_ARTIFACT", str(tmp_path / "missing.json"))
    cls_embed.reset_state()
    row = second(pinned_env(tmp_path))
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_emb_source"] == "no_artifact"
    assert row["cls_opened"] is False


def test_on_a_failing_proxy_local_lane_blocks_the_flip(fx, tmp_path):
    on(fx)
    fx.mp.setattr(cls_decide, "proxy_lane_fails", lambda: True)
    row = second(pinned_env(tmp_path))
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_opened"] is False


def test_a_new_human_turn_after_a_failed_flip_is_classified_afresh_and_the_failed_turn_is_not_retried(fx, tmp_path):
    from tests.test_proxy_local_mode import Backend, _ollama
    on(fx)
    e = pinned_env(tmp_path, backend=Backend(_ollama("", [{"function": {"name": "Nope", "arguments": {}}}])))
    e.post(turn_body(["rename the helper", "and the other"]))
    e.post(turn_body(["rename the helper", "and the other"], loops=1))
    flip, cont = e.rows()[-2:]
    assert flip["cls_opened"] is True and flip["decision"] == ledger.DECISION_FALLBACK
    assert cont["local_mode"]["reason"] == "pinned_claude" and cont["cls_opened"] is False and len(e.backend.calls) == 1


# -- on: every hard refusal stays Claude with a LOCAL verdict in hand ----------------------------------


def _hard(fx, tmp_path, mutate):
    on(fx)
    e = pinned_env(tmp_path)
    mutate(e)
    return e, second(e)


def test_hard_kill_switch(fx, tmp_path):
    e, row = _hard(fx, tmp_path, lambda e: e.kill.write_text("x"))
    assert row["local_mode"]["reason"] == "kill_switch" and row["cls_opened"] is False and e.backend.calls == []


def test_hard_exempt_session_asks_for_no_verdict(fx, tmp_path):
    def mutate(e):
        fx.mp.setattr(local_mode, "exempt_reason", lambda *a, **k: (local_mode.EXEMPT_COORDINATOR, None))
    e, row = _hard(fx, tmp_path, mutate)
    assert row["local_mode"]["reason"] == "exempt_session" and row["cls_opened"] is False
    assert no_embed_post(fx) and e.backend.calls == []


def test_hard_prompt_cap(fx, tmp_path):
    e, row = _hard(fx, tmp_path, lambda e: fx.mp.setattr(local_mode, "PROMPT_CAP_TOKENS", 5))
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_opened"] is False and e.backend.calls == []
    assert no_embed_post(fx)


def test_hard_structural_media_pin(fx, tmp_path):
    on(fx)
    e = Env(tmp_path)
    e.post(_with_image(turn_body(["what is in this image?"])))
    row = second(e, _with_image(turn_body(["what is in this image?", "and now"])))
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_opened"] is False and e.backend.calls == []
    assert no_embed_post(fx)


def test_hard_breaker_open(fx, tmp_path):
    on(fx)
    e = pinned_env(tmp_path)
    import httpx

    from llm_router.proxy import server as ps
    from tests.test_proxy_local_mode import MODEL
    cfg = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=e.ledger, kill_switch=e.kill, model=MODEL,
                         trim="none", serve="local-agent", hedge_s=None, warm_up=False)
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(e.up)),
                       backend_factory=lambda m: e.backend, breaker_fn=Breaker(allowed=False))
    asyncio.run(_post(app, turn_body(["rename the helper", "and the other"])))
    row = e.rows()[-1]
    assert row["decision"] != ledger.DECISION_SERVED and row["cls_opened"] is False and e.backend.calls == []


def test_hard_backend_unhealthy(fx, tmp_path):
    on(fx)
    e = pinned_env(tmp_path)
    from llm_router.proxy import server as ps
    fx.mp.setattr(ps.local_mode, "decide_local", lambda body, s: (
        local_mode.LocalDecision(False, local_mode.REASON_BACKEND_UNHEALTHY) if s.cls_local else
        local_mode.LocalDecision(True, "local") if s.pinned is None else
        local_mode.LocalDecision(False, local_mode.REASON_PINNED_CLAUDE)))
    row = second(e)  # the flipped re-decision refuses (backend), so the flip is dropped and the pin stands
    assert row["local_mode"]["reason"] == "pinned_claude" and row["cls_opened"] is False and e.backend.calls == []


def test_hard_cap_guard_refuses_after_the_flip(fx, tmp_path):
    on(fx)
    fx.mp.setenv("LLM_ROUTER_CAP_GUARD_RECONCILE", "on")
    e = pinned_env(tmp_path)
    from llm_router import local_context_guard
    fx.mp.setattr(local_context_guard, "overflow_estimate", lambda *a, **k: (10 ** 6, 1000))
    row = second(e)
    assert row["local_mode"]["reason"] == "prompt_over_guard" and row["cls_opened"] is False
    assert row["cls_applied"] is False and row["cls_source"] == "rules" and e.backend.calls == []


def test_hard_cold_guard_refuses_after_the_flip(fx, tmp_path):
    on(fx)
    e = pinned_env(tmp_path)
    from llm_router.proxy import cold_guard, deadline
    fx.mp.setattr(cold_guard, "enabled", lambda: True)
    fx.mp.setattr(deadline, "compute", lambda *a, **k: {"predicted_prefill_s": 10 ** 6})
    fx.mp.setattr(cold_guard.GUARD, "is_warm", lambda obs: False)
    fx.mp.setattr(cold_guard.GUARD, "begin_warm_up", lambda obs: None)
    row = second(e)
    assert row["local_mode"]["reason"] == local_mode.REASON_COLD_OVER_DEADLINE and row["cls_opened"] is False
    assert row["cls_applied"] is False and e.backend.calls == []


def test_hard_claude_marker(fx, tmp_path):
    fx.mp.setenv("LLM_ROUTER_CLAUDE_MARKER", "on")
    on(fx)
    first = f"rename the helper {local_mode.CLAUDE_MARKER}"
    e = pinned_env(tmp_path, turn_body([first]))
    row = second(e, turn_body([first, "and the other"]))
    assert row["local_mode"]["reason"] == local_mode.REASON_CLAUDE_MARKER and row["cls_opened"] is False
    assert e.backend.calls == [] and no_embed_post(fx)


def test_hard_pins_are_not_waived_by_a_local_verdict():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    S = local_mode.LocalSession
    for reason in local_mode.HARD_PIN_REASONS:
        assert local_mode.decide_local(body, S(pinned="claude", pin_reason=reason, cls_local=True)).reason == reason
    for kw, reason in (({"kill_switch": True}, "kill_switch"), ({"exempt": "x"}, "exempt_session"),
                       ({"backend_healthy": False}, "backend_unhealthy"), ({"breaker_closed": False}, "breaker_open"),
                       ({"verdict": "veto_tier"}, "classifier_veto")):
        assert local_mode.decide_local(body, S(pinned="claude", cls_local=True, **kw)).reason == reason


def test_hard_pin_through_the_proxy_keeps_claude(fx, tmp_path):
    on(fx)
    e = pinned_env(tmp_path)
    fx.mp.setattr(local_mode.LocalModeState, "pin_reason_of", lambda self, key: local_mode.REASON_PINNED_STICKY)
    row = second(e)
    assert row["local_mode"]["reason"] == "pinned_sticky" and row["cls_opened"] is False and e.backend.calls == []
    assert no_embed_post(fx)  # a hard/structural-style pin is not even a soft pin: no verdict asked


def test_a_veto_is_not_lifted(fx, tmp_path):
    body = {"messages": [{"role": "user", "content": "hi"}]}
    d = local_mode.decide_local(body, local_mode.LocalSession(cls_local=True, verdict="veto_tier"))
    assert d.reason == "classifier_veto" and d.local is False


# -- permission checks are never local (D-594) --------------------------------------------------------


def test_permission_check_side_call_is_never_classified_or_local(fx, tmp_path):
    on(fx)
    e = Env(tmp_path)
    side = _first_call()
    side.pop("tools")
    e.post(side)
    (row,) = e.rows()
    assert row["reason"] == "not_eligible" and row["served_by"] == "anthropic"
    assert not any(k in row for k in NEW_FIELDS) and no_embed_post(fx) and e.backend.calls == []


def test_allowed_row_refuses_side_calls_and_not_eligible():
    assert cls_embed_authority.allowed_row({"step_class": "turn_first"}) is True
    assert cls_embed_authority.allowed_row({"step_class": "side_call"}) is False
    assert cls_embed_authority.allowed_row({"reason": "not_eligible"}) is False


def test_eligible_verdict_embed_refuses_a_side_call_row(fx):
    on(fx)
    body = turn_body(["rename the helper"])
    row = {"supported": True, "step_class": "side_call", "session_id": "s", "cls_turn_idx": 1, "cls_turn_sha": "x"}
    out = asyncio.run(cls_decide.eligible_verdict(body, row, cls_decide.TurnDecisionStore(), None, embed=True))
    assert out["decided"] is False and no_embed_post(fx)


# -- kill file -----------------------------------------------------------------------------------------


def test_kill_file_stops_authority_per_call_without_a_restart(fx, tmp_path):
    on(fx)
    e = pinned_env(tmp_path)
    state_path(cls_embed_authority.KILL_FILE).write_text("x")
    assert cls_embed_authority.active() is False and cls_embed.enabled() is True  # authority only; shadow lane stays
    row = second(e)
    assert row["local_mode"]["reason"] == "pinned_claude" and not any(k in row for k in NEW_FIELDS)
    assert no_embed_post(fx) and e.backend.calls == []
    state_path(cls_embed_authority.KILL_FILE).unlink()
    assert cls_embed_authority.active() is True


def test_kill_file_mid_flight_abstains(fx):
    on(fx)
    snap = {"messages": turn_body(["fix it"])["messages"], "system": None}
    state_path(cls_embed_authority.KILL_FILE).write_text("x")
    v = asyncio.run(cls_embed_authority.verdict_for(snap))
    assert v.tier is None and v.source == cls_embed.SRC_KILLED and no_embed_post(fx)


# -- budget, executor, bounded exit (F2) ---------------------------------------------------------------


@pytest.mark.timing
def test_verdict_for_is_hard_budgeted_and_uses_its_own_executor(fx):
    on(fx)
    fx.fake.delay = 1.0
    fx.mp.setenv("LLM_ROUTER_CLS_EMBED_BUDGET_MS", "120")
    names = []
    real = cls_embed.classify
    fx.mp.setattr(cls_embed, "classify", lambda t: names.append(threading.current_thread().name) or real(t))
    snap = {"messages": turn_body(["fix the bug"])["messages"], "system": None}

    async def go():
        t0 = time.monotonic()
        v = await cls_embed_authority.verdict_for(snap)
        return v, time.monotonic() - t0

    v, took = asyncio.run(go())
    assert v.tier is None and v.source == cls_embed.SRC_TIMEOUT and took < 0.6
    assert names and all(n.startswith("cls-embed-authority") for n in names)


def test_pending_cap_abstains_instead_of_queueing(fx):
    on(fx)
    fx.mp.setenv("LLM_ROUTER_CLS_EMBED_BUDGET_MS", "50")
    gate = threading.Event()
    fx.mp.setattr(cls_embed, "classify", lambda t: gate.wait(5) and cls_embed.Verdict(None, "error", 0.0))
    snap = {"messages": turn_body(["fix the bug"])["messages"], "system": None}

    async def go():
        outs = [await cls_embed_authority.verdict_for(snap) for _ in range(cls_embed_authority.MAX_PENDING + 2)]
        gate.set()
        return outs

    outs = asyncio.run(go())
    assert [o.source for o in outs[:cls_embed_authority.MAX_PENDING]] == ["timeout"] * cls_embed_authority.MAX_PENDING
    assert [o.source for o in outs[cls_embed_authority.MAX_PENDING:]] == ["queue_full"] * 2
    assert all(o.tier is None for o in outs)


class _Hang:
    """A TCP server for F2: ``mode`` hang = accept and never answer; trickle = answer one byte at a time."""

    def __init__(self, mode):
        self.mode, self.stop = mode, False
        self.s = socket.socket()
        self.s.bind(("127.0.0.1", 0))
        self.s.listen(8)
        self.url = f"http://127.0.0.1:{self.s.getsockname()[1]}"
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        self.s.settimeout(0.2)
        while not self.stop:
            try:
                c, _ = self.s.accept()
            except OSError:
                continue
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            c.settimeout(5)
            c.recv(65536)
            if self.mode == "trickle":
                body = b'{"embeddings": [[1.0, 0.0]]}' + b" " * 400
                c.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n")
                for i in range(len(body)):
                    c.sendall(body[i:i + 1])
                    time.sleep(0.05)
            else:
                time.sleep(5)
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self.stop = True
        self.s.close()


@pytest.mark.timing
@pytest.mark.parametrize("mode", ["hang", "trickle"])
def test_pool_thread_exits_within_the_budget_window(fx, mode):
    """F2: the (non-daemon) pool thread ends at the embed deadline even when the server never answers or trickles
    bytes (each socket wait alone would restart the timeout); the slot frees, so exit and shutdown are bounded."""
    on(fx)
    srv = _Hang(mode)
    try:
        fx.mp.setenv("LLM_ROUTER_CLS_EMBED_URL", srv.url)
        fx.mp.setenv("LLM_ROUTER_CLS_EMBED_BUDGET_MS", "200")
        snap = {"messages": turn_body(["fix the bug"])["messages"], "system": None}

        async def go():
            v = await cls_embed_authority.verdict_for(snap)
            t0 = time.monotonic()
            lane = cls_embed_authority._lane
            while lane.pending and time.monotonic() - t0 < 3.0:
                await asyncio.sleep(0.02)
            return v, lane.pending, time.monotonic() - t0

        v, pending, drain_s = asyncio.run(go())
        assert v.tier is None and v.source == cls_embed.SRC_TIMEOUT
        assert pending == 0 and drain_s < 1.0  # the thread ended well inside the 5 s the server would have held it
    finally:
        srv.close()


def test_after_aclose_no_new_lane_is_built(fx):
    on(fx)
    snap = {"messages": turn_body(["fix the bug"])["messages"], "system": None}

    async def go():
        await cls_embed_authority.aclose()
        return await cls_embed_authority.verdict_for(snap)

    v = asyncio.run(go())
    assert v.tier is None and cls_embed_authority._lane is None
    cls_embed_authority.reopen()
    assert asyncio.run(cls_embed_authority.verdict_for(snap)).tier == "local"


# -- F1 / F3: shadow ordering and the canary ------------------------------------------------------------


def _shadow_recs(home):
    p = home / cls_embed_shadow.SHADOW_NAME
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _post_drain(e, body):
    async def go():
        r = await _post(e.app, body)
        await cls_embed_shadow.drain()
        return r
    return asyncio.run(go())


def test_f1_flipped_call_records_both_rules_and_final_decision(fx, tmp_path):
    on(fx)
    e = pinned_env(tmp_path)
    cls_embed_shadow.reset()
    _post_drain(e, turn_body(["rename the helper", "and the other"]))
    row = e.rows()[-1]
    assert row["cls_opened"] is True
    (rec,) = _shadow_recs(fx.home)
    assert rec["rules_decision"] == "pinned_claude" and rec["final_decision"] == "local" and rec["flipped"] is True


def test_f1_unflipped_call_records_equal_decisions(fx, tmp_path):
    fx.fake.vec = [0.0, 1.0]
    on(fx)
    e = pinned_env(tmp_path)
    cls_embed_shadow.reset()
    _post_drain(e, turn_body(["rename the helper", "and the other"]))
    (rec,) = _shadow_recs(fx.home)
    assert rec["rules_decision"] == rec["final_decision"] == "pinned_claude" and rec["flipped"] is False


def test_f3_end_to_end_canary_never_reaches_the_shadow_file_or_the_ledger(fx, tmp_path):
    on(fx)
    uniq = "ZQX-CANARY-7f3a91-do-not-persist"
    e = pinned_env(tmp_path, turn_body([f"first {uniq}"]))
    cls_embed_shadow.reset()
    _post_drain(e, turn_body([f"first {uniq}", f"second {uniq}"]))
    assert any(uniq in json.dumps(b) for b in fx.fake.bodies)  # control: the embed server did get the text
    assert e.rows()[-1]["cls_opened"] is True and _shadow_recs(fx.home)  # control: both paths ran
    for p in list(fx.home.rglob("*")):
        if p.is_file() and p.name not in ("head.json",):
            try:
                text = p.read_text(errors="ignore")
            except OSError:
                continue
            if "ZQX-CANARY" in text:
                # a body the harness itself writes (the mocked-upstream fixtures) is not a persisted router file
                assert False, f"canary text persisted in {p.name}"
    raw_ledger = e.ledger.read_text()
    assert uniq not in raw_ledger and uniq not in (fx.home / cls_embed_shadow.SHADOW_NAME).read_text()


def test_f3_shadow_after_aclose_and_empty_text_are_skipped(fx):
    cls_embed_shadow.reset()

    async def go():
        body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}], "system": None}
        row = {"ts": 1.0, "session_id": "s", "text_sha": "", "turn_origin": "typed", "step_class": "turn_first"}
        a = cls_embed_shadow.schedule(body, row, SimpleNamespace(local=True, reason=None))
        row["text_sha"] = "h"
        await cls_embed_shadow.aclose()
        b = cls_embed_shadow.schedule(body, row, SimpleNamespace(local=True, reason=None))
        return a, b

    assert asyncio.run(go()) == (cls_embed_shadow.SKIP_NO_TEXT, cls_embed_shadow.SKIP_CLOSED)
    assert cls_embed_shadow._lane is None
