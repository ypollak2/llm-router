# llm-router: measurement-selected single-model policy

Replaces our `llm-router` entry. Graded locally with this repo's own harness on the full split.

| metric | new | current entry | Δ |
|---|---|---|---|
| **Arena Score** | **75.75** | 71.26 | **+4.49** |
| Accuracy | 78.51% | — | — |
| Cost / 1K queries | $0.4905 | — | — |
| **Robustness** | **100.00** | 30.00 | **+70.00** |
| Failed / abnormal entries | 0 / 0 | — | — |

Model: `gemini-3-flash-preview` on every query.

## Why this is a single model and not a router

We built five routing mechanisms and each lost to a well-chosen constant on held-out data:

| mechanism | result |
|---|---|
| skill-cluster → model map | 0.078 normalised MI about which model wins; a *perfect* such map, fit with hindsight, is worth **+1.51** Arena |
| binary escalation classifier | 99.4% recall in-sample, **38.5%** held out |
| instance-level retrieval (k-NN over a measured outcome matrix) | **72.35** full split |
| per-model difficulty heads (AUC 0.73) | **72.37** full split |
| local LLM judge (`qwen3.8`, `qwen3-coder:30b`) | 68.4% / 68.8% accuracy vs 69.6% for the best constant |

The pattern is consistent: on this benchmark, at this pool, **which model you choose matters far
more than choosing per query.** The per-query oracle is 87.09, so the headroom is real — we could
not reach it with any method that survives a distribution shift from our training corpus to
RouterArena's.

Rather than ship a router the evidence does not support, this submission reports the constant
those experiments identified. The contribution is the selection method, not a routing policy.

## How the model was selected

1. **Screened 26 RouterArena-priced models** on an external corpus of 27 public datasets
   (29,693 items), audited SHA-256 exact plus MinHash near-duplicate against all 9,613
   RouterArena evaluation questions at **`overlap_count: 0`** on every source.
2. **Measured uncapped output length per model on external prompts**, cluster-stratified across
   all 13 skill clusters. This mattered: our first screen used capped output and mispriced
   verbose models by up to **23.7×** (`kimi-k2.5` emits 1,086 tokens uncapped, not the ~60 a
   capped screen suggests), which inverted the ranking.
3. Selected on external-holdout Arena under those corrected costs.

## Compliance

**No RouterArena prompt, answer, label or outcome informs the selection.** Training and
selection used the external corpus only; RouterArena data was used solely to *measure* frozen
policies, never to choose between them.

Two contaminations were found and discarded during this work, both self-reported here because
the alternative is a number we cannot defend:
* a hyperparameter sweep run directly on a held-out RouterArena split, and
* per-model costs derived from output lengths measured on RouterArena prompts, which had driven
  both a cost penalty and a pool choice. Every cost was re-measured on external prompts before
  this submission was built.

## Files

* `router_inference/config/llm-router-g3.json`
* `router_inference/predictions/llm-router-g3.json` (8,400 rows)
* `router_inference/predictions/llm-router-g3-robustness.json` (420 rows)

Both pass `check_config_prediction_files.py` on their respective splits.
