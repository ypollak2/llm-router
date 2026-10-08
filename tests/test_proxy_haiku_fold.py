"""M0.7: the Haiku "fold" (``haiku_fold_system``), OFF by default.

Haiku 4.5 rejects ``role: "system"`` inside ``messages`` (400 "role 'system' is not
supported on this model", recorded in the ``_has_mid_conversation_system_message``
docstring). With ``haiku_fold_system: true`` ``translate.for_haiku`` moves every such
message into a ``<system-reminder>`` text block of an ordinary user message instead of
the policy refusing to serve the turn on Haiku.

Fixtures are shapes only (roles, block types, lengths) with placeholder text, plus the
recorded Claude Code continuation request used by test_proxy_tiers.py.
"""

from __future__ import annotations

import copy
import json

import pytest

from llm_router.proxy import tiers as pt
from llm_router.proxy.translate import for_haiku
from tests.test_proxy_tiers import (
    HAIKU, SID, Stickiness, Upstream, _app, _classify, _first, _post, _raw_policy, _req, _rows,
)


def _t(text):
    return {"type": "text", "text": text}


def _rem(text):
    return f"<system-reminder>\n{text}\n</system-reminder>"


def _user(*blocks):
    return {"role": "user", "content": list(blocks)}


def _asst(*blocks):
    return {"role": "assistant", "content": list(blocks or [_t("ok")])}


def _sys(content):
    return {"role": "system", "content": content}


def _tool_use(i):
    return {"type": "tool_use", "id": f"toolu_{i}", "name": "Read", "input": {"file_path": "f"}}


def _tool_result(i):
    return {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": f"result {i}"}


def _body(messages, **extra):
    return {"model": HAIKU, "max_tokens": 4096, "messages": messages,
            "system": [{"type": "text", "text": "top-level system"}], "tools": [{"name": "Read"}],
            "metadata": {"user_id": "u"}, "stream": True, **extra}


def _no_system(body):
    return all(m.get("role") != "system" for m in body["messages"])


def _texts(content):
    """Every text in a message's content, in order."""
    if isinstance(content, str):
        return [content]
    return [b["text"] for b in content if b.get("type") == "text"]


def _all_texts(body):
    return [t for m in body["messages"] for t in _texts(m["content"])]


# ── shapes ────────────────────────────────────────────────────────────────────

SHAPES = {
    # system (string content) between a user message and the next user message: goes first in that one
    "string_before_user": _body([
        _user(_t("q1")), _sys("S1"), _asst(_tool_use(1)), _user(_tool_result(1), _t("q2"))]),
    # the next user message holds tool_result blocks: the text goes after all of them
    "after_tool_results": _body([
        _user(_t("q1")), _asst(_tool_use(1), _tool_use(2)),
        _sys([_t("S1")]), _user(_tool_result(1), _tool_result(2))]),
    # nothing follows: appended to the previous (user) message
    "trailing": _body([_user(_t("q1")), _asst(_tool_use(1)), _user(_tool_result(1)), _sys("S1")]),
    # two in a row keep their order
    "two_in_a_row": _body([_user(_t("q1")), _sys("S1"), _sys([_t("S2")]), _asst(_t("a")), _user(_t("q2"))]),
    # list content, a message-level effort control that must be dropped, text already wrapped
    "list_with_effort": _body([
        _user(_t("q1")), {"role": "system", "content": [_t(_rem("S1")), _t("S1b")], "output_config": {"effort": "low"}},
        _asst(_t("a")), _user(_t("q2"))]),
    # user content as a plain string
    "string_user": _body([_user(_t("q1")), _asst(_t("a")), _sys("S1"), {"role": "user", "content": "q2"}]),
    # the system message sits between two assistant turns: neither neighbour is a user message
    "between_assistants": _body([_user(_t("q1")), _asst(_t("a")), _sys("S1"), _asst(_t("b"))]),
    # a leading system message
    "leading": _body([_sys("S1"), _user(_t("q1"))]),
}


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_fold_leaves_no_system_role_keeps_every_text_in_order_and_other_fields_identical(name):
    body = SHAPES[name]
    before = copy.deepcopy(body)
    out = for_haiku(body, fold_system=True)
    assert body == before, "the input body (kept for the 4xx retry) must not be mutated"
    assert _no_system(out)
    # every original text survives, in the original order (folded ones are wrapped, so substring)
    orig_texts = _all_texts(before)
    out_joined = _all_texts(out)
    pos = 0
    for text in orig_texts:
        idx = next(i for i in range(pos, len(out_joined)) if text in out_joined[i])
        pos = idx
    # nothing else moved
    assert {k: v for k, v in out.items() if k != "messages"} == {k: v for k, v in before.items() if k != "messages"}
    non_system = [m for m in before["messages"] if m["role"] != "system"]
    assert [m["role"] for m in out["messages"] if m["role"] == "assistant"] == [m["role"] for m in non_system if m["role"] == "assistant"]


def test_fold_is_a_noop_when_there_is_no_system_message():
    body = _body([_user(_t("q1")), _asst(_tool_use(1)), _user(_tool_result(1))])
    assert for_haiku(body, fold_system=True) == for_haiku(body)
    assert for_haiku(body, fold_system=True)["messages"] == body["messages"]


def test_default_for_haiku_does_not_fold():
    body = SHAPES["string_before_user"]
    assert for_haiku(body)["messages"] == body["messages"]
    assert not _no_system(for_haiku(body))


def test_string_content_goes_to_the_start_of_the_next_user_message_wrapped():
    out = for_haiku(SHAPES["string_before_user"], fold_system=True)
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant", "user"]
    # the system message was between q1 and the assistant turn: no user follows directly, so it
    # is appended to the previous user message (q1)
    assert out["messages"][0]["content"] == [_t("q1"), _t(_rem("S1"))]
    assert out["messages"][2]["content"] == [_tool_result(1), _t("q2")]


def test_next_user_message_gets_the_text_first_when_it_follows_directly():
    out = for_haiku(SHAPES["two_in_a_row"], fold_system=True)
    assert [m["role"] for m in out["messages"]] == ["user", "assistant", "user"]
    assert out["messages"][0]["content"] == [_t("q1"), _t(_rem("S1")), _t(_rem("S2"))]
    body = _body([_user(_t("q1")), _asst(_t("a")), _sys("S1"), _user(_t("q2"))])
    out = for_haiku(body, fold_system=True)
    assert out["messages"][2]["content"] == [_t(_rem("S1")), _t("q2")]


def test_text_goes_after_all_tool_results_and_tool_result_order_is_kept():
    out = for_haiku(SHAPES["after_tool_results"], fold_system=True)
    last = out["messages"][-1]["content"]
    assert [b["type"] for b in last] == ["tool_result", "tool_result", "text"]
    assert [b.get("tool_use_id") for b in last[:2]] == ["toolu_1", "toolu_2"]
    assert last[2]["text"] == _rem("S1")
    mixed = _body([_user(_t("q1")), _asst(_tool_use(1)), _sys("S1"), _user(_tool_result(1), _t("tail"))])
    out = for_haiku(mixed, fold_system=True)
    assert [b["type"] for b in out["messages"][-1]["content"]] == ["tool_result", "text", "text"]
    assert _texts(out["messages"][-1]["content"]) == [_rem("S1"), "tail"]


def test_trailing_system_message_is_appended_to_the_previous_message():
    out = for_haiku(SHAPES["trailing"], fold_system=True)
    assert [m["role"] for m in out["messages"]] == ["user", "assistant", "user"]
    assert [b["type"] for b in out["messages"][-1]["content"]] == ["tool_result", "text"]
    assert out["messages"][-1]["content"][-1]["text"] == _rem("S1")


def test_effort_control_on_the_message_is_dropped_and_wrapped_text_is_not_wrapped_twice():
    out = for_haiku(SHAPES["list_with_effort"], fold_system=True)
    blob = json.dumps(out)
    assert "effort" not in blob and "output_config" not in blob
    folded = [t for t in _all_texts(out) if "S1" in t]
    assert folded == [_rem("S1"), _rem("S1b")]
    assert _rem(_rem("S1")) not in blob


def test_string_user_content_becomes_a_list_with_the_text_block():
    out = for_haiku(SHAPES["string_user"], fold_system=True)
    assert out["messages"][-1]["content"] == [_t(_rem("S1")), _t("q2")]


def test_a_system_message_with_no_user_neighbour_becomes_its_own_user_message():
    out = for_haiku(SHAPES["between_assistants"], fold_system=True)
    assert [m["role"] for m in out["messages"]] == ["user", "assistant", "user", "assistant"]
    assert out["messages"][2]["content"] == [_t(_rem("S1"))]


def test_leading_system_message_goes_to_the_first_user_message():
    out = for_haiku(SHAPES["leading"], fold_system=True)
    assert out["messages"] == [_user(_t(_rem("S1")), _t("q1"))]


def test_fold_composes_with_the_existing_haiku_strips():
    body = _body(SHAPES["string_user"]["messages"], thinking={"type": "adaptive"}, output_config={"effort": "high"},
                 max_tokens=128_000)
    out = for_haiku(body, fold_system=True)
    assert "thinking" not in out and "output_config" not in out and out["max_tokens"] == 64_000
    assert out["system"] == body["system"], "the top-level system field (the cache prefix) is untouched"
    assert _no_system(out)


def test_real_recorded_continuation_folds_to_a_haiku_acceptable_body():
    body = _req(HAIKU)
    out = for_haiku(body, fold_system=True)
    assert _no_system(out) and len(out["messages"]) == len(body["messages"]) - 3
    assert [m["role"] for m in out["messages"]] == [m["role"] for m in body["messages"] if m["role"] != "system"]
    for m in body["messages"]:
        if m["role"] == "system":
            text = m["content"] if isinstance(m["content"], str) else "".join(_texts(m["content"]))
            assert any(text in t for t in _all_texts(out))
    assert out["system"] == body["system"] and out["tools"] == body["tools"]
    # tool_result blocks come first in the user messages that hold them
    for m in out["messages"]:
        if m["role"] == "user" and isinstance(m["content"], list):
            kinds = [b["type"] for b in m["content"]]
            if "tool_result" in kinds:
                assert kinds.index("text") > max(i for i, k in enumerate(kinds) if k == "tool_result") if "text" in kinds else True


# ── policy: flag off (default) is today's behaviour, flag on makes the body eligible ───────────────


def _policy(*, fold, rewrite=True):
    raw = _raw_policy()
    raw["stickiness"] = dict(raw.get("stickiness") or {}, switch_after_first_call=True)
    raw["haiku_rewrite"] = rewrite
    if fold is not None:
        raw["haiku_fold_system"] = fold
    return pt.ClaudeTierPolicy.from_dict(raw, conversation_level=True)


def test_bundled_policy_ships_the_fold_off():
    assert pt.ClaudeTierPolicy.load().haiku_fold_system is False
    assert _raw_policy()["haiku_fold_system"] is False


def test_the_flag_defaults_off_and_rejects_non_booleans():
    assert _policy(fold=None).haiku_fold_system is False
    assert _policy(fold=True).haiku_fold_system is True
    for bad in ("llm_haiku_arm", "yes", 1):
        with pytest.raises(ValueError, match="haiku_fold_system"):
            _policy(fold=bad)


def test_block_reason_with_the_fold_skips_system_message_but_not_the_other_checks():
    assert pt.haiku_block_reason(_req()) == "system_message"
    assert pt.haiku_block_reason(_req(), fold_system=True) == "none"
    media = _req()
    media["messages"][0]["content"] = [_t("x"), {"type": "image", "source": {"type": "base64",
                                                                             "media_type": "image/png", "data": "AA"}}]
    assert pt.haiku_block_reason(media, fold_system=True) == "media"
    builtin = _req()
    builtin["tools"] = builtin["tools"] + [{"type": "web_search_20250305", "name": "web_search"}]
    assert pt.haiku_block_reason(builtin, fold_system=True) == "builtin_tools"


async def test_fold_off_a_real_shaped_continuation_still_floors_to_sonnet():
    policy = _policy(fold=False)
    sticky = Stickiness()
    await policy.decide(_first(), SID, sticky, classify=_classify("simple"))
    d = await policy.decide(_req(), SID, sticky, classify=_classify("simple"))
    assert (d.reason, d.tier, d.body_rewrite) == (pt.REASON_THINKING_FLOOR, "sonnet", None)
    assert policy._haiku_eligible(_req()) is False


async def test_fold_on_a_real_shaped_continuation_is_served_on_haiku_with_a_rewrite():
    policy = _policy(fold=True)
    sticky = Stickiness()
    await policy.decide(_first(), SID, sticky, classify=_classify("simple"))
    d = await policy.decide(_req(), SID, sticky, classify=_classify("simple"))
    assert (d.served_model, d.reason, d.body_rewrite) == (HAIKU, pt.REASON_HAIKU_REWRITE, pt.REWRITE_HAIKU)
    assert policy._haiku_eligible(_req()) is True


async def test_fold_without_haiku_rewrite_does_nothing():
    """The fold only runs inside the rewrite path, so it cannot make a body eligible alone."""
    policy = _policy(fold=True, rewrite=False)
    assert policy.haiku_folds_system is False
    assert policy._haiku_body_ok(_req()) is False
    sticky = Stickiness()
    await policy.decide(_first(), SID, sticky, classify=_classify("simple"))
    d = await policy.decide(_req(), SID, sticky, classify=_classify("simple"))
    assert (d.tier, d.body_rewrite) == ("sonnet", None)


async def test_fold_on_a_body_with_a_system_message_but_no_thinking_still_gets_the_rewrite():
    """Haiku accepts that body's thinking/effort as sent, so only the fold needs to run:
    the decision must still carry the rewrite marker or the system role would reach Haiku."""
    policy = _policy(fold=True)
    sticky = Stickiness()
    plain = _req(thinking=None)
    plain.pop("output_config", None)
    plain.pop("context_management", None)
    await policy.decide(_first(thinking=None), SID, sticky, classify=_classify("simple"))
    d = await policy.decide(plain, SID, sticky, classify=_classify("simple"))
    assert (d.tier, d.body_rewrite) == ("haiku", pt.REWRITE_HAIKU)
    # a body with no system message and nothing for Haiku to reject needs no rewrite
    clean = copy.deepcopy(plain)
    clean["messages"] = [m for m in clean["messages"] if m["role"] != "system"]
    d2 = await _policy(fold=True).decide(clean, "s2", Stickiness(), classify=_classify("simple"))
    assert d2.body_rewrite is None


# ── through the proxy: the body Anthropic receives ───────────────────────────────────────────────


@pytest.fixture
def simple(monkeypatch):
    from llm_router.proxy import backends as pb

    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "code", "complexity": "simple", "chain_head": [], "model": None}
    monkeypatch.setattr(pb, "tier_classify", choose)


async def test_proxy_forwards_a_folded_body_to_haiku_when_the_flag_is_on(tmp_path, simple):
    up = Upstream()
    app = _app(tmp_path, up, policy_overrides={"haiku_rewrite": True, "haiku_fold_system": True})
    assert (await _post(app, _first())).status_code == 200
    r = await _post(app, _req())
    assert r.status_code == 200
    sent = [json.loads(q.content) for q in up.requests]
    assert sent[1]["model"] == HAIKU
    assert _no_system(sent[1]) and "thinking" not in sent[1] and "output_config" not in sent[1]
    row = _rows(tmp_path)[1]
    assert (row["tier"], row["tier_body_rewrite"], row["has_mid_system"], row["tier_haiku_block"]) == (
        "haiku", "haiku", True, "none")
    assert row.get("tier_retry") is None


async def test_proxy_with_the_flag_off_never_sends_a_system_role_to_haiku(tmp_path, simple):
    up = Upstream()
    app = _app(tmp_path, up, policy_overrides={"haiku_rewrite": True, "haiku_fold_system": False})
    await _post(app, _first())
    await _post(app, _req())
    sent = [json.loads(q.content) for q in up.requests]
    assert sent[1]["model"] != HAIKU
    row = _rows(tmp_path)[1]
    assert row["tier_haiku_block"] == "system_message"
