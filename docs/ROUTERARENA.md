# What we actually measured on RouterArena

Most routing tools claim a savings percentage. Almost none of them say what was
measured, on which data, at what cost, or what failed. This page is the version
of that claim we would be willing to defend in a thread.

**Read the caveat first: the numbers below are graded locally, with this repo's
own harness, against the public RouterArena dataset.** They are reproducible —
every command is on this page — but they are not yet an independently verified
leaderboard placement. Where a figure is self-graded, it says so.

---

## What RouterArena measures

[RouterArena](https://github.com/RouteWorks/RouterArena) scores model routers on
an accuracy-versus-cost curve rather than accuracy alone, which is the only
framing under which a router can be evaluated honestly: any router can buy
accuracy by sending everything to the most expensive model, and any router can
buy cheapness by sending everything to the smallest one. The Arena Score
penalises both.

It also scores **robustness** — whether the router makes the *same* decision
when a question is paraphrased. This turns out to be where most routers,
including our first entry, fall over.

## Results

Full split, 8,400 queries, graded locally with this repo's harness:

| Metric | Value |
|---|---|
| **Arena Score** | **72.35** |
| Accuracy | 72.59% |
| Cost per 1K queries | $0.1093 |
| Robustness | 79.29 (87 flips of 420 paraphrase pairs) |
| Failed / abnormal entries | 0 / 0 |

Routed model distribution: `gemini-3.1-flash-lite-preview` 63.1%,
`qwen/qwen3-235b-a22b-2507` 20.9%, `deepseek/deepseek-v4-flash` 16.0%.

### Against our own previous entry

| | Arena | Robustness |
|---|---|---|
| First entry (`llm-router`) | 71.26 | 30.00 |
| Current (`llm-router-knn`) | **72.35** | **79.29** |
| Delta | +1.09 | **+49.29** |

The accuracy gain is small. The robustness gain is not, and it is the more
interesting number: the first entry routed on **hashed bag-of-words**, which
changed its model choice on **294 of 420** paraphrase pairs. Rewording a
question got you a different model and therefore a different answer quality, for
no reason a user could see or predict. The current router uses features that
survive paraphrase — a frozen sentence embedding plus deterministic structural
signals (length band, code markers, script, equation and MCQ shape) — and flips
on 87.

If you only take one thing from this page: **a router that changes its mind when
you rephrase the question is not routing, it is sampling.**

## How it routes

Per query, retrieve the 120 nearest questions from an external corpus whose
per-model outcomes have been measured, then estimate for each candidate model

```
P(correct | query, model) = (Σᵢ wᵢ·yᵢₘ + κ·priorₘ) / (Σᵢ wᵢ + κ)
```

over those neighbours, and take the argmax. Weights are a softmax over
similarity; κ shrinks sparse or distant neighbourhoods back toward each model's
global accuracy, so a query unlike anything measured falls back to the prior
rather than trusting one weak neighbour.

Frozen hyperparameters: K=120, κ=3.0, temperature=0.1, λ=0.0, channels = dense +
structural.

## What it cost to produce

| Run | Cost | Wall time |
|---|---|---|
| `sub_10` split | ~$0.50 | 30–40 min |
| Full split (8,400 queries) | ~$3–5 | 4–5 hours |
| Robustness split | $0 | seconds — routing only, no inference |

An `OPENROUTER_API_KEY` and the dataset parquets are the only prerequisites.
Reproduction commands are in
[`submissions/routerarena/README.md`](../submissions/routerarena/README.md).

## What did not work

Publishing only the winning configuration would make this a marketing page. The
attempts that failed are the more useful record:

- **Classification into skill clusters was not better than a constant.** The
  original entry assigned each prompt to a subject cluster and routed the cluster
  to its measured-best model. Across the variants tried, **instance-level
  retrieval was the only mechanism that beat simply always picking one model** on
  the Arena score. A per-cluster policy sounds principled and did not pay.
- **Proxy splits misled by 4.25 points.** Tuning against a convenient subset and
  extrapolating gave a number 4.25 points away from the real full-split result.
  A single-point calibration on a proxy distribution is a guess, not a
  measurement — evaluate on the target distribution or do not quote the figure.
- **Optimality entries are not included** in the current submission, so
  Opt.Sel / Opt.Cost / Opt.Acc show as unavailable. That is a gap, not a result.

## How this relates to the savings claim

RouterArena measures routing *quality* on a fixed public dataset. It does **not**
measure the 35–80% savings figure quoted elsewhere in this project's docs — that
comes from local usage aggregation on real sessions, is estimated against an
all-premium baseline, and is not independently verified.

Two different claims, two different evidence bases. Neither should be used to
support the other.
