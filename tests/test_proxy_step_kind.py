"""GE1 action census: every ledger row says what kind of step it was.

Before GE1 the ledger's ``step_class`` was ``continuation`` or null, so a census could
not tell a human turn from a side call or a sub-agent's first call (null on 15,150 of
26,111 live rows, 2026-09-28 to 2026-10-07, ``~/.llm-router/proxy_calls.jsonl`` copy).
The same field now carries ``turn_first``, ``side_call`` and ``subagent_first`` too;
``prev_tool_class`` says what the tool results it answers were; ``step_ineligible``
says why a continuation could not be served faithfully. Serving eligibility is
unchanged: ``steps.step_class(body, enabled)`` still decides it.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx

from llm_router.proxy import ledger
from llm_router.proxy import server as ps
from llm_router.proxy import steps
from llm_router.proxy import tool_classes as tc

FIXTURE = Path(__file__).parent / "fixtures" / "proxy" / "continuation_request.json"
TOKEN = "sk-ant-oat01-" + "Kd4" * 20  # fake
AGENT_TOOL = {"name": "Agent", "description": "launch a sub-agent", "input_schema": {"type": "object"}}
SECRET_CMD = "grep -rn GE1-PRIVACY-MARKER-7c1d src"


def _req() -> dict:
    return json.loads(FIXTURE.read_text())


def _first_call(*, main_thread: bool = True) -> dict:
    body = _req()
    body["messages"] = body["messages"][:2]
    if main_thread:
        body["tools"] = body["tools"] + [AGENT_TOOL]
    return body


def _later_human_turn() -> dict:
    body = _req()
    body["messages"] = body["messages"] + [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                                           {"role": "user", "content": [{"type": "text", "text": "next"}]}]
    return body


def _with_prev_tool_uses(body: dict, uses: list[tuple[str, dict]]) -> dict:
    """Replace the tool_use blocks the newest tool_result answers."""
    body = copy.deepcopy(body)
    msgs = [m for m in body["messages"] if m.get("role") != "system"]
    msgs[-2]["content"] = [{"type": "tool_use", "id": f"toolu_{i}", "name": n, "input": inp}
                           for i, (n, inp) in enumerate(uses)]
    return body


def _media(body: dict) -> dict:
    body = copy.deepcopy(body)
    tr = [m for m in body["messages"] if m["role"] == "user"][-1]["content"][0]
    tr["content"] = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}}]
    return body


# ── step_kind ────────────────────────────────────────────────────────────────


def test_every_request_gets_a_step_kind():
    assert steps.step_kind(_req()) == steps.STEP_CONTINUATION
    assert steps.step_kind(_first_call()) == steps.STEP_TURN_FIRST
    assert steps.step_kind(_later_human_turn()) == steps.STEP_TURN_FIRST
    assert steps.step_kind(dict(_req(), tools=[])) == steps.STEP_SIDE_CALL
    assert steps.step_kind({"messages": []}) == steps.STEP_SIDE_CALL
    assert set(steps.STEP_KINDS) == {"continuation", "turn_first", "side_call", "subagent_first"}


def test_a_first_call_without_the_agent_tool_is_a_sub_agent_first_call():
    # Claude Code gives sub-agents no Agent/Task tool (they cannot spawn sub-agents).
    assert steps.step_kind(_first_call(main_thread=False)) == steps.STEP_SUBAGENT_FIRST
    task = dict(_first_call(main_thread=False))
    task["tools"] = task["tools"] + [dict(AGENT_TOOL, name="Task")]
    assert steps.step_kind(task) == steps.STEP_TURN_FIRST


def test_continuation_shape_is_kept_when_it_cannot_be_served_and_the_reason_is_recorded():
    media = _media(_req())
    assert steps.step_kind(media) == steps.STEP_CONTINUATION
    assert steps.step_ineligible(media) == "media"
    forced = dict(_req(), tool_choice={"type": "any"})
    assert steps.step_ineligible(forced) == "forced_tool_choice"
    server = dict(_req(), tools=_req()["tools"] + [{"type": "web_search_20250305", "name": "web_search"}])
    assert steps.step_ineligible(server) == "server_tool"
    assert steps.step_ineligible(_req()) is None
    assert steps.step_ineligible(_first_call()) is None  # only continuations carry a reason
    # serving eligibility itself is unchanged
    assert steps.step_class(media, {steps.STEP_CONTINUATION}) is None
    assert steps.step_class(forced, {steps.STEP_CONTINUATION}) is None


def test_step_classes_that_may_be_served_are_still_only_continuation():
    assert set(steps.STEP_CLASSES) == {steps.STEP_CONTINUATION}
    try:
        ps.parse_steps("turn_first")
    except ValueError:
        pass
    else:
        raise AssertionError("turn_first must not be a servable step class")


def test_prev_tool_class_reads_the_tool_uses_the_results_answer():
    assert steps.prev_tool_class(_req()) == tc.EDIT  # the fixture's last tool_use is Edit
    assert steps.prev_tool_class(_with_prev_tool_uses(_req(), [("Read", {"file_path": "a"})])) == tc.TECHNICAL_OP
    assert steps.prev_tool_class(_with_prev_tool_uses(_req(), [("Bash", {"command": "git status"})])) == tc.TECHNICAL_OP
    assert steps.prev_tool_class(_with_prev_tool_uses(_req(), [("Bash", {"command": "make"})])) == tc.EXEC
    assert steps.prev_tool_class(_first_call()) is None
    assert steps.prev_tool_class({"messages": "junk"}) is None


# ── the ledger row ───────────────────────────────────────────────────────────


class _Upstream:
    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = {"id": "msg_ge1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 1}}
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()),
                              headers={"content-type": "application/json"})


async def _post_rows(tmp_path: Path, body: dict, **cfg) -> list[dict]:
    config = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=tmp_path / "proxy_calls.jsonl", **cfg)
    app = ps.build_app(config, client=httpx.AsyncClient(transport=httpx.MockTransport(_Upstream())),
                       backend_factory=lambda model: None)
    h = {"authorization": f"Bearer {TOKEN}", "anthropic-version": "2023-06-01", "content-type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        r = await c.post("/v1/messages", content=json.dumps(dict(body, stream=False)), headers=h)
    assert r.status_code == 200
    return ledger.read_rows(tmp_path / "proxy_calls.jsonl")


async def test_rows_carry_step_kind_prev_tool_class_and_ineligible_reason(tmp_path):
    body = _with_prev_tool_uses(_req(), [("Bash", {"command": SECRET_CMD})])
    (row,) = await _post_rows(tmp_path, body, steps=frozenset())
    assert row["step_class"] == "continuation"
    assert row["prev_tools"] == ["Bash"]
    assert row["prev_tool_class"] == tc.TECHNICAL_OP
    assert row["step_ineligible"] is None


async def test_side_call_and_first_call_rows_are_no_longer_null(tmp_path):
    (side,) = await _post_rows(tmp_path / "a", dict(_req(), tools=[]), steps=frozenset())
    assert side["step_class"] == "side_call" and side["prev_tool_class"] is None
    (first,) = await _post_rows(tmp_path / "b", _first_call(), steps=frozenset())
    assert first["step_class"] == "turn_first"
    (sub,) = await _post_rows(tmp_path / "c", _first_call(main_thread=False), steps=frozenset())
    assert sub["step_class"] == "subagent_first"


async def test_a_media_continuation_is_still_not_served_and_says_why(tmp_path):
    (row,) = await _post_rows(tmp_path, _media(_req()), steps=frozenset({"continuation"}))
    assert row["decision"] == "forwarded" and row["reason"] == "not_eligible"
    assert row["step_class"] == "continuation" and row["step_ineligible"] == "media"


async def test_bash_command_text_never_reaches_the_ledger(tmp_path):
    body = _with_prev_tool_uses(_req(), [("Bash", {"command": SECRET_CMD})])
    await _post_rows(tmp_path, body, steps=frozenset())
    raw = (tmp_path / "proxy_calls.jsonl").read_text()
    assert "GE1-PRIVACY-MARKER" not in raw and "grep -rn" not in raw


def test_continuation_cache_medians_count_continuations_only():
    usage = {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 100}
    base = {"decision": "forwarded", "session_id": "s", "usage": usage, "mixed_history": False}
    rows = [dict(base, step_class="continuation"), dict(base, step_class="turn_first"),
            dict(base, step_class="side_call"), dict(base, step_class="subagent_first")]
    s = ledger.stats(rows)
    analysis = next(v for v in s.values() if isinstance(v, dict) and "clean_n" in v)
    assert analysis["clean_n"] == 1
