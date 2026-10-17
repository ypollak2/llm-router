# PREREG-CLS-DEV (r5): DEV screening of learned tier classifiers (PLAN v16 CLS-v16, D-127..D-130)

Revision r5 (2026-10-17, text brought in line with the merged builds B0 #537, B1 #526 + F11 #539, B-L #527 and F20 #544, and with coordinator decisions D-143, D-147, D-148, D-160, D-161 and D-169). Status: DRAFT. Not frozen. This file is frozen only when `PREREG-CLS-DEV.sha256` exists beside it and
`scripts/check_prereg.py` reports a match, after an independent freeze-guard review says READY. Changing
any byte changes the hash. The freeze record is the last section. The author of this draft did not
review it.

**r5 changes (text only; no number that decides a candidate moved).**

* 1.1, 1.3: a real record with no timestamp is dropped and counted (`n_no_timestamp_dropped`); the manifest order and
  its counters are stated; DEV-SYN is also deduplicated within itself and secret-screened; the floor adds the 5 percent
  missing-truth term; the manifest outputs are registered in PINS-RUN.
* 2.2: how each reference arm is built (rules_tier file, K-B1 payload, E-0 call, the `oracle` arm).
* 3.1-3.3: GPT is pinned by `-m` (D-147); the Codex canary runs in plain mode and checks the `model:` header; a Codex
  non-zero exit parks the run and is never a void; the judge prompt contract (pinned gt500.py, supply.py, rubric.py;
  rubric as the first block of stdin; single-answer id rule, D-161); `calibration_call_failed` voids; file list.
* 3.2: `.env` key match is case-insensitive; exact judge argv; self-test is 23 shapes and goes stale with the scrubber.
* 3.4, 4: kappa tables split real and DEV-SYN; the non-inferiority bound is the 97.5th percentile, the guard the
  one-sided 95th (D-148); DEV prints SCREEN-PASS or SCREEN-REJECT only, `--mode test` is single-use.
* 5, 8, 8.1, 9: T-MB waits while criteria 1-4 are pending; the criteria table has one status vocabulary; criterion 1
  gets the 15 s async budget (D-160); criterion 3 excludes fallback items (D-160); criterion 8 statuses and the
  DEV-SYN leg (D-160, F20); a missing validity arm means no finalist; Arm B `t_conf` is an in-sample screen
  (D-160); ranking ties.
* 7.1: the threshold grid is 16 x 12 = 192 points (D-143).
* 10.1: the latency sample and p95 definition are stated.
* 14: CD-26..CD-31 added for the above.
* 15: PINS-STATIC also pins `gt500.py` and `supply.py` (D-161), the model-id keys, the Codex `--json` and `-m` flags,
  the argv templates and the session-log directories; the absolute-path note and `--freeze-line`.
* 2.1, 14 (D-169): CD-18 amended for C2 only: a 384 whitespace-word cap (digest kept whole, prompt tail kept), stated
  as an approximation because C2 has no local tokenizer; C3 follows CD-18 (512 tokens, digest whole, tail kept); C1
  unchanged.
* 2.1, 15 (D-169): `qwen3.5:latest` is listed twice by Ollama `/api/tags` (digests `2e16a80fe3d7...` and
  `44a38603922e...`); the pin is the sorted set of every digest listed under a name (never the last one), verify fails
  when the set changes, and C4 runs `qwen3.5:latest` as Ollama resolves it.
* 15 (D-169): a bare Ollama name is looked up as `name:latest` (Ollama semantics; `nomic-embed-text` ->
  `nomic-embed-text:latest`).
* 3.1, 15 (D-169): model ids `judge_gpt` = `gen_gpt` = `gpt-6-astra` (D-118; confirmed by a coordinator `codex exec -m`
  probe before the freeze), `judge_sonnet` = `claude-sonnet-5-5`, `judge_opus` = `gen_opus` = `claude-opus-5-5`.
* 1.3, 15 (D-169): the exclusion-list path is the P17D attach's `~/.llm-router/denylist.txt` (named and hashed only);
  the `.env` is `~/.llm-router/.env`, hashed by the coordinator at the real pins build, never a hard pin.
* 15 (D-169): the six v1 generator files are committed in `docs/prereg/cls-syn-gen/` (the `--gen-dir`), authored
  from the generator spec text and reviewed at the freeze; the spec's Appendix C digest-schema line gains one space
  after `0-6,`, because without it detector (e) of 3.2 fires on every generation request.

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

**GPT is not a screened candidate.** A GPT-family classifier would need one network call per prompt, which D-130
(h) forbids without a separate owner privacy decision, and the Codex CLI spawn costs several seconds against a
1.5 s sync budget (candidates file, section 6 addendum). GPT is used offline as a judge only (section 3).

**What this prereg does not decide.** Whether any candidate may take routing authority. A DEV result is
never evidence for authority. A survivor is only a finalist. P1.6-g is not cited as engine evidence (PASS by
D-100 only; measured FAIL under D-71).

The runner for everything in sections 1.3, 6, 7 (candidate fitting and scoring inputs), 9 and 10 is build **B0**
(`scripts/cls_dev_run.py`, brief `brief-cls-b0.txt`); the scorer and threshold fit are B1 (`scripts/cls_eval.py`).
The relabel runner, generator runner, resemblance tool, send library and `PINS-STATIC.json` / `PINS-RUN` builder are build **B-L**
(`scripts/cls_label_run.py`, `cls_gen_run.py`, `cls_resemble.py`, `cls_send_lib.py`, `cls_pins.py`, brief
`brief-cls-bl.txt`). `PINS-STATIC.json` pins every script's sha256 at the freeze (section 15).

## 0a. Order of events (r3)

1. Freeze this prereg, the generator spec (version 1) and **`PINS-STATIC.json`** (section 15). The builds B0, B1,
   B-L merge **before** the freeze, because `cls_pins.py --verify` pins them. **B2 and B3 may merge after the
   freeze**: the engine is pinned by commit (`git show`), so a later merge does not change a pin.
   Run-time values are pinned in **`PINS-RUN`** addenda written, hashed and logged before their first use
   (sections 0a steps 2-4 and 15).
2. **Attach step and manifest first** (counts and text, no labels): the real-DEV items, groups, length-band counts,
   dedup and secret exclusion (section 1.3). The manifest's **length-band counts file** is the only input the
   generator takes from real data (aggregate counts). There is no provisional-shares fallback: generation does not
   start without the counts file.
3. **DEV-SYN iterates alone.** DEV-SYN is generated under spec version 1 and checked by the resemblance tool
   (spec section 8.1). On failure the spec may be amended, using **only DEV-SYN's pass/fail and AUROC numbers**
   (nothing about TEST-SYN exists yet), at most **3 spec versions in all** (v1, v2, v3). The first version that
   passes is **frozen as vN** and hashed.
4. **TEST-SYN is generated once under vN**, by the separate TEST-SYN agent, before any DEV screening, with **one
   regeneration** allowed on its own failure of the text-only checks and before any TEST label exists. If that
   second TEST-SYN also fails, TEST-SYN is NOT INFORMATIVE as a proxy (no further regeneration). **If no DEV-SYN
   version passes within 3**, screening proceeds with DEV-SYN labelled `RESEMBLANCE_FAILED` (its results are
   reported with that label and carry no weight in criterion 6) and TEST-SYN is declared **NOT INFORMATIVE as a
   proxy** without being generated. A replaced TEST-SYN set is a **discarded set**: hashed, never read, never
   scored.
5. **Custody.** TEST-SYN is stored 0600 in `$CLS/test-syn/`, outside every DEV tooling path (B0 refuses the path,
   tested). `cls_resemble.py` runs on TEST-SYN **inside the TEST-SYN agent's own step** (it is the only tool
   that reads TEST-SYN text before release). Immediately afterwards the items file is made **chmod 0000** until
   release. The allocation table, `gen_requests.jsonl` and items-file hashes are **committed to git** before DEV
   screening starts. The **release token is held by the coordinator**. Control is procedural plus the path guard
   and file mode (same Unix user; residual in section 13).
6. Real-DEV relabel, DEV-SYN labelling, DEV screening (sections 1-9). The finalists file is hashed. Only then does
   the coordinator release TEST-SYN to the TEST prereg, which labels it before any finalist is scored on it.

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
  is applied to the whole DEV union (a DEV item with a later timestamp is dropped). A real record with **no recoverable
  timestamp** cannot be shown to precede T0 and is dropped too (`n_no_timestamp_dropped`; B0 `cls_dev_lib.time_filter`).
  The three drop reasons are counted separately (`n_after_T0_dropped`, `n_after_cutoff_dropped`, `n_no_timestamp_dropped`).
* `door_corpus.jsonl` (2,658 lines, no labels, 1,045 of 1,680 text-rule survivors above 150 words, not a pool
  of human prompts): not DEV data. It has no truth.
* RouterArena, MMLU-Pro and the other generic datasets in the HF cache: forbidden (RouterArena rules forbid
  tuning on its data; generic chat is not the target).

### 1.2 Synthetic set DEV-SYN

300 items from the frozen generator `CLS-SYN-GENERATOR-SPEC.md` (spec version 1, seed namespace DEV). Not
test evidence. Used for training and calibration of learned heads and for screening (section 6). Truth: **three
judges, the TEST rule** (section 3.4): GPT, Sonnet and Opus. The Opus pass is on all 300 items. Real items keep
at least 50 percent of the pooled loss weight (section 6.3).

Opus spend estimate for DEV-SYN: P17D measured $2.5377 for 150 items ($0.0169 per item); the design used
$0.019. 300 items is **$5.1-5.7 [EST]**, plus a small plant overhead. **D-134** sets the CLS-v16 Opus labelling cap
at **$20 in total** (DEV-SYN about $5.5 plus TEST about $7.6, both estimates), within the approved D-129 labelling
spend. The run **stops at 80 percent of the cap ($16)**; anything beyond goes to the owner.

### 1.3 Dedup and the DEV manifest

**Attach step (defined).** A coordinator-run sub-agent, read-only, no model call, counts only on stdout, writes
only 0600 files under `$CLS/dev/` (the P17D attach pattern, D-107 c). It: (1) takes each DEV set's items from the
item files that produced its labels (held under `$PP`); (2) recovers `session_id`, record timestamp and
`requested_tier` by the P17D attach rules (the GE3 record rules `gt500.py` `9cde774d...` and `supply.py`
`12cd3e1e...`, the `sha256(text)` join, earliest record wins); (3) reads, read-only, the exclusion list
`denylist.txt` that the P17D attach read (`~/.llm-router/denylist.txt`, the `--denylist` default of the pinned
`scripts/p17d_attach.py`; path named in `PINS-STATIC.json` with its sha256; its semantics is an
exclusion list of prompts, 0 entries at P17D, so any prompt matching an entry is excluded and counted) and the
**values of keys in the owner's `.env` whose name matches `KEY|TOKEN|SECRET|PASSWORD|PASS`, of length >= 8,
empty values skipped** (`~/.llm-router/.env`; path named in `PINS-STATIC.json`; only the count of qualifying values and the file sha256 are
recorded; **the `.env` sha256 is also logged at each use**; values are held in memory, never printed; **the `.env` sha256 changing is intentional and no snapshot
of the file is kept**); (4) joins existing labels: Opus (and Sonnet for GE3) labels
are keyed by `text_sha` in the earlier `labels.jsonl` files (`{text_sha, session_id, truth}`); the manifest holds
the `text_sha -> item_id` map and joins by `(session_id, text_sha)` as `shadow_eval.load_labels` does, falling
back to `("", text_sha)`; (5) writes `items.jsonl` (text, 0600) and the inputs of the manifest.

Before any candidate runs, the attach output builds `dev_manifest.json` (counts and ids only,
no text, mode 0600, never committed with text) with `cls_dev_run.py manifest`. Order inside it: time filter (1.1),
then dedup, then the secret detector, then group and fold assignment. The command also writes `length_counts.json`
(the four band counts, the generator's only real input) and `shingles_real.json`, and registers all three in a
PINS-RUN entry before first use (section 15); every later B0 command reads the manifest only through a matching entry.
It records:

1. the unique real DEV set after dedup: exact `text_sha` match, then 5-shingle Jaccard >= 0.8 (the
   `supply.py` rule). Within a duplicate pair the item with the earliest record timestamp is kept and the
   other is counted as `n_dropped_dup`;
2. each item's `group`: its session uuid where the attach step recovers it (the `text_sha` join the P17D
   attach used). An item whose session cannot be recovered gets group `unk:<set>:<label-batch>`; the count
   of such items is reported. **[CD]** If more than 15 percent of real DEV items have no recovered session,
   the screen is labelled `LOW CLUSTER FIDELITY` in its output and kill criterion 6 is reported only, not
   applied;
3. per item: `item_id` (opaque), `source_set`, `length_band` (w <= 12, 13-50, 51-150, > 150 words),
   truth tiers per judge, `requested_tier` when joined;
4. counts: `n_real_dev`, `n_sessions`, largest-session share, per-source n, per-length-band n, per-truth-tier
   n (per judge and per truth rule of section 3.4), `n_truth_opus`, `n_excluded_secret`, `n_dropped_dup`,
   `n_after_T0_dropped`, and `n_syn_dev` after the DEV-SYN dedup;
5. the paths and sha256 of the denylist and `.env` files used, and the manifest sha256;
6. excluded-by-secret counts **by Opus truth tier and by detector** (no text), so a tier-skewed exclusion is
   visible.

DEV-SYN is screened by the same secret detector (a hit drops the item, `n_syn_excluded_secret`) and deduplicated
against real DEV and within itself: exact `text_sha` and 5-shingle Jaccard >= 0.8; a hit drops the synthetic
item (counted as `n_syn_dropped_dup_vs_real` and `n_syn_dropped_dup_within`). If more than 3 percent of DEV-SYN
items hit real DEV (`syn_leak_share`), the generator is flagged as memorising (`syn_memorising_flag`) and the
coordinator is told (the generator never saw real text, so a hit means generic text, not copying).

**DEV validity floor.** The screen is NOT INFORMATIVE if, after the manifest: `n_real_dev` < 300, or
`n_truth_opus` (primary truth, real) < 40, or the largest session holds more than 20 percent of real DEV
items **[CD]** (the shadow_eval rule is 60 percent of scored items; 20 percent is stricter because DEV is
also used for training), or more than 5 percent of real DEV items lack the primary truth (section 3.3). The
two truth terms are `PENDING` until labels exist (B0 `validity_floor` returns OK, PENDING or NOT_INFORMATIVE;
PENDING is not a pass: criterion 0 is `pending` and blocks finalists).

## 2. Arms

Every arm scores on the same items, paired by construction. A prediction is one of haiku, sonnet, opus, or
`None` (refusal or error; section 6.2). **DEV figures are guard-free and screening only**: no secrets, prefix or
needs_tools guard is applied to any arm (the real guards stay outside the model and are not part of a DEV
score), and a DEV number is never a verdict or a claim.

### 2.1 Candidates (screened)

| Id | Candidate | Fit needed | Notes |
|---|---|---|---|
| C1 | `bge-small-en-v1.5` embedding + ordinal logistic regression | LR on DEV | offline from the HF cache; revision = the cached snapshot dir name recorded in the manifest (`5c38ec7c405ec4b44b94cc5a9bb96e735b38267a` at the time of writing; the run re-reads it) |
| C2 | `nomic-embed-text` (Ollama `/api/embed`) embedding + ordinal LR | LR on DEV | Ollama model digest recorded (the bare name is looked up as `nomic-embed-text:latest`, D-169); task prefix `classification: ` fixed [CD] |
| C3 | ModernBERT-base head, fine-tuned | fine-tune on DEV | **conditional** on trigger T-MB; one download, pinned revision sha, `HF_HOME` set to the CLS-v16 cache dir (never system-wide); card licence read and recorded before download (expected Apache-2.0, to be verified, not assumed) |
| C4 | Qwen3.5 (`qwen3.5:latest`) logprob readout of the existing v6 prompt (`local_classifier.py`, thinking off) | calibration only | runs `qwen3.5:latest` as Ollama resolves it; `/api/tags` lists that name twice (digests `2e16a80fe3d7...` and `44a38603922e...`, one manifest file on disk), so the pin is the sorted set of both digests and verify fails if the set changes (D-169). First-token logprobs of the tier tokens, then a 3-feature ordinal LR fitted in-fold. Needs an Ollama build that returns logprobs; if not available, kill criterion 1 applies (no silent fallback to argmax). `c4-precheck` splits the failure (D-160): no usable `logprobs` field is a criterion-1 kill (`no_logprobs_field`); logprobs present but the tier token not located is **parked for the coordinator** (`tier_token_not_located`, counts and shapes only, never token text) and is not a kill |

**Context digest.** A real item's digest is the one the P17D/GE3 judge saw if the attach step recovered it, else
none; it is **capped at 400 characters** (truncated, same cap as DEV-SYN digests). A candidate's feature set is
frozen with the candidate.

**Frozen fitting choices (r2).**

* **Input string.** `"[ctx] " + digest + " [prompt] " + canonical_text` when a digest exists, else
  `canonical_text`. `canonical_text` = harness blocks stripped, last 3,000 characters. Input length is capped
  **[CD]** (CD-18, amended r5, D-169): C1 (bge-small) and C3 (ModernBERT) at 512 tokens of their own tokenizer; C2
  (nomic, Ollama) at **384 whitespace-delimited words**, because no local nomic tokenizer exists; 384 words is a
  conservative stand-in for 512 tokens and is an **approximation**, not a token count (a text of unusual words can
  still exceed 512 tokens; it is then sent to Ollama as cut and no further cap is applied by the harness). For every embedder, when over the cap the
  **digest part is kept whole and the prompt part is truncated from its head (the tail is kept)**; no truncation
  cuts the digest.
* **Embedding.** L2-normalised vector, then `StandardScaler` fitted on the training fold only.
* **Binary heads.** `sklearn.linear_model.LogisticRegression`, penalty L2, solver lbfgs, `max_iter=1000`,
  `tol=1e-4`, `class_weight=None`. Head k takes `sample_weight = w_pool * m`, where `w_pool` is the real/synthetic
  weight of section 6.3 and `m = 2.0` for items whose truth is at or above the head's tier and 1.0 otherwise
  (the lambda = 2 under-route penalty as a positive-class weight). `C` is chosen by inner 4-fold group CV,
  grid {0.1, 1, 10}, by the weighted log-loss of the head.
* **Ordinal assembly.** P(>=sonnet) and P(>=opus) are clipped to [0.001, 0.999], P(>=opus) is forced <= P(>=sonnet),
  then P(haiku) = 1 - P(>=sonnet), P(sonnet) = P(>=sonnet) - P(>=opus), P(opus) = P(>=opus).
* **C4's three features.** The first-token logprobs of the three tier tokens of the v6 prompt, renormalised over
  those three tokens (softmax): `lp_haiku`, `lp_sonnet`, `lp_opus`. They feed the same two binary heads
  (three features, one fitted LR each, same weights, C grid and ordinal assembly). Mechanics (B0
  `cls_dev_lib`): the v6 request of `local_classifier._payload` plus `logprobs: true, top_logprobs: 20`; the tier
  token is the first token after `"tier": "`; a top-20 alternative counts for a tier when its stripped lowercase
  text is a non-empty prefix of exactly one tier word (best logprob wins); a tier absent from the top 20 takes
  logprob -30 before the softmax. A response without a usable tier token is an error (`None`), never an argmax.
* **Confidence** of any candidate is `max(P_haiku, P_sonnet, P_opus)`.

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
| K-B1 | `qwen3-coder:30b` rubric arm (rubric prompt, hard verdict) | known-better (only measured arm better than rules on tune: C-lambda2 6.79 vs 9.58, exact 55/91). Local $0, heavy; **exempt from criterion 1** (latency, RAM) because it is a reference, not a candidate. **Circularity, stated:** it is prompted with the same rubric the judges use for truth, so its advantage is partly by construction; the check shows the set can separate a rubric-aligned arm from a weak arm, not that either is right |
| K-W2 | always_haiku | known-worse |
| K-W3 | uniform random tier (seed 20261017) | known-worse |
| A-S, A-O | always_sonnet, always_opus | baselines. always_opus MUST fail the NOT-TRIVIAL guard (gate-validity check) |
| K-O | noised oracle: truth with 25 percent symmetric rank-1 flips (seed 20261017) | must PASS-STRONG, else the set cannot separate |
| R-E | rules_eff (live tier policy, haiku lifted to sonnet; the P17D section 4 resolver) | **the bar** |
| R-U | rules_unlifted (live tier policy, no lift) | reported only; the live baseline since haiku went live 2026-10-09 |
| E-0 | engine alone (L1 + L3 lexical, shadow defaults, frozen engine SHA) | null for arm B; reported |
| M-q | uninformed sonnet/opus (haiku/sonnet below 2.7) mixture at the candidate's raw cost | the NOT-TRIVIAL guard comparator (computed, not run) |

**How the reference arms are built (r5; `cls_dev_run.py reference-arms`).**

* R-E and R-U come from `rules_tier.jsonl` (header `{"schema": "rules_tier/1", resolver_commit, produced_utc}`, then
  one row per item). The file is read only through a matching PINS-RUN entry (exit 9 otherwise) and is refused (exit 5)
  when a row's `haiku_live` contradicts its record time (live from 2026-10-09T00:00:00Z). R-E lifts haiku to sonnet;
  R-U keeps haiku for `haiku_live` rows and equals R-E before the live date.
* K-W1 sends the live classifier payload (`local_classifier._payload`, verdict parsed by `parse_verdict`); K-B1 sends
  the pinned rubric through the B-L single-item judge payload (3.3) to the local Ollama (`think: false`,
  `num_predict` 400, `num_ctx` 8192) and parses it with the same label validator. Both run in 25-item units
  under the quiet gate and the night rule of section 10, one heavy model at a time.
* E-0 is `engine._decide_light(text, door="router", lexical=True)` (L1 + L3 lexical, no capability detector, no
  cache, no network; tier `local` is read as haiku), after the engine module's sha256 is checked against the
  PINS-STATIC engine pin.
* The constants (always_haiku, always_sonnet, always_opus), the random arm (seed 20261017) and the noised oracle
  (25 percent flips of the primary truth, seed 20261017; at the lowest or highest tier the only neighbour is used)
  are computed, not run. An `oracle` arm (the primary truth itself) is also written and is reported only.

### 2.3 Combination arms

Per finalist candidate two pre-registered arms, as D-130 f: **A** (L2 alone: tier from thresholds on L2
probabilities; secrets, prefix and needs_tools guards stay outside the model) and **B** (engine tier unless
L2 confidence >= t_conf, then L2's tier wins; below t_conf the engine tier stands). During screening every
candidate is scored as arm A only. Arm B is fitted and scored once per surviving candidate in section 9, so a
finalist is a (candidate, arm) pair. No stacking, no learned fusion.

## 3. Truth, the relabel, and what leaves the machine

### 3.1 Judges (r2: DEV truth = the TEST rule, D-132 c)

* **Opus**: existing rubric labels on real DEV (rubric md5 `1bcb3f44`; blinded batches of 8, planted items 6/6 under
  GE3/P17D rules): **reused, not re-bought** **[CD]**. **New Opus pass on all 300 DEV-SYN items** (section 1.2).
* **Sonnet**: labels **all real DEV**. GE3's 342 existing Sonnet labels (rubric kappa 0.6242 to Opus) are reused
  **[CD]**; every other real item gets a new Sonnet label. DEV-SYN: new Sonnet labels on all 300. Subscription, $0.
* **GPT**: new labels on all real DEV and all DEV-SYN via Codex CLI (`gpt-6-astra`, D-118; the coordinator confirms the id with one `codex exec -m gpt-6-astra` probe
  before the freeze, and if Codex is unavailable at run time the D-147 park rule applies; ChatGPT subscription, real
  HOME because its auth lives there). **Every Codex call passes `-m <pinned id>` (D-147).** The `--json` event stream
  of Codex 0.161.0 carries no model field (coordinator probe 2026-10-16), so the served model is enforced by the CLI,
  which exits non-zero on an unknown id, not read back; the id recorded for a GPT call is the pin. A non-zero
  exit (bad id, timeout, CLI error) **stops and parks the run (exit 24), resumable; it is never a void and never a
  `TRUTH_FALLBACK`.** For Sonnet and Opus the harness reads the model ids of `modelUsage`: an id of another
  family, or an id that changes inside a run, voids that judge's run (3.3); a call that reports no id parks the
  run (exit 22).
* OpenRouter: **only** with providers whose data policy is no-training and no-retention (the routing option that
  denies data collection and requires zero-data-retention endpoints, recorded per call). If no such provider
  serves the model, OpenRouter is **not used at all**. Shared $20 cap (D-128), per-call log.

### 3.2 Secret scrubber and what is sent

Applies to every item before it is placed in any prompt that leaves the machine (Codex/GPT, `claude -p` for
Sonnet and Opus, generator calls, OpenRouter). Every such send goes through `outgoing_check` on the **exact
bytes sent** (the final stdin payload: text, digest, template and ids). A hit aborts the send before the call
and counts as `scrub_abort`.

**Detector** = the union of (a) `engine._secret_names(text)`; (b) `secret_scrubber.scrub_text(text) != text`; (c)
`persist_redaction._PROSE_SECRET_RE`; (d) any entry of the exclusion list `denylist.txt` (a prompt exclusion list, D-134) and any qualifying `.env`
value (keys matching `KEY|TOKEN|SECRET|PASSWORD|PASS`, **case-insensitive**, length >= 8, empty skipped, surrounding quotes stripped; D-134, D-135) found in the
text (section 1.3 attach), with hits **reported by detector**; (e) any whitespace-delimited token of length >= 24 whose
Shannon entropy is >= 3.3 bits and that contains a letter and a digit.

Rule: **an item with any detector hit is excluded from DEV entirely**, never redacted-and-sent, never sent. It is
counted (`n_excluded_secret`, by detector and by Opus truth tier, no text) and is not scored by any arm, so
every arm sees the identical item set.

**PII replacement** on items that pass: emails, phone numbers, absolute home paths (`/Users/<name>`,
`/home/<name>`), **hostnames** (any dotted name ending in `.local`, `.internal`, `.lan`, `.corp`, `.home` and any
host:port), and **internal IP addresses** (RFC 1918, link-local, loopback) are replaced by fixed tokens. The
owner's own repository and project names are **allowed through and disclosed**: they go to GPT, Sonnet and Opus
in prompts and in the generator's cell vectors. Judges therefore see slightly different text than the earlier
Opus labels did; this is declared, not corrected, and shows in the kappa figures.

**Self-test (per shape).** One fake secret of every shape in the detector (23 shapes at r5: the engine, scrubber,
prose, denylist, `.env`-value and entropy shapes and six shapes of the independent scanner; fake values assembled
from fragments; never a real-looking literal in the repo) and one clean control per shape. The run starts only if **every shape
fires its own detector** (reported per shape, not as an aggregate) and 0 controls flag. A failed self-test is
terminal for the run. Every other B0 and B-L command refuses (exit 6) without a passing result file, and a result
file goes **stale** (exit 6) when the scrubber changes: the result records a sha over `cls_send_lib.py`, `engine.py`,
`secret_scrubber.py` and `persist_redaction.py`.

**Independent scan.** After the sent set is fixed and before the first call, a second scanner independent of
the detector (gitleaks or trufflehog if installed; else a documented regex set that shares no pattern with
(a)-(e)) scans **the full text of the sent set** and any later outgoing batch. A hit excludes the item
retroactively, voids its labels, and is counted. A spot-check sample is not a substitute for the full scan.

**Call hygiene (all judge and generator calls).** Empty scratch cwd, **read-only sandbox, canary-verified** (for
Codex; `claude -p` runs with its tools disabled), text via **stdin** (never argv).
* `claude -p` calls (the argv of `cls_send_lib.claude_argv`, recorded as `argv_template` in PINS-STATIC):
  `claude -p --safe-mode --settings <run-dir>/claude_settings.json` (holding `{"disableAllHooks": true}`) `--tools ""
  --strict-mcp-config --no-session-persistence --setting-sources project,local --model <pinned id> --output-format json`;
  `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` removed from the child environment (asserted), a fresh empty scratch
  cwd per call, real HOME (auth only), `LLM_ROUTER_SESSION_KIND=research` (as PREREG-P20 section 6). This matches the P2.0
  pattern so the router hooks and proxy cannot route, draft or alter a call. There is **no `--system-prompt`**: the
  rubric is the first block of the stdin payload (3.3).
* `codex exec` calls: `codex exec -s read-only --skip-git-repo-check --ephemeral -C <empty cwd> --json -m <pinned id>`,
  prompt on stdin; `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` removed so the subscription login
  is used. **Canary at run time (before any item is sent, plain mode without `--json`):** a file with a random token
  is placed outside the sandbox cwd; the canary request **instructs the model to attempt to read that path**
  and report the token (the path is sent in pieces of at most 12 characters so the request passes the entropy
  detector). The whole output, stdout and stderr, is scanned. If the token appears, the run **parks (exit 11) and goes to the
  coordinator** (a read-only sandbox may still read the filesystem), and the exposure is then a decision, not a
  default. A non-zero exit parks (exit 24). The plain-mode header line `model: <id>` (printed on stderr) must equal the pin,
  else the run parks (exit 22) (D-147). A negative result is **evidence, not proof** (the model may have declined or
  failed for other reasons); it is logged as such and the section 13 residual stands.
* **Session logs.** The CLIs keep copies of what they were sent. `claude -p` (Claude Code 2.1.299, whose help text
  lists `--safe-mode`, `--setting-sources`, `--no-session-persistence`, `--settings`; `--bare` exists and is not
  used) runs with `--no-session-persistence`, so no transcript is written under `~/.claude/projects` (verified by
  a listing at the end). `codex exec` (Codex 0.161.0, whose help text lists `-s/--sandbox`, `-C/--cd`,
  `--skip-git-repo-check`, `--json`, `-m` and **`--ephemeral`**, "Run without persisting session files to disk")
  runs with `--ephemeral`; **Codex has no `--no-session-persistence`**. The versions and flags above come from the
  coordinator's help-text check on 2026-10-16; `PINS-STATIC.json` records the actual `--help` output at freeze.
  If a listing after a run shows Codex session files were nevertheless created, the coordinator lists the
  directory (this prereg's author never reads `~/.codex`), records retention, and purges what the run created
  (only files created during the run, by path, never by pattern); the listing and purge are in the log.

### 3.3 Label procedure

* **Blinded**: opaque ids `L001...`, no tier, no session id, no set name, no other judge's label. The judge sees only
  the id, the scrubbed digest where one exists and the scrubbed prompt, in the P17D `prompt_view` form.
* **Judge prompt contract (r5, D-147, D-161): the P1.7-d labeller, loaded from pinned files.** `cls_label_run.py`
  loads `gt500.py` (sha256 pinned in PINS-STATIC and equal to `GT500_SHA256`) after verifying its sibling `supply.py`
  (sha256 `12cd3e1e...`, pinned, because gt500's directory goes on the import path), and `rubric.py` (md5
  `1bcb3f44...` and sha256 pinned) through `gt.load_rubric`; a `--prompt-module` that is not the pinned `rubric.py`, a
  `gt500.py` without `prompt_view`, or any hash mismatch exits 9 before anything is sent. The six planted items are
  `rub.PLANTED` (`cal-h1, cal-h2, cal-s1, cal-s2, cal-o1, cal-o2`). Item blocks are `rub.ITEM_TEMPLATE` with the prompt in
  `gt.prompt_view` form, joined by a blank line, then `rub.BATCH_TAIL` (or `rub.SINGLE_TAIL`) verbatim. **The rubric
  is the first block of the stdin payload** (the frozen judge argv has no `--system-prompt` flag, and Codex has none),
  so `outgoing_check` sees it with the items. A batch answer is `{"labels": [{"id", "tier", ...}]}` with an id per label;
  a single-item answer may omit the id and then takes the one id that was asked (the P1.7-d rule,
  `scripts/p17d_synthetic.py:1121`, D-161), an answer that carries an id must carry the asked one; a label counts
  only if `gt.valid_label` accepts it and its tier is haiku, sonnet or opus.
* **Batches of 8**, ordered by `sha256("clsdev:label:20261017:" + item_id)`.
* **Calibration, first call.** The first call of each judge's run is the 6 planted items of `rubric.py`
  (cal-h1/h2, cal-s1/s2, cal-o1/o2) plus the first 2 real items, shuffled with seed 20261017, planted items under
  opaque ids from the same id space. No reroll of a plant id: the first valid label is final.
* **Interleaved plants (r2).** Every 10th batch of a judge's run (and every resumed run's first batch) carries two
  of the 6 plants under **fresh opaque ids** and shuffled positions, rotating through the 6. A wrong plant at any
  point voids that judge's whole run (**NOT INFORMATIVE**; the judge's labels are moved to a quarantine file and
  never joined), not only the first-call check. A calibration call that fails its 3 tries also voids
  (`calibration_call_failed`). This
  covers a rate-limited multi-day run whose model could change silently. The TEST prereg adopts the same rule.
* Required: **all plants correct (6/6 first call; every interleaved plant)** per judge per run.
  **Pre-stated fallback (D-135):** if any judge's run is voided (a failed plant or calibration call, a model family
  or model-id change; **not** a Codex failure or a missing Claude model id, which park, 3.1), truth for the affected
  set becomes the **other two judges, a split resolving to the higher tier**; for
  a voided GPT this is exactly `gpt_excluded` (Opus + Sonnet). The result is **labelled** (`TRUTH_FALLBACK:<judge>`),
  criterion 8 on the voided judge is not evaluable, and the coordinator brings it to the owner. This applies to
  real DEV and to DEV-SYN alike (an Opus or Sonnet void on DEV-SYN falls back to the other two). The fallback is
  fixed here before any label exists; it is not chosen afterwards. **If two judges are voided in the same set, that
  set is NOT INFORMATIVE** (one judge cannot define truth; no screen result is drawn from it).
* A failed batch is retried up to 3 times, then items one at a time (a scrub abort goes straight to single items:
  the same bytes would abort again); an item still unlabelled is a label error, counted. A claude non-zero exit or
  timeout is a retry; every call writes a `pending` row to `calls_<judge>.jsonl` before the child starts and a
  `done` row after it (for Opus also to the shared spend log, a timeout charged at the estimate).
* Resume state: the append-only `labels_<judge>.jsonl`, `plants_<judge>.jsonl`, `calls_<judge>.jsonl`,
  `excluded_<judge>.jsonl` (retroactive independent-scan exclusions, removed from the labels at collection) and
  `quarantine_<judge>.jsonl`, plus `plan_<judge>.json`, `state_<judge>.json` and `status.json` (counts only; one
  run directory is one set). A resumed run re-checks the model id on its first call; a model-id change mid-run
  voids the run. `--max-batches N` pauses a run after N batches (resumable).
* Schemas. `labels_<judge>.jsonl` line: `{"item_id", "judge", "tier", "batch", "call_id", "model_id", "utc",
  "attempt"}`. `plants_<judge>.jsonl` line: `{"plant_id", "opaque_id", "expected", "got", "call_id", "position",
  "interleaved", "utc"}`. Existing Opus/Sonnet labels enter as `labels_opus.jsonl` / `labels_sonnet.jsonl` through
  the `text_sha -> item_id` map of the manifest, with `"attempt": 0` and `"call_id": "legacy"`.
* Spend: Codex and Sonnet $0; Opus per section 1.2. Stop and report at 80 percent of any cap: before an Opus call
  the runner stops (exit 21) when the spent total plus the estimate of the call would pass 80 percent of the
  cap (default `--cap-usd 20`); generator Opus calls write to the same spend log, so the $20 is shared.
* An item without a valid label from a required judge is excluded from every truth rule that needs it, counted.
  If more than 5 percent of items are missing the primary truth: NOT INFORMATIVE.

### 3.4 Truth rules (r2)

| Truth | Definition | Use |
|---|---|---|
| **T-primary** (real DEV and DEV-SYN) | **majority of Opus, GPT, Sonnet; a 3-way split resolves to sonnet** (the TEST rule); needs all three | threshold fitting, criteria 3-7 |
| T-higher2 | Opus + GPT, higher tier on disagreement (the r1 primary) | sensitivity row only |
| T-opus, T-gpt, T-sonnet | single-judge | criterion 8; sensitivity |
| T-agree | the subset where all three agree | sensitivity |
| T-gpt-excluded | Opus + Sonnet, split resolves to the higher tier | the **pre-stated fallback** when GPT is voided (above); otherwise a sensitivity row |

**Per-source truth rule.** The truth rule is supplied **per source set** to the scorer (`--truth-by-source`), so a
source that lacks a judge is never silently mixed: the primary rule is `majority3` for every source; a source
that cannot supply three judges is excluded from primary scoring and counted, and appears only in the
sensitivity rows (T-higher2 etc.).

Reported: weighted (quadratic) kappa and exact agreement for Opus-GPT, Opus-Sonnet, GPT-Sonnet, on real DEV and on
DEV-SYN **separately** (never pooled), with session-bootstrap CIs, and per-length-band (real and DEV-SYN separately)
and per-source tables as point values (a table cell with fewer than 5 pairs prints its n only). `cls_eval.py` also
prints the sensitivity truths `opus_only`, `gpt_only`, `agree_only`, `gpt_excluded` and `higher2`; `sonnet_only`
is available as a truth rule (criterion 8, DEV-SYN leg). The side figure R3 (effort signal)
is reported where it exists as a rank-agreement check across truth bases. There is no human validation (the owner
declined, D-R8-2); this is a stated gap.

## 4. Scoring (identical to the TEST scoring, so DEV numbers predict TEST numbers)

The scorer is `scripts/cls_eval.py` (build B1, brief `brief-cls-b1.txt`), over the cost functions of
`src/llm_router/shadow_eval.py` (sha256 pinned in `PINS-STATIC.json`, by commit; at the r2 base the full hash is `e9b89943f33b68b3095c5d2ea57976147368e0e6f28490aea7e74a82e32b1c57`):

* c(haiku) = 1.0, c(sonnet) = 2.7, c(opus) = 6.15, lambda = 2; `C-lambda2-clamp` = mean of
  c(min(pred, requested)) plus 2 c(truth) when the clamped pred is below truth; under-route is unclamped
  (pred below truth).
* **requested_tier**: the item's own joined tier when it exists. DEV-SYN and unjoined real items take
  **opus** (clamp inert) **[CD]**. This deviates from shadow_eval's imputation (session mode, else sonnet),
  which would clamp unjoined items to sonnet and manufacture a bar the data does not support. The scorer
  also prints the shadow_eval imputation result as a sensitivity row. P17D had requested = opus on 146/151
  items, so the clamp is nearly inert either way.
* Paired session/cluster bootstrap, **B = 2000, seed 20261017 for every bootstrap in this prereg** (admissibility,
  guard, CIs, AUROC, SE), clusters = `group` of section 1.3 (DEV-SYN: the generation call id). The scorer works
  on per-group sufficient sums, so a resample costs O(groups).
* Bars (necessary): under(cand) <= under(R-E) and C-lambda2-clamp(cand) <= C-lambda2-clamp(R-E). Non-
  inferiority: the upper bound of the paired bootstrap difference (cand - R-E) <= +3 points of under-route and
  <= +0.3 C-lambda2. **The non-inferiority bound is the 97.5th percentile of the paired bootstrap, the upper end of the
  reported two-sided 95 percent CI (D-148; index `int(0.975 B) - 1` of the sorted values, the `shadow_eval.boot_diff`
  convention).** NOT-TRIVIAL guard: under(cand) below the uninformed mixture M-q at equal raw cost, **one-sided**: the
  95th percentile of the bootstrap of (under(cand) - expected under of M-q) must be below -1e-9 (so always_opus,
  whose difference is 0 up to float noise, fails). Raw cost is reported with CI and not gated.
* A candidate row is labelled `PASS-STRONG`, `PASS-WEAK` or `FAIL` by `cls_eval.py --mode test` only (bars, non-
  inferiority and guard all hold, and no CI lower bound above 0; STRONG when both CI upper bounds are <= 0).
  **`--mode test` is single-use**: it needs `--single-use` and `--use-log`, refuses a log that already holds a
  `started` line (exit 4), and a single-use run whose validity arms are absent is NOT INFORMATIVE
  (`validity_unavailable`). **`--mode dev` (this prereg) prints `SCREEN-PASS` or `SCREEN-REJECT` only** and every
  output carries `guard_free`, `screening_only` and the label "DEV figures are guard-free and screening only".
* On DEV these are screening quantities (kill criteria 4-6), not verdicts. No DEV result carries a PASS label
  into any claim.

## 5. Candidate triggers

**T-MB (ModernBERT is screened).** C3 is screened only if, after C1 and C2 and C4 are scored under criteria
1-4, **no embedding or logprob arm survives to ranking, or the best of them has real-DEV AUROC(P(opus),
T-primary opus) below 0.75** **[CD]**. "Survives" here means no kill in rows 1-4 only (rows 5-8 are not read); **while
any of C1, C2, C4 still has a pending row among 1-4 and none survives, the trigger answers `wait`, not `fire`** (B0
`trigger-mb`; a latency block not yet measured must not fire a download). "Not enough" in D-129 is thereby fixed in advance. If T-MB does not
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
* Refusal or error (parse failure, timeout, runtime down): the prediction is `None` and is scored as **rules_eff's
  tier** (the shadow_eval fallback arm), counted as `n_fallback`. The same count feeds criterion 2. (A secret
  flag cannot occur at run time: secret items are excluded at the manifest, section 3.2.) **Note:** this
  fallback pulls an erroring candidate toward the bar and helps non-inferiority, which is why errors are
  reported and criterion 2 kills above 5 percent. B1 separately labels a result NOT INFORMATIVE above 10 percent
  errors; the 5 percent kill is the stricter and governs the screen.
* The candidates see item text and digest only. They never see truth, group, source set or another
  candidate's output.

### 6.3 Training and the real-weight rule

Learned heads (C1-C3, and C4's calibrator) are trained on the training folds of pooled DEV with item
`w_pool` weights (the `m` multiplier of section 2.1 applies on top): real items weight 1, DEV-SYN items weight `min(1, n_real / n_syn)` **[CD]** so real items carry
at least 50 percent of the loss. The loss is ordinal and asymmetric: under-route costs as in C-lambda2
(cost(pred) + 2 cost(truth) when pred < truth).

## 7. Threshold fitting

### 7.1 Decision form

For a candidate with probabilities (P_h, P_s, P_o): route **opus** if P_o >= t_hi; else **haiku** if
P_h >= t_lo; else **sonnet** (conservative default). Grid: t_hi in {0.20, 0.25, ..., 0.95} (16 values), t_lo in {0.50,
0.55, ..., 0.95, 0.99, 1.01} (12 values; 1.01 is "no haiku routing", constraint 3), **16 x 12 = 192 points** (D-143; r4
wrote 16 x 11 = 176 and missed the 1.01 column that constraint 3 needs). A hard-verdict arm (nimble, K-B1) has no thresholds and is scored at its verdict.

### 7.2 Constraints (all must hold at a grid point)

**Population.** The constraints below are evaluated on the **pooled weighted set** (real + DEV-SYN, weights of
section 6.3), with the group bootstrap over pooled groups. Criteria 3, 6 and 8 and the AUROC use **real DEV only**.
The pooled-versus-real split is stated per criterion in section 8.

1. **Admissibility screen (a screen, not the design's margin semantics).** A grid point is admissible only if the
   20th percentile of the cluster bootstrap of (under(R-E) - under(cand)) is >= 0, i.e. the candidate beats
   rules in at least 80 percent of resamples. The design wrote "the margin is the 20th percentile of the
   DEV-bootstrap", which is ambiguous; this is a coordinator reading **[CD]** and a screening device only. The TEST
   prereg may use different semantics. The bootstrap uses **the training folds' items only** (the four folds
   the thresholds are being fitted on), B = 2000, seed 20261017; the held-out fold is never in the bootstrap
   that admits a point.
2. opus share of predictions <= the truth-opus share (T-primary), training folds.
3. haiku-route precision (pred haiku and truth haiku, over pred haiku) >= 0.70 with at least 10 haiku routes;
   with fewer than 10, no haiku routing is allowed (t_lo = 1.01).
4. NOT-TRIVIAL guard holds on the training folds (section 4: the one-sided 95th percentile of the same bootstrap
   that gives the constraint 1 percentile, one bootstrap per distinct decision vector).

Objective: minimise the weighted C-lambda2-clamp on the training folds; ties broken by lower opus share, then
lower raw cost, then larger t_hi, then larger t_lo (deterministic). **No admissible grid point on a training
set: the candidate has no operating point there** (feeds criterion 4).

### 7.3 Out-of-sample protocol

Model scores come from the 5-fold session-grouped OOF predictions of section 6. Thresholds are then fitted by a
**second** session-grouped 5-fold over that OOF table: chosen on four folds of OOF scores, applied to the fifth.
The pooled fifth-fold decisions are the **pooled out-of-sample decisions** that criteria 4-8 score. The
**frozen thresholds** handed forward are fitted once on the full pooled OOF table with the model refit on all
DEV. The second-level folds are the same fold ids as section 6.1 (each group keeps its fold). Residual optimism:
the OOF scores depend on hyper-parameters (C chosen inside folds, so to first order none).

**What DEV can and cannot show.** DEV can **reject** a candidate; it cannot **confirm** an effect smaller than
about **12 points of under-route** (real DEV is about 450-500 items in about 50-75 clusters [EST]; at
design effect 2 the minimum detectable effect at 80 percent power is of that order, derived from the design's
section 2.5 table). A survivor is a finalist, not a result. **Real small improvements will be screened out** (the admissibility
screen and the non-inferiority bounds reject a candidate whose gain is real but smaller than DEV can resolve);
that is accepted, because the screen's job is to avoid carrying noise forward, not to find every small gain.

## 8. Ordered kill criteria

Applied in this order, per candidate, frozen before the first DEV score. The first failure kills; later ones
are still computed and reported (never skipped silently). Reference arms are exempt from criteria 1, 3-8 (they
are validity checks) but subject to section 8.1.

| # | Criterion | Population | Kill when |
|---|---|---|---|
| 0 | Data validity | real | the DEV validity floor of section 1.3 fails: no candidate verdict, the screen is NOT INFORMATIVE |
| 1 | Hard fit | real sample | **peak resident memory (process RSS plus Ollama's reported model size) > 12 GiB** (a quarter of this machine's 48 GiB **[CD]**; the machine's RAM is `sysctl -n hw.memsize`, recorded in the log) kills (`memory`); or the runtime/Ollama version needed is unavailable (`runtime`; for C4 no usable `logprobs` field, 2.1) kills. Pooled p95 latency (section 10.1) of **1,500 ms or less passes**; **above 1,500 ms and up to the 15,000 ms async budget (D-160) marks the candidate `async_only`** (no sync authority; it may be a finalist only with that label **[CD]**); **above 15,000 ms kills** (`slow_beyond_async_budget`). The row records which conditions fired. No latency file yet: `pending`. C4 parked for the coordinator (2.1): `pending` |
| 2 | Errors | pooled | parse/error/timeout rate > 5 percent (nimble precedent 4/151) |
| 3 | Signal | real | AUROC(score, truth is opus) < 0.65, or its session-bootstrap 95 percent lower bound < 0.55. For a hard-verdict arm the verdict rank is the score. **Items the candidate could not score (error or fallback) are excluded from this AUROC (D-160)**: B0 runs `cls_eval.py score` a second time on the table without those items; the AUROC with them included is reported beside it |
| 4 | Operating point | pooled out-of-sample | no admissible (t_hi, t_lo) in the second-level fits; **or** the **pooled out-of-sample decisions** fail any of: under(cand) <= under(R-E); C-lambda2-clamp(cand) <= C-lambda2-clamp(R-E); the NOT-TRIVIAL guard passes; the non-inferiority upper 95 percent bounds hold (under <= +3 points, C-lambda2-clamp <= +0.3) |
| 5 | Cost | pooled out-of-sample | **informative, not gating** (D-130 c: raw cost is reported, not gated; r1 wrongly made it a kill). Reported: raw cost with CI and the ratio to R-E raw cost (R-E raw 2.746 on P17D); a ratio above 1.35 prints the flag `COST_HIGH` and goes into the finalist file |
| 6 | Shift | real vs DEV-SYN | **definition (r3):** take the pooled out-of-sample decisions (section 7.3, thresholds fitted on the pooled data) and score separately their **real rows** and their **DEV-SYN rows**. Kill when the candidate is significantly worse than R-E on the real rows (CI lower bound of the paired difference > 0 on either under-route or C-lambda2-clamp) **while** passing criterion 4's four conditions on the DEV-SYN rows: it fits the generator, not the traffic. Report-only under `LOW CLUSTER FIDELITY`, and carries no weight when DEV-SYN is `RESEMBLANCE_FAILED` |
| 7 | Stability | pooled | decisions identical on a repeat run for < 98 percent of items (P1.6-b). Repeat = a second full run with an unchanged config in a different quiet window; `pending` until the repeat exists (B0 `stability` compares two OOF score files at the frozen thresholds) |

**Status vocabulary (r5; B0 `criteria_table`).** Each row is `pass`, `kill`, `pending`, `report_only` (rows 5, and 6 under its
conditions), `async_only` (row 1) or `not_evaluable` / `not_applicable` (row 8). `killed_by` is the first kill in
the order above. A candidate is **eligible** when no row kills and no row is `pending`; a `pending` row is never a
pass. Only eligible candidates enter ranking (9), and only when the DEV validity floor is `OK` and all of the
section 8.1 validity checks ran and passed.
| 8 | Judge robustness | real (and DEV-SYN: T-gpt vs T-sonnet) | two legs. **Real leg:** the sign of (under, C-lambda2-clamp) minus R-E under T-opus differs from the sign under T-gpt on more than 1 of the 2 metrics (T-sonnet is computed beside them and not gated). **DEV-SYN leg:** the same test between T-gpt and T-sonnet on the DEV-SYN rows. **A sign counts only if the paired difference under that truth is at least 1 bootstrap standard error from 0** (a **1-SE magnitude floor**: smaller differences are indeterminate, not discordant). Status per leg: discordant on both metrics `kill`; a leg whose judge (of its pair) is voided is `not_evaluable`; **any other leg that cannot be evaluated is `pending`** and blocks eligibility (F20: DEV-SYN rows present but the candidate's arm absent from the DEV-SYN leg is `pending`, never a pass); a table with no DEV-SYN rows at all is `not_applicable` for that leg. The row passes when no leg kills or is pending (legs `pass`, `not_evaluable`, `not_applicable`); `not_evaluable` passes only because a judge was voided (D-160) |

### 8.1 Validity of the screen (run in the same pass, never decisive for a candidate, decisive for the screen)

* always_opus must fail the NOT-TRIVIAL guard (gate validity). If it passes: the guard is broken, the
  screen is NOT INFORMATIVE.
* Noised oracle must PASS-STRONG on the bars. If it does not: DEV cannot separate, NOT INFORMATIVE.
* Known-better K-B1 (circularity note in section 2.2) must beat known-worse K-W1 on (under, C-lambda2-clamp) with the paired CI of the
  difference excluding 0 on at least one metric and no metric reversed. If not: **no kill based on
  separation** and no finalist is named; the coordinator reports to the owner. **[CD]** DEV has fewer items
  than TEST, so this check is weaker than the TEST one; it is a sanity floor.
* Spread (under, C-lambda2-clamp) of K-B1 minus K-W1 with its CI, and the known-worse trio (always_haiku,
  random, nimble) all failing the bars, are reported.
* **A validity check whose arm is absent is `not_run`, and a screen with any `not_run` check names no finalist**
  (B0 `no_finalist_reason: validity_8_1_failed_or_not_run`); a single-use `--mode test` run treats it as NOT INFORMATIVE
  (section 4). An empty set passes everything, so absence is never a pass. "Known-better separates" is computed
  as K-B1 versus K-W1 with an improvement on at least one metric (CI upper bound below 0) and no metric reversed
  (CI lower bound above 0).

## 9. Ranking and the finalist output

1. Survivors of criteria 0-8 are ranked by pooled out-of-sample C-lambda2-clamp subject to under-route no worse
   than R-E. Keep the top 2, preferring diverse families **[CD]**: one logprob-or-LLM arm and one small learned
   head if both exist among survivors. **Note (CD-13):** the diversity rule can displace the rank-2 candidate by a
   lower-ranked one of another family; the displaced candidate, its rank and its numbers are named in the
   finalist file so the choice is visible. If only one family survives, one finalist. Ties in the objective are
   broken by candidate id (C1 before C2 before C3 before C4). Families: C1, C2, C3 are small learned heads, C4 is the
   logprob/LLM arm. A candidate labelled `async_only` ranks like any other and keeps its label (CD-16).
2. **Arm definitions (frozen).** The L2 tier of a candidate is the section 7.1 threshold rule on its
   probabilities; its confidence is `max(P_haiku, P_sonnet, P_opus)`. Arm A returns the L2 tier. Arm B returns
   the engine tier (E-0) unless confidence >= t_conf, in which case it returns the L2 tier (up or down; the L2
   tier is the candidate's pooled out-of-sample decision). **`t_conf` is an in-sample screen, not a section 7.2/7.3
   fit (D-160).** For each finalist, each of the ten values of the grid {0.50, 0.55, ..., 0.95} gives one arm-B
   decision column, scored by `cls_eval.py score --mode dev` on the arm-choice items below; a value is eligible only
   if arm B holds the guard and the bars, its opus share is <= the truth-opus share, and it makes either no haiku
   route or at least 10 with precision >= 0.70. The chosen value minimises (C-lambda2-clamp, opus share, raw cost),
   ties to the larger `t_conf`. There is no admissibility bootstrap and no out-of-sample refit for `t_conf`
   (B0 does not reimplement `cls_eval` fitting); the finalist file labels the method
   `in-sample screen (no 7.2/7.3)`.
   **The choice between arm A and arm B is made only on items outside the engine's own fitting sets** (real items
   not in GE3 tune or E2-cal, which fitted engine L3 under D-69, plus DEV-SYN); both arms are also reported on the
   engine-fitted items, flagged `engine-seen`, so the bias is visible. The finalist pair (candidate, arm) is
   the better of arm A and arm B by the objective (C-lambda2-clamp, then opus share, then raw cost; a tie or no
   eligible `t_conf` gives arm A). **[CD]** Both arms are then scored
   once on TEST-SYN only for the winning arm of each finalist; the loser is reported from DEV only.
3. **Output** `cls_dev_result.json` (counts, ids and tiers only; no text) and `cls_dev_finalists.json`:
   per finalist: candidate id, family, labels (`async_only`) and `sync_authority`, arm and the arm-choice detail with
   `t_conf_method`, artifact sha256 list, frozen thresholds (t_hi, t_lo, t_conf), runtime
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
* Sample: 200 prompts drawn from real DEV stratified by length band (largest-remainder allocation, ties by band
  order, seed 20261017; the same sample in every block), run sequentially,
  single thread, the model resident (keep-alive set, `ollama ps` confirms only the candidate is resident; every
  candidate, C1 included, has `/api/ps` checked for foreign models). A percentile is the nearest rank
  (`ceil(p n)`-th sorted value); the p95 CI is a bootstrap with B = 2000, seed 20261017. The three blocks are
  summarised only when three valid blocks exist at least 30 minutes apart (`latency --summarize`).
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
candidate under test; no `claude -p` process other than the harness's own children is running (counted by `ps -axo comm,args`,
observed only; never `pkill` by pattern; r2 narrows the r1 list, per D-132 g); the machine is on AC power and
`pmset -g therm` shows no throttling. In addition the P2.0 L13 gate is applied unchanged, since both runs share this machine:
three readings 10 s apart, each with free memory >= 40 percent (`memory_pressure -Q`) and load1 < 4.0, and no
foreign model resident (`/api/ps` lists none besides the candidate's own); the stricter CLS load limits above
still apply. The harness never unloads a foreign model; failure parks the run. The readings are written into the log. An invalid block is discarded,
logged as `invalid:<reason>`, and re-run; the number of discarded blocks is reported.

### 10.3 Exclusion window

No timed block and no heavy-model run (C3 training, C4, K-W1, K-B1, any model above 4 GB resident) **starts
or is running** between **00:00 and 07:00 local machine time** (the same clock as the P2.0 L13 night window,
`PREREG-P20-requal.md` section 13, `NIGHT_WINDOW=00:00-07:00` "local time"; log stamps stay UTC, only the
window test uses local time). P2.0 runs its local arms inside that window, so CLS is excluded from it.

* **Stop rule by measured duration.** Before each unit of heavy work the harness checks the local clock. It does
  not start a unit if `now + 1.5 x (the longest unit duration measured so far in this run, or 10 minutes
  before one exists) >= 23:50` local. It then saves resume state, ends its own loop (a started child is
  terminated by its recorded PID only, never by pattern), logs, and exits parked. Resume after 07:00 local reads
  the state and re-verifies the quiet gate and the artifact shas.
* **CLS unloads its own model.** Before 23:50 local and at **every** stop (including errors), the harness sends
  Ollama a request with `keep_alive: 0` for **its own model only** and confirms by `/api/ps` that it is gone. It
  never unloads a foreign model. P2.0 therefore finds the slot free at 00:00.
* Embedding arms C1 and C2 with the model resident under 1 GB, and CPU-only work on the manifest, are not
  heavy-model runs but still obey the quiet-machine rule when timed.

Consequence stated: the design assumed nightly heavy slots (design 3.4, 4). That is **contradicted** by this
exclusion. Heavy arms (K-B1 about 18 GB, K-W1 about 9 GB, C4 about 6.6 GB) need quiet daytime windows with no
build or test run, which competes with Codex-build verification on the same machine.

## 11. Run order, spend and stop rule

1. Builds B0, B1, B-L merge; freeze-guard review; freeze prereg, spec v1 and `PINS-STATIC.json` (section 15). B2/B3
   may merge later.
2. Attach step and manifest (counts and text, no labels), scrubber self-test, independent scan, Codex canary.
3. DEV-SYN generation and resemblance iteration (max 3 versions, DEV-SYN numbers only); freeze vN; TEST-SYN
   generated once under vN by the separate agent, resemblance checked inside its step, chmod 0000, hashes
   committed (section 0a).
4. Labels: Sonnet on all real DEV (new items) and DEV-SYN; GPT on all real DEV and DEV-SYN (Codex); Opus on
   DEV-SYN. Plants first and interleaved.
5. C1 and C2 (cheap, also the floor), then C4, then K-W1, K-B1, then the T-MB decision, then C3 if triggered.
   One heavy model at a time, outside the night window.
6. Criteria, ranking, finalists file, hashes. Only then does the coordinator release TEST-SYN to the TEST prereg.

Spend: Codex and Sonnet $0. Opus on DEV-SYN about $5.1-5.7 [EST], inside the **D-134 $20 Opus cap, stop at 80
percent ($16)**. OpenRouter only under section 3.1, shared $20 cap (D-128), per-call log, stop at 80 percent.

## 12. Privacy

* Prompt text lives only in: the attach output under `$CLS/dev/` (0600, dirs 0700), the label-run and
  generator input files, the stdin of a judge or generator child after scrubbing, the HTTP body sent to
  localhost Ollama, and the CLI-kept session logs listed in section 3.2 (avoided or purged).
* Nothing is sent that did not pass section 3.2 on the exact bytes. No item with a detector hit is sent.
* Outputs and everything committed to the repo hold ids, counts, tiers and hashes only.
* No write to `~/.llm-router` or `~/.claude`; no read of `~/.codex` by any harness author. The exclusion
  list and `.env` values are read read-only by the attach step and never printed.
* D-130 h: no per-prompt network classifier. GPT is used offline for labels only. OpenRouter under section 3.1 only.
* Disclosed: owner repo and project names, and the sent text itself, go to OpenAI (Codex) and Anthropic.
* Residual: undetected secrets (section 13). Purging Codex session logs stays **coordinator-run**; no build or
  author touches `~/.codex`.

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
* The DEV truth rule now equals the TEST rule (3-judge majority, split to sonnet; D-132 c), so thresholds are not
  fitted under a different rule. Remaining differences: the existing Opus (and GE3 Sonnet) labels on real DEV were
  made on unscrubbed text in earlier batches; DEV-SYN is labelled fresh by all three. The Opus+GPT 'higher' rule
  is a sensitivity row (T-higher2).
* **TEST-SYN custody is procedural.** Same Unix user; the path guard and the file mode keep honest tooling out, not
  a hostile process. Residual stated.
* **Tuning and checking are not independent.** The generator spec may be amended (using pass/fail + AUROC
  numbers) until it passes the same resemblance checks that then certify it. A pass shows the spec was tuned to
  pass, not that the set resembles real traffic in every respect.
* **Undetected secrets.** The detectors and the independent scan are pattern based; a secret in a shape none of them
  knows would be sent. The exclusion, the PII replacement and the scan reduce this; they do not remove it. The
  Codex canary is evidence of read scope, not proof (section 3.2).
* **Forbidden-word filter residual (generator).** The narrow meta-statement filter can still let a generator leak
  difficulty cues in ordinary words, and can reject a few legitimate phrasings; term rates are reported, and the
  bias is not corrected.
* **TEST prereg duty.** The TEST prereg must report a **real-only comparator** and a **per-source breakdown**, so a
  pass on synthetic items cannot hide behavior on real turns.
* **Judge-defined truth** uses three LLM judges from two families; their errors can correlate (the same rubric).
* Thresholds, `n_real_dev` and `n_truth_opus` are not known at writing; the estimates in section 1 are not
  inputs to any rule.

## 14. Coordinator defaults (each answered here; r2 includes the coordinator's CD-12 override and D-132)

| # | Question | Default taken |
|---|---|---|
| CD-1 | Reuse existing Opus labels on real DEV | reuse (no spend); GE3 Sonnet labels reused |
| CD-2 | DEV primary truth | **r2: the TEST rule, majority of 3, split to sonnet** (D-132 c). Opus+GPT higher = sensitivity |
| CD-3 | Items with a detected secret | excluded from DEV entirely (not redacted, not sent) |
| CD-4 | requested_tier for items without a joined tier | opus (clamp inert); shadow_eval imputation reported as sensitivity |
| CD-5 | ModernBERT trigger | fires only if no C1/C2/C4 survivor, or best real-DEV AUROC < 0.75 |
| CD-6 | NVIDIA complexity feature | excluded (licence unread; second download not approved) |
| CD-7 | Real-weight rule | syn weight min(1, n_real/n_syn) |
| CD-8 | Constraint 1 | a screen: beats R-E in >= 80 percent of training-fold cluster resamples (not the design's margin semantics) |
| CD-9 | Haiku precision floor | >= 0.70 with >= 10 haiku routes, else no haiku routing |
| CD-10 | DEV validity floor | n_real >= 300, n_truth_opus >= 40, largest session <= 20 percent |
| CD-11 | Quiet thresholds | P2.0 gate unchanged plus load1 < 2.0 at start, block mean < 1.5, any sample >= 3.0 invalid |
| CD-12 | Clock of the exclusion | **coordinator override: same clock as P2.0 L13, local time 00:00-07:00** (read from PREREG-P20 section 13); heavy stop at 23:50 local |
| CD-13 | Finalist diversity | prefer one logprob/LLM and one small learned head; the displaced candidate is named |
| CD-14 | Arm B scoring on TEST | winning arm only per finalist |
| CD-15 | Latency kill uses | pooled p95 over 3 blocks |
| CD-16 | async-only candidate | may be a finalist, labelled, no sync authority |
| CD-17 | LOW CLUSTER FIDELITY | > 15 percent of real items without a recovered session: criterion 6 report-only |
| CD-18 | Embedding token cap | **r5 (D-169):** C1 and C3 512 tokens; C2 384 whitespace words (approximation, no local tokenizer); digest kept whole, prompt tail kept, for all |
| CD-19 | Peak-RSS limit (criterion 1) | 12 GiB (a quarter of 48 GiB) |
| CD-20 | Opus on DEV-SYN | all 300 items; D-134 cap $20 total, stop at 80 percent |
| CD-21 | Voided judge | truth = the other two judges, split to the higher tier, labelled `TRUTH_FALLBACK` (D-135) |
| CD-22 | `.env` detector | keys matching KEY\|TOKEN\|SECRET\|PASSWORD\|PASS, length >= 8, empty skipped (D-134/135) |
| CD-23 | Real digest | capped at 400 characters |
| CD-24 | PINS-RUN append-only hash chain (each entry carries the previous entry's sha) | accepted by the coordinator |
| CD-25 | Single-turn `claude -p` | no `--max-turns` (not in 2.1.299); an empty `--tools ""` list means no tools, so the call is single-turn print; the pins record this (the recorded `claude -p` help lines and the `--tools ""` argv) |
| CD-26 | Async latency budget (criterion 1) | p95 <= 1,500 ms pass; to 15,000 ms `async_only`; above, kill (D-160) |
| CD-27 | Non-inferiority bound | 97.5th percentile of the paired bootstrap; the guard stays one-sided 95th (D-148) |
| CD-28 | Threshold grid | 16 t_hi x 12 t_lo = 192 points, t_lo = 1.01 included (D-143) |
| CD-29 | Arm B `t_conf` | in-sample screen on the arm-choice items, labelled in the finalist file (D-160) |
| CD-30 | Criterion 8 non-evaluable | `not_evaluable` passes only when a judge of the leg's pair is voided; any other non-evaluable leg is `pending`; DEV-SYN leg pending when the arm is missing (D-160, F20) |
| CD-31 | Judge/Codex plumbing | GPT pinned by `-m`; a Codex failure parks, never voids; single-answer id rule of P1.7-d; `supply.py` and `gt500.py` pinned (D-147, D-161) |

## 15. Freeze record

Not frozen. To freeze: the coordinator commissions an independent freeze-guard review (not the author, without
the conversation). On READY write `PREREG-CLS-DEV.sha256` as `<sha256>  PREREG-CLS-DEV.md`, then
`frozen_utc <UTC stamp from date -u>`, then **`pins_sha256 <sha256 of PINS-STATIC.json>`**;
`scripts/check_prereg.py` must report a match and `cls_pins.py --verify` must report every STATIC pin matched.

**Two pin files (r4).** Only STATIC pins are freeze blockers.

**`PINS-STATIC.json`** (built by the B-L `pins --static` subcommand, hashed, committed with the freeze) holds what
exists at the freeze: the sha256 of `scripts/cls_eval.py`, `cls_dev_run.py`, `cls_dev_lib.py`, `cls_label_run.py`,
`cls_gen_run.py`, `cls_resemble.py`, `cls_send_lib.py`, `cls_pins.py`; the engine commit sha and the sha256 of
`src/llm_router/engine.py` and the **full** sha256 of `src/llm_router/shadow_eval.py`, **both pinned by commit**
(the hashed content is `git show <commit>:path`, not the working tree; at the r2 base the shadow_eval hash is
`e9b89943f33b68b3095c5d2ea57976147368e0e6f28490aea7e74a82e32b1c57`); the rubric md5
(`1bcb3f441a0c9d056b3343a9d47c5565`) and the sha256 of `rubric.py`; the sha256 of `gt500.py`
(`9cde774dfe93387d6ee3dfcbd1b5481a537f265e5961d82f85a87d29d6328959`, the `GT500_SHA256` of `cls_pins.py` and of
`scripts/p17d_synthetic.py`) and of its sibling `supply.py`
(`12cd3e1eed7fc15658b4b44246d83422e2b61adc4ccb270dd9f5e729608fb0e8`, `SUPPLY_SHA256`, loaded and verified first as
`p17d_synthetic.py` does; D-161), each at the path `params.gt500_py` names (the real `~/.rsi/research/primary-plan/v16/gt500/scripts/` of
the P17D labeller), so a changed helper blocks the freeze; the exclusion-list and `.env` **paths** with
sha256 and qualifying-value count (exclusion list `~/.llm-router/denylist.txt`, the path the P17D attach used; `.env`
`~/.llm-router/.env`, hashed by the coordinator at the real pins build and never a hard pin; the `.env` sha256 is as of the freeze, a later change is intentional, no
snapshot is kept, and the sha256 is logged at each use); the Ollama version and digests of `nimble:9b`,
`qwen3.5:latest`, `qwen3-coder:30b`, `nomic-embed-text`; the bge-small snapshot dir name; the `claude` and `codex`
CLI versions and the **recorded `--help` output** (sha256 and every flag line the runners use: for `claude -p`
`--safe-mode`, `--settings`, `--tools`, `--strict-mcp-config`, `--no-session-persistence`, `--setting-sources`,
`--output-format`, `--model`; for `codex exec` `-s`, `-C`, `--skip-git-repo-check`, `--ephemeral`, `--json`, `-m`; a flag counts
only when a help line **defines** it, not when another option's text names it), so a flag
the installed build lacks **blocks the freeze**; the exact `argv_template` of each CLI (the argv of 3.2) and the session-log directories
(`~/.claude/projects`, `~/.codex/sessions`, as strings, never read by a build); the model ids used for judging
and generating, as five keys (`judge_gpt`, `judge_sonnet`, `judge_opus`, `gen_gpt`, `gen_opus`; a missing key blocks the freeze;
values (D-169): `judge_gpt` = `gen_gpt` = `gpt-6-astra` (D-118, confirmed by the coordinator's `codex exec -m` probe before the
freeze), `judge_sonnet` = `claude-sonnet-5-5`, `judge_opus` = `gen_opus` = `claude-opus-5-5`); and the **v1**
generator files (templates, 12 x 5 mask, project list, intent phrases, meta patterns; spec appendices A-D, hashed
as files; committed in `docs/prereg/cls-syn-gen/`, which is the `--gen-dir` passed to `cls_pins.py` and `cls_gen_run.py`).
Generator files are versioned by name (e.g. `tmpl_gpt.v1.txt`, `tmpl_gpt.v2.txt`). The v1 files are never edited. An amendment creates new-named files and a PINS-RUN entry for them. A STATIC pin that cannot be computed is a freeze blocker, not an omission:
`pins --static` exits 9 and names the keys it could not compute. A generator template file holds the Appendix C body with the
marker `{{ITEMS}}` where the item blocks go (`cls_gen_run.py`; the spec author supplies the files and the tool does not invent
their wording).

**Building and checking (r5).** `PINS-STATIC.json` records absolute paths in `params` (repo, scripts directory, helper files,
Ollama URL, snapshot and generator directories) and `pins --verify` recomputes every pin from them, so it is built and verified from
the checkout that holds the frozen commit, with `--engine-commit` set to a commit that contains the frozen engine files (their hashes
are taken by `git show`, not from the working tree). `pins --verify PINS-STATIC.json` prints `{"ok", "mismatches", "warnings",
"env_sha_at_use", "run_entries"}` and exits 9 on any mismatch (names only). Before the freeze record exists it warns
`pre_freeze_no_pins_sha256_record`; once it exists, the `pins_sha256` line must match the file's sha256 or the check fails
closed. A changed `.env` sha256 is the warning `env_changed_since_freeze`, never a mismatch. `pins --freeze-line PINS-STATIC.json`
prints the `pins_sha256 <sha256>` line for the freeze record. Ollama digests are the ones `/api/tags` lists for the names
above, with a bare name looked up as `name:latest` (Ollama semantics; `nomic-embed-text` -> `nomic-embed-text:latest`). A name
that `/api/tags` lists more than once is pinned as the **sorted set of every digest listed under it**, never the last one, and
verify fails when that set changes. At r5 this holds for `qwen3.5:latest` (`2e16a80fe3d7...`, `44a38603922e...`; one manifest
file on disk); C4 runs that name as Ollama resolves it (D-169).

**`PINS-RUN` addenda** (built by `pins --run`, each entry hashed and appended to `pins_run.jsonl` and logged
**before its first use**; not freeze blockers, because their inputs do not exist at the freeze): the length
counts file; `gen_config.json` (counts, seeds, shares, spec version); `rules_tier.jsonl` (sha256, schema version,
provenance: resolver commit and resolution time, read from its header line; schema in the B0 brief); the manifest sha256 (one
entry, `cls_dev_run.py manifest`, that also holds `length_counts.json` and `shingles_real.json`); and the generator
files of the frozen spec vN. **A spec amendment (v2 or v3) writes a new PINS-RUN entry** for its generator files
and `gen_config.json`; entries are never overwritten. `pins --verify` checks **STATIC always** and **RUN once any
RUN entry exists**; every runner calls it at start and refuses (exit 9) on a mismatch, naming the key.

Items still open at the freeze must be answered in section 14 or stated as parked.
