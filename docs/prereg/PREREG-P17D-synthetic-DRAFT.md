# PREREG-P17D-synthetic: nimble:9b vs rules_eff on replayed past prompts (PLAN v16 P1.7-d, D-106)

Status: **DRAFT, not frozen.** Written 2026-10-15T13:13Z, before any shadow row, any nimble call, any
judge call and any label for this run. To freeze it, resolve "Open before freeze", delete that
section and this status paragraph, rename the file to `PREREG-P17D-synthetic.md`, and write
`PREREG-P17D-synthetic.sha256` (`<sha256>  PREREG-P17D-synthetic.md` and `frozen_utc <UTC>`, the format
`scripts/check_prereg.py` reads). Then log the hash in `PROGRESS.md`. A changed byte is a new prereg
with a later `frozen_utc`.

## Authority

* **D-106** (owner, 2026-10-15T12:53:01Z): P1.7-d runs synthetically under D-99. Past prompts are
  replayed through nimble:9b in shadow. This prereg is frozen before the first shadow row. Labels
  come from the Opus-rubric labeller, with its 20/day cap lifted for this run only. The hard spend
  cap is $10.
* **D-99**: every number from this run is labelled **`synthetic (D-99)`** and is never mixed into
  organic counts. D-42's organic-only clause for P1.7-d is overridden for G1 only.
* The decision rule is P1.7-d (PLAN-v16 MUST table), unchanged. The verdict-population rules are
  AMEND4-shadowlab (sha256 `5724370e…`), applied as §7 states. A PASS does not switch routing: the
  classifier stays in shadow (D-72, D-100). Switching L2 on is a 16.1 decision.

## 1. Question

On the same past human-typed Claude Code prompts, does nimble:9b's tier give an M1-12 clamp-aware
cost **and** an under-route rate that are each no worse than rules_eff's? Truth is the Opus rubric
label.

## 2. Population

**Frame.** `$PP/v16/door_corpus.jsonl`: n = 2,658 lines, sha256 `3f5d96ee…b7f4`, frozen 2026-10-09,
mode 0600. Its dates sidecar is `door_corpus_dates.jsonl` (sha256 `e68112f9…`). A corpus line has
only `{i, text}`. It has **no session id, no timestamp, no context, no source and no requested
tier**. So the session unit and turn-first status cannot come from the corpus. They come from the
owner-run attach step below.

**Attach (owner-run, the only step that reads `~/.claude/projects` and `~/.llm-router`, read-only).**
* It enumerates top-level `~/.claude/projects/*/*.jsonl` records. It uses the frozen GE3 record
  rules: `gt500.py` sha256 `9cde774d…` and `supply.py` sha256 `12cd3e1e…` (PREREG-GT §3). Those
  rules skip `isSidechain` records, skip `<session>/subagents/`, require human-typed records
  (promptSource and origin rules), and drop tool-result-only records.
* A record is a candidate when its stripped text equals a corpus line exactly. That is the
  sha256-of-text join `door_agreement.freeze_dates` used. If a text matches several records, the
  earliest is used.
* A human-typed prompt is one turn-first call in proxy terms, which makes it the **turn-first**
  unit. D-59's step_class defect concerns proxy labels and does not apply to this run.

**Eligibility.** Each exclusion is counted. A record is excluded when any of these holds:
* its record `timestamp` is at or after **2026-10-08T20:46:00Z**, the first live
  `classifier_shadow` row (AMEND4). This keeps every item free of any nimble verdict made before
  this freeze. It also keeps the PREREG-GT-amend4 fresh window (T0 2026-10-12) and the G0-REPLAY
  sessions (amend5) out;
* `session_kind` is not `organic` (`session_kind.classify_with_basis`). Research sessions are out, as
  in PREREG-shadowlab (repo) and AMEND4 2(b);
* the session is in `exec_sessions.txt` (sha256 `b47e88f9…`);
* it matches supply.py `HARNESS`, a leading `/`, an image, empty text, the denylist, or
  `supply.secret_hit` on the prompt;
* `secret_scrubber.SECRET_PATTERNS` matches the prompt or the context. This is the AMEND1 B
  sidecar guard. A context hit under `supply.secret_hit` only replaces the context with
  `WITHHELD`;
* it is a short text beyond 2 copies, an exact duplicate, or a near duplicate within the frame
  (the supply.py rules);
* it is a leakage item (§3);
* the served model of the turn is Haiku-class (AMEND4 2(a)). Expected: 0, because the Haiku arm
  went live 2026-10-09.

**Per item, attach stores** (in `items.jsonl`, 0600):
* the opaque `item_id`;
* `session_id` (the transcript `sessionId`), the original `ts` and `cwd`;
* the full prompt, capped at 20,000 characters as head and tail (AMEND1 B `PROMPT_STORE_CAP`);
* the classifier-input pieces `cls_input.assemble` reads (`cls_input.py:62-108`): up to 3 earlier
  human prompts of 300 characters, the assistant's last text (tail of up to 501 characters) and
  `capped`. These come from the main-thread messages before the prompt;
* `requested` (§4) and `requested_basis`;
* the served tier, and the strata fields (§7).

**Session unit** (used for the 60% cap and the bootstrap) is the transcript `sessionId`.

**Selection** (`SELECT_SEED = 20261015`).
* Order eligible items by `sha256("p17d:select:20261015:" + item_id)`.
* Walk that order. Accept an item if its session's count + 1 ≤ `max(2, floor(0.50 × (N + 1)))`,
  where N is the accepted total. Repeat the walk until a walk accepts nothing or N = `N_SELECT`
  = 150.
* The 0.50 share (stricter than 0.60) leaves headroom for attrition.
* If the pool is 150 or fewer, every item the cap allows is taken.

**Expected attrition**, estimated before any attach run from counts only (see "What was looked at"):

| Stage | Expected |
|---|---|
| Corpus lines passing the text rules plus leakage | 1,680 of 2,658. Most are not human-typed: 1,045 of the 1,680 are over 150 words, against 27 of 489 in GE3's human-typed supply |
| Organic human-typed Claude Code items after leakage and the window cut | **about 25-150, central about 70-90**. This is supply organic 366 (through 2026-10-08T10:00Z), minus 288 GE3 organic items, minus codex, plus the 2026-10-08 10:00-20:46Z prompts. The pool is concentrated in the 3 sessions GE3's 35-item cap trimmed |
| nimble errors after one retry | about 2-5%. M1.9 held-out: 2/89 invalid, 0 slow, warm p95 617 ms |
| Invalid labels after retries | about 0-2%. GE3: 0 of 342 missing |

**Consequence, stated first: on this frame the most likely result is NOT INFORMATIVE by supply.**
The attach count decides. **Feasibility gate (before any nimble or judge call, $0):** if fewer than
100 items are selected, or the largest selected session holds more than 0.55 of them, the run
stops. It writes a NOT INFORMATIVE gate file ("insufficient supply") and consumes the use (§10).

## 3. Leakage: excluded items, and what nimble and the rules were tuned on

**What nimble was tuned on.** nimble:9b is a vendor model (Bespoke Labs, fine-tuned from
Qwen3.5-9B). It was not fine-tuned on this owner's data (PREREG-v2-amend2 §2). Its router-specific
parts are:
* the `tier` question text and criteria, which are V7_SYSTEM (md5 `a19cedef…`) split into
  instructions and per-tier criteria (amend2 line 38; `decision_classifier.py:39-53`). v7 was
  written and selected on **E2-cal (91) + E1-tune (30)** (PREREG-v2-amend1, Items row);
* `tau_abs`, fitted on **E2-cal** only (amend2 §3.2; the result is 0.00, so it never abstains);
* selection on E2-cal (round 3, `tune_round3_20261007T214922Z.json`);
* the one held-out run on **E2-HO (89) + E1-HO (20)** (amend5, `heldout_M19_20261008T123204Z.json`).

**What the rules were tuned on.** `classify_signals(GATEWAY_POLICY)` heuristics plus the YAML
`route` table. No fitted parameters were found:
* `git log` of `classify.py` and `claude_tiers.yaml` since 2026-09-01 shows wiring and feature
  flags, no fit;
* `complexity_knn: false` in the bundled YAML.

Two neighbours are not arms here. The engine (P1.6) was tuned on **GE3 tune**
(`scripts/engine_policy_tune.py`, D-69). GE3 held-out was scored once for P1.6-g
(`heldout_p16g/heldout_use.log`).

**Excluded leakage set.** An item is excluded if either test holds:
* its `(session_id, timestamp)` equals a member's;
* its `supply.norm` text equals a member's, or, for texts over 5 words, its 5-character-shingle
  Jaccard with a member is 0.8 or more (the supply.py near-duplicate rule).

| Set | n | Source (pin) |
|---|---|---|
| GE3 tune | 108 | `gt500/items.jsonl` sha256 `c087c5b4…` + `split.json` sha256 `8921a154…` |
| GE3 held-out | 234 | same |
| E2-cal (p_eval calibration, v7 and nimble tune) | 91 | `complexity-v2/p_eval/work/sample.jsonl` sha256 `0e0318e8…`; population.jsonl sha256 `7a70e529…` |
| E2-HO (p_eval held-out, M1.9) | 89 | same |
| E1-tune (v7 tune) | 30 | `local-classifier/tasks_local.json` sha256 `128ed768…` |
| E1-HO (M1.9) | 20 | same |

Census on the corpus text, counts only: 497 of 2,658 lines hit the set. By set: GE3 held-out 221,
GE3 tune 163, E2-cal 102, E2-HO 89, E1 0. A line can hit more than one set.

Stated limit: the exclusion is per item, not per session. A kept item from a GE3 session can carry
up to 300 characters of an excluded earlier prompt in its context. Its label and both verdicts are
still new.

## 4. Arms (both on every selected item, paired by construction)

Everything runs in a scratch `HOME`/`LLM_ROUTER_HOME` (the `g1_synthetic_evidence.scratch_env`
pattern). The repo is pinned at the freeze SHA (draft: `64e3c84c`).

**Request body.** The harness rebuilds a minimal body from the stored pieces:
* `system`: `"Primary working directory: <cwd>"`;
* `messages`: the earlier prompts, then the assistant tail, then the full prompt;
* one custom client tool stub;
* `model`: the requested model id.

It asserts that `cls_input.assemble(min_body)` equals the attach-time assemble of the full history
(context and prompt). A mismatch is the error `assemble_mismatch`.
`text_sha = prompt_key.key(newest_human_text(body))`, which is the live key.

**nimble (the `llm` arm).**
* **Invocation.** The production seam: `ShadowScheduler(path=$RUN/classifier_shadow.jsonl)`
  `.maybe_schedule(body, row)`, then `drain()`, one item at a time. It runs through
  `local_classifier.classify_async` (`llm_shadow.py:104-180`).
* **Environment.** `LLM_ROUTER_LOCAL_CLASSIFIER=shadow`, `LLM_ROUTER_CLASSIFIER_BACKEND=systemone`,
  `LLM_ROUTER_DECISION_MODEL=nimble:9b`, `LLM_ROUTER_DECISION_ABSTAIN_BELOW` unset (0.0),
  `LLM_ROUTER_CLASSIFIER_KEEP_ALIVE=10m` (D-45), `LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS=2000`
  (D-4 default, `local_classifier.py:50`), text sidecar off.
* **Record fields.** The row gives `session_kind=organic`, `step_class=turn_first`,
  `tier_proposed` (the rules tier), `tier` = the served tier, `requested_model`, and
  `ts = time.time()` at replay. So every row is dated after the freeze, and `check_prereg.py
  --shadow-log` can count them.
* **Warm model.** Before the first item the harness runs these checks:
  * nimble digest: `ollama show nimble:9b --modelfile` names blob `sha256-bbf1d6fc…`, size
    9,527,501,312 B (amend2). A mismatch stops the run and is not a result;
  * a quiet gate (amend5 §6.2: free memory at least 40%, load1 under 4; foreign resident models are
    listed, never unloaded). If it fails, the run is PARKED with no row written;
  * one discarded synthetic warm-up call.
* **Errors.** An error is a final `source` that is not `llm` or `cache` (`shadow_eval.py:52`):
  `timeout`, `parse_error`, `cold`, or `abstain`. A `cold` triggers a wait of up to 120 s for
  `/api/ps` to show the model resident. A `timeout` waits out the 30 s cooldown
  (`local_classifier.py:58`) so it cannot cascade. Each is then **retried once**. Still failing is
  an error.

**rules_eff.**
* `rules.tier` = `ClaudeTierPolicy.load(<policy>).tier_for(*backends._classify_class(tier_text(body),
  capabilities=False))`. This is the policy table's own answer, `proposed = target.name`
  (`tiers.py:1031-1032`; `backends.py:463-479`; `tiers.py:725-728`). `<policy>` is the bundled
  `claude_tiers.yaml` (sha256 `a5028010…`) unless the owner supplies the live file (Open question 3).
* rules_eff = `shadow_eval.rules_eff` (`shadow_eval.py:69-73`): Haiku is lifted to Sonnet. No row
  was served on Haiku, so this equals the PREREGISTRATION.v2 §1.4 definition (AMEND4 §1-2(a)).
* It is computed for every item, including first prompts and pinned requests. Live, those leave
  `proposed_tier` None and the scorer skips them.

**Requested tier (the clamp).**
* In the proxy era (from 2026-09-28T14:41Z) it is the proxy ledger `requested_model`, joined to
  the turn's first main-thread assistant `msg_id`.
* Otherwise it is the transcript `message.model` of that record. Before the proxy existed, served
  equalled requested.
* An item with neither takes the scorer's imputation: the session mode, else Sonnet
  (`shadow_eval.py:177-179`). `n_joined` is reported.

## 5. Metrics (`src/llm_router/shadow_eval.py` at the freeze SHA, sha256 `e9b89943…`)

* Cost: c(haiku) = 1.0, c(sonnet) = 2.7, c(opus) = 6.15 (`:45`), λ = 2 (`:47`).
  `cost_eff(p, t) = c(p) + λ·c(t)` if rank(p) < rank(t), else c(p) (`:117-118`).
* **M1-12 = C-λ2-clamp** = mean of `cost_eff(clamp(pred, requested), truth)` (`:121-122`, `:213`).
* **Under-route** = the share of rows with rank(pred) < rank(truth), with pred **unclamped**
  (`:214`). nimble's `local` counts as haiku (`:61-62`).
* Paired session bootstrap of the differences (llm − rules_eff), for both C-λ2-clamp and
  under-route:
  * `shadow_eval.boot_diff` (`:139-156`) resamples whole sessions;
  * **B = 2,000, seed = 20261003** (`BOOT_B`, `BOOT_SEED`, `:50-51`; AMEND4 pins);
  * the cost bootstrap is the one `score()` computes. The harness calls `boot_diff` again for
    under-route with the same B and seed.

## 6. Labels

**Labeller.** The labeller is the Opus rubric through shadowlab's judge semantics (PREREG-shadowlab
§5 + AMEND1-3). It reuses:
* the call `claude -p --no-session-persistence --model opus --setting-sources project,local
  --strict-mcp-config --output-format json --max-turns 1 --tools ""` with the rubric as system
  prompt (`rubric.py` md5 `1bcb3f44…`). `ANTHROPIC_BASE_URL` is dropped, and the cwd is
  `$RUN/claude_cwd`;
* a `modelUsage` family check on every call. A non-Opus model discards the output and stops the
  run (exit 4);
* blindness: the judge sees only an opaque id, `scrub(context)` and `gt.prompt_view(scrub(prompt))`
  (AMEND1 A, C). It never sees a tier, the session id, or any field of a record.

The helpers are imported from `gt500.py` (`9cde774d…`, amend6: judge helpers unchanged) and
`shadowlab.py` (sha256 `b89c7e17…`). `shadowlab.run` is **not** used: it reads the live sidecar,
enforces the daily caps, and today refuses because its pin is the pre-amend6 gt500 sha
(`work/launchd.err.log`).

**Batching (draft default, Open question 1)** follows PREREG-GT §5:
* 8 items per call (`ITEM_TEMPLATE` × 8 + `BATCH_TAIL`);
* order by `sha256("p17d:label:20261015:" + item_id)`, with opaque ids L001… assigned in that
  order;
* a failed batch is retried up to 3 times, then its items go one at a time (`SINGLE_TAIL`). An item
  still unlabelled is a label error.

**Calibration.** The **first call is the 6 planted items** of `rubric.py` (cal-h1/h2, cal-s1/s2,
cal-o1/o2) plus 2 real items. Opus is trusted only at **6/6**. Otherwise the run stops at once and
is NOT INFORMATIVE.

**Every selected item is labelled**, including nimble-error items, for the §7 sensitivity analysis.
`labels.jsonl` (0600) holds `{text_sha, session_id, truth}`, which is what `shadow_eval.load_labels`
reads.

## 7. Analysis and verdict

**Verdict rows (AMEND4 §2):**
* `n` = labelled rows with nimble `source` in {`llm`, `cache`}. Error rows enter the two means as
  fallback (llm = rules_eff), are counted in `n_fallback`, and do not count toward n. The sign of
  the difference is the same either way;
* one row per `(session_id, text_sha)`;
* largest-session share is computed over the n verdict rows.

**Decision rule (P1.7-d verbatim; the point-estimate reading of PREREG-shadowlab, repo, frozen
2026-10-11).** **PASS (synthetic, D-99)** only if all of these hold:
1. n ≥ 100;
2. the largest session holds ≤ 60% of the n rows;
3. C-λ2-clamp(llm) ≤ C-λ2-clamp(rules_eff);
4. under(llm) ≤ under(rules_eff);
5. P1.7-c passed (PLAN P1.7 task 3, #327).

Conditions 3 and 4 use point estimates. The paired session-bootstrap CIs are printed and do not
gate (statistics rule 9). **FAIL** if 1, 2 and 5 hold and 3 or 4 does not.

**NOT INFORMATIVE** in any of these cases:
* n < 100, including when the spend cap stops labelling;
* the largest session holds more than 60% of the rows;
* errors (nimble errors + label errors) exceed **10% of selected items**;
* labeller calibration is below 6/6;
* the feasibility gate (§2) fails;
* any `classifier_shadow` row is dated before `frozen_utc`.

**Reported, never decisive:**
* the verdict with error items excluded from both arms;
* per-stratum C-λ2-clamp and under-route for both arms, with n. Strata: length band (w≤12,
  13-50, 51-150, >150), truth tier, first-in-session, requested tier, era (before or after
  2026-09-28T14:41Z), and the top-3 session counts;
* raw cost, exact, HP/HR (`shadow_eval.score`), `n_joined`, `n_capped`, nimble latency p50/p95, and
  error counts by source.

**Design validity check (never decisive).**
* Reference arms `always_opus` and `always_sonnet` are scored on the same labels. The spread
  under(always_sonnet) − under(always_opus) is the truth-opus share. If fewer than 10 rows are
  truth-opus, the report leads with "under-route cannot discriminate".
* The report says so when nimble beats rules_eff only through a higher opus share.

## 8. Run rule, spend and stop

* **Order:**
  1. freeze;
  2. `select`;
  3. feasibility gate;
  4. `shadow` (every nimble and rules row);
  5. `label` (calibration first);
  6. `score`.
* `label` refuses to start until `shadow` has finished. `shadow` refuses if `labels.jsonl` exists.
  So no label is made before every verdict exists, and the labeller never opens the record file.
* **Calls:**
  * nimble (local, $0): 1 warm-up + up to 150 items + at most 10% retries, so at most 166;
  * Opus at batch 8: ceil(156/8) = **20 calls** plus retries, about 20-24.
* **Cost estimate:** GE3 measured $6.68 / 45 batch calls = $0.149 per call ($0.019 per item). That
  gives **about $3.0-3.6** expected.
  The single-call alternative uses shadowlab's own measurement: $0.080 mean, $0.095 max, n = 7
  (`shadowlab/work/ledger.jsonl`). Its 6 + 110 calls come to about $9.3.
* **Spend guard.** The guard sums `total_cost_usd` in `$RUN/ledger.jsonl`. Before each call it
  estimates the next call at 1.25 × the most expensive call so far (first call: $0.30). It stops
  the run if spent + estimate would exceed **$10.00** (D-106). Labels already written are kept. No
  further tranche is run without a new prereg.
* **Quota pause.** Before every call the guard reads `usage.json`. It pauses (exit 3, resumable)
  at session ≥ 80%, at weekly ≥ 85%, or when the file is missing or more than 30 minutes old
  (PREREG-shadowlab §6).
* **Daily caps.** For this run only, shadowlab's 20 calls / 400,000 tokens per day are replaced by
  the $10 run cap (D-106).

## 9. Privacy

* Prompt text lives only in these places:
  * `$PP/v16/p17d/items.jsonl` (0600; directory 0700);
  * the argv of the judge child;
  * `$PP/v16/p17d/judgements.jsonl` (0600, judge reasons);
  * the HTTP body sent to `localhost:11434`.
* `classifier_shadow.jsonl`, `labels.jsonl` and `ledger.jsonl` hold no text.
* No subcommand prints text. Stdout carries counts and ids only.
* The repo receives only `docs/measurements/p17d-synthetic.json`. It holds item ids, session
  indices (not uuids), tiers, sources and the verdict.
* No write to `~/.llm-router` or `~/.claude`. Only attach reads them.

## 10. Single use

* `score` first appends `started <UTC> prereg_sha256=<…> git_sha=<…>` to `$PP/v16/p17d/use.log`
  (0600, append-only), **before it opens `labels.jsonl`**. It refuses if the log already holds a
  `started` line.
* A crash, a NOT INFORMATIVE result and a feasibility stop each consume the use.
* No re-selection, re-seed, re-label or re-score happens without a new prereg written first.

## 11. Known biases (stated before any data)

1. **Synthetic is not organic.** These are past prompts replayed warm and uncontended, with no
   live latency or GIL contention, no proxy row, and rules computed without pin, first-call,
   stickiness or quota effects. A PASS here is evidence for G1 under D-99, not a live result.
2. **Rubric labels are not outcome-validated.** GE3 κ(rubric, outcome) = 0.096
   [−0.004, 0.216], n = 100 (`gt500/report.json`). Every number says "Opus-rubric truth".
3. **Construct alignment favours nimble.** Its criteria (v7) were written in the same p_eval rubric
   world the labels come from. The rules are keyword heuristics.
4. **The pool is not representative.** After leakage exclusion the remaining items are mostly the
   ones GE3 did not sample: over-cap sessions and the last day. Few clusters make the bootstrap
   wide.
5. **Error rows** are excluded from n, which is selection on nimble behaviour. The sensitivity row
   shows its effect.
6. **Label context.** The judge sees the assemble context (300/500 characters, AMEND1), not GE3's
   context (600/1500). In batch mode it differs from live shadowlab's one-turn calls.
7. **Requested-tier basis** varies by era (ledger join vs `message.model`). An imputed clamp moves
   both arms.
8. **Code version.** The prompts are from August to October 2026. The rules and the scorer are
   today's code at the pinned SHA.

## What was looked at before writing (counts only)

* The census above (a counts-only script kept outside the repo). It applied the supply.py rules and the leakage
  rule to the corpus text and printed counts only.
* The GE3 `supply.json` aggregates, `heldout_M19` latency and fallback, GE3 and shadowlab spend
  ledgers, and the shadowlab launchd log.
* No label, no verdict, no rules tier and no model output was read or computed for any corpus item.

## Open before freeze (owner)

1. **Label mode.**
   * **Batch 8 + planted calibration (draft default):** about $3.3 and n up to 150. It departs from
     shadowlab's one-turn-per-call.
   * **Single call (D-106's literal "shadowlab.py"):** about $9.3. N_SELECT is capped at 110,
     margin to $10 is about $0.7, and a cost overrun means NOT INFORMATIVE.
2. **Supply.** If attach yields fewer than about 106 items, the options are:
   * admit GE3 held-out, which was never used to tune nimble or the rules, relabelled fresh
     (recommended);
   * admit research sessions;
   * accept NOT INFORMATIVE.
3. **Rules policy.** Is the live proxy's tier policy the bundled `claude_tiers.yaml` (`route` table,
   `complexity_knn`)? `llm-router doctor` reports it.
4. **Attach.** It reads `~/.claude/projects` and the proxy ledger, owner-run, as GE3 extract did.
   Without it the frame has no session or context, and the run cannot meet P1.7-d.

```pins
LABEL=synthetic (D-99)
FRAME_SHA256=3f5d96eee34ed72b90f62ab462b8f452b6ca2d465c0beb6158dcb94b4a85b7f4
WINDOW_END_UTC=2026-10-08T20:46:00Z
SESSION_KIND=organic
SELECT_SEED=20261015
N_SELECT=150
SELECT_MAX_SHARE=0.50
FEASIBILITY_MIN_SELECTED=100
FEASIBILITY_MAX_SHARE=0.55
GT500_SHA256=9cde774dfe93387d6ee3dfcbd1b5481a537f265e5961d82f85a87d29d6328959
SUPPLY_SHA256=12cd3e1eed7fc15658b4b44246d83422e2b61adc4ccb270dd9f5e729608fb0e8
SHADOWLAB_SHA256=b89c7e17b4f6746c5b42fe1c136dc48122388ca0d391b319fc68facc5e96098a
RUBRIC_MD5=1bcb3f441a0c9d056b3343a9d47c5565
SHADOW_EVAL_SHA256=e9b89943f33b68b3095c5d2ea57976147368e0e6f28490aea7e74a82e32b1c57
NIMBLE_BLOB=sha256-bbf1d6fc03bb0ed24d88f4c214ed7b5d1768aeb43d5cf433fb69eff0c8578013
NIMBLE_TIMEOUT_MS=2000
NIMBLE_KEEP_ALIVE=10m
NIMBLE_RETRIES=1
LABEL_BATCH=8
LABEL_SEED_FORMAT=p17d:label:20261015:{item_id}
CALIBRATION_REQUIRED=6/6
SPEND_CAP_USD=10.00
BOOTSTRAP_B=2000
BOOTSTRAP_SEED=20261003
MIN_N=100
MAX_SESSION_SHARE=0.60
MAX_ERROR_SHARE=0.10
COST=haiku:1.0,sonnet:2.7,opus:6.15
LAMBDA=2
```
