# PREREG-CLS-DEV: DEV screening of learned tier classifiers (PLAN v16 CLS-v16, D-127..D-130)

Status: DRAFT. Not frozen. This file is frozen only when `PREREG-CLS-DEV.sha256` exists beside it and
`scripts/check_prereg.py` reports a match, after an independent freeze-guard review says READY. Changing
any byte changes the hash. The freeze record is the last section. The author of this draft did not
review it.

Marks: **[CD]** = coordinator default for an open question (section 14 lists all of them; the coordinator
may overrule any before the freeze). **[EST]** = an estimate, not a measurement.

## 0. Authority and question

* **D-127** (owner 2026-10-16T12:55:33Z): a learned L2 classifier is in scope for v16. Workstream CLS-v16:
  (1) candidate search and offline screen, (2) tuning on a dev split only, (3) a new frozen prereg scored
  once on a held-out test, (4) on PASS an authority switch behind a kill switch. No result from the already
  seen P1.7-d set is test evidence; it may serve as dev data only.
* **D-128**: the best available GPT model may be a candidate, a second labeller or a builder. Codex CLI
  first ($0, rate-limited, reset about 16:06Z); OpenRouter only if needed (coordinator cap $20 total, per-call
  log). A GPT judge is blinded and never the only labeller of test truth.
* **D-129**: one download approved: `answerdotai/ModernBERT-base`, pinned by revision sha, in a CLS-v16 cache
  directory, used only if the embedding baseline is not enough (section 5, trigger T-MB). Label spend cap:
  the design's labelling estimate plus 50 percent, logged per call.
* **D-130** (a)-(h): the design `cls-v16-design.md`. This prereg implements (d) the DEV half and (e).

**Question.** Of the candidate classifiers listed in section 5, which (at most 2, each with frozen
thresholds) survive the ordered kill criteria of section 8 on DEV, so that they may be scored once on
TEST-SYN under a later prereg (`PREREG-CLS-TEST`)?

**What this prereg does not decide.** Whether any candidate may take routing authority. A DEV result is
never evidence for authority. A survivor is only a finalist. P1.6-g is not cited as engine evidence (PASS by
D-100 only; measured FAIL under D-71).

## 1. DEV data inventory

All sets below are DEV only (D-130 a). Nothing here is test evidence, and nothing here may enter TEST.
Sizes come from `cls-v16-design.md` section 2.1 and `gates/v16/GE3.json` / `gates/v16/P1.json`. They are
labelled as taken from those files; the **DEV manifest** (section 1.3) replaces every estimate with a
counted value before the first candidate is scored.

### 1.1 Real labelled sets

| Set | n | How truth was made | Source of the n |
|---|---|---|---|
| GE3 tune | 108 | Opus rubric label (`rubric.py` md5 `1bcb3f44`); Sonnet second label exists for all 342 GE3 items (kappa Opus-Sonnet 0.6242, session-boot CI 0.5587-0.6938) | `gates/v16/GE3.json` (108 tune + 234 held-out = 342, 50 sessions) |
| GE3 held-out | 234 | same; a 100-item outcome-truth subset exists (0 opus items) | same |
| E2-cal | 91 | Opus rubric (p_eval) | design 2.1 |
| E2-HO + E1-HO | 89 + 20 | rubric | design 2.1 |
| E1-tune | 30 | rubric | design 2.1 |
| P17D F | 32 (46 candidates from 4 sessions) | fresh Opus rubric, batches of 8, planted items 6/6 | `gates/v16/P1.json` p17d block: n_selected 151, n_labeled 150, 25 clusters |
| P17D G | 119 | same labels; G is the GE3 held-out re-used | design 2.1 |

Raw sum is more than the unique count: P17D G is the GE3 held-out again, and the E2/GE3 leak census had
large overlaps (design 2.1: 94+78+48+12 hits). The unique labelled real union is **about 450-500 [EST]**.
That figure is not used in any rule below; only the counted manifest values are.

Not in DEV (excluded, counted in the manifest):

* amend4 fresh window items (T0 2026-10-12T09:18:13Z) and any record with a timestamp at or after T0. They
  are reserved for PREREG-GT-amend4. Rule: no DEV item may have a record timestamp at or after T0.
* P17D items already carry the P17D cutoff (record timestamp before 2026-10-08T20:46:00Z); the same cutoff
  is applied to the whole DEV union (a DEV item with a later timestamp is dropped).
* `door_corpus.jsonl` (2,658 lines, no labels, 1,045 of 1,680 text-rule survivors above 150 words, not a pool
  of human prompts): not DEV data. It has no truth.
* RouterArena, MMLU-Pro and the other generic datasets in the HF cache: forbidden (RouterArena rules forbid
  tuning on its data; generic chat is not the target).

### 1.2 Synthetic set DEV-SYN

300 items from the frozen generator `CLS-SYN-GENERATOR-SPEC.md` (spec version 1, seed namespace DEV). Not
test evidence. Used for training and calibration of learned heads and for screening (section 6). Truth:
GPT + Sonnet (two judges, section 3.3), not Opus (Opus quota is kept for TEST labels). Real items keep at
least 50 percent of the pooled loss weight (section 7.3).

### 1.3 Dedup and the DEV manifest

Before any candidate runs, the coordinator-run attach step builds `dev_manifest.json` (counts and ids only,
no text, mode 0600, never committed with text). It records:

1. the unique real DEV set after dedup: exact `text_sha` match, then 5-shingle Jaccard >= 0.8 (the
   `supply.py` rule). Within a duplicate pair the item with the earliest record timestamp is kept and the
   other is counted as `dropped_dup`;
2. each item's `group`: its session uuid where the attach step recovers it (the `text_sha` join the P17D
   attach used). An item whose session cannot be recovered gets group `unk:<set>:<label-batch>`; the count
   of such items is reported. **[CD]** If more than 15 percent of real DEV items have no recovered session,
   the screen is labelled `LOW CLUSTER FIDELITY` in its output and kill criterion 6 is reported only, not
   applied;
3. per item: `item_id` (opaque), `source_set`, `length_band` (w <= 12, 13-50, 51-150, > 150 words),
   truth tiers per judge, `requested_tier` when joined, `secret_flag` (section 3.2);
4. counts: `n_real_dev`, `n_sessions`, largest-session share, per-source n, per-length-band n, per-truth-tier
   n (per judge and per truth rule of section 3.4), `n_truth_opus`, `n_excluded_secret`, `n_dropped_dup`,
   `n_after_T0_dropped`, and `n_syn_dev` after the DEV-SYN dedup;
5. the manifest sha256.

DEV-SYN dedup against real DEV: exact `text_sha` and 5-shingle Jaccard >= 0.8; a hit drops the synthetic
item (counted). If more than 3 percent of DEV-SYN items hit, the generator is flagged as memorising and the
coordinator is told (the generator never saw real text, so a hit means generic text, not copying).

**DEV validity floor.** The screen is NOT INFORMATIVE if, after the manifest: `n_real_dev` < 300, or
`n_truth_opus` (primary truth, real) < 40, or the largest session holds more than 20 percent of real DEV
items **[CD]** (the shadow_eval rule is 60 percent of scored items; 20 percent is stricter because DEV is
also used for training).

## 2. Arms

Every arm scores on the same items, paired by construction. A prediction is one of haiku, sonnet, opus, or
`None` (refusal or error; section 6.2).

### 2.1 Candidates (screened)

| Id | Candidate | Fit needed | Notes |
|---|---|---|---|
| C1 | `bge-small-en-v1.5` embedding + ordinal logistic regression | LR on DEV | offline from the HF cache; revision = the cached snapshot dir name recorded in the manifest (`5c38ec7c405ec4b44b94cc5a9bb96e735b38267a` at the time of writing; the run re-reads it) |
| C2 | `nomic-embed-text` (Ollama `/api/embed`) embedding + ordinal LR | LR on DEV | Ollama model digest recorded; task prefix `classification: ` fixed [CD] |
| C3 | ModernBERT-base head, fine-tuned | fine-tune on DEV | **conditional** on trigger T-MB; one download, pinned revision sha, `HF_HOME` set to the CLS-v16 cache dir (never system-wide); card licence read and recorded before download (expected Apache-2.0, to be verified, not assumed) |
| C4 | Qwen3.5 (`qwen3.5:latest`) logprob readout of the existing v6 prompt (`local_classifier.py`, thinking off) | calibration only | first-token logprobs of the tier tokens, then a 3-feature ordinal LR fitted in-fold. Needs an Ollama build that returns logprobs; if not available, kill criterion 1 applies (no silent fallback to argmax) |

Heads for C1-C3 are two binary ordinal heads, P(tier >= sonnet) and P(tier >= opus), giving P(haiku),
P(sonnet), P(opus). Input text is `canonical_text` (harness blocks stripped, last 3,000 characters) plus the
context digest where one exists (DEV-SYN has one; real DEV items use the digest the P17D/GE3 judge saw if the
attach step recovered it, else none). A candidate's score feature set is frozen with the candidate.

**C7 (NVIDIA prompt-task-and-complexity-classifier as a feature): EXCLUDED in this prereg [CD].** Two
independent reasons: its licence (NVIDIA Open Model License, "other" on HF) has not been read here, and a
second model download is not covered by D-129, which approves exactly one. It enters only through an
amendment that records the licence text decision and an owner approval for the download. Until then it is
not screened and not in any table.

Not screened (reason on record): Arch-Router (preference routes, non-OSI licence, no difficulty), RouteLLM
weights (wrong model pair), NotDiamond (stale, binary), DeBERTa-v3 (kept as the fallback if C3 cannot be
downloaded or fails; would need its own amendment because its download is not approved).

### 2.2 Reference arms (validity and baselines, never finalists)

| Id | Arm | Role |
|---|---|---|
| K-W1 | `nimble:9b` (v6/v7 hard verdict, frozen config) | known-worse (rules-dominated on under-route in P17D: under 72/150 vs 61/150). Re-run on every DEV item, not reused from the P17D shadow rows |
| K-B1 | `qwen3-coder:30b` rubric arm (rubric prompt, hard verdict) | known-better (only measured arm better than rules on tune: C-lambda2 6.79 vs 9.58, exact 55/91). Local $0, heavy; **exempt from criterion 1** (latency, RAM) because it is a reference, not a candidate |
| K-W2 | always_haiku | known-worse |
| K-W3 | uniform random tier (seed 20261017) | known-worse |
| A-S, A-O | always_sonnet, always_opus | baselines. always_opus MUST fail the NOT-TRIVIAL guard (gate-validity check) |
| K-O | noised oracle: truth with 25 percent symmetric rank-1 flips (seed 20261017) | must PASS-STRONG, else the set cannot separate |
| R-E | rules_eff (live tier policy, haiku lifted to sonnet; the P17D section 4 resolver) | **the bar** |
| R-U | rules_unlifted (live tier policy, no lift) | reported only; the live baseline since haiku went live 2026-10-09 |
| E-0 | engine alone (L1 + L3 lexical, shadow defaults, frozen engine SHA) | null for arm B; reported |
| M-q | uninformed sonnet/opus (haiku/sonnet below 2.7) mixture at the candidate's raw cost | the NOT-TRIVIAL guard comparator (computed, not run) |

### 2.3 Combination arms

Per finalist candidate two pre-registered arms, as D-130 f: **A** (L2 alone: tier from thresholds on L2
probabilities; secrets, prefix and needs_tools guards stay outside the model) and **B** (engine tier unless
L2 confidence >= t_conf, then L2's tier wins; below t_conf the engine tier stands). During screening every
candidate is scored as arm A only. Arm B is fitted and scored once per surviving candidate in section 9, so a
finalist is a (candidate, arm) pair. No stacking, no learned fusion.

## 3. Truth and the two-judge relabel of real DEV

### 3.1 Judges

* Opus: the existing rubric labels (rubric md5 `1bcb3f44`). They were produced in blinded batches of 8 with
  planted items 6/6 under GE3/P17D rules, so they are **reused, not re-bought** **[CD]**. If the coordinator
  wants a fresh Opus pass, that is an amendment with its own spend line.
* GPT: relabelled now (this prereg) via Codex CLI (`gpt-6-astra`, ChatGPT subscription, run with the real
  HOME because its auth lives there; the harness records the model id returned on every call and discards a
  call whose model family differs from the pinned one, as the Opus family check did). OpenRouter only if
  Codex stalls, inside the D-128 cap, per-call log, same prompts.
* Sonnet: used where it already exists (GE3, 342 items) and for DEV-SYN (section 3.3).

### 3.2 Secret scrubber (nothing leaves the machine unscrubbed)

Applies to every item before it is placed in any prompt that goes to GPT (Codex or OpenRouter). The Opus
labels already exist; no new Opus send is part of this prereg, except as section 3.3.

Detector = the union of (a) `engine._secret_names(text)` (high-precision shapes: anthropic, openrouter,
openai, google, aws id, github, slack, private key, jwt, bearer, db url password, assigned high-entropy
literal), (b) `secret_scrubber.scrub_text(text) != text` (the canonical anchored scrubber), (c)
`persist_redaction._PROSE_SECRET_RE` match, (d) any value from the live secret denylist the P17D attach used
(read read-only by the coordinator-run step), and (e) any whitespace-delimited token of length >= 24 whose
Shannon entropy is >= 3.3 bits and that contains both a letter and a digit.

Rule: **an item with any detector hit is excluded from DEV entirely**, never redacted-and-sent, never sent.
It is counted (`n_excluded_secret`, per detector, no text) and it is not scored by any arm, so every arm
sees the identical item set (a split set would make truth bases differ between arms). Items that pass the
detector still get PII replaced by fixed tokens (email, phone, absolute home path) before sending; judges
therefore see slightly different text than the earlier Opus labels did. This is declared; the effect shows as
kappa(Opus, GPT) and is not corrected.

The scrubber is a pure function with a unit test that plants one fake secret of every shape above (fake
values, never real) and one clean control per shape. The relabel run starts only if the planted set is 100
percent detected and 0 controls flag. A failed self-test is terminal for the run.

Canary at send time: after scrubbing, the outgoing batch text is re-scanned by the same detector; any hit
aborts the batch before the call and counts as `scrub_abort`.

### 3.3 Relabel procedure (GPT on real DEV; GPT and Sonnet on DEV-SYN)

* Blinded: opaque ids `L001...`, no tier, no session id, no set name, no other judge's label. The judge sees
  only the id, the scrubbed digest where one exists and the scrubbed prompt, in the P17D `prompt_view` form.
* Batches of 8. Order by `sha256("clsdev:label:20261017:" + item_id)`.
* **Calibration.** The first call of each judge's run is the 6 planted items of `rubric.py` (cal-h1/h2,
  cal-s1/s2, cal-o1/o2) plus the first 2 real items, shuffled with seed 20261017, planted items under opaque
  ids from the same id space. No reroll: the first valid label for each planted id is final. Required: **6/6
  per judge per run**. A judge below 6/6 is NOT INFORMATIVE for that run: its labels are discarded.
  Consequence: if GPT fails 6/6, real DEV has Opus-only truth, criterion 8 is not evaluable and the screen
  is labelled `NO SECOND JUDGE`; the coordinator brings it back to the owner. Nothing proceeds on a
  guessed truth.
* Retry a failed batch up to 3 times, then items one at a time; an item still unlabelled is a label error,
  counted.
* Rate limit stalls: resume state is the append-only `labels_gpt.jsonl`; a resumed run re-checks the model id
  on its first call. Calibration is not re-asked on resume (no reroll), but a model-id change mid-run
  stops the run.
* Spend: Codex $0. OpenRouter use is logged per call and counts toward the shared $20 cap. The DEV-SYN
  Sonnet pass runs on the subscription ($0).
* Item with no valid label from a required judge: excluded from every truth rule that needs it, counted. If
  more than 5 percent of items are missing the primary truth: NOT INFORMATIVE.

### 3.4 Truth rules

| Truth | Definition | Use |
|---|---|---|
| **T-primary (real DEV)** | Opus + GPT; if they differ, the **higher** tier **[CD]** (conservative: under-route is the failure we fear) | threshold fitting, criteria 3-7 |
| T-opus | Opus label only | criterion 8; sensitivity |
| T-gpt | GPT label only | criterion 8; sensitivity |
| T-agree | the subset where Opus and GPT agree | sensitivity |
| T-3j (GE3 subset only) | majority of Opus, GPT, Sonnet; 3-way split resolves to sonnet (the TEST rule) | report only: shows how much T-primary moves toward the TEST rule |
| **T-primary (DEV-SYN)** | GPT + Sonnet; if they differ, the higher tier | fitting and criteria on DEV-SYN |
| T-gpt-syn, T-son-syn | single-judge | criterion 8 on DEV-SYN |

Reported: weighted (quadratic) kappa and exact agreement of Opus vs GPT on real DEV (session-bootstrap CI),
Opus vs Sonnet on GE3 (already 0.6242), GPT vs Sonnet on DEV-SYN, with per-length-band and per-source
tables. For GPT-family finalists the GPT-excluded truth (Opus only on real DEV; Opus+Sonnet on the GE3
subset, split to the higher tier) is reported beside T-primary (D-128, D-130 b). The side figure R3 (effort
signal) is reported on the real subset where it exists, as a rank-agreement check across truth bases.
There is no human validation (the owner declined, D-R8-2); this is a stated gap.

## 4. Scoring (identical to the TEST scoring, so DEV numbers predict TEST numbers)

The scorer is `scripts/cls_eval.py` (build B1, brief `brief-cls-b1.txt`), over the cost functions of
`src/llm_router/shadow_eval.py` (sha256 pinned; at this draft's base `e9b89943...`):

* c(haiku) = 1.0, c(sonnet) = 2.7, c(opus) = 6.15, lambda = 2; `C-lambda2-clamp` = mean of
  c(min(pred, requested)) plus 2 c(truth) when the clamped pred is below truth; under-route is unclamped
  (pred below truth).
* **requested_tier**: the item's own joined tier when it exists. DEV-SYN and unjoined real items take
  **opus** (clamp inert) **[CD]**. This deviates from shadow_eval's imputation (session mode, else sonnet),
  which would clamp unjoined items to sonnet and manufacture a bar the data does not support. The scorer
  also prints the shadow_eval imputation result as a sensitivity row. P17D had requested = opus on 146/151
  items, so the clamp is nearly inert either way.
* Paired session/cluster bootstrap, B = 2000, fixed seed 20261017, clusters = `group` of section 1.3
  (DEV-SYN: the generation call id).
* Bars (necessary): under(cand) <= under(R-E) and C-lambda2-clamp(cand) <= C-lambda2-clamp(R-E). Non-
  inferiority: upper 95 percent bound of (cand - R-E) <= +3 points of under-route and <= +0.3 C-lambda2.
  NOT-TRIVIAL guard: under(cand) below the uninformed mixture M-q at equal raw cost with the one-sided 95
  percent bootstrap bound below 0. always_opus must fail it. Raw cost is reported with CI and not gated.
* On DEV these are screening quantities (kill criteria 4-6), not verdicts. No DEV result carries a PASS label
  into any claim.

## 5. Candidate triggers

**T-MB (ModernBERT is screened).** C3 is screened only if, after C1 and C2 and C4 are scored under criteria
1-4, **no embedding or logprob arm survives to ranking, or the best of them has real-DEV AUROC(P(opus),
T-primary opus) below 0.75** **[CD]**. "Not enough" in D-129 is thereby fixed in advance. If T-MB does not
fire, C3 is not downloaded and the manifest says so. If it fires, the coordinator runs the download once:
official repo only, revision sha recorded, cache dir `$CLS/hf`, licence from the card recorded in the log
before the first byte, file sha256s recorded after.

Data volume warning stated before any result: about 450-500 real plus 300 synthetic items is below the 2-5k
a 3-way ModernBERT fine-tune normally wants (candidates file, section 4). C3 is therefore expected to
overfit; it is screened because D-129 approved the download, not because the data supports it. Its
screening result is not read as evidence about ModernBERT in general.

## 6. DEV protocol

### 6.1 Folds

Pooled DEV = real DEV (manifest) + DEV-SYN. Folds: session-grouped 5-fold, grouping by `group`
(DEV-SYN groups = generation call id). A group never spans folds. Fold assignment: groups sorted by
`sha256("clsdev:fold:20261017:" + group)`, dealt round-robin, then balanced greedily so fold sizes differ by
at most one group size; fold ids are written to the manifest before any candidate runs and are the same for
every candidate. A DEV-SYN group and a real group are never merged.

### 6.2 Run rules

* Temperature 0, one inference config per candidate, frozen with its artifact sha256 (weights, prompt,
  schema, tokenizer, quantisation, runtime version, `ollama` digest). Hyper-parameters are fixed here and not
  searched on DEV scores: LR C chosen by inner 4-fold CV within each outer training set from {0.1, 1, 10};
  ModernBERT lr 2e-5, 4 epochs, batch 16, last 512 tokens, one seed 20261017.
* Any hyper-parameter change after the first DEV score is an amendment with a later `frozen_utc`, and the
  earlier scores stay in the log.
* **Append-only screening log** (`cls_dev_log.jsonl`): every candidate run, config sha, start and end time,
  machine-quiet readings (section 10), counts. Nothing is deleted. A crash is logged as a run.
* Refusal or error (parse failure, timeout, runtime down, secret flag at run time): the prediction is `None`
  and is scored as **rules_eff's tier** (the shadow_eval fallback arm), counted as `n_fallback`. The same
  count feeds criterion 2.
* The candidates see item text and digest only. They never see truth, group, source set or another
  candidate's output.

### 6.3 Training and the real-weight rule

Learned heads (C1-C3, and C4's calibrator) are trained on the training folds of pooled DEV with item
weights: real items weight 1, DEV-SYN items weight `min(1, n_real / n_syn)` **[CD]** so real items carry
at least 50 percent of the loss. The loss is ordinal and asymmetric: under-route costs as in C-lambda2
(cost(pred) + 2 cost(truth) when pred < truth).

## 7. Threshold fitting

### 7.1 Decision form

For a candidate with probabilities (P_h, P_s, P_o): route **opus** if P_o >= t_hi; else **haiku** if
P_h >= t_lo; else **sonnet** (conservative default). Grid: t_hi in {0.20, 0.25, ..., 0.95}, t_lo in {0.50,
0.55, ..., 0.99}. A hard-verdict arm (nimble, K-B1) has no thresholds and is scored at its verdict.

### 7.2 Constraints (all must hold at a grid point)

1. under(cand) <= under(R-E) minus a margin, where the margin is fixed by the **20th percentile of the
   session-bootstrap distribution of (under(R-E) - under(cand))**: the grid point is admissible only if
   that 20th percentile is >= 0 (the candidate beats rules in at least 80 percent of cluster resamples)
   **[CD]** (the design's wording, "the margin is the 20th percentile of the DEV-bootstrap", is read this way).
2. opus share of predictions <= DEV truth-opus share (T-primary).
3. haiku-route precision (pred haiku and truth haiku, over pred haiku) >= 0.70, with at least 10 haiku
   routes **[CD]** (the minimum stops a tiny-n precision from passing; with fewer than 10, no haiku routes is
   allowed and t_lo is set to 1.01).
4. NOT-TRIVIAL guard holds on DEV (section 4).

Objective: minimise weighted pooled C-lambda2-clamp; ties broken by lower opus share, then lower raw cost,
then larger t_hi, then larger t_lo (fully deterministic). **No admissible grid point: the candidate is
killed** (kill criterion 4).

### 7.3 Out-of-sample protocol

Threshold CV on the out-of-fold score table: model scores come from the 5-fold session-grouped OOF
predictions. Thresholds are then fitted by a second session-grouped 5-fold over that OOF table: thresholds
are chosen on four folds of OOF scores and applied to the fifth. The pooled fifth-fold decisions are what
criteria 4-8 score. The **frozen thresholds** handed forward are the ones fitted once on the full pooled OOF
table, with the model refit on all DEV. That the OOF score table is not fully independent of the model
hyper-parameters (C chosen inside folds, so it is, to first order) is a declared residual optimism.

## 8. Ordered kill criteria

Applied in this order, per candidate, frozen before the first DEV score. The first failure kills; later ones
are still computed and reported (never skipped silently). Computed on real DEV with T-primary unless stated.
Reference arms are exempt from criteria 1, 3, 4, 5, 6, 8 (they are validity checks), but are subject to the
validity check (section 8.1).

| # | Criterion | Kill when |
|---|---|---|
| 0 | Data validity | the DEV validity floor of section 1.3 fails: no candidate verdict, the screen is NOT INFORMATIVE |
| 1 | Hard fit | p95 latency (section 9) > 1,500 ms uncontended for a sync candidate; or RAM conflicts with the single-model-resident rule; or the runtime/Ollama version needed is unavailable. A candidate that is fast enough only async is marked **async-only** (no sync authority) and may be a finalist only with that label **[CD]** |
| 2 | Errors | parse/error/timeout rate > 5 percent (nimble precedent 4/151) |
| 3 | Signal | AUROC(score, truth is opus) on real DEV < 0.65, or its session-bootstrap 95 percent lower bound < 0.55. For a hard-verdict arm the verdict rank is the score |
| 4 | Operating point | no admissible (t_hi, t_lo) under section 7 with the guard on the pooled DEV threshold CV |
| 5 | Cost | raw cost at the operating point > 1.35 x R-E raw cost (R-E raw 2.746 on P17D, so about 3.71, near the oracle's 3.79; the value is recomputed on the DEV manifest and written to the log first) |
| 6 | Shift | significantly worse than R-E on real DEV (CI lower bound of the paired difference > 0 on either under-route or C-lambda2-clamp) **while** passing criteria 4-5 on DEV-SYN: the candidate fits the generator, not the traffic. Reported only under `LOW CLUSTER FIDELITY` |
| 7 | Stability | decisions identical on a repeat run for < 98 percent of items (P1.6-b). Repeat = a second full run with an unchanged config on a different quiet window |
| 8 | Judge robustness | the sign of (under, C-lambda2-clamp) minus R-E under T-opus differs from T-gpt on more than 1 of the 2 metrics (the candidate learns one judge). Evaluated on real DEV; on DEV-SYN with T-gpt-syn vs T-son-syn |

### 8.1 Validity of the screen (run in the same pass, never decisive for a candidate, decisive for the screen)

* always_opus must fail the NOT-TRIVIAL guard (gate validity). If it passes: the guard is broken, the
  screen is NOT INFORMATIVE.
* Noised oracle must PASS-STRONG on the bars. If it does not: DEV cannot separate, NOT INFORMATIVE.
* Known-better K-B1 must beat known-worse K-W1 on (under, C-lambda2-clamp) with the paired CI of the
  difference excluding 0 on at least one metric and no metric reversed. If not: **no kill based on
  separation** and no finalist is named; the coordinator reports to the owner. **[CD]** DEV has fewer items
  than TEST, so this check is weaker than the TEST one; it is a sanity floor.
* Spread (under, C-lambda2-clamp) of K-B1 minus K-W1 with its CI, and the known-worse trio (always_haiku,
  random, nimble) all failing the bars, are reported.

## 9. Ranking and the finalist output

1. Survivors of criteria 0-8 are ranked by pooled DEV-CV C-lambda2-clamp subject to under-route no worse
   than R-E. Keep the top 2, preferring diverse families **[CD]**: one LLM-or-GPT-or-logprob arm and one
   small learned head if both exist among survivors. If only one family survives, one finalist.
2. For each finalist (candidate), fit arm B's `t_conf` (grid {0.50, 0.55, ..., 0.95}) by the section 7
   procedure with the same constraints and the engine tier from E-0. The finalist pair (candidate, arm) is
   the better of arm A and arm B by the same deterministic objective. **[CD]** Both arms are then scored
   once on TEST-SYN only for the winning arm of each finalist; the loser is reported from DEV only.
3. **Output** `cls_dev_result.json` (counts, ids and tiers only; no text) and `cls_dev_finalists.json`:
   per finalist: candidate id, arm, artifact sha256 list, frozen thresholds (t_hi, t_lo, t_conf), runtime
   config, DEV-CV metrics with CIs, the full kill-criteria table including every number, the per-stratum
   tables (length band, truth tier, task type, tool need, source, generator family) and the manifest sha256.
   Both files get a `.sha256` and are cited by sha256 in `PREREG-CLS-TEST`.
4. **At most 2 finalists.** If 0 survive: the CLS-v16 L2 route stops here; the result is "no candidate
   passed DEV", recorded with the table, not reworked on TEST. A candidate killed here is not re-run without
   an amendment that says what changed and why.
5. The coordinator names the finalists; no model is run on TEST-SYN during this prereg. TEST-SYN labels are
   produced and hashed under the TEST prereg before any finalist is scored on them.

## 10. Latency, quiet-machine rule and the 00:00-07:00 exclusion

### 10.1 Latency measurement

* Measured on this machine, on the interface B2 defines, `l2_score(text, ctx) -> (probs, ms)`, wall time by
  `time.perf_counter()` around the whole call (tokenise or embed, HTTP to Ollama where used, the head, and
  parse). Sync candidates only; Codex/OpenRouter candidates are not sync candidates and are not measured.
* Sample: 200 prompts drawn from real DEV stratified by length band (seed 20261017), run sequentially,
  single thread, the model resident (keep-alive set, `ollama ps` confirms only the candidate is resident).
  5 warm-up calls are discarded and reported separately with the cold first-load time.
* Three blocks, each in a different quiet window (at least 30 minutes apart). Pooled p50, p95, p99 with a
  bootstrap CI on p95. Criterion 1 uses pooled p95 (point estimate) and the worst block's p95, both
  reported; the kill uses the pooled value **[CD]**.
* RAM: peak RSS of the process, and Ollama's reported model size, from the same blocks.
* Number of calls and blocks are printed beside every latency figure.

### 10.2 Quiet-machine rule (every timed block and every heavy-model run)

A block is valid only if, sampled at the start and every 60 s: `sysctl -n vm.loadavg` 1-minute value is
below 2.0 at start and the block's mean below 1.5, no sample at or above 3.0 **[CD]** (thresholds
are coordinator defaults, to be checked against what P2.0 uses); `ollama ps` shows no model other than the
candidate under test; no other `codex`, `claude -p`, `pytest`, `ruff` or build process owned by this user is
running (counted by `ps -axo comm`, observed only; never `pkill` by pattern); the machine is on AC power and
`pmset -g therm` shows no throttling. The readings are written into the log. An invalid block is discarded,
logged as `invalid:<reason>`, and re-run; the number of discarded blocks is reported.

### 10.3 Exclusion window

No timed block and no heavy-model run (C3 training, C4, K-W1, K-B1, any model above 4 GB resident) **starts
or is running** between **00:00 and 07:00** (**UTC [CD]**: the plan stamps every log in UTC; the P2.0
prereg's clock is to be confirmed). A run that reaches 00:00 is stopped by its own recorded PID (never by
pattern), logged, and resumed after 07:00 from its resume state. Embedding arms C1 and C2 with the model
resident under 1 GB, and CPU-only work on the manifest, are not heavy-model runs but still obey the
quiet-machine rule when timed. This is a hard exclusion of the P2.0 requal windows (L13: one heavy slot).

Consequence stated: the design assumed nightly heavy slots (design 3.4, 4). That is **contradicted** by this
exclusion for 00:00-07:00. Heavy arms (K-B1 about 18 GB, K-W1 about 9 GB, C4 about 6.6 GB) therefore need
quiet daytime windows with no build or test run, which competes with Codex-build verification on the same
machine. Section 11 gives the schedule consequence.

## 11. Run order, spend and stop rule

1. Freeze-guard review, then freeze (section 15).
2. Manifest build and secret exclusion count. Scrubber self-test.
3. GPT relabel of real DEV (Codex), calibration first. DEV-SYN generation (spec doc) in parallel; DEV-SYN
   labels (GPT, Sonnet).
4. C1 and C2 (cheap, also the floor), then C4, then K-W1, K-B1, then T-MB decision, then C3 if triggered.
   One heavy model at a time, outside 00:00-07:00.
5. Criteria, ranking, finalists file.

Spend: Codex $0. OpenRouter only on a Codex stall, shared cap $20 across CLS-v16, per-call log. Stop and
report to the coordinator at 80 percent of any cap. No Opus call is part of this prereg.

## 12. Privacy

* Prompt text lives only in the coordinator-run attach output and the label-run input files (0600,
  directories 0700), the argv/stdin of a judge call after scrubbing, and the HTTP body sent to localhost
  Ollama. Nothing else holds text.
* No prompt text goes to GPT unless it passed section 3.2. No item with a secret hit is sent.
* Outputs and everything committed to the repo hold ids, counts, tiers and hashes only.
* No write to `~/.llm-router` or `~/.claude`; no read of `~/.codex` by any harness author.
* D-130 h: no per-prompt network classifier. GPT is used offline for labels only.

## 13. Known biases (stated before any data)

* Rubric truth is a judge's opinion, not realised adequacy; kappa(rubric, outcome) = 0.096 (n = 100, CI
  -0.004..0.216). Different truths gave different verdicts in P1.6-g. Truth here is judge-defined, and the
  DEV result says nothing about outcome adequacy.
* rules_eff is almost always sonnet (149/151); "no worse than rules_eff" is "no worse than a constant". The
  guard and the non-inferiority bounds exist because of this, but the DEV screen is weak at detecting small
  gains (minimum detectable about 6-8 points of under-route at TEST size; DEV is smaller).
* DEV real items overlap and re-use: GE3 tune fitted engine L3 (D-69); E2/E1 tuned nimble v7. Candidates
  trained on DEV pooled with items that tuned the baselines sit closer to those baselines than a fresh set
  would. This favours nobody in particular but biases the Cost and Shift figures.
* GPT generates 70 percent of DEV-SYN and GPT labels it: circularity. Reported by generator family;
  criterion 6 and the real-weight rule are the guards.
* The truth rule for DEV (two judges, higher) differs from the TEST rule (three judges, majority, split to
  sonnet). Thresholds fitted here move when the truth rule changes. T-3j on the GE3 subset reports the size
  of this shift. This is a stated deviation, not corrected.
* Thresholds, `n_real_dev` and `n_truth_opus` are not known at writing; the estimates in section 1 are not
  inputs to any rule.

## 14. Coordinator defaults (open questions, each answered here and overridable before the freeze)

| # | Question | Default taken |
|---|---|---|
| CD-1 | Reuse existing Opus labels for the two-judge DEV truth, or re-buy Opus | reuse (no spend) |
| CD-2 | Two-judge disagreement rule on DEV | higher tier |
| CD-3 | Items with a detected secret | excluded from DEV entirely (not redacted, not sent) |
| CD-4 | requested_tier for items without a joined tier | opus (clamp inert); shadow_eval imputation reported as sensitivity |
| CD-5 | ModernBERT trigger | fires only if no C1/C2/C4 survivor, or best real-DEV AUROC < 0.75 |
| CD-6 | NVIDIA complexity feature | excluded (licence unread; second download not approved) |
| CD-7 | Real-weight rule for pooled loss | syn weight min(1, n_real/n_syn) |
| CD-8 | Constraint 1 margin | the candidate beats R-E in >= 80 percent of cluster resamples |
| CD-9 | Haiku precision floor | >= 0.70 with >= 10 haiku routes, else no haiku routing |
| CD-10 | DEV validity floor | n_real >= 300, n_truth_opus >= 40, largest session <= 20 percent |
| CD-11 | Quiet thresholds | load1 < 2.0 at start, block mean < 1.5, any sample >= 3.0 invalid |
| CD-12 | Clock of the exclusion | UTC, 00:00-07:00 |
| CD-13 | Finalist diversity | prefer one LLM/logprob and one small learned head |
| CD-14 | Arm B scoring on TEST | winning arm only per finalist |
| CD-15 | Latency kill uses | pooled p95 over 3 blocks |
| CD-16 | async-only candidate | may be a finalist, labelled, no sync authority |
| CD-17 | LOW CLUSTER FIDELITY | > 15 percent of real items without a recovered session: criterion 6 report-only |

## 15. Freeze record

Not frozen. To freeze: the coordinator commissions an independent freeze-guard review (it must not be the
author and does not see the conversation); on READY, write `PREREG-CLS-DEV.sha256` as
`<sha256>  PREREG-CLS-DEV.md` and `frozen_utc <UTC stamp from date -u>`; `scripts/check_prereg.py` must
report a match. Items still open at the freeze must be answered in section 14 or stated as parked.
