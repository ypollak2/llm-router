# llm-router-knn: instance-level retrieval routing

Adds `llm-router-knn`, a router that chooses per query by retrieving similar questions it has
already measured the candidate pool on, rather than by classifying the query into a category.

## Results (graded locally with this repo's own harness, full split, 8,400 queries)

| metric | value |
|---|---|
| **Arena Score** | **72.35** |
| Accuracy | 72.59% |
| Cost / 1K queries | $0.1093 |
| Robustness | 79.29 (87 flips / 420) |
| Failed / abnormal entries | 0 / 0 |

Model distribution: `gemini-3.1-flash-lite-preview` 63.1%, `qwen/qwen3-235b-a22b-2507` 20.9%,
`deepseek/deepseek-v4-flash` 16.0%.

This supersedes our existing `llm-router` entry (Arena 71.26, Robustness 30.00): **+1.09 Arena
and +49.29 Robustness**. The robustness gain comes from routing on features that survive
paraphrase — the previous entry used hashed bag-of-words, which flipped its choice on 294 of
420 paraphrase pairs.

Optimality entries are not included in this submission, so Opt.Sel / Opt.Cost / Opt.Acc will
show as unavailable.

## How it routes

For each query, retrieve the 120 nearest questions from an external corpus whose per-model
outcomes we have measured, then estimate for each candidate model

    P(correct | query, model) = (Σ_i w_i·y_im + κ·prior_m) / (Σ_i w_i + κ)

over those neighbours, and select the argmax. Weights are a softmax over similarity;
similarity combines a frozen sentence embedding with deterministic structural features
(length band, code markers, script, equation and MCQ shape). κ shrinks sparse or distant
neighbourhoods back toward each model's global accuracy.

Frozen hyperparameters: K=120, κ=3.0, temperature=0.1, λ=0.0, channels = dense + structural.

Inference is deterministic and offline: no sampling, no network call, no clock. The same query
always produces the same model choice.

## Data provenance and compliance

**No RouterArena prompt, answer, label or outcome informs any parameter.**

* **Retrieval index** — 5,200 questions from an external corpus of 27 public datasets. Every
  source carries a `compared`-mode contamination audit against all 9,613 RouterArena
  evaluation questions: SHA-256 exact match after NFC → strip → collapse-whitespace →
  casefold, plus a MinHash near-duplicate pass (5-gram shingles, Jaccard 0.5). **Every source
  reports `overlap_count: 0`**; a source that could not be proven disjoint was dropped rather
  than filtered. Audit reports ship with the corpus.
* **Outcomes** — measured by us by running the candidate pool over that external corpus.
* **Hyperparameters and model pool** — selected on a 50/50 split of the external corpus, with
  the retrieval index half disjoint from the validation half. Frozen before any RouterArena
  data was scored.
* **Per-model cost estimates** — RouterArena's *published* price table combined with output
  lengths we measured on **external prompts only**, sampled 4 per skill cluster across all 13
  clusters so that long-output clusters are represented.
* **RouterArena data** was used only to *measure* the frozen policy, never to select or tune
  it.

An earlier iteration of this work derived its cost estimates from output lengths measured on
RouterArena prompts. That is arguably fitting a router component on benchmark data, so it was
discarded and every cost re-measured on external prompts before this submission was built.

## Reproducing

```
python experiments/routerarena/knn_router.py --sweep          # hyperparameter sweep
python scripts/routerarena/run_inference_full.py              # generate predictions
python router_inference/check_config_prediction_files.py llm-router-knn full --check-generated-result
```

## Files

* `router_inference/config/llm-router-knn.json`
* `router_inference/predictions/llm-router-knn.json` (8,400 rows)
* `router_inference/predictions/llm-router-knn-robustness.json` (420 rows)

Both prediction files pass `check_config_prediction_files.py` on their respective splits.
