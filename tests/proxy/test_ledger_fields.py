"""M0.5: proxy ledger fields (text_sha, has_mid_system, req_bytes, tier_haiku_block,
cls_*) are recorded as hashes and shapes, never as prompt text, and G3 measures each
one only on rows written at or after its first appearance.

Anthropic is a mocked upstream (httpx.MockTransport); the classifier is stubbed.
Fixture bodies are the recorded Claude Code continuation request used by
test_proxy_tiers.py, with placeholder text only where a test needs its own prompt.
"""

from __future__ import annotations

import copy
import json

import pytest

from llm_router import prompt_key
from llm_router.commands import kpi
from llm_router.proxy import backends as pb
from llm_router.proxy import tiers as pt
from llm_router.proxy.steps import newest_human_text, tier_text
from tests.test_proxy_tiers import (
    Upstream, _app, _first, _no_system_reminders, _post, _req, _rows,
)

MARKER = "UNIQUE-MARKER-9f3c1a7e-do-not-log"


@pytest.fixture
def simple(monkeypatch):
    """The classifier says simple/code, so the policy proposes Haiku."""
    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "code", "complexity": "simple", "chain_head": [], "model": None}
    monkeypatch.setattr(pb, "tier_classify", choose)


def _typed(body: dict) -> str:
    """The text of the conversation's first (typed) user message."""
    return "".join(b["text"] for b in body["messages"][0]["content"] if b.get("type") == "text")


# ── prompt_key ───────────────────────────────────────────────────────────────


def test_key_ignores_reminder_blocks_and_whitespace_and_never_truncates():
    prompt = "fix the failing test in pkg/mod01.py"
    wrapped = f"<system-reminder>\nhook context\n</system-reminder>\n  {prompt}  \n<system-reminder>x</system-reminder>"
    assert prompt_key.key(prompt) == prompt_key.key(wrapped) == prompt_key.key("fix  the failing\ttest in pkg/mod01.py")
    assert len(prompt_key.key(prompt)) == 16
    assert prompt_key.key("a") != prompt_key.key("b")
    long_a, long_b = "x" * 4999 + "A", "x" * 4999 + "B"
    assert prompt_key.key(long_a) != prompt_key.key(long_b)   # a difference at char 5,000 still counts
    assert prompt_key.key(None) == prompt_key.key("")


@pytest.mark.parametrize("n", [40, 3_000, 3_001, 5_000])
def test_hook_side_key_equals_proxy_side_key(n):
    """The hook hashes the raw typed prompt; the proxy hashes the newest human text of
    the API body, where Claude Code has wrapped hook context in reminder blocks and put
    it in the same user message. They must agree, including above tier_text's 3,000-char
    tail. M4 joins on this."""
    typed = ("word " * 2000)[:n]
    body = _first()
    body["messages"] = [{"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>\nrouter hint\n</system-reminder>"},
        {"type": "text", "text": typed},
        {"type": "text", "text": "<system-reminder>\nUSD budget\n</system-reminder>"},
    ]}]
    assert prompt_key.key(newest_human_text(body)) == prompt_key.key(typed)
    if n > 3_000:
        assert len(tier_text(body)) == 3_000 and len(newest_human_text(body)) == len(typed.strip())
        assert prompt_key.key(tier_text(body)) != prompt_key.key(typed)   # why tier_text is the wrong thing to hash


def test_tier_text_still_returns_the_last_3000_chars():
    body = _first()
    body["messages"] = [{"role": "user", "content": [{"type": "text", "text": "a" * 10 + "b" * 3_000}]}]
    assert tier_text(body) == "b" * 3_000
    assert tier_text(body, limit=5) == "b" * 5
    assert tier_text({"messages": []}) == ""


# ── haiku_block_reason ───────────────────────────────────────────────────────


def _media(body):
    body = _no_system_reminders(body)
    body["messages"][-1]["content"] = [{"type": "text", "text": "x"},
                                       {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                     "data": "AAAA"}}]
    return body


def _builtin(body):
    body = _no_system_reminders(body)
    body["tools"] = body["tools"] + [{"type": "web_search_20250305", "name": "web_search"}]
    return body


def _big(body):
    body = _no_system_reminders(body)
    body["messages"][-1]["content"] = [{"type": "text", "text": "x " * 400_000}]
    return body


@pytest.mark.parametrize("make, reason", [
    (lambda: _req(), "system_message"),
    (lambda: _no_system_reminders(_req()), "none"),
    (lambda: _media(_req()), "media"),
    (lambda: _builtin(_req()), "builtin_tools"),
    (lambda: _big(_req()), "context"),
])
def test_haiku_block_reason_names_the_first_failing_check(make, reason):
    assert pt.haiku_block_reason(make()) == reason


def test_haiku_block_reason_lists_the_system_message_first_and_agrees_with_eligibility():
    both = _builtin(_req())
    both["messages"].insert(1, {"role": "system", "content": "reminder"})
    assert pt.haiku_block_reason(both) == "system_message"
    raw = {"stickiness": {"switch_after_first_call": True}, "haiku_rewrite": True}
    import yaml
    full = yaml.safe_load(pt.DEFAULT_POLICY_PATH.read_text())
    policy = pt.ClaudeTierPolicy.from_dict({**full, **raw})
    for body in (_req(), _no_system_reminders(_req()), _media(_req()), _builtin(_req()), _big(_req()), both):
        assert policy._haiku_eligible(body) is (pt.haiku_block_reason(body) == "none")


# ── the ledger rows ──────────────────────────────────────────────────────────


async def test_plain_first_call_row_has_the_new_fields(tmp_path, simple):
    app = _app(tmp_path, Upstream())
    body = _no_system_reminders(_first())
    body["messages"] = [{"role": "user", "content": [{"type": "text", "text": "rename foo to bar"}]}]
    assert (await _post(app, body)).status_code == 200
    (row,) = _rows(tmp_path)
    assert row["text_sha"] == prompt_key.key("rename foo to bar") and len(row["text_sha"]) == 16
    assert row["has_mid_system"] is False
    assert row["req_bytes"] == len(json.dumps(body))
    assert (row["cls_source"], row["cls_ms"], row["cls_arm"], row["cls_applied"]) == ("rules", None, None, False)
    assert "tier_haiku_block" not in row   # no tier was proposed on a first call


async def test_mid_system_continuation_and_plain_continuation(tmp_path, simple):
    up = Upstream()
    app = _app(tmp_path, up)
    assert (await _post(app, _first())).status_code == 200
    with_system = _req()
    assert (await _post(app, with_system)).status_code == 200
    clean = _no_system_reminders(_req())
    assert (await _post(app, clean)).status_code == 200
    first, cont_sys, cont_clean = _rows(tmp_path)
    assert (first["has_mid_system"], cont_sys["has_mid_system"], cont_clean["has_mid_system"]) == (False, True, False)
    # a continuation's newest HUMAN text is the typed prompt, not the tool results: one key per turn
    assert first["text_sha"] == cont_sys["text_sha"] == cont_clean["text_sha"] == prompt_key.key(_typed(_first()))
    assert cont_sys["req_bytes"] == len(json.dumps(with_system)) and cont_clean["req_bytes"] == len(json.dumps(clean))
    assert cont_sys["req_bytes"] > first["req_bytes"]
    assert (cont_sys["tier_proposed"], cont_sys["tier_haiku_block"]) == ("haiku", "system_message")
    assert (cont_clean["tier_proposed"], cont_clean["tier_haiku_block"]) == ("haiku", "none")
    # the request body forwarded upstream is untouched by the new fields (they live on the row only)
    assert [json.loads(q.content).get("text_sha") for q in up.requests] == [None, None, None]


async def test_tier_haiku_block_covers_every_reason_and_is_absent_when_haiku_was_not_proposed(tmp_path, monkeypatch):
    seen = {"complexity": "simple"}

    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "code", "complexity": seen["complexity"], "chain_head": [], "model": None}

    monkeypatch.setattr(pb, "tier_classify", choose)
    app = _app(tmp_path, Upstream())
    await _post(app, _first())
    for body in (_media(_req()), _builtin(_req()), _big(_req())):
        await _post(app, body)
    seen["complexity"] = "complex"
    await _post(app, _no_system_reminders(_req()))
    rows = _rows(tmp_path)
    assert [r.get("tier_haiku_block") for r in rows] == [None, "media", "builtin_tools", "context", None]
    assert [r["tier_proposed"] for r in rows[1:4]] == ["haiku"] * 3
    assert rows[4]["tier_proposed"] != "haiku"


async def test_no_prompt_text_reaches_the_ledger(tmp_path, simple):
    app = _app(tmp_path, Upstream())
    typed = f"please look at {MARKER} and fix it"
    body = _first()
    body["messages"] = [{"role": "user", "content": [
        {"type": "text", "text": f"<system-reminder>{MARKER}-reminder</system-reminder>"},
        {"type": "text", "text": typed}]}]
    await _post(app, body)
    cont = _req()
    cont["messages"][0] = body["messages"][0]
    await _post(app, cont)
    raw = (tmp_path / "proxy_calls.jsonl").read_text()
    rows = _rows(tmp_path)
    assert len(rows) == 2 and all(r["text_sha"] == prompt_key.key(typed) for r in rows)
    assert MARKER not in raw and MARKER not in json.dumps(rows)
    assert typed not in raw


async def test_the_new_fields_do_not_change_the_forwarded_body_or_the_decision(tmp_path, simple):
    """Same request, same decision: the fields are observation only."""
    up = Upstream()
    app = _app(tmp_path, up)
    await _post(app, _first())
    r = await _post(app, _req())
    assert r.status_code == 200
    sent = [json.loads(q.content) for q in up.requests]
    assert sent[1]["messages"] == _req()["messages"]
    assert copy.deepcopy(sent[1]["tools"]) == _req()["tools"]


# ── G3 ───────────────────────────────────────────────────────────────────────

NOW = 1_800_000_000.0
_NEW = ("text_sha", "has_mid_system", "req_bytes", "cls_source", "cls_ms", "cls_arm", "cls_applied")


def _row(ts, *, late=True, **kw):
    row = {"ts": ts, "session_id": "s-1", "decision": "forwarded", "tier_mode": "conversation",
           "tier": "sonnet", "tier_reason": "policy", "tier_detail": None,
           "session_kind": "organic", "tier_policy_version": "abc123def456",
           "tier_proposed": "sonnet", "tier_retry": None}
    if late:
        row.update(text_sha="0123456789abcdef", has_mid_system=False, req_bytes=1234,
                   cls_source="rules", cls_ms=None, cls_arm=None, cls_applied=False)
    row.update(kw)
    return row


def _g3(rows):
    return kpi._g3_completeness(rows, 7, NOW, None)


def test_g3_ignores_rows_written_before_a_late_field_existed():
    old = [_row(NOW - 7200 + i, late=False) for i in range(100)]
    new = [_row(NOW - 3600 + i) for i in range(60)]
    g3 = _g3(old + new)
    assert g3["measurable"] and g3["rows_counted"] == 160
    assert g3["value"].startswith("100.0% (n=160;")                     # old rows are not penalised
    for f in ("text_sha", "has_mid_system", "req_bytes", "cls_source", "cls_applied"):
        st = g3["fields"][f]
        assert (st["applicable"], st["recorded"], st["coverage"]) == (60, 60, 1.0), f
    assert g3["field_first_seen"]["text_sha"] == kpi._iso(NOW - 3600)


def test_g3_counts_a_missing_late_field_after_its_first_appearance():
    rows = [_row(NOW - 7200 + i, late=False) for i in range(100)] + [_row(NOW - 3600 + i) for i in range(60)]
    gone = _row(NOW - 1000)
    del gone["text_sha"]
    nulled = _row(NOW - 900, has_mid_system=None)
    g3 = _g3(rows + [gone, nulled])
    assert g3["fields"]["text_sha"]["recorded"] == 61 and g3["fields"]["text_sha"]["applicable"] == 62
    assert g3["fields"]["has_mid_system"]["recorded"] == 61
    assert g3["value"].startswith(f"{160 / 162 * 100:.1f}% (n=162;")


def test_g3_null_by_design_cls_fields_and_false_booleans_are_complete():
    g3 = _g3([_row(NOW - 3600 + i) for i in range(60)])   # cls_ms/cls_arm None, has_mid_system/cls_applied False
    assert g3["value"].startswith("100.0% (n=60;")
    assert g3["fields"]["cls_ms"]["recorded"] == 60 and g3["fields"]["has_mid_system"]["recorded"] == 60


def test_g3_owes_tier_haiku_block_only_on_haiku_proposals():
    haiku = [_row(NOW - 3600 + i, tier_proposed="haiku", tier_haiku_block="none") for i in range(30)]
    other = [_row(NOW - 3000 + i) for i in range(30)]
    g3 = _g3(haiku + other)
    st = g3["fields"]["tier_haiku_block"]
    assert (st["applicable"], st["recorded"]) == (30, 30)
    missing = _g3(haiku + other + [_row(NOW - 100, tier_proposed="haiku")])
    assert missing["fields"]["tier_haiku_block"]["applicable"] == 31
    assert missing["fields"]["tier_haiku_block"]["recorded"] == 30


def test_g3_with_no_late_field_anywhere_is_unchanged():
    """Before the deploy no row carries any new key: G3 reads as it did (the late fields are not owed)."""
    g3 = _g3([_row(NOW - 3600 + i, late=False) for i in range(60)])
    assert g3["measurable"] and g3["value"].startswith("100.0% (n=60;")
    assert g3["fields"]["text_sha"]["applicable"] == 0
    assert "text_sha" not in g3["field_first_seen"]


async def test_late_fields_are_recorded_on_at_least_99_percent_of_250_mixed_requests(tmp_path, simple):
    """The M0-4 bar (>= 99% of rows carrying the fields, n >= 200), measured on the real app
    with a mocked upstream over every request shape the handler sees. This is a synthetic
    population: the live figure comes from the deployed ledger."""
    import time

    app = _app(tmp_path, Upstream())
    shapes = [_first, _req, lambda: _no_system_reminders(_req()), lambda: _media(_req()),
              lambda: _builtin(_req()), lambda: dict(_req(), tools=[])]
    expected = []
    for i in range(250):
        body = shapes[i % len(shapes)]()
        body["messages"][0] = {"role": "user", "content": [{"type": "text", "text": f"prompt number {i}"}]}
        expected.append(prompt_key.key(newest_human_text(body)))
        assert (await _post(app, body)).status_code == 200
    rows = _rows(tmp_path)
    assert len(rows) == 250
    g3 = kpi._g3_completeness(rows, 7, time.time() + 1, None)
    assert g3["rows_counted"] >= 200
    for f in ("text_sha", "has_mid_system", "req_bytes", "cls_source", "cls_ms", "cls_arm", "cls_applied",
              "tier_haiku_block"):
        st = g3["fields"][f]
        assert st["applicable"] > 0 and st["recorded"] / st["applicable"] >= 0.99, (f, st)
    assert [r["text_sha"] for r in rows] == expected        # each row keys its own request's newest human text
