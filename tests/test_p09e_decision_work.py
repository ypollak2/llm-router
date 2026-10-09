"""PLAN v16 P0.9-e: bounds on the work inside the proxy's tier decision phases.

Live turn-first decisions (2026-10-08T19:58Z..10-09, n=133) had p95 54.3 ms over
the 50 ms bar; the tail was ``classify`` and ``haiku_checks``. Two per-call costs
grew with the request, and these tests pin them down:

* ``haiku_checks`` serialized the whole 0.5-2.8 MB body (``_approx_context_tokens``)
  on every classified call, though the verdict matters only when the Haiku tier is
  considered, and the proxy serialized it again for ``tier_haiku_block`` whenever the
  classifier proposed Haiku. Now: at most once per call, and only then.
* ``classify`` ran 18 IGNORECASE regexes plus the capability detector over up to
  3,000 characters (~4.4 ms). The tier decision reads only the class; the scoring
  now uses exact case-sensitive twins of the patterns.

Each change must leave every decision and every score as it was; the equivalence
tests below compare against the eager / IGNORECASE originals.
"""

from __future__ import annotations

import copy
import itertools
import random
import re
import string

import pytest

from llm_router import classify as C
from llm_router.proxy import backends as pb
from llm_router.proxy import quota_pressure
from llm_router.proxy import server as ps
from llm_router.proxy import tiers as pt
from llm_router.proxy.cache_cost import Stickiness, conversation_key
from tests.test_proxy_tiers import (
    HAIKU,
    OPUS,
    SID,
    SONNET,
    Clock,
    Upstream,
    _app,
    _first,
    _image_in_tool_result,
    _no_system_reminders,
    _post,
    _raw_policy,
    _req,
    _rows,
    _with_builtin_tool,
)

# ── classify: the case-sensitive twins score exactly like the originals ──────


def _reference_scores(text: str) -> dict[str, int]:
    """``_score_categories`` as it was: every IGNORECASE pattern on the raw text."""
    scores = {}
    for category, layers in C._SIGNALS.items():
        total = 0
        for layer, weight in (("intent", C._INTENT_W), ("topic", C._TOPIC_W), ("format", C._FORMAT_W)):
            pat = layers.get(layer)
            if pat:
                found = pat.findall(text)
                total += len({m.lower() if isinstance(m, str) else m[0].lower() for m in found}) * weight
        scores[category] = total
    return scores


def test_exactly_four_non_ascii_characters_fold_to_an_ascii_letter():
    """The twins' exactness argument rests on this set (module comment in classify.py)."""
    every = "".join(chr(i) for i in range(0x110000) if not 0xD800 <= i <= 0xDFFF)
    folds = set(re.findall("[a-z]", every, re.IGNORECASE)) - set(string.ascii_letters)
    assert folds == {"İ", "ı", "ſ", "K"}
    assert {c for c in folds if C._FOLDS_TO_ASCII.search(c)} == folds


def test_nearly_every_signal_pattern_has_a_twin():
    have = sum(len(v) for v in C._SIGNALS_CS.values())
    total = sum(len(v) for v in C._SIGNALS.values())
    # analyze.intent keeps IGNORECASE: its source holds the literal "I" ("should (?:I|we)").
    assert (have, total) == (17, 18)


def _vocabulary() -> list[str]:
    """Words and phrases lifted from the patterns' own alternations, so random texts hit them."""
    words = set()
    for layers in C._SIGNALS.values():
        for pat in layers.values():
            words.update(w for w in re.findall(r"[a-z][a-z' ]{1,30}[a-z]", pat.pattern) if "?" not in w)
    return sorted(words)


@pytest.mark.parametrize("seed", range(4))
def test_twin_scores_equal_the_ignorecase_scores(seed):
    rng = random.Random(seed)
    vocab = _vocabulary()
    noise = ["the", "and", "?", "\n", "  ", "API", "JSON", "Fix", "WRITE", "Should I", "naïve", "café", "—",
             "→", "שלום", "日本語", "İstanbul", "ı", "ſtyle", "Kelvin", "_x_", "x1", "2026", "!"]
    texts = [p for p in ("", "?", "fix the typo in the README", "What is the latest news on AI funding?")]
    for _ in range(400):
        parts = []
        for _ in range(rng.randint(1, 120)):
            w = rng.choice(vocab) if rng.random() < 0.5 else rng.choice(noise)
            w = rng.choice((w, w.upper(), w.title(), w))
            parts.append(w)
        texts.append(rng.choice((" ", "", "-", "\n")).join(parts)[:3000])
    for text in texts:
        assert C._score_categories(text) == _reference_scores(text), text[:200]


def test_the_tier_classify_skips_the_capability_detector_and_keeps_the_class(monkeypatch):
    import asyncio

    from llm_router import capabilities

    texts = ["fix the typo in the README", "write a function that parses ISO dates and add tests",
             "explain how the cache works " * 30, "design a migration plan for the billing database " * 60]
    want = [(C.classify_signals(t, C.GATEWAY_POLICY).task_type.value,
             C.classify_signals(t, C.GATEWAY_POLICY).complexity.value) for t in texts]

    calls = []
    real = capabilities.detect_capabilities

    def counting(*a, **k):  # classify_signals swallows exceptions from it, so count instead
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(capabilities, "detect_capabilities", counting)
    got = [asyncio.run(pb.tier_classify(t, None, anthropic=True)) for t in texts]
    assert [(g["task_type"], g["complexity"]) for g in got] == want
    assert calls == [], "the tier decision ran the capability detector"
    C.classify_signals(texts[0], C.GATEWAY_POLICY)  # every other caller still gets the vector
    assert len(calls) == 1


def test_the_proxy_imports_the_classifier_at_start_not_in_the_first_decision(tmp_path):
    """Cold, the import cost the first classified call ~8 ms of its classify phase."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(f"""
        import sys
        from llm_router.proxy import server as ps
        assert "llm_router.classify" not in sys.modules, "already imported by the server module"
        ps.build_app(ps.ProxyConfig(steps=frozenset(), tiers=ps.TIERS_CONVERSATION,
                                    ledger_path={str(tmp_path / "l.jsonl")!r}))
        assert "llm_router.classify" in sys.modules, "build_app left the classifier import to the first call"
    """)
    env = {**__import__("os").environ, "LLM_ROUTER_HOME": str(tmp_path)}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]


# ── haiku_checks: computed only when the Haiku tier is considered, at most once ──


@pytest.fixture
def block_calls(monkeypatch):
    calls = []
    real = pt.haiku_block_reason

    def counting(body, *, fold_system=False):
        calls.append(fold_system)
        return real(body, fold_system=fold_system)

    monkeypatch.setattr(pt, "haiku_block_reason", counting)
    monkeypatch.setattr(ps, "haiku_block_reason", counting)
    return calls


def _classify(cx, task="code"):
    async def fn(text):
        return {"task_type": task, "complexity": cx, "chain_head": [], "model": None}
    return fn


_RAW = _raw_policy()


def _policy(*, rewrite=True, fold=True, conversation=True, handoff=False, pressure=None):
    raw = copy.deepcopy(_RAW)
    raw["stickiness"] = dict(raw.get("stickiness") or {}, switch_after_first_call=handoff)
    raw.update(haiku_rewrite=rewrite, haiku_fold_system=fold)
    quota = (lambda: quota_pressure.QuotaReading(pressure, quota_pressure.STATE_OK)) if pressure is not None else None
    return pt.ClaudeTierPolicy.from_dict(raw, conversation_level=conversation, quota=quota)


async def test_a_decision_that_never_considers_haiku_never_serializes_the_body(block_calls):
    d = await _policy().decide(_req(model=SONNET), SID, Stickiness(), classify=_classify("moderate"))
    assert (d.tier, d.proposed_tier) == ("sonnet", "sonnet")
    assert block_calls == [] and d.haiku_block is None
    assert "haiku_checks" in d.phases_ms  # the phase still runs (the rewrite check), and is named


async def test_a_haiku_proposal_serializes_the_body_once(block_calls):
    body = _no_system_reminders(_req(model=SONNET))
    d = await _policy(pressure=0.95).decide(body, SID, Stickiness(), classify=_classify("simple"))
    assert d.proposed_tier == "haiku" and d.haiku_block == "none"
    assert len(block_calls) == 1


async def test_the_proxy_row_reuses_the_decisions_block_reason(tmp_path, monkeypatch, block_calls):
    monkeypatch.setattr(pb, "tier_classify", lambda *a, **k: _classify("simple")(a[0]))
    app = _app(tmp_path, Upstream(), policy_overrides={"haiku_rewrite": True})
    await _post(app, _first())
    block_calls.clear()
    await _post(app, _req())
    row = _rows(tmp_path)[-1]
    assert row["tier_proposed"] == "haiku" and row["tier_haiku_block"] == "system_message"
    assert len(block_calls) == 1, "the proxy serialized the body again for tier_haiku_block"


async def test_with_the_rewrite_off_the_proxy_still_records_the_block_reason(tmp_path, monkeypatch, block_calls):
    monkeypatch.setattr(pb, "tier_classify", lambda *a, **k: _classify("simple")(a[0]))
    app = _app(tmp_path, Upstream())  # bundled policy: haiku_rewrite off, so the decision never asks
    await _post(app, _first())
    block_calls.clear()
    await _post(app, _req())
    row = _rows(tmp_path)[-1]
    assert (row["tier_proposed"], row["tier_haiku_block"]) == ("haiku", "system_message")
    assert len(block_calls) == 1


class _Eager:
    """The pre-P0.9-e evaluation order: the Haiku body check runs up front."""

    def __init__(self, fn):
        self._value = bool(fn())

    def __bool__(self):
        return self._value


def _bodies():
    big = _no_system_reminders(_req())
    # Over the context limit the grid runs with (``_CONTEXT_LIMIT``); the real 150K-token
    # limit would make every check a 600 KB json.dumps and the test minutes long.
    big["messages"][-1]["content"] = [{"type": "text", "text": "x " * 15_000}]
    out = {"fixture": _req(), "clean": _no_system_reminders(_req()), "image": _image_in_tool_result(),
           "builtin": _with_builtin_tool(_no_system_reminders(_req())), "context": big}
    return out


_CONTEXT_LIMIT = 5_000  # tokens; the "context" body is ~9K, every other body under 2.5K

_FIELDS = ("requested_model", "served_model", "tier", "reason", "switched", "switch_cost_usd", "task_type",
           "complexity", "body_rewrite", "detail", "proposed_tier", "quota_pressure", "quota_state")


async def _decide_all(monkeypatch, lazy_cls, name):
    monkeypatch.setattr(pt, "_Lazy", lazy_cls)
    monkeypatch.setattr(pt, "HAIKU_MAX_CONTEXT_TOKENS", _CONTEXT_LIMIT)
    out = []
    base = _bodies()[name]
    grid = itertools.product((True, False), (True, False), (True, False), (True, False), (None, 0.75, 0.95),
                             (OPUS, SONNET, HAIKU), ("adaptive", "enabled", None),
                             ("simple", "moderate", "complex"), (None, HAIKU, SONNET))
    for rewrite, fold, conv, handoff, pressure, model, thinking, cx, prev in grid:
        policy = _policy(rewrite=rewrite, fold=fold, conversation=conv, handoff=handoff, pressure=pressure)
        body = dict(base)
        body["model"] = model
        if thinking is None:
            body.pop("thinking", None)
        else:
            body["thinking"] = {"type": thinking}
        sticky = Stickiness(clock=Clock())
        if prev is not None:
            sticky.record(conversation_key(body, SID), prev, "moderate", "policy")
        d = await policy.decide(body, SID, sticky, classify=_classify(cx))
        state = sticky.get(conversation_key(body, SID))
        out.append((tuple(getattr(d, f) for f in _FIELDS), state and (state.model, state.complexity, state.reason)))
    return out


@pytest.mark.parametrize("name", sorted(_bodies()))
async def test_lazy_haiku_checks_change_no_decision(monkeypatch, name):
    """Every combination of policy switches, quota pressure, requested model, thinking,
    class and prior sticky model decides the same, per body shape, with the body check
    evaluated lazily as with it evaluated up front (the old order). 3,888 decisions per
    body shape, 19,440 in all."""
    lazy = await _decide_all(monkeypatch, pt._Lazy, name)
    eager = await _decide_all(monkeypatch, _Eager, name)
    assert len(lazy) == len(eager) == 2 ** 4 * 3 ** 5
    diffs = [i for i, (a, b) in enumerate(zip(lazy, eager)) if a != b]
    assert diffs == [], f"{len(diffs)} decisions differ, first: {lazy[diffs[0]]} vs {eager[diffs[0]]}"
    # The grid reaches the Haiku paths, so the comparison is not vacuous.
    reasons = {row[0][3] for row in lazy}
    blocks = {pt.haiku_block_reason(_bodies()[name], fold_system=f) for f in (True, False)}
    assert blocks != {pt.HAIKU_BLOCK_NONE} or name == "clean", (name, blocks)
    assert {pt.REASON_QUOTA_PRESSURE, pt.REASON_THINKING_FLOOR, pt.REASON_STICKY} <= reasons
    if name == "clean":  # the one shape Haiku may serve
        assert pt.REASON_HAIKU_REWRITE in reasons
        assert any(row[0][8] == pt.REWRITE_HAIKU for row in lazy)
