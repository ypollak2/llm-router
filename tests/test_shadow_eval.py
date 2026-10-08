"""P1.7 live shadow: ``shadow_eval`` scores classifier-shadow records against rules_eff.

A 7-record fixture gives hand-computed C-lambda2, C-lambda2-clamp (M1-12), under-route,
Haiku precision, joins and fallbacks (the arithmetic is in the comments). Larger
synthetic sets pin the M1-12 verdict rule: pass / fail only on >= 100 labeled turns
with no session above 60%, else "not informative".
"""

from __future__ import annotations

import json

import pytest

from llm_router import shadow_eval as se
from llm_router.commands import kpi
from llm_router.proxy import llm_shadow as ls
from tests.test_kpi_classifier_shadow import NOW, _isolated, _summary, _write  # noqa: F401 - fixture reused

S1, S2, S3 = "s1", "s2", "s3"
OPUS, SONNET, HAIKU = "claude-opus-4-6", "claude-sonnet-4-5-20250929", "claude-haiku-4-5"


def _rec(sid, sha, rules, live, llm, requested, source="llm", model="nimble:9b"):
    ok = source in ("llm", "cache")
    return {"kind": ls.KIND, "ts": NOW - 3600, "session_id": sid, "text_sha": sha, "session_kind": "organic",
            "step_class": None, "rules": {"task_type": "code", "complexity": "simple", "tier": rules},
            "llm": {"tier": llm if ok else None, "derivation": "direct"}, "source": source, "ms": 400.0,
            "tier_reason_live": "policy", "tier_live": live, "requested_tier": requested,
            "requested": se.tier_name(requested), "policy_version": "abc", "model": model,
            "prompt_version": "sys1", "backend": "systemone", "assemble_capped": False}


def _fixture():
    return [
        _rec(S1, "t1", "sonnet", "sonnet", "haiku", OPUS),
        _rec(S1, "t2", "haiku", "sonnet", "opus", SONNET),       # haiku proposed, not served: rules_eff sonnet
        _rec(S2, "t3", "opus", "opus", "sonnet", OPUS),
        _rec(S2, "t4", "sonnet", "sonnet", None, "some-unknown-model", source="timeout"),  # fallback; imputed opus
        _rec(S2, "t3", "opus", "opus", "haiku", OPUS),           # the same turn again: not scored twice
        _rec(S3, "t5", None, None, "haiku", OPUS),               # no rules tier: not comparable
        _rec(S3, "t6", "sonnet", "sonnet", "local", HAIKU),      # unlabeled; local counts as haiku
    ]


LABELS = {(S1, "t1"): "haiku", (S1, "t2"): "opus", ("", "t3"): "sonnet", (S2, "t4"): "haiku"}


def test_tier_name_reads_tiers_and_model_ids():
    assert [se.tier_name(x) for x in (OPUS, SONNET, HAIKU, "local", "Opus", "gpt-5", None, 7)] == \
        ["opus", "sonnet", "haiku", "haiku", "opus", None, None, None]


def test_fixture_gives_the_hand_computed_values():
    v = se.score(_fixture(), LABELS)
    assert (v["n_turns"], v["n_sessions"], v["n_joined"], v["n_fallback"], v["agree"]) == (5, 3, 4, 1, 1)
    assert (v["clamp_binds_llm"], v["clamp_binds_rules"]) == (1, 1)   # t2 opus > sonnet; t6 sonnet > haiku
    assert v["craw_llm"] == pytest.approx(13.55 / 5)            # 1 + 6.15 + 2.7 + 2.7 + 1
    assert v["craw_rules_eff"] == pytest.approx(16.95 / 5)      # 2.7 + 2.7 + 6.15 + 2.7 + 2.7
    assert v["craw_clamp_llm"] == pytest.approx(10.1 / 5)       # 1 + 2.7 + 2.7 + 2.7 + 1
    assert v["craw_clamp_rules_eff"] == pytest.approx(15.25 / 5)  # 2.7 + 2.7 + 6.15 + 2.7 + 1
    assert (v["n_labeled"], v["n_labeled_sessions"], v["largest_session_share"]) == (4, 2, 0.5)
    a, r = v["arms"]["llm"], v["arms"]["rules_eff"]
    assert a["c_lambda2"] == pytest.approx(12.55 / 4)           # 1 + 6.15 + 2.7 + 2.7
    assert a["c_lambda2_clamp"] == pytest.approx(21.4 / 4)      # t2: opus clamped to sonnet, truth opus: 2.7 + 12.3
    assert r["c_lambda2"] == pytest.approx(26.55 / 4)           # 2.7 + 15.0 + 6.15 + 2.7
    assert r["c_lambda2_clamp"] == pytest.approx(26.55 / 4)
    assert (a["under"]["k"], r["under"]["k"], a["exact"]["k"], r["exact"]["k"]) == (0, 1, 3, 0)
    assert (a["HP"]["k"], a["HP"]["n"], a["HR"]["k"], a["HR"]["n"]) == (1, 1, 1, 2)
    assert (r["HP"]["n"], r["HP"]["rate"], r["HR"]["k"]) == (0, None, 0)
    boot = v["boot_c_lambda2_clamp_llm_minus_rules"]
    assert boot["diff"] == pytest.approx((21.4 - 26.55) / 4, abs=1e-4) and boot["clusters"] == 2
    assert boot["ci95"][0] <= boot["diff"] <= boot["ci95"][1]
    assert v["M1_12"] == {"verdict": "not informative", "why": "4 labeled turns < 100", "min_n": 100}
    json.dumps(v)


def test_without_labels_truth_metrics_are_absent_and_m1_12_is_not_informative():
    v = se.score(_fixture())
    assert v["n_turns"] == 5 and v["n_labeled"] == 0
    assert v["arms"] == {"llm": None, "rules_eff": None} and v["boot_c_lambda2_clamp_llm_minus_rules"] is None
    assert v["M1_12"]["verdict"] == "not informative"


def _synthetic(n_per_session, sessions, llm_tier, truth="haiku"):
    recs, labels = [], {}
    for s in range(sessions):
        for i in range(n_per_session[s] if isinstance(n_per_session, list) else n_per_session):
            sid, sha = f"S{s}", f"x{s}-{i}"
            recs.append(_rec(sid, sha, "sonnet", "sonnet", llm_tier, OPUS))   # opus requested: no clamp
            labels[(sid, sha)] = truth
    return recs, labels


@pytest.mark.parametrize("llm_tier,verdict", [("haiku", "pass"), ("sonnet", "pass"), ("opus", "fail")])
def test_m1_12_verdict_needs_100_labeled_turns(llm_tier, verdict):
    recs, labels = _synthetic(30, 4, llm_tier)                  # 120 labeled turns, largest session 25%
    v = se.score(recs, labels)
    assert v["n_labeled"] == 120 and v["M1_12"]["verdict"] == verdict
    clamped = se.score([dict(r, requested="sonnet") for r in recs], labels)
    assert clamped["M1_12"]["verdict"] == "pass"                # sonnet requested: an opus pick is clamped away
    few = se.score(recs[:99], labels)
    assert few["M1_12"]["verdict"] == "not informative"         # 99 < 100, whatever the costs say


def test_m1_12_is_not_informative_when_one_session_dominates():
    recs, labels = _synthetic([80, 10, 10, 10], 4, "haiku")   # 80/110 = 73% > 60%
    v = se.score(recs, labels)
    assert v["n_labeled"] == 110 and v["M1_12"]["verdict"] == "not informative"
    assert "largest session" in v["M1_12"]["why"]


def test_models_are_scored_apart():
    recs = _fixture() + [_rec(S1, "t9", "sonnet", "sonnet", "haiku", OPUS, model="llmr-classifier")]
    by = se.score_by_model(recs, LABELS)
    assert sorted(by) == ["llmr-classifier", "nimble:9b"]
    assert by["nimble:9b"]["n_turns"] == 5 and by["llmr-classifier"]["n_turns"] == 1


def test_load_labels_skips_bad_lines(tmp_path):
    p = tmp_path / "labels.jsonl"
    p.write_text("\n".join([json.dumps({"text_sha": "a", "session_id": "s", "truth": "opus"}),
                            json.dumps({"text_sha": "b", "truth": "local"}), "not json",
                            json.dumps({"text_sha": "c", "truth": "huge"}), json.dumps(["x"])]))
    assert se.load_labels(p) == {("s", "a"): "opus", ("", "b"): "haiku"}
    assert se.load_labels(tmp_path / "missing.jsonl") == {} and se.load_labels(None) == {}


def test_kpi_carries_and_renders_the_vs_rules_line(tmp_path, monkeypatch):
    _write(_fixture())
    labels = tmp_path / "labels.jsonl"
    labels.write_text("\n".join(json.dumps({"session_id": s, "text_sha": t, "truth": x})
                                for (s, t), x in LABELS.items()))
    monkeypatch.setenv("LLM_ROUTER_SHADOW_LABELS", str(labels))
    s = _summary()
    assert s["vs_rules"]["nimble:9b"]["n_labeled"] == 4
    (line,) = kpi._classifier_vs_rules_lines(s)
    assert line.startswith("classifier shadow vs rules [nimble:9b]: n=5 turns in 3 sessions")
    assert "requested tier real 4/5" in line and "labeled 4" in line
    assert "M1-12 C-lambda2-clamp llm 5.35 vs rules 6.64" in line and "under llm 0/4 vs rules 1/4" in line
    assert "HP llm 1/1" in line and "M1-12 not informative (4 labeled turns < 100; cost only" in line
    # statistics rule 4: the largest-session share and the top-3 session sizes are printed, and so are
    # the capped turns; the default population is organic only (R1)
    assert "labeled 4 in 2 sessions (largest 50%, top-3 sessions 2/2 labeled turns)" in line
    assert "assemble capped 0" in line and "(organic sessions)" in line
    assert "fallback counts as agree" in line and "R1 adoption also needs under-route llm <= rules" in line
    monkeypatch.delenv("LLM_ROUTER_SHADOW_LABELS")
    (bare,) = kpi._classifier_vs_rules_lines(_summary())
    assert "labeled 0" in bare and "no truth labels" in bare and "raw cost clamp-aware llm 2.02 vs rules 3.05" in bare
    rendered = kpi.render_scorecard(kpi.compute_scorecard(7))
    assert "classifier shadow vs rules [nimble:9b]" in rendered


# --- reviewer mutants (#327 review): each test below fails on one surviving mutant ------------


def test_m1_12_is_informative_at_exactly_60_percent():
    """Rule 4 excludes a sample only when one session holds MORE than 60%."""
    recs, labels = _synthetic([60, 20, 20], 3, "haiku")       # 60/100 = 60%: not above the bar
    v = se.score(recs, labels)
    assert (v["n_labeled"], v["largest_session_share"]) == (100, 0.6)
    assert v["M1_12"]["verdict"] == "pass" and v["top3_session_counts"] == [60, 20, 20]


def test_a_missing_requested_tier_is_imputed_from_the_session_else_sonnet():
    """v2 imputation: the session's most common requested tier, else Sonnet (never Opus)."""
    alone = se.score([_rec("A", "a1", "opus", "opus", "opus", "some-unknown-model")])
    assert alone["n_joined"] == 0 and alone["craw_clamp_llm"] == pytest.approx(2.7)    # opus clamped to sonnet
    mixed = se.score([_rec("B", "b1", "sonnet", "sonnet", "sonnet", HAIKU),
                      _rec("B", "b2", "sonnet", "sonnet", "sonnet", HAIKU),
                      _rec("B", "b3", "sonnet", "sonnet", "sonnet", OPUS),
                      _rec("B", "b4", "opus", "opus", "opus", "some-unknown-model")])
    # b4 imputes haiku (2 of 3 real requests): clamp-aware cost 1.0 for b1, b2, b4 and 2.7 for b3
    assert mixed["n_joined"] == 3 and mixed["craw_clamp_llm"] == pytest.approx((1.0 * 3 + 2.7) / 4)


@pytest.mark.parametrize("rules,live,expected", [("sonnet", "sonnet", "sonnet"), ("opus", "opus", "opus"),
                                                  ("haiku", "sonnet", "sonnet"), ("haiku", "haiku", "haiku")])
def test_a_fallback_scores_rules_eff(rules, live, expected):
    """M1.9 "fallback rules_eff": an unusable verdict takes the rules' effective tier, not a constant."""
    for source in ("timeout", "parse_error", "cold"):
        v = se.score([_rec("F", "f1", rules, live, None, OPUS, source=source)])
        assert (v["n_fallback"], v["agree"]) == (1, 1)
        assert v["craw_llm"] == pytest.approx(se.COST[expected]) == v["craw_rules_eff"]
