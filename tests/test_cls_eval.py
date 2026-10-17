"""scripts/cls_eval.py (CLS-v16 B1): the scoring harness, on synthetic tables only.

No network, no model, no real prompt: every table is built here (ids, groups, tiers, scores). The P17D regression
reads docs/measurements/p17d-synthetic.json (ids and tiers, no text). Output files go to tmp_path.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import os
import random
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("cls_eval", ROOT / "scripts" / "cls_eval.py")
ce = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ce)

from llm_router import shadow_eval as se  # noqa: E402

B_FAST = 200
TIERS = ("haiku", "sonnet", "opus")


# --------------------------------------------------------------------------- builders

def judges(t, g=None, s=None):
    return {"opus": t, "gpt": t if g is None else g, "sonnet": t if s is None else s}


def item(iid, group, truth, preds, requested="opus", kind="real", source="src", scores=None, fold=None,
         strata=None, weight=None, truths=None):
    rec = {"item_id": iid, "group": group, "requested": requested, "kind": kind,
           "strata": {"source": source, "length_band": "13-50", "task_type": "code", "tool_need": "none",
                      "generator": "real", "contested": False, **(strata or {})},
           "truths": truths if truths is not None else judges(truth), "preds": preds}
    if scores is not None:
        rec["scores"] = scores
    if fold is not None:
        rec["fold"] = fold
    if weight is not None:
        rec["weight"] = weight
    return rec


def write_table(path, recs, header=None):
    lines = ([json.dumps({"_header": header})] if header else []) + [json.dumps(r) for r in recs]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def run(argv, capsys):
    rc = ce.main([str(a) for a in argv])
    cap = capsys.readouterr()
    out = json.loads(cap.out) if cap.out.strip().startswith("{") else None
    return rc, out, cap.out, cap.err


def pt(x):
    return TIERS[x - 1]


def gen_recs(seed=7, n_groups=60, per=5, kinds=("real",), scored="cand", folds=False, arms=True):
    """A realistic population: ~20% haiku / 38% sonnet / 42% opus truth, rules_eff nearly constant sonnet."""
    rng = random.Random(seed)
    recs = []
    for g in range(n_groups):
        for k in range(per):
            r = rng.random()
            truth = "haiku" if r < 0.20 else ("sonnet" if r < 0.58 else "opus")
            rk = {"haiku": 1, "sonnet": 2, "opus": 3}[truth]

            def noisy(p):
                if rng.random() < p:
                    opts = [x for x in (rk - 1, rk + 1) if 1 <= x <= 3]
                    return pt(rng.choice(opts))
                return truth
            preds = {"rules_eff": "sonnet" if rng.random() < 0.98 else "opus"}
            if arms:
                preds.update({"always_haiku": "haiku", "always_sonnet": "sonnet", "always_opus": "opus",
                              "random": rng.choice(TIERS), "nimble": noisy(0.6) if rng.random() < 0.7 else "haiku",
                              "qwen3coder": noisy(0.12), "oracle": truth})
            scores = None
            if scored:
                po = 0.8 if truth == "opus" else 0.1
                ph = 0.8 if truth == "haiku" else 0.1
                po = min(0.99, max(0.01, po + rng.uniform(-0.08, 0.08)))
                ph = min(0.99, max(0.01, ph + rng.uniform(-0.08, 0.08)))
                scores = {scored: {"p_haiku": round(ph, 3), "p_sonnet": round(max(0.0, 1 - ph - po), 3),
                                   "p_opus": round(po, 3)}}
            if scored:
                preds[scored] = "opus" if po > 0.5 else ("haiku" if ph > 0.5 else "sonnet")
            kind = kinds[g % len(kinds)]
            recs.append(item(f"i{g}_{k}", f"{kind}{g}", truth, preds, kind=kind, scores=scores,
                             fold=(g % 5) if folds else None))
    return recs


# --------------------------------------------------------------------------- a) golden

def _golden():
    spec = ([("haiku", ["haiku", "haiku", "haiku", "sonnet"])] + [("sonnet", ["sonnet", "sonnet", "sonnet", "opus"])]
            + [("opus", ["opus", "opus", "opus", "sonnet"])])
    recs, n = [], 0
    for truth, cand in spec:
        for j, c in enumerate(cand):
            req = "sonnet" if (truth == "sonnet" and j == 3) else "opus"
            recs.append(item(f"g{n}", f"grp{n}", truth,
                             {"rules_eff": "sonnet", "always_sonnet": "sonnet", "always_opus": "opus", "cand": c},
                             requested=req))
            n += 1
    return recs


def test_golden_twelve_items_exact_values(tmp_path, capsys):
    rc, out, _, _ = run(["score", write_table(tmp_path / "t.jsonl", _golden()), "--boot-b", B_FAST], capsys)
    assert rc == 0
    a = out["arms"]
    # hand-computed: rules_eff = sonnet always: under 4/12 (the opus truths); cost (8 x 2.7 + 4 x (2.7 + 2 x 6.15)) / 12
    assert a["rules_eff"]["under"]["k"] == 4 and a["rules_eff"]["under"]["n"] == 12
    assert a["rules_eff"]["c_lambda2_clamp"] == pytest.approx(6.8, abs=1e-6)
    assert a["rules_eff"]["raw_cost"] == pytest.approx(2.7, abs=1e-6)
    # always_opus: under 0; cost (11 x 6.15 + 1 x 2.7) / 12 (one sonnet-truth item requested sonnet: clamp to 2.7)
    assert a["always_opus"]["under"]["k"] == 0
    assert a["always_opus"]["c_lambda2_clamp"] == pytest.approx(5.8625, abs=1e-6)
    assert a["always_opus"]["raw_cost"] == pytest.approx(6.15, abs=1e-6)
    # cand: under 1/12; cost (5.7 + 10.8 + 33.45) / 12 = 4.1625; raw (5.7 + 14.25 + 21.15) / 12 = 3.425
    assert a["cand"]["under"]["k"] == 1
    assert a["cand"]["c_lambda2_clamp"] == pytest.approx(4.1625, abs=1e-6)
    assert a["cand"]["raw_cost"] == pytest.approx(3.425, abs=1e-6)
    assert a["cand"]["share"] == {"haiku": 0.25, "sonnet": pytest.approx(5 / 12, abs=1e-6),
                                  "opus": pytest.approx(4 / 12, abs=1e-6)}
    assert a["cand"]["haiku_precision"]["k"] == 3 and a["cand"]["haiku_precision"]["n"] == 3
    assert a["cand"]["haiku_recall"]["k"] == 3 and a["cand"]["haiku_recall"]["n"] == 4
    comp = out["primary"]["comparisons"]["cand"]
    assert comp["under"]["diff"] == pytest.approx(1 / 12 - 4 / 12, abs=1e-6)
    assert comp["cost"]["diff"] == pytest.approx(4.1625 - 6.8, abs=1e-6)
    assert comp["bars"] is True
    # guard arithmetic: q = (3.425 - 2.7) / 3.45; E[under(mix)] = (1 - q) / 3
    q = (3.425 - 2.7) / 3.45
    assert comp["guard"]["q"] == pytest.approx(q, abs=1e-6)
    assert comp["guard"]["e_under_mix"] == pytest.approx((1 - q) / 3, abs=1e-6)
    # verdict: only 4 opus truths (< 40) -> NOT INFORMATIVE, with low power
    assert out["status"] == "NOT INFORMATIVE" and "n_truth_opus_lt_40" in out["reasons"]
    assert out["primary"]["low_power"] is True
    assert comp["status"] == "NOT INFORMATIVE"
    # the underlying label on the same data, before the informativeness rules
    rows, _ = ce.prepare(ce.load_table(tmp_path / "t.jsonl")[1], ["rules_eff", "cand"], "majority3")
    an = ce.analyze_arm(ce.group_sums(rows, ["rules_eff", "cand"]), "cand", "rules_eff", B_FAST, ce.SEED)
    assert an["bars"] and an["_core"] == "PASS-STRONG"


# --------------------------------------------------------------------------- P17D regression

P17D = ROOT / "docs" / "measurements" / "p17d-synthetic.json"


def _p17d_table(tmp_path, drop=15):
    d = json.loads(P17D.read_text(encoding="utf-8"))
    recs = []
    for i, it in enumerate(d["items"]):
        if i == drop:
            continue
        preds = {"rules_eff": it["rules_eff"], "llm": it["llm"] if it["source"] == "llm" else None,
                 "always_opus": "opus", "always_sonnet": "sonnet"}
        recs.append(item(it["item_id"], f"s{it['session_index']:02d}", it["truth"], preds,
                         requested=it["requested"], truths={"opus": it["truth"], "gpt": None, "sonnet": None},
                         source="p17d"))
    return write_table(tmp_path / "p17d.jsonl", recs), d


def test_p17d_regression_reproduces_published_numbers(tmp_path, capsys):
    """The items table has 151 rows and the published figures use n_labeled 150: the extra row is most likely a
    second item id on an already-scored turn. Dropping any one haiku-verdict / sonnet-truth row reproduces the
    published figures (verified for all 33 such rows in the next test)."""
    table, d = _p17d_table(tmp_path)
    rc, out, _, _ = run(["score", table, "--truth", "opus_only", "--mode", "test", "--single-use", "--use-log",
                         tmp_path / "use.log", "--calibration", _cal(tmp_path), "--boot-b", B_FAST], capsys)
    assert rc == 0
    pub = d["arms"]
    a = out["arms"]
    assert a["llm"]["n"] == a["rules_eff"]["n"] == 150 == d["n_labeled"]
    assert round(a["llm"]["c_lambda2_clamp"], 3) == pub["llm"]["c_lambda2_clamp"]
    assert round(a["llm"]["c_lambda2_clamp"], 3) == 7.006
    assert round(a["rules_eff"]["c_lambda2_clamp"], 3) == pub["rules_eff"]["c_lambda2_clamp"] == 7.748
    assert (a["llm"]["under"]["k"], a["rules_eff"]["under"]["k"]) == (72, 61)
    assert (pub["llm"]["under"]["k"], pub["rules_eff"]["under"]["k"]) == (72, 61)
    assert round(a["always_opus"]["c_lambda2_clamp"], 3) == d["reference_arms"]["always_opus"]["c_lambda2_clamp"] == 6.117
    assert a["always_opus"]["under"]["k"] == 0 == d["reference_arms"]["always_opus"]["under"]["k"]
    assert round(a["always_sonnet"]["c_lambda2_clamp"], 3) == d["reference_arms"]["always_sonnet"]["c_lambda2_clamp"]
    assert a["always_sonnet"]["under"]["k"] == d["reference_arms"]["always_sonnet"]["under"]["k"] == 63
    assert a["llm"]["n_fallback"] == d["n_fallback"] == 4          # the four parse errors fall back to rules_eff
    assert (a["llm"]["haiku_precision"]["k"], a["llm"]["haiku_precision"]["n"]) == (19, 63)
    assert (a["llm"]["haiku_recall"]["k"], a["llm"]["haiku_recall"]["n"]) == (19, 29)
    assert out["n_groups"] == 25 == d["boot_under"]["clusters"]
    # the contradiction P17D exposed: always_opus beats both bars yet fails the guard
    ao = out["primary"]["comparisons"]["always_opus"]
    assert ao["bars"] is True and ao["guard"]["passes"] is False
    # llm fails a bar on the point estimates, as published (verdict FAIL)
    assert out["primary"]["comparisons"]["llm"]["bars"] is False
    assert d["verdict"] == "FAIL"


def test_p17d_dropped_row_choice_is_immaterial(tmp_path):
    d = json.loads(P17D.read_text(encoding="utf-8"))
    def fig(rows, f):
        u = sum(se.RANK[f(x)] < se.RANK[x["truth"]] for x in rows)
        c = sum(se.cost_eff(se.clamp(f(x), x["requested"]), x["truth"]) for x in rows) / len(rows)
        return u, round(c, 3)
    arms = [lambda x: x["llm"], lambda x: x["rules_eff"], lambda x: "opus", lambda x: "sonnet"]
    published = [(72, 7.006), (61, 7.748), (0, 6.117), (63, 7.866)]
    hits = [k for k in range(len(d["items"]))
            if [fig(d["items"][:k] + d["items"][k + 1:], f) for f in arms] == published]
    assert len(hits) == 33
    # every one of them is a haiku-verdict, sonnet-truth, requested-opus row
    assert all(d["items"][k]["llm"] == "haiku" and d["items"][k]["truth"] == "sonnet"
               and d["items"][k]["requested"] == "opus" for k in hits)


# --------------------------------------------------------------------------- b) truth rules

@pytest.mark.parametrize("j,rule,expected", [
    (judges("opus"), "majority3", "opus"),
    (judges("opus", "opus", "haiku"), "majority3", "opus"),
    (judges("haiku", "sonnet", "sonnet"), "majority3", "sonnet"),
    ({"opus": "opus", "gpt": "haiku", "sonnet": "sonnet"}, "majority3", "sonnet"),          # 3-way split
    ({"opus": "haiku", "gpt": "opus", "sonnet": "sonnet"}, "majority3", "sonnet"),
    ({"opus": "opus", "gpt": "opus", "sonnet": None}, "majority3", None),                  # needs all three
    ({"opus": "opus", "gpt": None, "sonnet": None}, "opus_only", "opus"),
    ({"opus": "opus", "gpt": "haiku", "sonnet": None}, "gpt_only", "haiku"),
    ({"opus": "opus", "gpt": "haiku", "sonnet": "sonnet"}, "sonnet_only", "sonnet"),
    ({"opus": None, "gpt": "haiku", "sonnet": None}, "opus_only", None),
    ({"opus": "opus", "gpt": "opus", "sonnet": None}, "agree_only", "opus"),               # >= 2 present, agree
    ({"opus": "opus", "gpt": "haiku", "sonnet": None}, "agree_only", None),
    ({"opus": "opus", "gpt": None, "sonnet": None}, "agree_only", None),                   # one judge is not agreement
    ({"opus": "opus", "gpt": "haiku", "sonnet": "opus"}, "agree_only", None),
    ({"opus": "opus", "gpt": "sonnet", "sonnet": None}, "higher2", "opus"),
    ({"opus": "sonnet", "gpt": "opus", "sonnet": None}, "higher2", "opus"),
    ({"opus": "sonnet", "gpt": None, "sonnet": "opus"}, "higher2", None),
    ({"opus": "haiku", "gpt": None, "sonnet": "sonnet"}, "gpt_excluded", "sonnet"),
    ({"opus": "opus", "gpt": "haiku", "sonnet": "sonnet"}, "gpt_excluded", "opus"),
    ({"opus": "opus", "gpt": "haiku", "sonnet": None}, "gpt_excluded", None),
    ({"opus": "opus", "gpt": "haiku", "sonnet": "sonnet"}, "other_two_higher:gpt", "opus"),
    ({"opus": "opus", "gpt": "haiku", "sonnet": "sonnet"}, "other_two_higher:opus", "sonnet"),
    ({"opus": "opus", "gpt": "haiku", "sonnet": "sonnet"}, "other_two_higher:sonnet", "opus"),
    ({"opus": None, "gpt": "haiku", "sonnet": "sonnet"}, "other_two_higher:opus", "sonnet"),
    ({"opus": None, "gpt": "haiku", "sonnet": "sonnet"}, "other_two_higher:gpt", None),
])
def test_truth_rules(j, rule, expected):
    assert ce.truth_for({"truths": j}, rule) == expected


def test_unknown_rule_is_refused():
    with pytest.raises(ce.Refuse) as e:
        ce.truth_for({"truths": judges("opus")}, "nonsense")
    assert e.value.code == 2


def test_missing_judge_excluded_counted_and_over_5pct_not_informative(tmp_path, capsys):
    recs = gen_recs(n_groups=40, per=5, arms=False, scored=None)
    for r in recs[:4]:                                  # 4 / 200 = 2% missing: informative on this axis
        r["truths"]["gpt"] = None
    rc, out, _, _ = run(["score", write_table(tmp_path / "a.jsonl", recs), "--boot-b", B_FAST], capsys)
    assert rc == 0 and out["primary"]["n_no_truth"] == 4 and out["n"] == 196
    assert "truth_missing" not in out["reasons"]
    for r in recs[:14]:                                 # 14 / 200 = 7% missing
        r["truths"]["gpt"] = None
    rc, out, _, _ = run(["score", write_table(tmp_path / "b.jsonl", recs), "--boot-b", B_FAST], capsys)
    assert out["primary"]["n_no_truth"] == 14 and "truth_missing" in out["reasons"]
    assert out["status"] == "NOT INFORMATIVE"
    assert out["sensitivity"]["opus_only"]["n_no_truth"] == 0     # the sensitivity rows still run


def test_truth_by_source_and_unsupplied_source(tmp_path, capsys):
    recs = gen_recs(n_groups=40, per=5, arms=False, scored=None)
    for i, r in enumerate(recs):
        r["strata"]["source"] = "A" if i % 2 else "B"
        if r["strata"]["source"] == "B":
            r["truths"] = {"opus": r["truths"]["opus"], "gpt": None, "sonnet": None}   # B has only Opus
    rc, out, _, _ = run(["score", write_table(tmp_path / "t.jsonl", recs), "--boot-b", B_FAST], capsys)
    assert out["primary"]["n_no_truth_by_source"] == {"B": 100}                  # excluded and counted per source
    rc, out2, _, _ = run(["score", tmp_path / "t.jsonl", "--truth-by-source", "B=opus_only", "--boot-b", B_FAST],
                         capsys)
    assert out2["primary"]["n_no_truth"] == 0 and out2["n"] == 200
    assert out2["truth"]["by_source"] == {"B": "opus_only"}


def test_truth_fallback_is_labelled_and_sensitivity_rows_run(tmp_path, capsys):
    recs = gen_recs(n_groups=40, per=5, arms=False, scored=None)
    for r in recs:
        r["truths"]["gpt"] = None
    rc, out, _, _ = run(["score", write_table(tmp_path / "t.jsonl", recs), "--truth", "other_two_higher",
                         "--voided", "gpt", "--boot-b", B_FAST], capsys)
    assert rc == 0 and out["truth"]["truth_fallback"] == "gpt" and out["primary"]["n_no_truth"] == 0
    assert out["sensitivity"]["gpt_only"]["evaluable"] is False        # nothing to score under a voided judge
    assert out["sensitivity"]["opus_only"]["evaluable"] is True
    assert out["robust_under_single_judge_truths"] == {}               # no candidate arms in this table


# --------------------------------------------------------------------------- c) refusals

def _refusal_recs(n_none):
    recs = []
    for i in range(40):
        truth = "opus" if i % 2 else "sonnet"
        recs.append(item(f"r{i}", f"g{i}", truth,
                         {"rules_eff": "sonnet", "x": None if i < n_none else truth}))
    return recs


def test_refusal_scored_as_rules_eff_counted_never_correct(tmp_path, capsys):
    rc, out, _, _ = run(["score", write_table(tmp_path / "t.jsonl", _refusal_recs(2)), "--list-fallback-ids",
                         "--boot-b", B_FAST], capsys)
    x = out["arms"]["x"]
    assert x["n_fallback"] == 2 and out["fallback_ids"]["x"] == ["r0", "r1"]
    # r1 is an opus-truth item the arm refused: it is scored as rules_eff's sonnet, so it is under-routed
    assert x["under"]["k"] == 1 and x["share"]["sonnet"] == 1.0 - x["share"]["opus"] - x["share"]["haiku"]
    assert not x["errors_gt_5pct"] and not x["errors_gt_10pct"]
    rows, _ = ce.prepare(ce.load_table(tmp_path / "t.jsonl")[1], ["rules_eff", "x"], "majority3")
    assert [r["preds"]["x"] for r in rows[:2]] == ["sonnet", "sonnet"]
    assert [r["fb"]["x"] for r in rows[:2]] == [True, True]


def test_refusal_rates_flag_and_not_informative(tmp_path, capsys):
    rc, out, _, _ = run(["score", write_table(tmp_path / "a.jsonl", _refusal_recs(3)), "--boot-b", B_FAST], capsys)
    assert out["arms"]["x"]["errors_gt_5pct"] is True and out["arms"]["x"]["errors_gt_10pct"] is False
    rc, out, _, _ = run(["score", write_table(tmp_path / "b.jsonl", _refusal_recs(5)), "--boot-b", B_FAST], capsys)
    assert out["arms"]["x"]["errors_gt_10pct"] is True
    assert "errors" in out["primary"]["comparisons"]["x"]["reasons"]
    assert out["primary"]["comparisons"]["x"]["status"] == "NOT INFORMATIVE"


def test_rules_eff_refusal_excludes_the_item(tmp_path, capsys):
    recs = _refusal_recs(0)
    recs[0]["preds"]["rules_eff"] = None
    rc, out, _, _ = run(["score", write_table(tmp_path / "t.jsonl", recs), "--boot-b", B_FAST], capsys)
    assert out["primary"]["n_no_rules_eff"] == 1 and out["n"] == 39


# --------------------------------------------------------------------------- d) the P17D contradiction

def _p17d_like(n_groups=50, per=6, seed=3):
    rng = random.Random(seed)
    recs = []
    for g in range(n_groups):
        for k in range(per):
            truth = "opus" if rng.random() < 0.42 else rng.choice(["haiku", "sonnet"])
            recs.append(item(f"p{g}_{k}", f"g{g}", truth,
                             {"rules_eff": "sonnet", "always_opus": "opus", "always_sonnet": "sonnet"}))
    return recs


def test_always_opus_beats_both_bars_but_fails_the_guard(tmp_path, capsys):
    rc, out, _, _ = run(["score", write_table(tmp_path / "t.jsonl", _p17d_like()), "--boot-b", B_FAST], capsys)
    ao = out["primary"]["comparisons"]["always_opus"]
    assert ao["under"]["cand"] == 0.0 and ao["under"]["diff"] < 0 and ao["cost"]["diff"] < 0
    assert ao["bars"] is True
    assert ao["non_inferior"]["ok"] is True
    assert ao["guard"]["passes"] is False and ao["guard"]["q"] == 1.0 and ao["guard"]["e_under_mix"] == 0.0
    assert ao["status"] == "SCREEN-REJECT"
    assert out["validity"]["checks"]["always_opus_fails_guard"] is True


# --------------------------------------------------------------------------- e) guard math

def test_mix_q_both_sides_of_the_knee():
    assert ce.mix_q(2.7) == 0.0                                  # all sonnet
    assert ce.mix_q(6.15) == 1.0                                 # all opus
    assert ce.mix_q(3.425) == pytest.approx((3.425 - 2.7) / 3.45)
    assert ce.mix_q(1.0) == 0.0                                  # all haiku (sonnet share 0)
    assert ce.mix_q(1.85) == pytest.approx(0.5)                  # half haiku, half sonnet
    assert ce.mix_q(2.6999999) == pytest.approx((2.6999999 - 1.0) / 1.7)
    assert ce.mix_q(9.0) == 1.0 and ce.mix_q(0.5) == 0.0         # clipped to [0, 1]


@pytest.mark.parametrize("raw", [1.4, 2.3, 2.7, 3.425, 5.0, 6.15])
def test_mix_under_matches_brute_force_enumeration(raw):
    truths = ["haiku", "sonnet", "opus", "opus", "sonnet"]
    q = ce.mix_q(raw)
    lo, hi = ("sonnet", "opus") if raw >= 2.7 else ("haiku", "sonnet")
    # enumerate every assignment of the mixture tiers to the five items
    total = 0.0
    for combo in itertools.product((lo, hi), repeat=len(truths)):
        p = 1.0
        for c in combo:
            p *= q if c == hi else 1 - q
        total += p * sum(se.RANK[c] < se.RANK[t] for c, t in zip(combo, truths)) / len(truths)
    uh = sum(se.RANK[t] > 1 for t in truths) / 5
    us = sum(se.RANK[t] > 2 for t in truths) / 5
    assert ce.mix_under(raw, uh, us) == pytest.approx(total, abs=1e-12)


def test_guard_bound_is_below_zero_with_tolerance():
    assert ce.guard(-0.01, 3.0) is True
    assert ce.guard(0.0, 6.15) is False
    assert ce.guard(-1e-12, 6.15) is False       # float noise around exactly zero is not a pass
    assert ce.guard(None, 3.0) is False


# --------------------------------------------------------------------------- f) bootstrap

def _cluster_table(clustered: bool):
    recs = []
    for i in range(20):
        grp = ("a" if i < 10 else "b") if clustered else f"s{i}"
        # items 0-9: the candidate under-routes where rules does not; 10-19 identical
        truth = "opus" if i < 10 else "sonnet"
        recs.append(item(f"c{i}", grp, truth, {"rules_eff": "opus" if i < 10 else "sonnet",
                                              "cand": "sonnet" if i < 10 else "sonnet"}))
    return recs


def _an(recs, seed=ce.SEED, B=B_FAST, arm="cand"):
    items_ = [ce._parse_item(r, k) for k, r in enumerate(recs)]
    rows, _ = ce.prepare(items_, ["rules_eff", arm], "majority3")
    return ce.analyze_arm(ce.group_sums(rows, ["rules_eff", arm]), arm, "rules_eff", B, seed)


def test_bootstrap_is_group_clustered():
    one_group = _an(_cluster_table(True))["under"]["ci95"]
    singles = _an(_cluster_table(False))["under"]["ci95"]
    assert one_group[1] - one_group[0] > singles[1] - singles[0] + 0.1
    assert _an(_cluster_table(True))["under"]["diff"] == _an(_cluster_table(False))["under"]["diff"]


def test_bootstrap_seed_changes_result_same_seed_identical():
    recs = gen_recs(n_groups=30, per=3, arms=False, scored=None)
    for r in recs:
        r["preds"]["cand"] = r["truths"]["opus"] if random.Random(r["item_id"]).random() < 0.6 else "sonnet"
    a1, a2, b = _an(recs, seed=1), _an(recs, seed=1), _an(recs, seed=2)
    assert a1 == a2
    assert (a1["under"]["ci95"], a1["cost"]["ci95"]) != (b["under"]["ci95"], b["cost"]["ci95"])
    assert a1["under"]["diff"] == b["under"]["diff"]


def test_percentile_indices_follow_shadow_eval():
    vals = [float(i) for i in range(2000)]
    assert ce.ci95(vals) == [50.0, 1949.0]                 # int(0.025 B), int(0.975 B) - 1
    assert ce.upper95(vals) == 1899.0                      # one-sided 95th percentile
    assert ce.lower_pct(vals, 0.20) == 400.0


def test_bootstrap_matches_shadow_eval_boot_diff_on_the_same_resamples():
    recs = gen_recs(n_groups=25, per=4, arms=False, scored=None)
    for r in recs:
        r["preds"]["cand"] = "opus" if random.Random(r["item_id"]).random() < 0.5 else "sonnet"
    an = _an(recs, seed=20261003, B=2000)
    its = [ce._parse_item(r, k) for k, r in enumerate(recs)]
    rows, _ = ce.prepare(its, ["rules_eff", "cand"], "majority3")
    se_items = [{"session_id": r["group"], "rows": r} for r in rows]
    u = lambda arm: (lambda it: 1.0 if se.RANK[it["rows"]["preds"][arm]] < se.RANK[it["rows"]["truth"]] else 0.0)  # noqa: E731
    c = lambda arm: (lambda it: se.cost_eff(se.clamp(it["rows"]["preds"][arm], it["rows"]["req"]), it["rows"]["truth"]))  # noqa: E731
    bu = se.boot_diff(se_items, u("cand"), u("rules_eff"), 2000, 20261003)
    bc = se.boot_diff(se_items, c("cand"), c("rules_eff"), 2000, 20261003)
    assert an["under"]["ci95"] == pytest.approx(bu["ci95"], abs=1e-4)
    assert an["cost"]["ci95"] == pytest.approx(bc["ci95"], abs=1e-4)
    assert an["under"]["diff"] == pytest.approx(bu["diff"], abs=1e-4)


def test_bootstrap_cost_is_per_group_not_per_item():
    """r2-3: a resample costs O(groups). Counted, not timed."""
    def visits(n_items, n_groups):
        recs = [item(f"v{i}", f"g{i % n_groups}", "sonnet", {"rules_eff": "sonnet", "cand": "sonnet"})
                for i in range(n_items)]
        before = ce.OPS["group_visits"]
        _an(recs, B=B_FAST)
        return ce.OPS["group_visits"] - before
    assert visits(80, 10) == visits(800, 10) == B_FAST * 10
    assert visits(800, 100) == B_FAST * 100


# --------------------------------------------------------------------------- g) non-inferiority

def test_non_inferiority_boundary():
    assert ce.non_inferior(0.03, 0.0) is True
    assert ce.non_inferior(0.0301, 0.0) is False
    assert ce.non_inferior(0.0, 0.3) is True
    assert ce.non_inferior(0.0, 0.3001) is False
    assert ce.non_inferior(-0.2, -1.0) is True


def test_bars_hold_on_point_estimates():
    assert ce.bars_hold(0.2, 0.2, 5.0, 5.0) is True
    assert ce.bars_hold(0.2001, 0.2, 5.0, 5.0) is False
    assert ce.bars_hold(0.1, 0.2, 5.0001, 5.0) is False
    assert ce.bars_hold(0.19, 0.2, 4.0, 5.0) is True


@pytest.mark.parametrize("bars,ni,g,cu,cc,label", [
    (True, True, True, [-0.2, -0.01], [-1.0, -0.1], "PASS-STRONG"),
    (True, True, True, [-0.2, 0.02], [-1.0, -0.1], "PASS-WEAK"),
    (True, True, True, [-0.2, -0.01], [-1.0, 0.1], "PASS-WEAK"),
    (False, True, True, [-0.2, -0.01], [-1.0, -0.1], "FAIL"),
    (True, False, True, [-0.2, -0.01], [-1.0, -0.1], "FAIL"),
    (True, True, False, [-0.2, -0.01], [-1.0, -0.1], "FAIL"),
    (True, True, True, [0.01, 0.2], [-1.0, -0.1], "FAIL"),          # significantly worse on under
    (True, True, True, [-0.2, -0.01], [0.01, 0.2], "FAIL"),         # significantly worse on cost
])
def test_verdict_labels(bars, ni, g, cu, cc, label):
    assert ce.label_core(bars, ni, g, cu, cc) == label


# --------------------------------------------------------------------------- h) unlifted arm

def test_unlifted_arm_is_reported_and_never_the_bar(tmp_path, capsys):
    base = gen_recs(n_groups=30, per=4, arms=False, scored=None)
    for r in base:
        r["preds"]["rules_unlifted"] = "haiku"
    rc, out1, _, _ = run(["score", write_table(tmp_path / "a.jsonl", base), "--boot-b", B_FAST], capsys)
    assert "rules_unlifted" in out1["arms"] and "rules_unlifted" in out1["primary"]["comparisons"]
    for r in base:
        r["preds"]["rules_unlifted"] = "opus"
    rc, out2, _, _ = run(["score", write_table(tmp_path / "b.jsonl", base), "--boot-b", B_FAST], capsys)
    assert out1["arms"]["rules_eff"] == out2["arms"]["rules_eff"]
    assert out1["primary"]["comparisons"]["rules_unlifted"]["under"]["base"] == \
        out1["arms"]["rules_eff"]["under"]["rate"]
    assert out1["primary"]["comparisons"]["rules_unlifted"]["under"]["cand"] != \
        out2["primary"]["comparisons"]["rules_unlifted"]["under"]["cand"]


# --------------------------------------------------------------------------- i) single use

def test_single_use_log_second_run_exits_4_and_started_precedes_scoring(tmp_path, capsys, monkeypatch):
    table = write_table(tmp_path / "t.jsonl", _golden())
    log = tmp_path / "use.log"
    seen = {}
    real = ce.run_score

    def spy(*a, **k):
        seen["log_at_scoring"] = log.read_text(encoding="utf-8")
        return real(*a, **k)
    monkeypatch.setattr(ce, "run_score", spy)
    rc, out, _, _ = run(["score", table, "--use-log", log, "--single-use", "--boot-b", B_FAST], capsys)
    assert rc == 0
    started = [json.loads(x) for x in seen["log_at_scoring"].splitlines()]
    assert len(started) == 1 and started[0]["event"] == "started"
    assert started[0]["harness_sha"] == out["harness_sha"] and started[0]["table_sha"] == out["table_sha"]
    assert started[0]["utc"].endswith("Z")
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o600
    before = log.read_text(encoding="utf-8")
    rc, out2, stdout, err = run(["score", table, "--use-log", log, "--single-use"], capsys)
    assert rc == 4 and out2 is None and stdout == ""
    assert log.read_text(encoding="utf-8") == before            # never rewritten, never extended


def test_single_use_crash_counts_as_the_use(tmp_path, capsys, monkeypatch):
    table = write_table(tmp_path / "t.jsonl", _golden())
    log = tmp_path / "use.log"

    def boom(*a, **k):
        raise RuntimeError("scorer crashed")
    monkeypatch.setattr(ce, "run_score", boom)
    with pytest.raises(RuntimeError):
        ce.main(["score", table, "--use-log", str(log), "--single-use"])
    capsys.readouterr()
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1
    monkeypatch.undo()
    rc, _, _, _ = run(["score", table, "--use-log", log, "--single-use"], capsys)
    assert rc == 4


def test_single_use_needs_a_log_and_a_bad_table_does_not_consume_it(tmp_path, capsys):
    rc, _, _, err = run(["score", write_table(tmp_path / "t.jsonl", _golden()), "--single-use"], capsys)
    assert rc == 2
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"item_id": "x"}\n', encoding="utf-8")
    log = tmp_path / "use.log"
    rc, _, _, _ = run(["score", bad, "--use-log", log, "--single-use"], capsys)
    assert rc == 2 and not log.exists()


def test_single_use_without_calibration_is_not_informative_dev_run_only_warns(tmp_path, capsys):
    table = write_table(tmp_path / "t.jsonl", _golden())
    rc, out, _, _ = run(["score", table, "--use-log", tmp_path / "u.log", "--single-use", "--boot-b", B_FAST],
                        capsys)
    assert "calibration_unknown" in out["reasons"] and out["status"] == "NOT INFORMATIVE"
    rc, out, _, _ = run(["score", table, "--boot-b", B_FAST], capsys)
    assert "calibration_unknown" in out["warnings"] and "calibration_unknown" not in out["reasons"]


def test_calibration_failure_is_not_informative(tmp_path, capsys):
    table = write_table(tmp_path / "t.jsonl", _golden())
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps({"opus": {"planted_ok": 6, "planted_n": 6}, "gpt": {"planted_ok": 5, "planted_n": 6}}))
    rc, out, _, _ = run(["score", table, "--calibration", cal, "--boot-b", B_FAST], capsys)
    assert "calibration" in out["reasons"] and out["calibration"]["failed_judges"] == ["gpt"]
    cal.write_text(json.dumps({"opus": {"planted_ok": 6, "planted_n": 6}}))
    rc, out, _, _ = run(["score", table, "--calibration", cal, "--boot-b", B_FAST], capsys)
    assert "calibration" not in out["reasons"]


# --------------------------------------------------------------------------- j) input guard

@pytest.mark.parametrize("path", [("text",), ("prompt",), ("digest",), ("reason",), ("content",),
                                  ("strata", "prompt"), ("preds", "content"), ("scores", "x", "reason")])
def test_forbidden_key_is_refused_naming_the_key_never_the_value(tmp_path, capsys, path):
    sentinel = "SENTINEL-PROMPT-BODY-91827"
    rec = _golden()[0]
    if len(path) == 1:
        rec[path[0]] = sentinel
    else:
        cur = rec.setdefault(path[0], {})
        for p in path[1:-1]:
            cur = cur.setdefault(p, {})
        cur[path[-1]] = sentinel
    out_file = tmp_path / "out.json"
    rc, _, stdout, err = run(["score", write_table(tmp_path / "t.jsonl", [rec] + _golden()[1:]), "--out", out_file],
                             capsys)
    assert rc == 2
    assert path[-1] in err
    assert sentinel not in stdout + err and not out_file.exists()


def test_unknown_tier_is_an_error_and_bad_records_are_refused(tmp_path, capsys):
    for mutate in (lambda r: r["preds"].update(cand="gpt-5"), lambda r: r["truths"].update(opus="mega"),
                   lambda r: r.update(requested="large"), lambda r: r.update(group=""),
                   lambda r: r["truths"].update(claude="opus"), lambda r: r.update(fold=9),
                   lambda r: r.update(weight=0)):
        recs = _golden()
        mutate(recs[0])
        rc, _, stdout, _ = run(["score", write_table(tmp_path / "t.jsonl", recs)], capsys)
        assert rc == 2 and stdout == ""
    recs = _golden()
    recs[1]["item_id"] = recs[0]["item_id"]
    assert run(["score", write_table(tmp_path / "t.jsonl", recs)], capsys)[0] == 2


def test_local_counts_as_haiku_and_model_ids_normalise(tmp_path, capsys):
    recs = _golden()
    recs[0]["preds"]["cand"] = "local"
    recs[1]["preds"]["cand"] = "claude-opus-4"
    items_ = [ce._parse_item(r, k) for k, r in enumerate(recs)]
    assert items_[0]["preds"]["cand"] == "haiku" and items_[1]["preds"]["cand"] == "opus"


# --------------------------------------------------------------------------- k) counts only

def test_output_is_counts_only(tmp_path, capsys):
    recs = gen_recs(n_groups=30, per=4, arms=False, scored=None)
    for i, r in enumerate(recs):
        r["item_id"] = f"SENTINEL-ID-{i}-ZQ"
        r["group"] = f"SENTINEL-GRP-{i // 4}-ZQ"
        r["strata"]["task_type"] = "SENTINEL-STRATUM-QZ" if i < 3 else "code"
    rc, out, stdout, _ = run(["score", write_table(tmp_path / "t.jsonl", recs), "--boot-b", B_FAST], capsys)
    assert "SENTINEL-ID" not in stdout and "SENTINEL-GRP" not in stdout
    assert "fallback_ids" not in out
    stripped = dict(out)
    stripped.pop("strata")
    assert "SENTINEL-STRATUM" not in json.dumps(stripped)           # a stratum name only ever as a stratum key
    tab = out["strata"]["rules_eff"]["task_type"]
    assert tab["SENTINEL-STRATUM-QZ"] == {"n": 3, "under_k": tab["SENTINEL-STRATUM-QZ"]["under_k"]}
    assert set(tab["code"]) >= {"n", "under_k", "under", "c_lambda2_clamp", "raw_cost"}      # n >= 5: rates too
    assert set(out["strata"]["rules_eff"]) == {"length_band", "truth", "task_type", "tool_need", "generator",
                                                "contested", "source"}


def test_out_file_is_private_and_equals_stdout(tmp_path, capsys):
    dest = tmp_path / "out.json"
    dest.write_text("old")
    os.chmod(dest, 0o644)
    rc, out, stdout, _ = run(["score", write_table(tmp_path / "t.jsonl", _golden()), "--out", dest,
                              "--boot-b", B_FAST], capsys)
    assert stat.S_IMODE(os.stat(dest).st_mode) == 0o600 and dest.read_text() == stdout


# --------------------------------------------------------------------------- l) fit

def _fit_recs(n_rep=4, haiku_hi=False, opus_heavy=False):
    """Pattern (per replica, 100 items): 10 truth-opus + 10 truth-sonnet that look alike (p_opus 0.5), 10 sure opus,
    70 others (sonnet/haiku, p_opus 0.02). With opus_heavy the confusable block is where routing opus is cheapest."""
    recs = []
    k = 0
    for rep in range(n_rep):
        block = (["opus"] * 10 + ["sonnet"] * 10, 0.5), (["opus"] * 10, 0.95), (["sonnet"] * 40 + ["haiku"] * 30, 0.02)
        for truths, po in block:
            for t in truths:
                ph = 0.9 if (haiku_hi and t == "haiku" and k % 6 == 0) else 0.01
                recs.append(item(f"f{k}", f"g{k // 2}", t, {"rules_eff": "sonnet"}, fold=(k // 2) % 5,
                                 scores={"cand": {"p_haiku": ph, "p_sonnet": 0.2, "p_opus": po}}))
                k += 1
    return recs


def _fit_args(table, extra=()):
    return ["fit", table, "--arm", "cand", "--boot-b", B_FAST, *extra]


def test_fit_is_deterministic_and_counts_only(tmp_path, capsys):
    table = write_table(tmp_path / "t.jsonl", _fit_recs())
    rc1, o1, s1, _ = run(_fit_args(table), capsys)
    rc2, o2, s2, _ = run(_fit_args(table), capsys)
    assert rc1 == rc2 == 0 and s1 == s2                         # byte-identical
    assert o1["admissible"] is True and o1["grid_size"] == 192 and len(o1["fold_thresholds"]) == 5
    assert '"f0"' not in s1 and "item_id" not in s1
    assert o1["oos"]["bars"] is True
    assert o1["frozen"]["t_hi"] in ce.GRID_HI and o1["frozen"]["t_lo"] in ce.GRID_LO


def test_fit_grid_is_the_192_point_grid():
    assert len(ce.GRID_HI) == 16 and ce.GRID_HI[0] == 0.20 and ce.GRID_HI[-1] == 0.95
    assert ce.GRID_LO == [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99, 1.01]
    # 16 x 11 = 176 points with t_lo in {0.50 .. 0.99}; the brief's extra t_lo = 1.01 (never haiku) adds 16
    assert len(ce.GRID_HI) * (len(ce.GRID_LO) - 1) == 176
    assert len(ce.GRID_HI) * len(ce.GRID_LO) == 192


def test_fit_no_admissible_point_exits_5(tmp_path, capsys):
    recs = _fit_recs()
    for r in recs:                                       # rules_eff is perfect: no candidate beats it
        r["preds"]["rules_eff"] = r["truths"]["opus"]
    rc, out, _, _ = run(_fit_args(write_table(tmp_path / "t.jsonl", recs)), capsys)
    assert rc == 5 and out["admissible"] is False


def _fit_recs_with_haiku_routes(n_precise):
    """The _fit_recs pattern x4 (distinct groups), with n_precise haiku-truth items scoring p_haiku 0.9."""
    recs = []
    for rep in range(4):
        for r in _fit_recs(n_rep=1):
            r = json.loads(json.dumps(r))
            r["item_id"] = f"{r['item_id']}_{rep}"
            r["group"] = f"{r['group']}_{rep}"
            r["fold"] = (int(r["group"].split("_")[0][1:]) + rep) % 5
            recs.append(r)
    groups = {}
    for r in recs:                                       # a group keeps one fold
        r["fold"] = groups.setdefault(r["group"], len(groups) % 5)
    for r in [r for r in recs if r["truths"]["opus"] == "haiku"][:n_precise]:
        r["scores"]["cand"]["p_haiku"] = 0.9
    return recs


def test_fit_constraint_3_forces_no_haiku_below_10_routes(tmp_path, capsys):
    # 6 precise haiku routes are cheaper than sonnet, but 6 < 10 routes: only t_lo = 1.01 is admissible
    rc, out, _, _ = run(_fit_args(write_table(tmp_path / "a.jsonl", _fit_recs_with_haiku_routes(6))), capsys)
    assert rc == 0 and out["frozen"]["t_lo"] == 1.01 and out["frozen"]["n_haiku_routes"] == 0
    # with >= 10 precise haiku routes the fit may route haiku
    rc, out, _, _ = run(_fit_args(write_table(tmp_path / "b.jsonl", _fit_recs_with_haiku_routes(14))), capsys)
    assert rc == 0 and out["frozen"]["t_lo"] < 1.01 and out["frozen"]["n_haiku_routes"] >= 10


def test_fit_guard_rejects_an_uninformative_scorer(tmp_path, capsys, monkeypatch):
    """Constraint 4: scores that carry no signal can still beat rules_eff on under-route by routing opus at random,
    exactly as an uninformed mixture does. Only the NOT-TRIVIAL guard rejects that."""
    rng = random.Random(9)
    recs = []
    for k in range(600):
        truth = "opus" if rng.random() < 0.42 else rng.choice(["haiku", "sonnet"])
        recs.append(item(f"u{k}", f"g{k // 3}", truth, {"rules_eff": "sonnet"}, fold=(k // 3) % 5,
                          scores={"cand": {"p_haiku": 0.01, "p_sonnet": 0.2, "p_opus": round(rng.random(), 3)}}))
    table = write_table(tmp_path / "t.jsonl", recs)
    rc, out, _, _ = run(_fit_args(table), capsys)
    assert rc == 5 and out["admissible"] is False
    monkeypatch.setattr(ce, "guard", lambda upper, raw: True)
    rc, out, _, _ = run(_fit_args(table), capsys)
    assert rc == 0 and out["admissible"] is True            # without the guard the random scorer is admitted


def test_weights_enter_the_guard_mixture():
    recs = [item("a", "g1", "opus", {"rules_eff": "sonnet", "c": "sonnet"}, weight=3.0),
            item("b", "g2", "sonnet", {"rules_eff": "sonnet", "c": "sonnet"}, weight=1.0)]
    an = _an(recs, arm="c")
    assert an["guard"]["e_under_mix"] == pytest.approx(0.75)      # raw 2.7: all sonnet; weighted opus share 3/4
    assert an["under"]["cand"] == pytest.approx(0.75)
    recs = [item("a", "g1", "opus", {"rules_eff": "sonnet", "c": "haiku"}, weight=3.0),
            item("b", "g2", "haiku", {"rules_eff": "sonnet", "c": "haiku"}, weight=1.0)]
    assert _an(recs, arm="c")["guard"]["e_under_mix"] == pytest.approx(0.75)    # raw 1.0: all haiku; share above haiku


def test_fit_haiku_precision_floor():
    assert ce.haiku_ok(0, None, 1.01) is True
    assert ce.haiku_ok(9, 1.0, 0.9) is False            # fewer than 10 routes
    assert ce.haiku_ok(10, 0.70, 0.9) is True
    assert ce.haiku_ok(10, 0.699, 0.9) is False
    assert ce.haiku_ok(40, None, 0.9) is False


def test_fit_refuses_a_group_spanning_folds_and_missing_folds_or_scores(tmp_path, capsys):
    recs = _fit_recs()
    recs[1]["group"] = recs[0]["group"]
    recs[1]["fold"] = (recs[0]["fold"] + 1) % 5
    rc, _, stdout, err = run(_fit_args(write_table(tmp_path / "t.jsonl", recs)), capsys)
    assert rc == 2 and stdout == "" and "spans folds" in err
    recs = _fit_recs()
    del recs[3]["fold"]
    assert run(_fit_args(write_table(tmp_path / "u.jsonl", recs)), capsys)[0] == 2
    recs = _fit_recs()
    del recs[3]["scores"]
    assert run(_fit_args(write_table(tmp_path / "v.jsonl", recs)), capsys)[0] == 2
    assert run(["fit", write_table(tmp_path / "w.jsonl", _fit_recs()), "--arm", "nope"], capsys)[0] == 2


def test_fit_opus_share_constraint_binds(tmp_path):
    rows = _fit_rows()
    res = ce.fit(rows, "cand", B_FAST, ce.SEED)
    assert res["admissible"] and res["opus_share"] <= res["truth_opus_share"] + 1e-9
    assert res["t_hi"] > 0.5            # opus on the confusable block would exceed the truth share


def _fit_rows():
    items_ = [ce._parse_item(r, k) for k, r in enumerate(_fit_recs())]
    return ce.prepare(items_, ["rules_eff"], "majority3")[0]


def test_fit_decision_form():
    assert ce.decide((0.1, 0.1, 0.30), 0.25, 0.5) == "opus"
    assert ce.decide((0.9, 0.0, 0.10), 0.25, 0.5) == "haiku"
    assert ce.decide((0.9, 0.0, 0.30), 0.25, 0.5) == "opus"            # opus test comes first
    assert ce.decide((0.4, 0.5, 0.10), 0.25, 0.5) == "sonnet"
    assert ce.decide((0.9, 0.0, 0.10), 0.25, 1.01) == "sonnet"          # 1.01: never haiku


def test_fit_bootstrap_work_is_counted_per_group_and_per_distinct_decision():
    """r2-3: 192 grid points are decided (176 plus the never-haiku column), but bootstraps only run for points that pass the cheap constraints, at most
    once per distinct decision vector, each costing B x groups."""
    rows = _fit_rows()
    n_groups = len({r["group"] for r in rows})
    before = dict(ce.OPS)
    ce.fit(rows, "cand", B_FAST, ce.SEED)
    boots = ce.OPS["bootstraps"] - before["bootstraps"]
    visits = ce.OPS["group_visits"] - before["group_visits"]
    assert 1 <= boots <= len(ce.GRID_HI) * len(ce.GRID_LO)
    assert visits == boots * B_FAST * n_groups


def test_held_out_fold_cannot_change_the_admitted_thresholds(tmp_path, capsys):
    base = _fit_recs()
    rc, o1, _, _ = run(_fit_args(write_table(tmp_path / "a.jsonl", base)), capsys)
    changed = _fit_recs()
    for r in changed:
        if r["fold"] == 2:                     # flip every held-out truth of fold 2
            t = r["truths"]["opus"]
            new = "opus" if t != "opus" else "haiku"
            r["truths"] = {"opus": new, "gpt": new, "sonnet": new}
    rc, o2, _, _ = run(_fit_args(write_table(tmp_path / "b.jsonl", changed)), capsys)
    f1 = {x["fold"]: x for x in o1["fold_thresholds"]}
    f2 = {x["fold"]: x for x in o2["fold_thresholds"]}
    assert f1[2] == f2[2]                      # fold 2 was held out for its own fit
    assert any(f1[k] != f2[k] for k in (0, 1, 3, 4)) or o1["frozen"] != o2["frozen"]   # the data did matter


def test_fit_writes_the_oos_decisions_privately(tmp_path, capsys):
    dest = tmp_path / "dec.jsonl"
    rc, out, _, _ = run(_fit_args(write_table(tmp_path / "t.jsonl", _fit_recs()), ["--decisions-out", dest]), capsys)
    lines = [json.loads(x) for x in dest.read_text().splitlines()]
    assert rc == 0 and len(lines) == out["n"] == 400 and stat.S_IMODE(os.stat(dest).st_mode) == 0o600
    assert set(lines[0]) == {"item_id", "decision"} and {x["decision"] for x in lines} <= set(TIERS)


def test_calibration_record_in_the_table_header_is_read(tmp_path, capsys):
    table = write_table(tmp_path / "t.jsonl", _golden(), header={"calibration": {"gpt": {"planted_ok": 5, "planted_n": 6}}})
    rc, out, _, _ = run(["score", table, "--boot-b", B_FAST], capsys)
    assert "calibration" in out["reasons"]


def test_pooled_oos_and_split_rows_helpers(tmp_path, capsys):
    recs = _fit_recs()
    for i, r in enumerate(recs):
        r["kind"] = "syn" if i % 3 == 0 else "real"
        r["group"] = f"{r['kind']}-{r['group']}"
    # a group must keep one fold even after the kind relabel: groups are per kind and per original group
    folds = {}
    for r in recs:
        r["fold"] = folds.setdefault(r["group"], len(folds) % 5)
    table = write_table(tmp_path / "t.jsonl", recs)
    rc, out, _, _ = run(["score", table, "--arm", "cand", "--pooled-oos", "--split-rows", "real,syn",
                         "--boot-b", B_FAST], capsys)
    assert rc == 0
    po = out["pooled_oos"]
    assert po["admissible"] is True and set(("meets_bars", "guard", "non_inf")) <= set(po)
    sr = out["split_rows"]["kinds"]
    assert set(sr) == {"real", "syn"}
    assert sr["real"]["n"] == sum(r["kind"] == "real" for r in recs)
    assert sr["syn"]["n"] == sum(r["kind"] == "syn" for r in recs)
    assert set(("bars", "guard", "non_inf", "under", "cost", "significantly_worse",
                "meets_criterion4_conditions")) <= set(sr["real"])


def test_split_rows_flags_a_decision_set_good_on_syn_bad_on_real():
    """r3-2: the same pooled decisions, scored per kind, expose a shift: good on syn, bad on real."""
    rng = random.Random(5)
    rows = []
    for g in range(120):
        kind = "syn" if g % 2 else "real"
        for k in range(4):
            truth = rng.choice(["sonnet", "opus"])
            pred = truth if kind == "syn" else "haiku"            # perfect on syn, always haiku on real
            rows.append({"id": f"{g}_{k}", "group": f"{kind}{g}", "kind": kind, "w": 1.0, "fold": g % 5,
                         "truth": truth, "req": "opus", "req_imp": "opus", "strata": {}, "scores": {},
                         "preds": {"rules_eff": "sonnet", "d": pred}, "fb": {"rules_eff": False, "d": False}})
    parts = ce.split_by_kind(rows, ["real", "syn"])
    res = {k: ce.analyze_arm(ce.group_sums(v, ["rules_eff", "d"]), "d", "rules_eff", B_FAST, ce.SEED)
           for k, v in parts.items()}
    assert res["real"]["_worse"] is True and res["real"]["bars"] is False
    assert res["syn"]["_worse"] is False and res["syn"]["bars"] is True
    assert res["syn"]["non_inferior"]["ok"] is True


# --------------------------------------------------------------------------- m) pin

def test_shadow_eval_pin_mismatch_exits_3_and_match_passes(tmp_path, capsys):
    table = write_table(tmp_path / "t.jsonl", _golden())
    rc, _, stdout, err = run(["score", table, "--pin-shadow-eval-sha", "0" * 64], capsys)
    assert rc == 3 and stdout == ""
    sha = ce.sha256_file(se.__file__)
    rc, out, _, _ = run(["score", table, "--pin-shadow-eval-sha", sha, "--boot-b", B_FAST], capsys)
    assert rc == 0 and out["shadow_eval_sha"] == sha
    assert out["harness_sha"] == ce.sha256_file(ROOT / "scripts" / "cls_eval.py")
    assert out["table_sha"] == ce.sha256_file(tmp_path / "t.jsonl")


# --------------------------------------------------------------------------- n) cost parity

def test_cost_matches_shadow_eval_on_random_tables():
    rng = random.Random(11)
    for _ in range(25):
        recs = []
        for i in range(30):
            truth = rng.choice(TIERS)
            recs.append(item(f"c{i}", f"g{i % 7}", truth,
                             {"rules_eff": rng.choice(TIERS), "a": rng.choice(TIERS)},
                             requested=rng.choice([None, *TIERS])))
        items_ = [ce._parse_item(r, k) for k, r in enumerate(recs)]
        rows, _ = ce.prepare(items_, ["rules_eff", "a"], "majority3")
        direct = sum(se.cost_eff(se.clamp(r["preds"]["a"], r["req"]), r["truth"]) for r in rows) / len(rows)
        assert ce.cost_mean(rows, "a") == pytest.approx(direct, abs=1e-12)
        assert all(r["req"] == (r0["requested"] or "opus") for r, r0 in zip(rows, items_))
        raw = sum(se.COST[r["preds"]["a"]] for r in rows) / len(rows)
        assert ce.raw_mean(rows, "a") == pytest.approx(raw, abs=1e-12)
        under = sum(se.RANK[r["preds"]["a"]] < se.RANK[r["truth"]] for r in rows) / len(rows)
        assert ce.under_rate(rows, "a") == pytest.approx(under, abs=1e-12)


def test_requested_null_is_opus_and_the_imputation_is_a_sensitivity_row():
    recs = [item("a", "g", "sonnet", {"rules_eff": "opus", "x": "opus"}, requested=None),
            item("b", "g", "opus", {"rules_eff": "sonnet", "x": "sonnet"}, requested="sonnet"),
            item("c", "g", "opus", {"rules_eff": "sonnet", "x": "sonnet"}, requested="sonnet")]
    items_ = [ce._parse_item(r, k) for k, r in enumerate(recs)]
    rows, _ = ce.prepare(items_, ["rules_eff", "x"], "majority3")
    assert [r["req"] for r in rows] == ["opus", "sonnet", "sonnet"]
    assert [r["req_imp"] for r in rows] == ["sonnet", "sonnet", "sonnet"]       # group mode is sonnet
    assert ce.cost_mean(rows[:1], "x") == pytest.approx(6.15)                    # clamp inert (requested null)
    assert ce.cost_mean(rows[:1], "x", imputed=True) == pytest.approx(2.7)      # shadow_eval imputation clamps


def test_weights_apply_to_the_means():
    recs = [item("a", "g1", "opus", {"rules_eff": "sonnet"}, weight=3.0),
            item("b", "g2", "sonnet", {"rules_eff": "sonnet"}, weight=1.0)]
    items_ = [ce._parse_item(r, k) for k, r in enumerate(recs)]
    rows, _ = ce.prepare(items_, ["rules_eff"], "majority3")
    assert ce.under_rate(rows, "rules_eff") == pytest.approx(0.75)


# --------------------------------------------------------------------------- DEV labels, validity, robustness

def _full_out(tmp_path, capsys, mode="dev", extra=()):
    recs = gen_recs(n_groups=80, per=5, scored="cand")
    table = write_table(tmp_path / f"full-{mode}.jsonl", recs)
    # add the noised oracle through the harness itself
    noised = tmp_path / f"noised-{mode}.jsonl"
    assert ce.main(["selftest", table, "--make-noised-oracle", "20261017", "--out", str(noised)]) == 0
    rc, out, stdout, _ = run(["score", noised, "--mode", mode, "--boot-b", B_FAST, *extra], capsys)
    return rc, out, stdout


def test_dev_mode_never_prints_a_pass_label(tmp_path, capsys):
    rc, out, stdout = _full_out(tmp_path, capsys, "dev")
    assert rc == 0 and "PASS-" not in stdout
    assert out["guard_free"] is True and out["screening_only"] is True and out["label"] == ce.DEV_LABEL
    statuses = {c["status"] for c in out["primary"]["comparisons"].values()}
    assert statuses <= {"SCREEN-PASS", "SCREEN-REJECT", "NOT INFORMATIVE"} and "SCREEN-PASS" in statuses
    assert out["primary"]["comparisons"]["oracle"]["status"] == "SCREEN-PASS"
    assert out["primary"]["comparisons"]["always_opus"]["status"] == "SCREEN-REJECT"
    assert ce.display_status("PASS-STRONG", "dev") == "SCREEN-PASS"
    assert ce.display_status("PASS-WEAK", "dev") == "SCREEN-PASS"
    assert ce.display_status("FAIL", "dev") == "SCREEN-REJECT"
    assert ce.display_status("PASS-STRONG", "test") == "PASS-STRONG"


def _cal(tmp_path):
    f = tmp_path / "cal-ok.json"
    f.write_text(json.dumps({j: {"planted_ok": 6, "planted_n": 6} for j in ce.JUDGES}))
    return f


def _noised(tmp_path, header=None, n_groups=80):
    recs = gen_recs(n_groups=n_groups, per=5, scored=None)
    src = write_table(tmp_path / "base.jsonl", recs, header=header)
    dst = tmp_path / "noised.jsonl"
    assert ce.main(["selftest", src, "--make-noised-oracle", "20261017", "--out", str(dst)]) == 0
    return str(dst)


def _test_run(tmp_path, capsys, table, tag="u"):
    return run(["score", table, "--mode", "test", "--single-use", "--use-log", tmp_path / f"{tag}.log",
                "--calibration", _cal(tmp_path), "--boot-b", B_FAST], capsys)


def test_test_mode_is_code_gated_and_prints_labels_when_run_single_use(tmp_path, capsys):
    table = _noised(tmp_path)
    cases = (([], "--mode test needs --single-use and --use-log"),
             (["--single-use"], "--mode test needs --single-use and --use-log"),
             (["--use-log", tmp_path / "x.log"], "--mode test needs --single-use and --use-log"))
    for extra, expected in cases:
        rc, out, stdout, err = run(["score", table, "--mode", "test", *extra], capsys)
        assert rc == 2 and stdout == "" and expected in err
    assert not (tmp_path / "x.log").exists()
    rc, out, _, _ = _test_run(tmp_path, capsys, table)
    assert rc == 0 and out["guard_free"] is True and out["screening_only"] is False and "label" not in out
    assert out["primary"]["comparisons"]["oracle"]["status"] == "PASS-STRONG"
    assert out["primary"]["comparisons"]["noised_oracle"]["status"] == "PASS-STRONG"
    assert out["primary"]["comparisons"]["always_opus"]["status"] == "FAIL"
    assert out["status"] == "INFORMATIVE"


def test_guard_free_follows_the_header_in_test_mode(tmp_path, capsys):
    table = _noised(tmp_path, header={"guards_applied": True})
    rc, out, _, _ = _test_run(tmp_path, capsys, table)
    assert out["guard_free"] is False


def test_validity_checks_pass_on_a_separable_table_and_report_the_spread(tmp_path, capsys):
    rc, out, _ = _full_out(tmp_path, capsys, "dev")
    v = out["validity"]
    assert v["ok"] is True and v["reasons"] == [] and v["not_run"] == []
    assert v["checks"] == {"always_opus_fails_guard": True, "noised_oracle_strong": True,
                           "known_better_separates": True}
    assert v["spread_qwen3coder_minus_nimble"]["under"]["diff"] < 0
    assert set(v["known_worse"]) == {"always_haiku", "random", "nimble"}
    assert all(x["fails_bars"] for x in v["known_worse"].values())
    assert out["status"] == "INFORMATIVE"


def test_validity_failure_makes_the_screen_not_informative(tmp_path, capsys):
    recs = gen_recs(n_groups=80, per=5, scored=None)
    for r in recs:                               # qwen3coder worse than nimble: not separated
        r["preds"]["qwen3coder"], r["preds"]["nimble"] = r["preds"]["nimble"], r["preds"]["qwen3coder"]
    rc, out, _, _ = run(["score", write_table(tmp_path / "t.jsonl", recs), "--boot-b", B_FAST], capsys)
    assert "not_separated" in out["reasons"] and out["status"] == "NOT INFORMATIVE"
    rc, sj, _, _ = run(["selftest", tmp_path / "t.jsonl", "--boot-b", B_FAST], capsys)
    assert rc == 1 and sj["status"] == "NOT INFORMATIVE"


def test_noised_oracle_generator_is_deterministic_and_25_percent(tmp_path):
    recs = gen_recs(n_groups=100, per=10, arms=False, scored=None)
    table = write_table(tmp_path / "t.jsonl", recs)
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    assert ce.main(["selftest", table, "--make-noised-oracle", "5", "--out", str(a)]) == 0
    assert ce.main(["selftest", table, "--make-noised-oracle", "5", "--out", str(b)]) == 0
    assert a.read_bytes() == b.read_bytes() and stat.S_IMODE(os.stat(a).st_mode) == 0o600
    out_recs = [json.loads(x) for x in a.read_text().splitlines()]
    flips = [abs(se.RANK[r["preds"]["noised_oracle"]] - se.RANK[r["truths"]["opus"]]) for r in out_recs]
    assert set(flips) <= {0, 1} and 0.20 < sum(flips) / len(flips) < 0.30
    c = tmp_path / "c.jsonl"
    ce.main(["selftest", table, "--make-noised-oracle", "6", "--out", str(c)])
    assert c.read_bytes() != a.read_bytes()


def test_sensitivity_rules_and_robust_flag(tmp_path, capsys):
    rc, out, _ = _full_out(tmp_path, capsys, "dev")
    assert set(out["sensitivity"]) == {"opus_only", "gpt_only", "agree_only", "gpt_excluded", "higher2"}
    assert out["robust_under_single_judge_truths"]["oracle"] is True
    assert out["robust_under_single_judge_truths"]["always_haiku"] is False
    assert out["population"] == "pooled"


def test_floor_sign_one_se_boundary():
    assert ce.floor_sign(1.0, 1.0) == 1                  # exactly 1 SE counts
    assert ce.floor_sign(0.999999, 1.0) is None          # below the floor: indeterminate
    assert ce.floor_sign(-1.0, 1.0) == -1
    assert ce.floor_sign(-0.5, 1.0) is None
    assert ce.floor_sign(0.0, 0.0) is None and ce.floor_sign(None, 1.0) is None


def test_judge_robustness_block_shape(tmp_path, capsys):
    rc, out, _ = _full_out(tmp_path, capsys, "dev")
    jr = out["judge_robustness"]
    assert jr["population"] == "real"
    e = jr["arms"]["always_haiku"]
    for metric in ("under", "cost"):
        assert set(("sign_opus", "sign_gpt", "indeterminate")) <= set(e[metric])
    # always_haiku is far worse than rules_eff under every truth
    assert e["under"]["sign_opus"] == e["under"]["sign_gpt"] == 1 and e["under"]["indeterminate"] is False


def _sub_se_table():
    """Per kind: 11 items where cand under-routes and rules does not, 10 the reverse, 80 neutral. The paired under
    diff is +1/101 (non-zero) against a bootstrap SE of about 0.04-0.05, so every judge's sign must be None."""
    recs, i = [], 0
    for kind in ("real", "syn"):
        spec = [(11, "sonnet", "haiku"), (10, "opus", "opus"), (80, "sonnet", "sonnet")]
        for n, truth, cand in spec:
            rules = "sonnet"
            for _ in range(n):
                recs.append(item(f"{kind}{i}", f"{kind}{i}", truth, {"rules_eff": rules, "cand": cand}, kind=kind))
                i += 1
    return recs


def _sub_se_legs():
    its = [ce._parse_item(r, k) for k, r in enumerate(_sub_se_table())]
    jr = ce.judge_robustness(its, ["rules_eff", "cand"], 200, ce.SEED, None)
    return jr["arms"]["cand"]["under"], jr["syn_leg"]["arms"]["cand"]["under"]


def test_criterion_8_floor_is_wired_into_both_legs_below_one_se():
    real, syn = _sub_se_legs()
    for leg, judges_ in ((real, ("opus", "gpt", "sonnet")), (syn, ("gpt", "sonnet"))):
        for j in judges_:
            assert leg[f"diff_{j}"] != 0 and abs(leg[f"diff_{j}"]) < leg[f"se_{j}"]
            assert leg[f"sign_{j}"] is None
        assert leg["indeterminate"] is True and leg["discordant"] is False


# --------------------------------------------------------------------------- population, AUROC, kappa

def test_auroc_is_tie_aware_and_exact():
    def rows(pairs):
        return [{"id": str(i), "group": f"g{i}", "w": 1.0, "truth": "opus" if y else "sonnet", "kind": "real",
                 "scores": {"a": (0.0, 0.0, s)}, "preds": {"a": "sonnet"}} for i, (s, y) in enumerate(pairs)]
    perfect = ce.auroc_block(rows([(0.1, 0), (0.2, 0), (0.8, 1), (0.9, 1)]), "a", 100, 1)
    assert perfect["auroc"] == 1.0
    tied = ce.auroc_block(rows([(0.5, 0), (0.5, 1), (0.5, 0), (0.5, 1)]), "a", 100, 1)
    assert tied["auroc"] == 0.5
    mixed = ce.auroc_block(rows([(0.1, 0), (0.4, 1), (0.4, 0), (0.9, 1)]), "a", 100, 1)
    assert mixed["auroc"] == pytest.approx((1 + 0.5 + 1 + 1) / 4)         # pairs: 4, one tie
    assert mixed["basis"] == "p_opus"


def test_auroc_uses_the_real_population_whatever_population_is_scored(tmp_path, capsys):
    recs = gen_recs(n_groups=60, per=5, kinds=("real", "syn"), scored="cand")
    for r in recs:
        if r["kind"] == "syn":                           # uninformative scores on syn
            r["scores"]["cand"]["p_opus"] = 0.5
    table = write_table(tmp_path / "t.jsonl", recs)
    rc, pooled, _, _ = run(["score", table, "--boot-b", B_FAST], capsys)
    rc, syn, _, _ = run(["score", table, "--population", "syn", "--boot-b", B_FAST], capsys)
    assert pooled["auroc_population"] == "real"
    assert pooled["auroc"]["cand"]["auroc"] == syn["auroc"]["cand"]["auroc"]
    assert pooled["auroc"]["cand"]["auroc"] > 0.9
    assert syn["population"] == "syn" and syn["n"] < pooled["n"]
    real_rows = [r for r in recs if r["kind"] == "real"]
    assert pooled["auroc"]["cand"]["n"] == len(real_rows)


def test_hard_verdict_arm_auroc_uses_verdict_rank(tmp_path, capsys):
    recs = gen_recs(n_groups=50, per=4, scored=None)
    rc, out, _, _ = run(["score", write_table(tmp_path / "t.jsonl", recs), "--boot-b", B_FAST], capsys)
    assert out["auroc"]["oracle"]["basis"] == "verdict_rank" and out["auroc"]["oracle"]["auroc"] == 1.0


def test_kappa_exact_values():
    assert ce.qwk([[5, 0, 0], [0, 5, 0], [0, 0, 5]]) == 1.0
    # all disagreement by one step on a symmetric table: kappa = 1 - 2/(2.0) style closed form
    m = [[2, 1, 0], [1, 2, 1], [0, 1, 2]]
    N = 10
    rs = [3, 4, 3]
    exp_num = 0.25 * (1 + 1 + 1 + 1)
    exp_den = sum(((i - j) ** 2 / 4) * rs[i] * rs[j] / N for i in range(3) for j in range(3))
    assert ce.qwk(m) == pytest.approx(1 - exp_num / exp_den)
    assert ce.qwk([[0, 0, 0]] * 3) is None


def test_kappa_block_in_output(tmp_path, capsys):
    rc, out, _ = _full_out(tmp_path, capsys, "dev")
    k = out["kappa"]["real:opus-gpt"]
    assert k["n"] == 400 and k["kappa"] == 1.0 and len(k["ci95"]) == 2       # the judges agree in this fixture
    assert out["kappa"]["syn:opus-gpt"] == {"n": 0, "kappa": None}


def test_largest_group_share_and_counts(tmp_path, capsys):
    recs = _golden()
    rc, out, _, _ = run(["score", write_table(tmp_path / "t.jsonl", recs), "--boot-b", B_FAST], capsys)
    assert out["n_items"] == 12 and out["n"] == 12 and out["n_groups"] == 12
    assert out["largest_group_share"] == pytest.approx(1 / 12, abs=1e-6)


def test_help_and_module_entry_point():
    env = {"PATH": "/usr/bin:/bin", "HOME": str(ROOT), "PYTHONPATH": str(ROOT / "src"), "NO_COLOR": "1"}
    for args in (["--help"], ["score", "--help"], ["fit", "--help"]):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "cls_eval.py"), *args], capture_output=True,
                           text=True, timeout=60, env=env, stdin=subprocess.DEVNULL)
        assert r.returncode == 0 and "usage" in r.stdout.lower()


def test_module_runs_as_a_script_on_a_table(tmp_path):
    table = write_table(tmp_path / "t.jsonl", _golden())
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "cls_eval.py"), "score", table, "--boot-b", "100"],
                       capture_output=True, text=True, timeout=60, env=env, stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and json.loads(r.stdout)["n"] == 12


# --------------------------------------------------------------------------- decision wiring (D-148)

def _guard_table():
    """400 singleton groups: 160 opus-truth, 240 sonnet-truth; cand routes opus on 36 + 36 of them. The guard bootstrap
    lands at -0.0024 on the one-sided 95th percentile and +0.0008 on the 97.5th (checked in the tests)."""
    recs, k = [], 0
    for i in range(160):
        recs.append(item(f"g{k}", f"g{k}", "opus", {"rules_eff": "sonnet", "cand": "opus" if i < 36 else "sonnet"}))
        k += 1
    for i in range(240):
        recs.append(item(f"g{k}", f"g{k}", "sonnet", {"rules_eff": "sonnet", "cand": "opus" if i < 36 else "sonnet"}))
        k += 1
    return recs


def _ni_table():
    """300 singleton groups: 13 items the candidate under-routes (rules fine), 13 the reverse, rest neutral. The
    under-route diff is 0 (bars hold); its bootstrap upper is 0.0267 at the 95th and 0.0333 at the 97.5th."""
    recs, i = [], 0
    for _ in range(13):
        recs.append(item(f"n{i}", f"n{i}", "sonnet", {"rules_eff": "sonnet", "cand": "haiku"}))
        i += 1
    for _ in range(13):
        recs.append(item(f"n{i}", f"n{i}", "sonnet", {"rules_eff": "haiku", "cand": "sonnet"}))
        i += 1
    while i < 300:
        recs.append(item(f"n{i}", f"n{i}", "sonnet", {"rules_eff": "sonnet", "cand": "sonnet"}))
        i += 1
    return recs


def _adm_table():
    """300 singletons, all cand scores bimodal so every grid point makes the same decisions: 13 items cand routes to
    opus that rules under-routes, 10 the reverse. Gain bootstrap: 20th percentile -0.0033, 25th percentile 0.0."""
    recs, i = [], 0

    def sc(po):
        return {"cand": {"p_haiku": 0.0, "p_sonnet": 0.3, "p_opus": po}}
    for _ in range(13):
        recs.append(item(f"a{i}", f"a{i}", "opus", {"rules_eff": "sonnet"}, scores=sc(0.99), fold=i % 5))
        i += 1
    for _ in range(10):
        recs.append(item(f"a{i}", f"a{i}", "opus", {"rules_eff": "opus"}, scores=sc(0.0), fold=i % 5))
        i += 1
    while i < 300:
        recs.append(item(f"a{i}", f"a{i}", "sonnet", {"rules_eff": "sonnet"}, scores=sc(0.0), fold=i % 5))
        i += 1
    return recs


def _boot_vals(recs, arm, keys, stat):
    its = [ce._parse_item(r, k) for k, r in enumerate(recs)]
    rows, _ = ce.prepare(its, ["rules_eff", arm], "majority3")
    return rows, sorted(ce.bootstrap(ce.group_sums(rows, ["rules_eff", arm]), keys, stat, 2000, ce.SEED))


def test_guard_decision_uses_the_one_sided_95th_percentile():
    def stat(s):
        W = s["W"]
        return s["u:cand"] / W - ce.mix_under(s["r:cand"] / W, s["Uh"] / W, s["Us"] / W)
    _, vals = _boot_vals(_guard_table(), "cand", ["W", "u:cand", "r:cand", "Uh", "Us"], stat)
    assert ce.upper95(vals) < -ce.GUARD_EPS <= ce.ci95(vals)[1]          # between the 95th and the 97.5th
    an = _an(_guard_table(), B=2000)
    assert an["guard"]["passes"] is True                                  # decided at the one-sided 95th


def test_non_inferiority_decision_uses_the_97_5th_percentile():
    an = _an(_ni_table(), B=2000)
    assert an["bars"] is True and an["under"]["diff"] == 0.0
    assert an["under"]["upper95"] <= ce.NI_UNDER < an["under"]["ni_upper"]       # between the 95th and the 97.5th
    assert an["non_inferior"]["ok"] is False and an["_core"] == "FAIL"
    assert an["non_inferior"]["under_upper"] == an["under"]["ci95"][1]


def test_admissibility_is_decided_at_the_20th_percentile(monkeypatch):
    its = [ce._parse_item(r, k) for k, r in enumerate(_adm_table())]
    rows, _ = ce.prepare(its, ["rules_eff"], "majority3")
    trial = [dict(r, preds=dict(r["preds"], _c=ce.decide(r["scores"]["cand"], 0.5, 1.01))) for r in rows]
    gs = ce.group_sums(trial, ["rules_eff", "_c"])
    vals = sorted(ce.bootstrap(gs, ["W", "u:_c", "u:rules_eff"], lambda s: (s["u:rules_eff"] - s["u:_c"]) / s["W"],
                               2000, ce.SEED))
    assert ce.lower_pct(vals, 0.20) < 0 <= ce.lower_pct(vals, 0.25)       # 20th percentile < 0 <= 25th percentile
    monkeypatch.setattr(ce, "guard", lambda upper, raw: True)             # isolate constraint 1
    assert ce.ADMISSIBLE_PCT == 0.20
    assert ce.fit(rows, "cand", 2000, ce.SEED)["admissible"] is False


def test_tie_between_t_hi_values_freezes_the_larger_threshold(tmp_path, capsys):
    rows = _fit_rows()
    res = ce.fit(rows, "cand", B_FAST, ce.SEED)
    # every t_hi in 0.55..0.95 gives the same decisions here (p_opus is 0.95, 0.5 or 0.02); the larger wins
    assert res["admissible"] and res["t_hi"] == 0.95 and res["t_lo"] == 1.01
    assert ce.tie_key({"cost": 1.0, "opus": 0.1, "raw": 2.0}, 0.9, 1.01) < ce.tie_key(
        {"cost": 1.0, "opus": 0.1, "raw": 2.0}, 0.5, 1.01)


def test_single_use_without_validity_arms_is_not_informative(tmp_path, capsys):
    rc, out, _, _ = run(["score", write_table(tmp_path / "t.jsonl", _golden()), "--use-log", tmp_path / "u.log",
                         "--single-use", "--calibration", _cal(tmp_path), "--boot-b", B_FAST], capsys)
    assert out["reasons"] == ["n_truth_opus_lt_40", "validity_unavailable"] and out["status"] == "NOT INFORMATIVE"
    assert out["validity"]["not_run"]
    rc, out, _, _ = run(["score", write_table(tmp_path / "t.jsonl", _golden()), "--calibration", _cal(tmp_path),
                         "--boot-b", B_FAST], capsys)
    assert "validity_unavailable" not in out["reasons"]            # a DEV run without them only reports not_run


def test_dev_labels_on_fit_and_selftest(tmp_path, capsys):
    rc, out, _, _ = run(_fit_args(write_table(tmp_path / "t.jsonl", _fit_recs())), capsys)
    assert out["guard_free"] is True and out["screening_only"] is True and out["label"] == ce.DEV_LABEL
    rc, out, _, _ = run(["selftest", write_table(tmp_path / "s.jsonl", _golden()), "--boot-b", B_FAST], capsys)
    assert out["guard_free"] is True and out["screening_only"] is True and out["label"] == ce.DEV_LABEL


def test_criterion_8_has_sonnet_and_the_dev_syn_leg(tmp_path, capsys):
    recs = gen_recs(n_groups=80, per=5, kinds=("real", "syn"), scored=None)
    rc, out, _, _ = run(["score", write_table(tmp_path / "t.jsonl", recs), "--boot-b", B_FAST], capsys)
    jr = out["judge_robustness"]
    e = jr["arms"]["always_haiku"]["under"]
    assert jr["population"] == "real" and jr["pair"] == ["opus", "gpt"]
    assert {"sign_opus", "sign_gpt", "sign_sonnet", "indeterminate", "discordant"} <= set(e)
    leg = jr["syn_leg"]
    assert leg["population"] == "syn" and leg["pair"] == ["gpt", "sonnet"]
    es = leg["arms"]["always_haiku"]["under"]
    assert es["sign_gpt"] == es["sign_sonnet"] == 1 and es["indeterminate"] is False and es["discordant"] is False
    # no syn rows at all: the leg is reported as not evaluable, not invented
    rc, out, _, _ = run(["score", write_table(tmp_path / "r.jsonl", gen_recs(n_groups=40, per=5)), "--boot-b", B_FAST],
                        capsys)
    assert out["judge_robustness"]["syn_leg"]["arms"]["always_haiku"]["under"] == {"evaluable": False}


def test_robust_flag_is_computed_when_truth_is_itself_a_robust_rule(tmp_path, capsys):
    table = write_table(tmp_path / "t.jsonl", gen_recs(n_groups=60, per=5))
    rc, out, _, _ = run(["score", table, "--truth", "opus_only", "--boot-b", B_FAST], capsys)
    assert out["robust_under_single_judge_truths"]["oracle"] is True
    assert out["robust_under_single_judge_truths"]["always_haiku"] is False


def test_kappa_per_length_band_and_per_source(tmp_path, capsys):
    recs = gen_recs(n_groups=40, per=5, kinds=("real", "syn"), arms=False, scored=None)
    for i, r in enumerate(recs):
        r["strata"]["length_band"] = "w<=12" if i % 2 else "51-150"
        if r["kind"] == "syn":
            r["strata"]["length_band"] = "13-50"
        r["strata"]["source"] = "A" if i < 150 else "B"
        if i >= 150 and i % 2 == 0 and r["kind"] == "real":
            r["truths"]["gpt"] = "opus" if r["truths"]["gpt"] != "opus" else "haiku"
    rc, out, _, _ = run(["score", write_table(tmp_path / "t.jsonl", recs), "--boot-b", B_FAST], capsys)
    k = out["kappa"]
    assert set(k["by_length_band"]) == {"real", "syn"}              # real DEV and DEV-SYN never share a band row
    assert set(k["by_length_band"]["real"]) == {"w<=12", "51-150"} and set(k["by_length_band"]["syn"]) == {"13-50"}
    n_real = sum(r["kind"] == "real" for r in recs)
    n_syn = len(recs) - n_real
    assert sum(v["opus-gpt"]["n"] for v in k["by_length_band"]["real"].values()) == n_real
    assert k["by_length_band"]["syn"]["13-50"]["opus-gpt"]["n"] == n_syn
    assert set(k["by_source"]) == {"A", "B"}
    assert k["by_length_band"]["real"]["w<=12"]["opus-gpt"]["kappa"] == 1.0
    assert k["by_source"]["B"]["opus-gpt"]["n"] == sum(r["strata"]["source"] == "B" for r in recs)   # by_source keeps both kinds
    assert k["by_source"]["B"]["opus-gpt"]["kappa"] < 1.0
    assert k["by_source"]["A"]["opus-gpt"]["kappa"] == 1.0
    # a kind with no rows still has its (empty) table
    rc, out, _, _ = run(["score", write_table(tmp_path / "r.jsonl", gen_recs(n_groups=40, per=5, arms=False,
                                                                           scored=None)), "--boot-b", B_FAST], capsys)
    assert out["kappa"]["by_length_band"]["syn"] == {}


def test_kappa_point_prints_n_only_below_five_pairs():
    def pairs(n):
        return [(f"g{i}", i % 3, (i + 1) % 3) for i in range(n)]
    assert ce._kappa_point(pairs(0)) == {"n": 0}
    assert ce._kappa_point(pairs(3)) == {"n": 3}                     # a `< 1` threshold would print kappa here
    assert ce._kappa_point(pairs(4)) == {"n": 4}                     # a `< 4` threshold would too
    assert "kappa" in ce._kappa_point(pairs(5)) and "exact_agreement" in ce._kappa_point(pairs(5))


# --------------------------------------------------------------------------- mutants

def _checks():
    """Invariant checks. Each takes no argument, reads the (possibly patched) module ``ce`` and asserts."""
    def bars():
        assert ce.bars_hold(0.19, 0.2, 4.0, 5.0) and not ce.bars_hold(0.21, 0.2, 5.0, 5.0)
        assert not ce.bars_hold(0.2, 0.2, 5.01, 5.0)

    def clamp_applied():
        assert ce.item_cost("opus", "sonnet", "sonnet") == pytest.approx(2.7)
        rows, _ = ce.prepare([ce._parse_item(item("a", "g", "sonnet", {"rules_eff": "sonnet", "x": "opus"},
                                                 requested="sonnet"), 1)], ["rules_eff", "x"], "majority3")
        assert ce.cost_mean(rows, "x") == pytest.approx(2.7)

    def lam2():
        assert ce.item_cost("sonnet", "opus", "opus") == pytest.approx(2.7 + 2 * 6.15)

    def q_denominator():
        assert ce.mix_q(3.425) == pytest.approx((3.425 - 2.7) / (6.15 - 2.7))
        assert ce.mix_q(1.85) == pytest.approx(0.5)

    def one_sided_bound():
        assert ce.upper95([float(i) for i in range(2000)]) == 1899.0

    def clustered():
        a = _an(_cluster_table(True))["under"]["ci95"]
        b = _an(_cluster_table(False))["under"]["ci95"]
        assert a[1] - a[0] > b[1] - b[0] + 0.1

    def split_to_sonnet():
        assert ce.truth_for({"truths": {"opus": "opus", "gpt": "haiku", "sonnet": "sonnet"}}, "majority3") == "sonnet"

    def refusal_not_correct():
        r = item("a", "g", "opus", {"rules_eff": "sonnet", "x": None})
        rows, _ = ce.prepare([ce._parse_item(r, 1)], ["rules_eff", "x"], "majority3")
        assert rows[0]["preds"]["x"] == "sonnet" and ce.under_rate(rows, "x") == 1.0

    def majority_needs_three():
        assert ce.truth_for({"truths": {"opus": "opus", "gpt": "opus", "sonnet": None}}, "majority3") is None

    def ni_bound():
        assert ce.non_inferior(0.03, 0.3) and not ce.non_inferior(0.0301, 0.0) and not ce.non_inferior(0.04, 0.0)

    def always_opus_fails_guard():
        assert ce.guard(0.0, 6.15) is False
        an = _an([dict(r, preds={**r["preds"], "cand": "opus"}) for r in _p17d_like(20, 5)], arm="cand")
        assert an["guard"]["passes"] is False

    def seed_matters():
        recs = gen_recs(n_groups=30, per=3, arms=False, scored=None)
        for r in recs:
            r["preds"]["cand"] = r["truths"]["opus"] if random.Random(r["item_id"]).random() < 0.6 else "sonnet"
        a, b = _an(recs, seed=1), _an(recs, seed=2)
        assert (a["under"]["ci95"], a["cost"]["ci95"]) != (b["under"]["ci95"], b["cost"]["ci95"])

    def percentile_index():
        assert ce.ci95([float(i) for i in range(2000)]) == [50.0, 1949.0]

    def higher2():
        assert ce.truth_for({"truths": {"opus": "opus", "gpt": "sonnet", "sonnet": None}}, "higher2") == "opus"

    def fit_opus_share():
        res = ce.fit(_fit_rows(), "cand", B_FAST, ce.SEED)
        assert res["admissible"] and res["opus_share"] <= res["truth_opus_share"] + 1e-9

    def population_for_auroc(tmp=None):
        rows = []
        for g in range(30):
            for kind in ("real", "syn"):
                truth = "opus" if g % 2 else "sonnet"
                p = (0.9 if truth == "opus" else 0.1) if kind == "real" else 0.5
                rows.append({"id": f"{kind}{g}", "group": f"{kind}{g}", "kind": kind, "w": 1.0, "truth": truth,
                             "req": "opus", "req_imp": "opus", "strata": {}, "fold": 0,
                             "scores": {"a": (0.0, 0.0, p)}, "preds": {"rules_eff": "sonnet", "a": "sonnet"},
                             "fb": {"rules_eff": False, "a": False}})
        # the same population call run_score makes for AUROC
        real = ce.select_population(rows, "real")
        assert ce.auroc_block(real, "a", 100, 1)["auroc"] == 1.0
        assert len(real) == 30 and all(r["kind"] == "real" for r in real)

    def held_out_not_in_fit():
        rows = _fit_rows()
        tr = ce.train_rows(rows, 2)
        assert tr and all(r["fold"] != 2 for r in tr) and len(tr) < len(rows)
        res_a = ce.oos_decisions(rows, "cand", B_FAST, ce.SEED)
        flipped = [dict(r, truth="opus" if r["truth"] != "opus" else "haiku") if r["fold"] == 2 else r for r in rows]
        res_b = ce.oos_decisions(flipped, "cand", B_FAST, ce.SEED)
        fa = {x["fold"]: x for x in res_a["fold_thresholds"]}
        fb = {x["fold"]: x for x in res_b["fold_thresholds"]}
        assert fa[2] == fb[2]

    def one_se_floor():
        assert ce.floor_sign(0.5, 1.0) is None and ce.floor_sign(1.0, 1.0) == 1

    def one_se_floor_wiring():
        real, syn = _sub_se_legs()
        assert real["sign_opus"] is None and real["sign_gpt"] is None and real["indeterminate"] is True
        assert syn["sign_gpt"] is None and syn["sign_sonnet"] is None and syn["indeterminate"] is True

    def dev_no_pass_label():
        assert ce.display_status("PASS-STRONG", "dev") == "SCREEN-PASS"
        assert "PASS-" not in ce.display_status("PASS-WEAK", "dev")

    def per_source_rule():
        a = item("a", "g1", None, {"rules_eff": "sonnet"}, source="B", truths={"opus": "opus", "gpt": None, "sonnet": None})
        b = item("b", "g2", "sonnet", {"rules_eff": "sonnet"}, source="A")
        its = [ce._parse_item(a, 1), ce._parse_item(b, 2)]
        rows, info = ce.prepare(its, ["rules_eff"], "majority3", {"B": "opus_only"})
        assert len(rows) == 2 and info["n_no_truth"] == 0

    def other_two_higher():
        j = {"opus": "opus", "gpt": "haiku", "sonnet": "sonnet"}
        assert ce.truth_for({"truths": j}, "other_two_higher:gpt") == "opus"
        assert ce.truth_for({"truths": j}, "gpt_excluded") == "opus"

    def split_rows_by_kind():
        rows = [{"kind": "real"}, {"kind": "syn"}, {"kind": "real"}]
        parts = ce.split_by_kind(rows, ["real", "syn"])
        assert len(parts["real"]) == 2 and len(parts["syn"]) == 1

    def guard_decision_95th():
        assert _an(_guard_table(), B=2000)["guard"]["passes"] is True

    def ni_decision_97_5th():
        an = _an(_ni_table(), B=2000)
        assert an["non_inferior"]["ok"] is False

    def admissible_pct_20():
        its = [ce._parse_item(r, k) for k, r in enumerate(_adm_table())]
        rows, _ = ce.prepare(its, ["rules_eff"], "majority3")
        orig = ce.guard
        ce.guard = lambda upper, raw: True
        try:
            assert ce.fit(rows, "cand", 2000, ce.SEED)["admissible"] is False
        finally:
            ce.guard = orig

    def tie_break_larger_t_hi():
        res = ce.fit(_fit_rows(), "cand", B_FAST, ce.SEED)
        assert res["t_hi"] == 0.95 and res["t_lo"] == 1.01

    def validity_unavailable():
        assert ce.single_use_validity_reasons({"not_run": ["x"]}, True) == ["validity_unavailable"]
        assert ce.single_use_validity_reasons({"not_run": ["x"]}, False) == []
        assert ce.single_use_validity_reasons({"not_run": []}, True) == []

    return {k: v for k, v in locals().items() if callable(v)}


CHECKS = _checks()
TMP_FREE = tuple(CHECKS)


_ORIG_FLOOR_SIGN = ce.floor_sign


def _run_checks():
    failed = []
    for name, fn in CHECKS.items():
        try:
            fn()
        except Exception:  # noqa: BLE001 - any failure of an invariant counts
            failed.append(name)
    return failed


def _mut_bars(under_c, under_b, cost_c, cost_b):
    return under_c >= under_b and cost_c >= cost_b


def _mut_guard_true(upper, raw):
    return True if raw >= 2.7 else (upper is not None and upper < 0)


def _mut_mix_q(raw):
    if raw >= 2.7:
        q = (raw - 2.7) / (6.15 - 1.0)
    else:
        q = (raw - 1.0) / (2.7 - 1.0)
    return min(1.0, max(0.0, q))


def _mut_upper_two_sided(vals):
    n = len(vals)
    return vals[int(0.975 * n) - 1] if n else None


def _mut_cluster_rows(rows):
    return [r["id"] for r in rows]


def _mut_resample_ignores_seed(n, B, seed):
    rng = random.Random(0)
    return tuple(tuple(rng.randrange(n) for _ in range(n)) for _ in range(B))


def _mut_ci95_off_by_one(vals):
    n = len(vals)
    return [vals[int(0.05 * n)], vals[int(0.975 * n) - 1]] if n else [None, None]


def _mut_majority_two(j):
    vals = [j.get(k) for k in ce.JUDGES if j.get(k) is not None]
    if len(vals) < 2:
        return None
    tier, n = ce.Counter(vals).most_common(1)[0]
    return tier if n >= 2 else "sonnet"


def _mut_split_opus(j):
    if any(j.get(k) is None for k in ce.JUDGES):
        return None
    tier, n = ce.Counter(j[k] for k in ce.JUDGES).most_common(1)[0]
    return tier if n >= 2 else "opus"


def _mut_higher2_lower(j):
    a, b = j.get("opus"), j.get("gpt")
    if a is None or b is None:
        return None
    return a if ce.RANK[a] <= ce.RANK[b] else b


def _mut_other_lower(j, voided):
    rest = [j.get(k) for k in ce.JUDGES if k != voided]
    if len(rest) != 2 or any(v is None for v in rest):
        return None
    return rest[0] if ce.RANK[rest[0]] <= ce.RANK[rest[1]] else rest[1]


def _mut_refusal_correct(pred, fallback, truth):
    return truth if pred is None else pred


def _mut_ni(under_upper, cost_upper):
    return under_upper <= 0.05 and cost_upper <= ce.NI_COST


def _mut_no_floor(diff, se_):
    return None if diff is None or diff == 0 else (1 if diff > 0 else -1)


def _mut_dev_pass(core, mode):
    return core


def _mut_population_ignored(rows, population):
    return list(rows)


def _mut_train_all(rows, fold):
    return list(rows)


def _mut_source_ignored(source, default, by_source):
    return default


def _mut_split_mix(rows, kinds):
    return {k: list(rows) for k in kinds}


def _mut_tie_small_hi(st, hi, lo):
    return (round(st["cost"], 12), round(st["opus"], 12), round(st["raw"], 12), hi, -lo)


MUTANTS = [
    ("m01_bars_use_ge", "bars_hold", _mut_bars, "bars"),
    ("m02_clamp_dropped", "clamp", lambda p, r: p, "clamp_applied"),
    ("m03_lambda_one", "LAMBDA", 1.0, "lam2"),
    ("m04_q_denominator_6_15_minus_1", "mix_q", _mut_mix_q, "q_denominator"),
    ("m05_guard_two_sided_bound", "upper95", _mut_upper_two_sided, "one_sided_bound"),
    ("m06_bootstrap_by_row", "cluster_keys", _mut_cluster_rows, "clustered"),
    ("m07_split_resolves_to_opus", "rule_majority3", _mut_split_opus, "split_to_sonnet"),
    ("m08_refusal_scored_correct", "resolve_pred", _mut_refusal_correct, "refusal_not_correct"),
    ("m09_majority3_accepts_two_judges", "rule_majority3", _mut_majority_two, "majority_needs_three"),
    ("m10_ni_bound_5_points", "non_inferior", _mut_ni, "ni_bound"),
    ("m11_always_opus_passes_guard", "guard", _mut_guard_true, "always_opus_fails_guard"),
    ("m12_seed_ignored", "resample_indices", _mut_resample_ignores_seed, "seed_matters"),
    ("m13_percentile_off_by_one", "ci95", _mut_ci95_off_by_one, "percentile_index"),
    ("m14_higher2_returns_lower", "rule_higher2", _mut_higher2_lower, "higher2"),
    ("m15_fit_ignores_opus_share", "opus_share_ok", lambda share, truth_share: True, "fit_opus_share"),
    ("m16_population_ignored", "select_population", _mut_population_ignored, "population_for_auroc"),
    ("m17_held_out_leaks_into_fit", "train_rows", _mut_train_all, "held_out_not_in_fit"),
    ("m18_one_se_floor_removed", "floor_sign", _mut_no_floor, "one_se_floor"),
    ("m28_floor_call_site_se_zero", "floor_sign", lambda d, se_: _ORIG_FLOOR_SIGN(d, 0.0), "one_se_floor_wiring"),
    ("m19_dev_prints_pass", "display_status", _mut_dev_pass, "dev_no_pass_label"),
    ("m20_per_source_rule_ignored", "rule_for_source", _mut_source_ignored, "per_source_rule"),
    ("m21_other_two_higher_uses_lower", "rule_other_two_higher", _mut_other_lower, "other_two_higher"),
    ("m22_split_rows_mixes_kinds", "split_by_kind", _mut_split_mix, "split_rows_by_kind"),
    ("m23_ni_at_95th_percentile", "ni_upper", lambda v: ce.upper95(v), "ni_decision_97_5th"),
    ("m24_guard_at_97_5th_percentile", "upper95", _mut_upper_two_sided, "guard_decision_95th"),
    ("m25_admissibility_at_25th_percentile", "ADMISSIBLE_PCT", 0.25, "admissible_pct_20"),
    ("m26_tie_break_smaller_t_hi", "tie_key", _mut_tie_small_hi, "tie_break_larger_t_hi"),
    ("m27_validity_unavailable_removed", "single_use_validity_reasons", lambda v, su: [], "validity_unavailable"),
]


def test_unmutated_module_passes_every_invariant_check():
    assert len(CHECKS) >= 28
    assert _run_checks() == []


def test_there_are_at_least_fifteen_distinct_mutants_each_with_a_named_killer():
    assert len(MUTANTS) >= 28 and len({m[0] for m in MUTANTS}) == len(MUTANTS)
    assert all(m[3] in CHECKS for m in MUTANTS)


@pytest.mark.parametrize("name,target,replacement,killer", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_mutant_is_killed(monkeypatch, name, target, replacement, killer):
    assert hasattr(ce, target)
    monkeypatch.setattr(ce, target, replacement)
    failed = _run_checks()
    assert failed, f"{name}: no invariant check failed"
    assert killer in failed, f"{name}: expected {killer} to fail, got {failed}"
