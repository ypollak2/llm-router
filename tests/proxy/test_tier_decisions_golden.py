"""Golden: tier decisions on 50 fixtures are unchanged by the M0.5 ledger fields.

M0.5 adds ledger fields only. It must not move a single tier decision. The golden
file was recorded from ``origin/main`` at c2ed278, BEFORE any M0.5 code existed.
Each case builds a request shape (placeholder text only), runs ``decide`` under a
deterministic classifier stub and compares the whole decision tuple.

Regenerate ONLY for an intended routing change:
``TIER_GOLDEN_UPDATE=1 pytest tests/proxy/test_tier_decisions_golden.py``.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import yaml

from llm_router.proxy import tiers as pt
from llm_router.proxy.cache_cost import Stickiness

FIXTURE = Path(__file__).parent.parent / "fixtures" / "proxy" / "continuation_request.json"
GOLDEN = Path(__file__).parent.parent / "fixtures" / "proxy" / "golden_tier_decisions_m05.json"
SID = "11111111-2222-3333-4444-555555555555"
OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"


def _base(model=OPUS, thinking="adaptive") -> dict:
    body = json.loads(FIXTURE.read_text())
    body["model"] = model
    if thinking is None:
        body.pop("thinking", None)
    else:
        body["thinking"] = {"type": thinking}
    return body


def _first(model=OPUS, thinking="adaptive") -> dict:
    body = _base(model, thinking)
    body["messages"] = body["messages"][:1]
    return body


def _no_system(body: dict) -> dict:
    body = copy.deepcopy(body)
    body["messages"] = [m for m in body["messages"] if m.get("role") != "system"]
    return body


def _shapes() -> list[tuple[str, dict]]:
    """25 request shapes. Text is placeholder; only roles, block types and sizes matter."""
    media = _no_system(_base())
    media["messages"][-1]["content"] = [{"type": "text", "text": "x"},
                                        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                      "data": "AAAA"}}]
    builtin = _no_system(_base())
    builtin["tools"] = builtin["tools"] + [{"type": "web_search_20250305", "name": "web_search"}]
    big = _no_system(_base())
    big["messages"][-1]["content"] = [{"type": "text", "text": "x " * 400_000}]
    pinned = _no_system(_base())
    pinned["messages"][0]["content"] = [{"type": "text", "text": "<command-name>/model</command-name> hi"}]
    return [
        ("first_opus", _first()),
        ("first_sonnet", _first(SONNET)),
        ("first_haiku_model", _first(HAIKU)),
        ("first_no_thinking", _first(thinking=None)),
        ("cont_system_opus", _base()),
        ("cont_system_sonnet", _base(SONNET)),
        ("cont_system_no_thinking", _base(thinking=None)),
        ("cont_clean_opus", _no_system(_base())),
        ("cont_clean_sonnet", _no_system(_base(SONNET))),
        ("cont_clean_haiku_model", _no_system(_base(HAIKU))),
        ("cont_clean_no_thinking", _no_system(_base(thinking=None))),
        ("cont_media", media),
        ("cont_builtin_tool", builtin),
        ("cont_big_context", big),
        ("cont_user_pinned", pinned),
        ("side_call_no_tools", dict(_base(), tools=[])),
        ("first_long_prompt", dict(_first(), messages=[{"role": "user", "content": [
            {"type": "text", "text": "1. do this\n2. do that\n3. then this\n" + "word " * 700}]}])),
        ("cont_unknown_model", _no_system(_base("gpt-5.5"))),
        ("cont_fable", _no_system(_base("claude-fable-5-1"))),
        ("cont_clean_adaptive_sonnet_first_msg_short", _no_system(_base(SONNET))),
        ("cont_system_haiku_model", _base(HAIKU)),
        ("first_sonnet_no_thinking", _first(SONNET, None)),
        ("cont_clean_no_output_config", {k: v for k, v in _no_system(_base()).items() if k != "output_config"}),
        ("cont_system_no_output_config", {k: v for k, v in _base().items() if k != "output_config"}),
        ("first_no_tools_pinned_opus_prefix", dict(_first(), messages=[
            {"role": "user", "content": [{"type": "text", "text": "opus: explain"}]}])),
    ]


def _policy(haiku_rewrite: bool) -> pt.ClaudeTierPolicy:
    raw = yaml.safe_load(pt.DEFAULT_POLICY_PATH.read_text())
    raw["stickiness"] = dict(raw.get("stickiness") or {}, switch_after_first_call=True)
    raw["haiku_rewrite"] = haiku_rewrite
    return pt.ClaudeTierPolicy.from_dict(raw, conversation_level=True)


def _classify(complexity: str):
    async def fn(text):
        return {"task_type": "code", "complexity": complexity, "chain_head": ["ollama/x"], "model": None}
    return fn


async def _decisions() -> dict[str, list]:
    out: dict[str, list] = {}
    shapes = _shapes()
    assert len(shapes) == 25
    for rewrite in (True, False):
        policy = _policy(rewrite)
        for i, (name, body) in enumerate(shapes):
            complexity = ("simple", "moderate", "complex")[i % 3]
            d = await policy.decide(copy.deepcopy(body), SID, Stickiness(), classify=_classify(complexity))
            out[f"{name}|rewrite={rewrite}"] = [d.served_model, d.tier, d.reason, d.proposed_tier, d.body_rewrite,
                                                  d.switched]
    return out


async def test_tier_decisions_are_unchanged_on_50_fixtures():
    got = await _decisions()
    assert len(got) == 50
    if os.environ.get("TIER_GOLDEN_UPDATE") == "1":
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    assert json.loads(GOLDEN.read_text()) == got
