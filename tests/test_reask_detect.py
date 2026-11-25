"""Paraphrase re-ask detector v1 (shadow): synthetic sets, flag semantics, audit wiring.

The sets are synthetic strings written for this test (D-281: no real session text). Numbers here are a
sanity check on invented pairs, not a measured precision on real prompts.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from llm_router import reask_detect as R

REPO = Path(__file__).resolve().parents[1]
FIX = REPO / "tests" / "fixtures"


def _load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIX / name).read_text().splitlines() if line.strip()]


def _score(rows: list[dict]) -> dict:
    tp = fp = fn = tn = 0
    neg_target_tn = neg_target_n = 0
    for r in rows:
        hit, _ = R.is_paraphrase_reask(r["prev"], r["next"], R.extract_targets(r["prev"]),
                                       R.extract_targets(r["next"]), r["gap_s"], r["turns"])
        if r["label"]:
            tp += hit
            fn += not hit
        else:
            fp += hit
            tn += not hit
            if r["group"] == "hard_neg_new_target":
                neg_target_n += 1
                neg_target_tn += not hit
    return {"n": len(rows), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None,
            "hard_neg_new_target_tn": f"{neg_target_tn}/{neg_target_n}"}


def test_tune_set_numbers():
    s = _score(_load("reask_paraphrase_tune.jsonl"))
    print("TUNE", s)
    assert s["n"] == 48 and s["tp"] + s["fn"] == 24 and s["tn"] + s["fp"] == 24
    assert s["precision"] >= 0.9 and s["recall"] >= 0.9
    assert s["hard_neg_new_target_tn"] == "12/12"


def test_holdout_set_numbers():
    s = _score(_load("reask_paraphrase_holdout.jsonl"))
    print("HOLDOUT", s)
    assert s["n"] == 12
    assert s["precision"] >= 0.9 and s["recall"] >= 0.8
    assert s["hard_neg_new_target_tn"] == "3/3"


def test_new_target_excluded_even_with_overlapping_words():
    prev = "add a retry loop to src/a.py around the fetch call"
    nxt = "add a retry loop to src/b.py around the fetch call"
    assert R.is_paraphrase_reask(prev, nxt, R.extract_targets(prev), R.extract_targets(nxt), 30, 1) == (False, R.REASON_NONE)


def test_far_apart_same_target_is_not_c():
    prev = "rename parse_row in lib/p.py to read_row"
    nxt = "in lib/p.py rename parse_row to read_row"
    t = R.extract_targets(prev)
    assert R.is_paraphrase_reask(prev, nxt, t, t, 30, 1)[0]
    assert not R.is_paraphrase_reask(prev, nxt, t, t, 5000, 5)[0]


def test_reason_codes_and_empty():
    assert R.is_paraphrase_reask("x", "that didn't work", None, None, None, None) == (True, R.REASON_D)
    assert R.is_paraphrase_reask("x", "  ", None, None, 1, 1) == (False, R.REASON_EMPTY)
    assert R.is_paraphrase_reask(None, "hello there", None, None, 1, 1) == (False, R.REASON_NONE)


def test_quoted_failure_text_is_not_d():
    nxt = "```\nERROR try again\n```\nwhat does it mean"
    assert not R.is_paraphrase_reask("run it", nxt, None, None, 1, 1)[0]


def test_extract_targets_ignores_versions():
    assert R.extract_targets("use python 3.11 and fix src/a/b.py") == frozenset({"src/a/b.py"})


def test_flag_default_off_and_registered(monkeypatch):
    from llm_router import env_registry
    monkeypatch.delenv(R.FLAG, raising=False)
    assert R.reask_mode() == "off"
    monkeypatch.setenv(R.FLAG, "SHADOW")
    assert R.reask_mode() == "shadow"
    monkeypatch.setenv(R.FLAG, "on")
    assert R.reask_mode() == "off"
    assert R.FLAG in env_registry.registered_names()


# ── audit wiring ─────────────────────────────────────────────────────────────

def _audit():
    name = "_oa_for_reask_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / "release" / "outcome_audit.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


CTX = {"prev_prompt": "rename parse_row in lib/p.py to read_row",
       "next_prompt": "in lib/p.py rename parse_row to read_row", "gap_s": 30.0, "turns": 1}
CASES = [
    ("label_proxy", lambda A, c: A.label_proxy([], 0, "in lib/p.py rename parse_row to read_row", paraphrase_ctx=c)),
    ("label_routed_mcp", lambda A, c: A.label_routed_mcp(
        "a long enough routed answer body " * 5, False, "host reply words " * 10, True,
        "in lib/p.py rename parse_row to read_row", paraphrase_ctx=c)),
    ("label_draft", lambda A, c: A.label_draft(
        "draft text " * 20, "host reply different words entirely " * 5, "in lib/p.py rename parse_row to read_row",
        "unused", paraphrase_ctx=c)),
]


@pytest.mark.parametrize("name,call", CASES)
def test_labels_identical_across_flag_and_counters_only_in_shadow(monkeypatch, name, call):
    A = _audit()
    monkeypatch.delenv(R.FLAG, raising=False)
    A.reset_paraphrase_shadow()
    base = call(A, None)
    off = call(A, CTX)
    assert off == base and A.paraphrase_shadow_summary({"labels": {}}, []) is None
    monkeypatch.setenv(R.FLAG, "shadow")
    A.reset_paraphrase_shadow()
    shadow = call(A, CTX)
    assert shadow == base
    summ = A.paraphrase_shadow_summary({"labels": {}}, [])
    assert summ["n_checked"] == 1 and summ["hits"] == {R.REASON_CA: 1}


def test_explicit_reask_is_not_double_counted(monkeypatch):
    A = _audit()
    monkeypatch.setenv(R.FLAG, "shadow")
    A.reset_paraphrase_shadow()
    assert A.label_proxy([], 0, "claude: do it again", paraphrase_ctx=CTX)[0] == "redone"
    assert A.paraphrase_shadow_summary({"labels": {}}, [])["n_checked"] == 0


def test_summary_used_if_paraphrase_counted(monkeypatch):
    A = _audit()
    monkeypatch.setenv(R.FLAG, "shadow")
    A.reset_paraphrase_shadow()
    A.label_proxy([], 0, "in lib/p.py rename parse_row to read_row", paraphrase_ctx=CTX)  # used + hit
    A.label_proxy([], 0, "unrelated question about the weather", paraphrase_ctx=CTX)       # used, no hit
    rows = [{"label": "used", "signal": "proxy_text_not_reasked"}] * 2 + [
        {"label": "redone", "signal": "proxy_explicit_reask"}]
    s = A.paraphrase_shadow_summary({"headline": {"used": 2, "attempted": 3}}, rows)
    assert s["reissue_explicit"] == 1
    assert s["used_if_paraphrase_counted"] == {"used": 1, "attempted": 3}
    assert s["hits_total"] == 1


# ── review fixes: last-turn draft, raising detector, quoted spans, "I meant" ─

def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def test_last_turn_draft_in_shadow_does_not_crash(monkeypatch):
    """A draft in a session's final turn (no later human turn): reply_after returns len(records)."""
    A = _audit()
    sid = "10ea9ab2-8cdc-4058-b9f8-3a6543727978"
    t = 1_800_000_000.0
    sep = "─── DRAFT ───\nthe drafted answer text\n─── END ───"
    recs = [
        {"type": "user", "timestamp": _iso(t - 5), "message": {"content": "rename parse_row in lib/p.py"}},
        {"type": "attachment", "timestamp": _iso(t), "attachment": {"type": "hook_additional_context", "content": [sep]}},
        {"type": "assistant", "timestamp": _iso(t + 1), "message": {"content": [{"type": "text", "text": "done"}]}},
    ]
    s = A.index_session(A.Session(sid, recs, {}))
    assert s.paraphrase_ctx(1, len(recs)) is None
    rec = {"msgs": ["DIRECT SUCCESS"], "session8": sid[:8]}
    for mode in ("off", "shadow"):
        monkeypatch.setenv(R.FLAG, mode)
        A.reset_paraphrase_shadow()
        units = A._draft_units({sid: s}, {str(t): rec}, {}, t - 10, t + 10)
        assert len(units) == 1
    assert A._PARAPHRASE["checked"] == 0


def test_raising_detector_never_breaks_a_label(monkeypatch):
    A = _audit()
    monkeypatch.setenv(R.FLAG, "shadow")
    A.reset_paraphrase_shadow()
    base = A.label_proxy([], 0, "in lib/p.py rename parse_row to read_row")

    class Boom:
        @staticmethod
        def is_paraphrase_reask(*a, **k):
            raise RuntimeError("boom")
        extract_targets = staticmethod(lambda t: frozenset())

    monkeypatch.setattr(A, "_load_script", lambda rel, name: Boom)
    assert A.label_proxy([], 0, "in lib/p.py rename parse_row to read_row", paraphrase_ctx=CTX) == base
    s = A.paraphrase_shadow_summary({}, [])
    assert s["detector_unavailable"] == 1 and s["n_checked"] == 0 and s["hits_total"] == 0


@pytest.mark.parametrize("nxt,expected", [
    ("what does the log say 'that didn't work' mean in src/a.py", False),
    ('what does "try again" mean in src/a.py', False),
    ("didn't work, try again", True),
    ("I meant the lexer, not the parser", True),
])
def test_quoted_spans_and_i_meant(nxt, expected):
    assert R.is_paraphrase_reask("fix the parser", nxt, None, None, 5, 1)[0] is expected
