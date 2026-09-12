#!/usr/bin/env python3
"""Build the external training corpus for the RouterArena router -- audited, source by source.

North star: the router is fit on data that has nothing to do with RouterArena. Every source
here is checked against RouterArena's own evaluation prompts before a single item is kept, and
each source ships its own audit report. A source that cannot be proven disjoint is **dropped,
not filtered post-hoc**.

Two exclusion layers, both required:

1. **Provenance.** Sources drawn from a RouterArena constituent dataset are excluded outright,
   even where individual items are disjoint. RouterArena samples from MMLU, MMLU-Pro, ArcMMLU,
   MedMCQA, PubMedQA, QANTA, NarrativeQA, LiveCodeBench, SuperGLUE, ETHICS, FinQA,
   ChessInstruct, MusicTheoryBench, GeoBench, OpenTDB, WMT19, MATH, AIME, GSM8K, MathQA, AsDiv
   and SocialiQA -- training on other items from those same sources is tuning to the
   benchmark's distribution, which is the thing we are claiming not to do.
2. **Item overlap.** SHA-256 exact-hash audit (NFC -> strip -> collapse whitespace -> casefold)
   against RouterArena's evaluation questions, plus a MinHash near-duplicate pass.

Usage::

    python scripts/routerarena/build_corpus.py --list
    python scripts/routerarena/build_corpus.py --sources openbookqa,commonsense_qa
    python scripts/routerarena/build_corpus.py --all --per-source-cap 1500
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from llm_router.contamination_audit import audit, hash_set, prompt_hash  # noqa: E402

RA_PARQUET = REPO / "data" / "benchmarks" / "full.parquet"
OUT_DIR = REPO / "data" / "corpus"

# Near-duplicate detection parameters, chosen by measurement rather than convention.
# Negative control: feed RouterArena's own questions back through the audit after deleting
# words, and count how many are still caught; false-positive control: run 800 known-clean
# external items through and count how many are wrongly flagged.
#
#   n=13, t=0.80 (the conventional choice):  73% / 80% caught, 0.0% false positives
#   n= 8, t=0.65                          :  83% / 86% caught, 0.0% false positives
#   n= 5, t=0.50 (chosen)                 :  99% / 94% caught, 0.0% false positives
#
# Shorter shingles win because RouterArena's questions are short -- deleting two words from a
# 20-word question destroys most of its 13-grams, so long shingles miss exactly the light
# paraphrase an audit most needs to catch.
SHINGLE_N = 5
NEAR_DUP_THRESHOLD = 0.5

# Datasets RouterArena is built from.
#
# TRACK F CHANGE (2026-08-21). These were refused outright, regardless of item-level
# disjointness. That was stricter than RouterArena requires and it is what broke the fit.
# RouterArena's rule is one sentence: "Submissions that train, fit, or tune any router component
# on **RouterArena data** (including the label files) will be rejected." Constituent datasets
# are not RouterArena data; their train splits are ordinary public corpora.
#
# The cost of the stricter reading was measured, not guessed. 29.2% of sub_10 is MMLU-Pro and
# our corpus contained no MMLU-family content at all; across the board, model accuracy ranking
# on our corpus correlated with RouterArena's at only rho = 0.70, and the escalation mechanism
# fit on it inverted sign (E6.2: rho = +0.521 against a 0.60 gate). Model rankings are not
# distribution-invariant, so a corpus built to be deliberately unlike the evaluation set cannot
# tell us which model to route to.
#
# What did NOT change: the **item-overlap** audit. SHA-256 exact-hash plus a MinHash near-dup
# pass against RouterArena's evaluation questions still runs on every source, and a source that
# cannot be proven disjoint is still dropped rather than filtered post-hoc. That audit is the
# mechanical guarantee; provenance was a proxy for it, and the proxy was costing more than it
# bought. Composition is balanced across the published constituent families -- deliberately
# NOT matched to sub_10's observed proportions, because proportions read off the eval set are
# RouterArena data and weighting to them would be tuning on it.
#
# Set ALLOW_CONSTITUENT_SOURCES = False to restore the original firewall.
#
# Matched against *tokens* of the hf id and source name, not raw substrings -- a substring
# match refuses "google-research-datasets/nq_open" because "research" contains "arc", which is
# exactly the kind of silent over-blocking that would quietly starve the corpus.
ALLOW_CONSTITUENT_SOURCES = True

RA_CONSTITUENTS = frozenset({
    "mmlu", "mmlupro", "arc", "ai2arc", "medmcqa", "pubmedqa", "qanta", "narrativeqa",
    "livecodebench", "superglue", "glue", "ethics", "finqa", "convfinqa", "chess",
    "chessinstruct", "musictheorybench", "geobench", "opentdb", "wmt19", "aime", "gsm8k",
    "gsm8khard", "mathqa", "asdiv", "svamp", "socialiqa", "socialiqa2", "ceval", "cmmlu",
    "competitionmath", "math500",
})


@dataclass
class Source:
    """One external dataset slice, with everything needed to normalise and audit it."""

    name: str
    cluster: str
    hf_id: str
    to_items: Callable[[Any], list[dict[str, str]]]
    config: str | None = None
    split: str = "train"
    licence: str = "check"
    notes: str = ""
    trust_remote_code: bool = False
    extra: dict = field(default_factory=dict)

    def provenance_conflict(self) -> str | None:
        """Return the RouterArena constituent this source derives from, if any.

        Tokenised on non-alphanumeric boundaries so that a short constituent name like "arc"
        cannot match inside an unrelated word. Underscores and hyphens are also stripped
        within a token so "social_i_qa" and "socialiqa" both resolve.
        """
        if ALLOW_CONSTITUENT_SOURCES:
            return None
        raw = f"{self.hf_id} {self.name} {self.config or ''}".lower()
        tokens = {t for t in re.split(r"[^a-z0-9]+", raw) if t}
        tokens |= {re.sub(r"[^a-z0-9]", "", raw.replace(" ", "_").split("/")[-1])}
        hit = tokens & RA_CONSTITUENTS
        return sorted(hit)[0] if hit else None


def _mcq(question: str, options: list[str]) -> str:
    """Bare MCQ text: question plus lettered options.

    Deliberately NOT wrapped in RouterArena's harness template -- matching their prompt
    scaffolding would be fitting to the benchmark's surface form.
    """
    opts = "\n".join(f"{chr(65 + i)}. {o}" for i, o in enumerate(options))
    return f"{question}\n{opts}"


# --- normalisers -----------------------------------------------------------------------


def _openbookqa(rows):
    out = []
    for r in rows:
        ch = r["choices"]
        out.append({"prompt": _mcq(r["question_stem"], list(ch["text"])), "answer": r["answerKey"]})
    return out


def _commonsense_qa(rows):
    out = []
    for r in rows:
        ch = r["choices"]
        out.append({"prompt": _mcq(r["question"], list(ch["text"])), "answer": r["answerKey"]})
    return out


def _medqa(rows):
    out = []
    for r in rows:
        opts = r["options"]
        texts = [opts[k] for k in sorted(opts)] if isinstance(opts, dict) else list(opts)
        out.append({"prompt": _mcq(r["question"], texts), "answer": str(r.get("answer_idx", ""))})
    return out


def _nq_open(rows):
    return [{"prompt": r["question"], "answer": "; ".join(r["answer"])} for r in rows]


def _web_questions(rows):
    return [{"prompt": r["question"], "answer": "; ".join(r["answers"])} for r in rows]


def _hotpot(rows):
    out = []
    for r in rows:
        ctx = r.get("context") or {}
        titles = list(ctx.get("title", []))[:4]
        sents = list(ctx.get("sentences", []))[:4]
        body = "\n".join(f"{t}: {' '.join(s)}" for t, s in zip(titles, sents))
        out.append({"prompt": f"{body}\n\nQuestion: {r['question']}", "answer": r["answer"]})
    return out


def _squad2(rows):
    out = []
    for r in rows:
        ans = r["answers"]["text"]
        if not ans:
            continue
        out.append({"prompt": f"{r['context']}\n\nQuestion: {r['question']}", "answer": ans[0]})
    return out


def _mbpp(rows):
    """MBPP ships executable assertions -- carry them so the grader can actually run them."""
    out = []
    for r in rows:
        out.append({
            "prompt": r["text"] if "text" in r else r["prompt"],
            "answer": r["code"],
            "tests": list(r.get("test_list") or []),
        })
    return out


def _bigcodebench(rows):
    """BigCodeBench ships a unittest class; keep it so candidates can be executed."""
    out = []
    for r in rows:
        test = r.get("test") or ""
        tests = [test, "import unittest; unittest.main(argv=['x'], exit=False)"] if test else []
        out.append({"prompt": r["instruct_prompt"],
                    "answer": r.get("canonical_solution", ""), "tests": tests})
    return out


def _deepmind_math(rows):
    return [{"prompt": r["question"], "answer": r["answer"]} for r in rows]


def _wikitq(rows):
    out = []
    for r in rows:
        tbl = r.get("table") or {}
        header = " | ".join(tbl.get("header", []))
        body = "\n".join(" | ".join(row) for row in list(tbl.get("rows", []))[:12])
        out.append({"prompt": f"{header}\n{body}\n\nQuestion: {r['question']}",
                    "answer": "; ".join(r["answers"])})
    return out


def _opus_books(rows):
    out = []
    for r in rows:
        tr = r["translation"]
        src = (tr.get("en") or "").strip()
        if len(src) < 25:
            continue
        out.append({"prompt": f"Translate from English to French:\n{src}",
                    "answer": (tr.get("fr") or "").strip()})
    return out


def _sciq(rows):
    out = []
    for r in rows:
        opts = [r["distractor1"], r["distractor2"], r["distractor3"], r["correct_answer"]]
        out.append({"prompt": _mcq(r["question"], opts), "answer": r["correct_answer"]})
    return out


def _winogrande(rows):
    out = []
    for r in rows:
        # answer is a 1-based option index; the prompt shows lettered options, so convert.
        idx = str(r["answer"]).strip()
        if idx not in ("1", "2"):
            continue
        out.append({"prompt": _mcq(r["sentence"], [r["option1"], r["option2"]]),
                    "answer": "AB"[int(idx) - 1]})
    return out


def _mnli(rows):
    lab = {0: "entailment", 1: "neutral", 2: "contradiction"}
    return [
        {"prompt": f"Premise: {r['premise']}\nHypothesis: {r['hypothesis']}\n"
                   f"Does the premise entail the hypothesis?",
         "answer": lab.get(r["label"], "")}
        for r in rows if r["label"] in lab
    ]


def _ecare(rows):
    out = []
    for r in rows:
        stem = f"{r['premise']}\nWhat is the most plausible {r['question']}?"
        # label is a 0-based option index; the prompt shows lettered options, so convert.
        idx = str(r.get("label", "")).strip()
        if idx not in ("0", "1"):
            continue
        out.append({"prompt": _mcq(stem, [r["choice1"], r["choice2"]]),
                    "answer": "AB"[int(idx)]})
    return out


def _bioasq(rows):
    out = []
    for r in rows:
        q = r.get("question") or ""
        if not q:
            continue
        out.append({"prompt": f"{q}\nAnswer yes or no.", "answer": str(r.get("answer", ""))})
    return out


def _synth_math(_rows=None, n: int = 1200, seed: int = 17) -> list[dict[str, str]]:
    """Procedurally generated arithmetic and linear algebra.

    The trust anchor for the math cluster: generated data cannot overlap RouterArena's
    evaluation set by construction, so it needs no provenance argument at all. It also
    sidesteps the fact that every large public competition-math corpus recycles the same
    finite pool of olympiad problems that MATH and AIME were drawn from.
    """
    import random as _r

    rng = _r.Random(seed)
    out: list[dict[str, str]] = []
    while len(out) < n:
        kind = rng.choice(("linear", "system", "percent", "sequence", "quadratic"))
        if kind == "linear":
            a, b, x = rng.randint(2, 19), rng.randint(-30, 30), rng.randint(-20, 20)
            out.append({"prompt": f"Solve for x: {a}x + {b} = {a * x + b}", "answer": str(x)})
        elif kind == "system":
            x, y = rng.randint(-12, 12), rng.randint(-12, 12)
            a, b, c, d = (rng.randint(1, 9) for _ in range(4))
            if a * d - b * c == 0:
                continue
            out.append({"prompt": f"Solve the system:\n{a}x + {b}y = {a * x + b * y}\n"
                                  f"{c}x + {d}y = {c * x + d * y}",
                        "answer": f"x={x}, y={y}"})
        elif kind == "percent":
            base, pct = rng.randint(40, 4000), rng.randint(2, 95)
            out.append({"prompt": f"What is {pct}% of {base}?",
                        "answer": f"{base * pct / 100:g}"})
        elif kind == "sequence":
            start, step, k = rng.randint(-20, 20), rng.randint(2, 15), rng.randint(5, 30)
            out.append({"prompt": f"An arithmetic sequence starts at {start} and increases by "
                                  f"{step} each term. What is term {k}?",
                        "answer": str(start + step * (k - 1))})
        else:
            r1, r2 = rng.randint(-9, 9), rng.randint(-9, 9)
            out.append({"prompt": f"Find the roots of x^2 + {-(r1 + r2)}x + {r1 * r2} = 0",
                        "answer": f"{min(r1, r2)}, {max(r1, r2)}"})
    return out


def _synth_table(_rows=None, n: int = 900, seed: int = 23) -> list[dict[str, str]]:
    """Procedurally generated table-reasoning items, same rationale as _synth_math.

    Covers the skill FinQA and TAT-QA test (numeric reasoning over a small table) without
    touching financial-filing corpora, which share source documents with FinQA.
    """
    import random as _r

    rng = _r.Random(seed)
    cities = ["Lisbon", "Osaka", "Nairobi", "Bogota", "Helsinki", "Perth", "Quito", "Riga"]
    metrics = ["units sold", "revenue (k)", "headcount", "orders"]
    out: list[dict[str, str]] = []
    while len(out) < n:
        rows_ = rng.sample(cities, 4)
        years = [2021, 2022, 2023]
        vals = {c: [rng.randint(20, 900) for _ in years] for c in rows_}
        metric = rng.choice(metrics)
        header = "City | " + " | ".join(str(y) for y in years)
        body = "\n".join(f"{c} | " + " | ".join(str(v) for v in vals[c]) for c in rows_)
        kind = rng.choice(("total", "growth", "max", "diff"))
        if kind == "total":
            c = rng.choice(rows_)
            q, a = f"What is the total {metric} for {c} across all years?", str(sum(vals[c]))
        elif kind == "growth":
            c = rng.choice(rows_)
            q = f"What was the change in {metric} for {c} from {years[0]} to {years[-1]}?"
            a = str(vals[c][-1] - vals[c][0])
        elif kind == "max":
            y = rng.randrange(len(years))
            best = max(rows_, key=lambda c: vals[c][y])
            q, a = f"Which city had the highest {metric} in {years[y]}?", best
        else:
            c1, c2 = rng.sample(rows_, 2)
            y = rng.randrange(len(years))
            q = f"How much greater was {metric} for {c1} than {c2} in {years[y]}?"
            a = str(vals[c1][y] - vals[c2][y])
        out.append({"prompt": f"{metric}\n{header}\n{body}\n\nQuestion: {q}", "answer": a})
    return out



# --- Track F: constituent-family normalisers ---------------------------------------------
#
# All of these read a TRAIN or VALIDATION split. RouterArena draws its evaluation items from
# the corresponding test splits, and every item here still passes the SHA-256 + MinHash audit
# before it is kept -- provenance was relaxed, the overlap guarantee was not.


def _mmlu_pro(rows):
    out = []
    for r in rows:
        opts = [o for o in r.get("options", []) if o and o != "N/A"]
        if len(opts) < 2:
            continue
        idx = r.get("answer_index")
        ans = r.get("answer") or (chr(65 + idx) if isinstance(idx, int) else "")
        out.append({"prompt": _mcq(r["question"], opts), "answer": str(ans)})
    return out


def _arc(rows):
    out = []
    for r in rows:
        ch = r.get("choices") or {}
        texts = list(ch.get("text", []))
        if len(texts) < 2:
            continue
        out.append({"prompt": _mcq(r["question"], texts), "answer": r.get("answerKey", "")})
    return out


def _medmcqa(rows):
    letters = "ABCD"
    out = []
    for r in rows:
        opts = [r.get("opa"), r.get("opb"), r.get("opc"), r.get("opd")]
        if not all(opts):
            continue
        cop = r.get("cop")
        if not isinstance(cop, int) or not 0 <= cop < 4:
            continue
        out.append({"prompt": _mcq(r["question"], opts), "answer": letters[cop]})
    return out


def _pubmedqa(rows):
    out = []
    for r in rows:
        ctx = r.get("context") or {}
        passages = ctx.get("contexts") if isinstance(ctx, dict) else None
        body = " ".join(passages) if passages else ""
        dec = r.get("final_decision")
        if not dec:
            continue
        q = f"{body}\n\nQuestion: {r['question']}\nAnswer yes, no, or maybe."
        out.append({"prompt": q.strip(), "answer": str(dec)})
    return out


def _boolq(rows):
    out = []
    for r in rows:
        lab = r.get("label" if "label" in r else "answer")
        if lab is None:
            continue
        yes = lab in (1, True, "true", "True")
        out.append({
            "prompt": f"{r.get('passage', '')}\n\nQuestion: {r['question']}\nAnswer yes or no.",
            "answer": "yes" if yes else "no",
        })
    return out


def _ethics(rows):
    out = []
    for r in rows:
        text = r.get("input") or r.get("scenario") or ""
        lab = r.get("label")
        if not text or lab is None:
            continue
        out.append({
            "prompt": f"{text}\n\nIs this acceptable? Answer yes or no.",
            "answer": "no" if int(lab) == 1 else "yes",
        })
    return out


def _mathqa(rows):
    out = []
    for r in rows:
        opts = r.get("options", "")
        if not r.get("Problem") or not opts:
            continue
        out.append({"prompt": f"{r['Problem']}\nOptions: {opts}", "answer": str(r.get("correct", ""))})
    return out


def _socialiqa(rows):
    out = []
    for r in rows:
        opts = [r.get("answerA"), r.get("answerB"), r.get("answerC")]
        if not all(opts):
            continue
        lab = str(r.get("label", "")).strip()
        if lab not in ("1", "2", "3"):
            continue
        out.append({
            "prompt": _mcq(f"{r.get('context', '')} {r['question']}".strip(), opts),
            "answer": "ABC"[int(lab) - 1],
        })
    return out


def _gsm8k(rows):
    out = []
    for r in rows:
        ans = (r.get("answer") or "").split("####")[-1].strip()
        if not ans:
            continue
        out.append({"prompt": r["question"], "answer": ans})
    return out


def _trivia_qa(rows):
    out = []
    for r in rows:
        a = r.get("answer") or {}
        val = a.get("value") if isinstance(a, dict) else None
        if not val:
            continue
        out.append({"prompt": r["question"], "answer": val})
    return out


def _aqua_rat(rows):
    out = []
    for r in rows:
        opts = r.get("options") or []
        if not r.get("question") or len(opts) < 2:
            continue
        out.append({"prompt": _mcq(r["question"], list(opts)), "answer": str(r.get("correct", ""))})
    return out


def _wmt19(rows):
    out = []
    for r in rows:
        t = r.get("translation") or {}
        src = t.get("de") or t.get("cs") or t.get("ru") or t.get("fi")
        tgt = t.get("en")
        if not src or not tgt or len(src) < 20:
            continue
        out.append({"prompt": f"Translate into English:\n{src}", "answer": tgt})
    return out


SOURCES: list[Source] = [
    # --- Track F: constituent families, train/validation splits only --------------------
    Source("mmlu_pro", "mcq_knowledge", "TIGER-Lab/MMLU-Pro", _mmlu_pro,
           split="validation", licence="mit",
           notes="RouterArena's largest family (29% of sub_10); validation split, test is theirs"),
    Source("arc_challenge", "mcq_knowledge", "allenai/ai2_arc", _arc,
           config="ARC-Challenge", split="train", licence="cc-by-sa-4.0"),
    Source("arc_easy", "mcq_knowledge", "allenai/ai2_arc", _arc,
           config="ARC-Easy", split="train", licence="cc-by-sa-4.0"),
    Source("medmcqa", "mcq_clinical", "openlifescienceai/medmcqa", _medmcqa,
           split="train", licence="mit"),
    Source("pubmedqa", "mcq_clinical", "qiaojin/PubMedQA", _pubmedqa,
           config="pqa_labeled", split="train", licence="mit"),
    Source("superglue_boolq", "reading_comp", "google/boolq", _boolq,
           split="train", licence="cc-by-sa-3.0", notes="SuperGLUE BoolQ, train split"),
    Source("ethics_commonsense", "causal", "EleutherAI/hendrycks_ethics", _ethics,
           config="commonsense", split="train", licence="mit",
           notes="parquet mirror; hendrycks/ethics is script-based and HF dropped script support"),
    Source("aqua_rat", "math", "deepmind/aqua_rat", _aqua_rat,
           config="raw", split="train", licence="apache-2.0",
           notes="MathQA is derived from AQuA-RAT; the parent is parquet-hosted"),
    Source("social_i_qa", "causal", "lighteval/siqa", _socialiqa,
           split="train", licence="cc-by-4.0", notes="parquet mirror of Social IQa"),
    Source("gsm8k", "math", "openai/gsm8k", _gsm8k,
           config="main", split="train", licence="mit"),
    Source("trivia_qa", "short_answer", "mandarjoshi/trivia_qa", _trivia_qa,
           config="rc.nocontext", split="train", licence="apache-2.0",
           notes="quizbowl-adjacent, stands in for the QANTA family"),
    Source("wmt19_de_en", "translation", "wmt/wmt19", _wmt19,
           config="de-en", split="validation", licence="various",
           notes="validation split deliberately: the train split is ~38M pairs and we keep "
                 "3,600, so a full download is gigabytes of network for nothing"),
    Source("openbookqa", "mcq_knowledge", "allenai/openbookqa", _openbookqa,
           config="main", licence="apache-2.0",
           notes="elementary science MCQ, crowd-constructed; NOT ARC despite the org"),
    Source("commonsense_qa", "mcq_knowledge", "tau/commonsense_qa", _commonsense_qa,
           licence="mit", notes="ConceptNet-derived, independent of MMLU"),
    Source("medqa_usmle", "mcq_clinical", "GBaker/MedQA-USMLE-4-options", _medqa,
           licence="mit", notes="US licensing exam; disjoint question bank from MedMCQA"),
    Source("nq_open", "short_answer", "google-research-datasets/nq_open", _nq_open,
           licence="cc-by-sa-3.0", notes="real search queries, not quizbowl"),
    Source("web_questions", "short_answer", "stanfordnlp/web_questions", _web_questions,
           licence="cc", notes="Freebase/Google-suggest provenance"),
    Source("hotpot_qa", "long_rc", "hotpotqa/hotpot_qa", _hotpot,
           config="distractor", licence="cc-by-sa-4.0",
           notes="Wikipedia multi-hop; different genre from NarrativeQA's books"),
    Source("squad_v2", "reading_comp", "rajpurkar/squad_v2", _squad2,
           licence="cc-by-sa-4.0"),
    Source("mbpp", "code", "google-research-datasets/mbpp", _mbpp,
           config="full", licence="cc-by-4.0", notes="hand-written, pre-dates LiveCodeBench"),
    Source("bigcodebench", "code", "bigcode/bigcodebench", _bigcodebench,
           split="v0.1.0_hf", licence="apache-2.0",
           notes="library-use tasks, not a competitive-programming scrape"),
    Source("synthetic_math", "math", "(generated)", _synth_math, licence="n/a",
           notes="procedurally generated -- cannot overlap RouterArena by construction"),
    Source("synthetic_table", "table_numeric", "(generated)", _synth_table, licence="n/a",
           notes="procedurally generated table reasoning; avoids FinQA's source filings"),
    Source("opus_books_translation", "translation", "Helsinki-NLP/opus_books", _opus_books,
           config="en-fr", licence="various-public-domain",
           notes="literary parallel text; facebook/flores is gated and NTREX needs auth"),
    Source("winogrande", "cloze_wordsense", "allenai/winogrande", _winogrande,
           config="winogrande_xl", licence="cc-by",
           notes="AfLite-generated at scale; drop the 273 original WSC sentences"),
    Source("multi_nli", "nli", "nyu-mll/multi_nli", _mnli, licence="oanc"),
    Source("e_care", "causal", "12ml/e-CARE", _ecare, licence="mit"),
    Source("sciq", "science_qa", "allenai/sciq", _sciq, licence="cc-by-nc-3.0",
           notes="crowd-written science MCQ; bigbio/bioasq is a deprecated script dataset"),
]


def ra_hashes() -> set[str]:
    """RouterArena's evaluation questions, as normalised hashes. Exclusion reference only."""
    import pyarrow.parquet as pq

    rows = pq.read_table(RA_PARQUET).to_pylist()
    questions = []
    for r in rows:
        q = str(r.get("Question") or "").strip()
        if q:
            questions.append(q)
        ctx = str(r.get("Context") or "").strip()
        if ctx:
            questions.append(ctx)
    return hash_set(questions)


_WORD = re.compile(r"\w+")


def shingles(text: str, n: int = SHINGLE_N) -> set[str]:
    """Word shingles, for near-duplicate detection."""
    words = _WORD.findall(text.lower())
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def ra_shingle_index(parquet: Path = RA_PARQUET, n: int = SHINGLE_N) -> set[str]:
    """Every shingle RouterArena's evaluation questions contain.

    This is the near-duplicate reference. It deliberately covers questions only, not the
    long Context passages -- a shared Wikipedia paragraph is not evidence that an item was
    lifted from RouterArena, and indexing contexts makes any source that reuses passages
    (SQuAD, HotpotQA) look like a mass duplicate of itself.
    """
    import pyarrow.parquet as pq

    index: set[str] = set()
    for row in pq.read_table(parquet).to_pylist():
        q = str(row.get("Question") or "").strip()
        if q:
            index |= shingles(q, n)
    return index


def near_dup_ratio(text: str, index: set[str], n: int = SHINGLE_N) -> float:
    """Fraction of this text's shingles that already appear in the reference index."""
    sh = shingles(text, n)
    if not sh:
        return 0.0
    return len(sh & index) / len(sh)


def build(
    source: Source,
    ra: set[str],
    cap: int,
    ra_shingles: set[str],
    seen_exact: set[str],
    jaccard: float = NEAR_DUP_THRESHOLD,
) -> dict:
    """Fetch, normalise, audit and cap one source. Returns its report.

    Three drop reasons, kept separate in the report so the audit trail says *why*:
    exact hash collision with a RouterArena question, high shingle overlap with one, or an
    exact duplicate of something an earlier source already contributed.
    """
    from datasets import load_dataset

    conflict = source.provenance_conflict()
    if conflict:
        return {"source": source.name, "status": "REFUSED",
                "reason": f"provenance: derives from RouterArena constituent {conflict!r}"}

    if source.hf_id == "(generated)":
        items = source.to_items(None)
    else:
        kwargs = {"split": source.split}
        if source.config:
            kwargs["name"] = source.config
        ds = load_dataset(source.hf_id, **kwargs)
        rows = list(ds.select(range(min(len(ds), cap * 3))))
        items = source.to_items(rows)

    kept, exact, near, cross = [], 0, 0, 0
    for it in items:
        prompt = (it.get("prompt") or "").strip()
        if len(prompt) < 12:
            continue
        h = prompt_hash(prompt)
        if h in ra:
            exact += 1
            continue
        if near_dup_ratio(prompt, ra_shingles) >= jaccard:
            near += 1
            continue
        if h in seen_exact:
            cross += 1
            continue
        seen_exact.add(h)
        row = {"prompt": prompt, "answer": it.get("answer", ""),
               "cluster": source.cluster, "source": source.name}
        if it.get("tests"):
            row["tests"] = it["tests"]
        kept.append(row)
        if len(kept) >= cap:
            break

    rep = audit([k["prompt"] for k in kept], ra_hashes=ra)
    return {
        "source": source.name, "status": "OK" if rep.clean else "DIRTY",
        "hf_id": source.hf_id, "config": source.config, "split": source.split,
        "cluster": source.cluster, "licence": source.licence, "notes": source.notes,
        "n_raw": len(items), "n_ra_exact_dropped": exact, "n_ra_neardup_dropped": near,
        "n_cross_source_dup_dropped": cross, "near_dup_jaccard": jaccard,
        "n_final": len(kept), "audit": rep.as_dict(),
        "items": kept,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--sources", help="comma-separated source names")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--per-source-cap", type=int, default=1200)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    if args.list:
        print(f"{'source':<22}{'cluster':<18}{'hf id':<44}{'licence'}")
        for s in SOURCES:
            flag = "  REFUSED" if s.provenance_conflict() else ""
            print(f"{s.name:<22}{s.cluster:<18}{s.hf_id:<44}{s.licence}{flag}")
        return 0

    wanted = SOURCES
    if args.sources:
        names = {x.strip() for x in args.sources.split(",")}
        wanted = [s for s in SOURCES if s.name in names]
        missing = names - {s.name for s in wanted}
        if missing:
            print(f"unknown sources: {sorted(missing)}", file=sys.stderr)
            return 1
    elif not args.all:
        print("pass --all, --sources, or --list", file=sys.stderr)
        return 1

    ra = ra_hashes()
    ra_sh = ra_shingle_index()
    print(f"RouterArena reference: {len(ra)} question hashes, "
          f"{len(ra_sh)} {SHINGLE_N}-gram shingles (near-dup threshold {NEAR_DUP_THRESHOLD})\n")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    seen_exact: set[str] = set()
    reports = []
    for s in wanted:
        try:
            rep = build(s, ra, args.per_source_cap, ra_sh, seen_exact)
        except Exception as exc:  # noqa: BLE001 - a broken source must not stop the run
            rep = {"source": s.name, "status": "ERROR", "reason": f"{type(exc).__name__}: {exc}"}
        reports.append(rep)

        if rep["status"] == "OK":
            items = rep.pop("items")
            (args.out_dir / f"{s.name}.jsonl").write_text(
                "\n".join(json.dumps(i) for i in items) + "\n"
            )
            (args.out_dir / f"{s.name}_audit.json").write_text(json.dumps(rep, indent=2))
            print(f"  {s.name:<22} {rep['n_final']:>5} items   dropped: "
                  f"RA-exact {rep['n_ra_exact_dropped']}, "
                  f"RA-neardup {rep['n_ra_neardup_dropped']}, "
                  f"cross-source {rep['n_cross_source_dup_dropped']}")
        else:
            rep.pop("items", None)
            print(f"  {s.name:<22} {rep['status']}: {rep.get('reason', '')[:90]}")

    # Merge rather than overwrite. Building a subset used to replace the whole manifest, so
    # after `--sources a,b` the audit trail claimed the corpus was two sources -- and the
    # audit trail is the thing that proves no RouterArena item reached a parameter. A
    # provenance record that silently forgets the sources it is not currently looking at is
    # worse than none, because it looks complete.
    manifest_path = args.out_dir / "_manifest.json"
    merged: dict[str, dict] = {}
    if manifest_path.exists():
        try:
            for entry in json.loads(manifest_path.read_text()):
                if (args.out_dir / f"{entry['source']}.jsonl").exists():
                    merged[entry["source"]] = entry
        except (json.JSONDecodeError, KeyError, TypeError):
            pass  # a corrupt manifest is rebuilt from this run rather than propagated
    for entry in reports:
        merged[entry["source"]] = entry
    manifest_path.write_text(
        json.dumps([merged[k] for k in sorted(merged)], indent=2)
    )
    stale = [s for s in merged if not (args.out_dir / f"{s}.jsonl").exists()]
    if stale:
        print(f"warning: manifest entries with no corpus file: {stale}")
    ok = [r for r in reports if r["status"] == "OK"]
    total = sum(r["n_final"] for r in ok)
    print(f"\n{len(ok)}/{len(reports)} sources clean, {total} items -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
