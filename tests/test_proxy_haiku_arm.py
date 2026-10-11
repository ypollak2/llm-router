"""D-31: the scoped Haiku experiment arm (proxy/haiku_arm.py) against ``pinned_models``.

The classifier is stubbed and Anthropic is a mocked upstream, so no test makes a network
call. Fixtures carry no prompt text beyond short synthetic strings.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from llm_router.proxy import haiku_arm as arm
from llm_router.proxy import ledger
from llm_router.proxy import server as ps
from llm_router.proxy import tiers as pt
from llm_router.proxy.cache_cost import Stickiness

FIXTURE = Path(__file__).parent / "fixtures" / "proxy" / "continuation_request.json"
OPUS, HAIKU = "claude-opus-5-5", "claude-haiku-4-5"
SHARE = 0.10


def _raw(share=SHARE, **over) -> dict:
    raw = yaml.safe_load(pt.DEFAULT_POLICY_PATH.read_text())
    raw.update({"haiku_rewrite": True, "pinned_models": [OPUS], **over})
    if share is not None:
        raw["haiku_arm_share"] = share
    return raw


def _policy(share=SHARE, **over) -> pt.ClaudeTierPolicy:
    return pt.ClaudeTierPolicy.from_dict(_raw(share, **over))


def _body(*texts: str, model=OPUS, sid: str | None = None) -> dict:
    """Plain text turns, no system-role reminders (Haiku 400s on them), client tools kept."""
    body = json.loads(FIXTURE.read_text())
    body["model"] = model
    msgs: list[dict] = []
    for i, t in enumerate(texts):
        if i:
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        msgs.append({"role": "user", "content": [{"type": "text", "text": t}]})
    body["messages"] = msgs
    # the main thread carries the sub-agent launcher; a sub-agent does not
    body["tools"] = body["tools"] + [{"name": "Agent", "description": "d", "input_schema": {"type": "object"}}]
    if sid:
        body["metadata"] = {"user_id": json.dumps({"session_id": sid})}
    return body


def _sid_for(turn_id: int, share: float = SHARE, *, inside: bool = True) -> str:
    """A session id whose bucket at ``turn_id`` is inside (or outside) the arm's share."""
    for i in range(10_000):
        sid = f"sess-{i}"
        if (arm.bucket(sid, turn_id) < share) == inside:
            return sid
    raise AssertionError("no session found")


def _classify(task="query", cx="simple", calls=None):
    async def fn(text):
        if calls is not None:
            calls.append(text)
        return {"task_type": task, "complexity": cx, "chain_head": [], "model": None}
    return fn


TURN3 = ("what is a mutex?", "and a semaphore?", "and a spinlock?")  # 5 messages: turn_id 5
SID_IN = _sid_for(5)


async def _decide(policy, body, sid, classify=None):
    return await policy.decide(body, sid, Stickiness(), classify=classify or _classify())


# ── off by default ───────────────────────────────────────────────────────────

def test_bundled_policy_ships_the_arm_off():
    assert pt.ClaudeTierPolicy.load().haiku_arm_share == 0.0
    assert "haiku_arm_share" not in yaml.safe_load(pt.DEFAULT_POLICY_PATH.read_text())


@pytest.mark.parametrize("share", [None, 0, 0.0])
async def test_off_pinned_turn_is_untouched_and_the_classifier_never_runs(share):
    calls: list = []
    d = await _decide(_policy(share), _body(*TURN3), SID_IN, _classify(calls=calls))
    assert (d.reason, d.served_model, d.arm, d.body_rewrite) == (pt.REASON_CONFIG_PINNED, OPUS, None, None)
    assert calls == []


@pytest.mark.parametrize("bad", [-0.1, 1.5, True, "0.1"])
def test_share_must_be_a_number_in_0_1(bad):
    with pytest.raises(ValueError, match="haiku_arm_share"):
        pt.ClaudeTierPolicy.from_dict(_raw(bad))


# ── the treatment ────────────────────────────────────────────────────────────

async def test_in_bucket_simple_qa_is_served_on_haiku_and_labelled():
    d = await _decide(_policy(), _body(*TURN3), SID_IN)
    assert (d.reason, d.served_model, d.tier, d.body_rewrite) == (pt.REASON_HAIKU_REWRITE, HAIKU, "haiku", "haiku")
    assert (d.arm, d.arm_assignment, d.arm_reason) == (arm.ARM_NAME, arm.ASSIGNED, "matched:query/simple")
    assert d.arm_bucket == pytest.approx(arm.bucket(SID_IN, 5), abs=1e-6) and d.arm_bucket < SHARE
    assert (d.task_type, d.complexity) == ("query", "simple")


async def test_out_of_bucket_turn_stays_pinned_without_classifying():
    calls: list = []
    sid = _sid_for(5, inside=False)
    d = await _decide(_policy(), _body(*TURN3), sid, _classify(calls=calls))
    assert (d.reason, d.served_model, d.arm) == (pt.REASON_CONFIG_PINNED, OPUS, None)
    assert calls == []


async def test_no_session_id_is_never_armed():
    d = await _decide(_policy(1.0), _body(*TURN3), None)
    assert (d.reason, d.arm) == (pt.REASON_CONFIG_PINNED, None)


# ── eligibility: in-bucket but ineligible stays pinned, logged with why ──────

def _tool_result_turn() -> dict:
    b = _body(*TURN3)
    b["messages"][-1] = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}
    return b


def _side_call() -> dict:
    b = _body(*TURN3)
    b.pop("tools")
    return b


def _with(text_idx_last: str) -> dict:
    return _body(TURN3[0], TURN3[1], text_idx_last)


def _image() -> dict:
    b = _body(*TURN3)
    b["messages"][-1]["content"].append(
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}})
    return b


INELIGIBLE = [
    ("tool_result continuation", _tool_result_turn, "query", "simple", arm.WHY_NOT_TURN_FIRST),
    ("side call (no tools)", _side_call, "query", "simple", arm.WHY_SIDE_CALL),
    ("correction", lambda: _with("no, that's wrong"), "query", "simple", arm.WHY_CORRECTION),
    ("claude: re-ask", lambda: _with("claude: again"), "query", "simple", arm.WHY_CORRECTION),
    ("opus: pin", lambda: _with("opus: think hard"), "query", "simple", arm.WHY_OPUS_PIN),
    # /model is a local command: its echo is an earlier turn, the newest one is typed (TURNFIRST-1 makes a
    # newest turn that is only the echo a harness turn)
    ("/model pin", lambda: _body(TURN3[0], "<command-name>/model</command-name>", TURN3[2]), "query", "simple",
     arm.WHY_USER_PIN),
    ("notification turn", lambda: _with("<task-notification>\n<status>completed</status>\n</task-notification>"),
     "query", "simple", arm.WHY_HARNESS_TURN),
    ("image in body", _image, "query", "simple", arm.WHY_BODY),
    ("code edit task", lambda: _body(*TURN3), "code", "simple", arm.WHY_NOT_SIMPLE_QA),
    ("moderate query", lambda: _body(*TURN3), "query", "moderate", arm.WHY_NOT_SIMPLE_QA),
    ("agentic task", lambda: _body(*TURN3), "coordinate", "simple", arm.WHY_NOT_SIMPLE_QA),
]


@pytest.mark.parametrize("name,make,task,cx,why", INELIGIBLE, ids=[i[0] for i in INELIGIBLE])
async def test_ineligible_in_bucket_turns_keep_the_pin_and_say_why(name, make, task, cx, why):
    body = make()
    sid = _sid_for(len([m for m in body["messages"] if m["role"] != "system"]))
    d = await _decide(_policy(), body, sid, _classify(task, cx))
    assert (d.reason, d.served_model, d.body_rewrite) == (pt.REASON_CONFIG_PINNED, OPUS, None)
    assert (d.arm, d.arm_assignment, d.arm_reason) == (arm.ARM_NAME, arm.INELIGIBLE, why)


async def test_first_call_and_subagent_first_call_stay_pinned():
    first = _body(TURN3[0])
    d = await _decide(_policy(), first, _sid_for(1))
    assert (d.arm_reason, d.reason) == (arm.WHY_FIRST_CALL, pt.REASON_CONFIG_PINNED)
    first["tools"] = [t for t in first["tools"] if t["name"] != "Agent"]
    d = await _decide(_policy(), first, _sid_for(1))
    assert d.arm_reason == arm.WHY_NOT_TURN_FIRST


async def test_subagent_follow_up_turn_is_not_armed():
    body = _body(*TURN3)
    body["tools"] = [t for t in body["tools"] if t["name"] != "Agent"]  # newest message is text, 5 messages
    d = await _decide(_policy(), body, SID_IN)
    assert (d.reason, d.served_model, d.arm_assignment, d.arm_reason) == (
        pt.REASON_CONFIG_PINNED, OPUS, arm.INELIGIBLE, arm.WHY_NOT_MAIN_THREAD)


@pytest.mark.parametrize("kind,armed", [("headless", False), ("harness", False), ("research", False),
                                        ("organic", True), (None, True)])
async def test_only_organic_or_untagged_sessions_are_armed(monkeypatch, kind, armed):
    from llm_router import session_kind
    monkeypatch.setattr(session_kind, "kind_of", lambda sid: kind)
    d = await _decide(_policy(), _body(*TURN3), SID_IN)
    assert (d.arm_assignment == arm.ASSIGNED) is armed
    if not armed:
        assert (d.reason, d.arm_reason) == (pt.REASON_CONFIG_PINNED, arm.WHY_SESSION_KIND)


async def test_turn_and_full_precision_bucket_are_recorded_and_recomputable():
    d = await _decide(_policy(), _body(*TURN3), SID_IN)
    assert d.arm_turn == 5 and d.arm_bucket == arm.bucket(SID_IN, d.arm_turn) < SHARE


async def test_eligible_pairs_are_configurable_and_default_to_query_simple():
    assert pt.ClaudeTierPolicy.from_dict(_raw()).haiku_arm_eligible == (("query", "simple"),)
    wide = _policy(haiku_arm_eligible=["query/simple", "analyze/simple"])
    assert (await _decide(wide, _body(*TURN3), SID_IN, _classify("analyze", "simple"))).arm_assignment == arm.ASSIGNED
    assert (await _decide(_policy(), _body(*TURN3), SID_IN, _classify("analyze", "simple"))).arm_reason == arm.WHY_NOT_SIMPLE_QA
    for bad in ([], "query/simple", ["query"], ["*/simple"]):
        with pytest.raises(ValueError, match="haiku_arm_eligible"):
            _policy(haiku_arm_eligible=bad)


async def test_classifier_failure_stays_pinned():
    async def boom(text):
        raise RuntimeError("down")
    d = await _decide(_policy(), _body(*TURN3), SID_IN, boom)
    assert (d.reason, d.arm_reason) == (pt.REASON_CONFIG_PINNED, arm.WHY_CLASSIFY_ERROR)


async def test_pin_still_applies_to_a_model_that_is_not_pinned_and_to_other_traffic():
    # an unpinned request follows the normal policy path, no arm fields
    d = await _decide(_policy(1.0, pinned_models=[]), _body(*TURN3), SID_IN)
    assert d.arm is None
    # the pinned id with the arm at 100% but a non-QA classification is still Opus
    d = await _decide(_policy(1.0), _body(*TURN3), SID_IN, _classify("code", "complex"))
    assert d.served_model == OPUS


# ── kill switches and caps ──────────────────────────────────────────────────

async def test_haiku_rewrite_off_or_guard_override_turns_the_arm_off(tmp_path, monkeypatch):
    calls: list = []
    p = pt.ClaudeTierPolicy.from_dict(_raw(haiku_rewrite=False))
    d = await _decide(p, _body(*TURN3), SID_IN, _classify(calls=calls))
    assert (d.reason, d.arm, calls) == (pt.REASON_CONFIG_PINNED, None, [])
    p = _policy()
    p.haiku_rewrite = False  # what haiku_guard.apply_override does
    assert (await _decide(p, _body(*TURN3), SID_IN)).arm is None


async def test_haiku_body_limits_still_apply():
    big = _body(*TURN3)
    big["messages"][0]["content"][0]["text"] = "x" * (4 * (pt.HAIKU_MAX_CONTEXT_TOKENS + 1000))
    d = await _decide(_policy(), big, SID_IN)
    assert (d.reason, d.arm_reason) == (pt.REASON_CONFIG_PINNED, arm.WHY_BODY)


# ── determinism and share on a fixed corpus ─────────────────────────────────

CORPUS = [(f"corpus-session-{s}", t) for s in range(2000) for t in (3, 5, 7, 9, 11, 13, 15, 17, 19, 21)]  # N=20000


def test_assignment_is_deterministic_and_the_share_is_within_tolerance():
    first = [arm.in_arm(s, t, SHARE) is not None for s, t in CORPUS]
    assert first == [arm.in_arm(s, t, SHARE) is not None for s, t in CORPUS]
    share = sum(first) / len(first)
    # binomial sd at n=20000, p=0.10 is 0.0021; 0.008 is ~3.8 sd
    assert abs(share - SHARE) < 0.008, share
    for target in (0.01, 0.5):
        got = sum(arm.in_arm(s, t, target) is not None for s, t in CORPUS) / len(CORPUS)
        assert abs(got - target) < 0.012, (target, got)
    assert all(arm.in_arm(s, t, 0.0) is None for s, t in CORPUS[:100])
    assert all(arm.in_arm(s, t, 1.0) is not None for s, t in CORPUS[:100])


async def test_policy_level_share_over_eligible_turns_matches_the_hash():
    p = _policy()
    sids = [f"corpus-session-{s}" for s in range(600)]
    treated = 0
    for s in sids:
        d = await _decide(p, _body(*TURN3), s)
        expect = arm.in_arm(s, 5, SHARE) is not None
        assert (d.arm_assignment == arm.ASSIGNED) == expect
        treated += expect
    assert 0.05 < treated / len(sids) < 0.15


# ── the ledger carries the arm, and no prompt text ──────────────────────────

class Upstream:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        model = json.loads(request.content)["model"]
        sse = ("event: message_start\ndata: " + json.dumps({"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
            "usage": {"input_tokens": 5, "output_tokens": 1}}}) + "\n\n"
               "event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n").encode()
        return httpx.Response(200, stream=httpx.ByteStream(sse), headers={"content-type": "text/event-stream"})


async def _post(app, body):
    h = {"authorization": "Bearer sk-ant-oat01-" + "Zq9" * 20, "anthropic-version": "2023-06-01",
         "content-type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        return await c.post("/v1/messages?beta=true", content=json.dumps(body), headers=h)


async def test_ledger_rows_carry_the_arm_and_no_prompt_text(tmp_path, monkeypatch):
    from llm_router.proxy import backends as pb

    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "query", "complexity": "simple", "chain_head": [], "model": None}
    monkeypatch.setattr(pb, "tier_classify", choose)
    policy_file = tmp_path / "tiers.yaml"
    policy_file.write_text(yaml.safe_dump(_raw()))
    cfg = ps.ProxyConfig(steps=frozenset(), upstream="http://127.0.0.1:9", tiers=ps.TIERS_ON,
                         tier_policy=str(policy_file), ledger_path=tmp_path / "proxy_calls.jsonl")
    up = Upstream()
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(up)))
    secret = "zebra-secret-prompt-text"
    treated = _body(TURN3[0], TURN3[1], secret, sid=SID_IN)
    outside = _body(*TURN3, sid=_sid_for(5, inside=False))
    assert (await _post(app, treated)).status_code == 200
    assert (await _post(app, outside)).status_code == 200
    sent_treated, sent_outside = (json.loads(q.content) for q in up.requests)
    assert sent_treated["model"] == HAIKU and "thinking" not in sent_treated
    assert sent_outside["model"] == OPUS
    rows = ledger.read_rows(tmp_path / "proxy_calls.jsonl")
    t, o = rows
    assert (t["tier_reason"], t["tier_arm"], t["tier_arm_assignment"], t["tier_arm_reason"]) == (
        pt.REASON_HAIKU_REWRITE, arm.ARM_NAME, arm.ASSIGNED, "matched:query/simple")
    assert t["tier_arm_bucket"] == pytest.approx(arm.bucket(SID_IN, 5), abs=1e-6)
    assert (t["served_model"], t["tier_body_rewrite"]) == (HAIKU, "haiku")
    assert o["tier_reason"] == pt.REASON_CONFIG_PINNED and "tier_arm" not in o
    assert secret not in (tmp_path / "proxy_calls.jsonl").read_text()


async def test_haiku_4xx_retry_relabels_the_arm_row_as_not_a_haiku_row(tmp_path, monkeypatch):
    from llm_router.proxy import backends as pb

    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "query", "complexity": "simple", "chain_head": [], "model": None}
    monkeypatch.setattr(pb, "tier_classify", choose)
    policy_file = tmp_path / "tiers.yaml"
    policy_file.write_text(yaml.safe_dump(_raw()))
    cfg = ps.ProxyConfig(steps=frozenset(), upstream="http://127.0.0.1:9", tiers=ps.TIERS_ON,
                         tier_policy=str(policy_file), ledger_path=tmp_path / "proxy_calls.jsonl")
    seen: list[str] = []
    ok = Upstream()

    def up(request):
        seen.append(json.loads(request.content)["model"])
        if len(seen) == 1:
            return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "no"}})
        return ok(request)
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(up)))
    assert (await _post(app, _body(*TURN3, sid=SID_IN))).status_code == 200
    assert seen == [HAIKU, OPUS]
    row = ledger.read_rows(tmp_path / "proxy_calls.jsonl")[-1]
    assert row["served_model"] == OPUS and row["tier_retry"]["status"] == 400
    assert row["tier_arm_assignment"] == arm.TREATMENT_RETRIED
    assert row["tier_arm_turn"] == 5


async def test_query_star_makes_query_moderate_eligible_and_default_does_not():
    star = _policy(0.25, haiku_arm_eligible=["query/*"])
    sid = _sid_for(5, 0.25)
    for cx in ("simple", "moderate", "complex"):
        d = await _decide(star, _body(*TURN3), sid, _classify("query", cx))
        assert (d.arm_assignment, d.arm_reason) == (arm.ASSIGNED, "matched:query/*")
    d = await _decide(star, _body(*TURN3), sid, _classify("code", "simple"))
    assert d.arm_reason == arm.WHY_NOT_SIMPLE_QA
    d = await _decide(_policy(0.25), _body(*TURN3), sid, _classify("query", "moderate"))
    assert (d.arm_assignment, d.arm_reason, d.reason) == (arm.INELIGIBLE, arm.WHY_NOT_SIMPLE_QA, pt.REASON_CONFIG_PINNED)
