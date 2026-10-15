# PREREG-P17D-synthetic: nimble:9b vs rules_eff on replayed past prompts (PLAN v16 P1.7-d, D-106)

Status: this file is frozen only when `PREREG-P17D-synthetic.sha256` exists beside it and
`scripts/check_prereg.py` reports a match; until then it is a draft. Changing any byte changes the
hash; a changed file is a new prereg with a later `frozen_utc`. The freeze record is the last section.

## Authority

* **D-106** (owner, 2026-10-15T12:53:01Z): P1.7-d runs synthetically under D-99. Past prompts are
  replayed through nimble:9b in shadow. This prereg is frozen before the first shadow row. Labels
  come from the Opus-rubric labeller, with its 20/day cap lifted for this run only. The hard spend
  cap is $10.
* **D-99**: every number from this run is labelled **`synthetic (D-99)`** and is never mixed into
  organic counts. D-42's organic-only clause for P1.7-d is overridden for G1 only.
* **D-107** (owner answers to the draft's open questions, recorded in PLAN-v16):
  * (a) labels in batches of 8 with the 6 planted calibration items, under GE3 batch rules; the
    hard cap stays $10;
  * (b) if fewer than 100 eligible organic prompts survive, the GE3 held-out prompts are admitted
    as a separately reported group (§2a);
  * (c) a sub-agent runs attach read-only and writes only 0600 files under `$PP`;
  * Q3 (the coordinator's): the rules arm uses the live tier policy, resolved read-only (§4).
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

**Attach.** A sub-agent runs it (D-107 c). It is the only step that reads `~/.claude/projects`, the
proxy ledger, `~/.llm-router/.env`, `denylist.txt` and the live install. It reads them read-only and
writes only 0600 files under `$PP/v16/p17d/`. It calls no model and prints counts only. It may run
before the freeze, because it reads no label and no verdict; its counts are recorded in "What was
looked at" when the prereg is frozen.
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
* `item_id` = the first 16 hex of `sha256("p17d:item:" + session_id + ":" + record_timestamp)`.
  It is deterministic, and G items also keep `ge3_id`;
* `session_id` (the transcript `sessionId`), the original `ts` and `cwd`;
* the full prompt, capped at 20,000 characters as head and tail (AMEND1 B `PROMPT_STORE_CAP`);
* the classifier-input pieces `cls_input.assemble` reads (`cls_input.py:62-108`): up to 3 earlier
  human prompts and the assistant's last text, and `capped`. These come from the main-thread messages
  before the prompt. Each earlier prompt is stored to 301 characters and the assistant tail to its last
  502, one and two characters past assemble's own cuts (300 and 500). **D-109 defect fix:** with no
  margin, re-normalising the rebuilt body stripped a space at the cut and gave a false
  `assemble_mismatch` (attach run 1). The equality check itself is unchanged and strict;
* `requested` (§4) and `requested_basis`;
* the served tier, and the strata fields (§7).

**Attach is single-shot.** At freeze the pins record the sha256 of `items.jsonl` and of
`attach_counts.json`. `attach` refuses to run when `items.jsonl` exists, or when
`$RUN/attach.pinned` exists (written at freeze), or when a frozen `PREREG-P17D-synthetic.md`
exists. Every later step refuses if either file's sha256 differs from its pin.

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

**Consequence, stated first: on the frame alone the most likely result is fewer than 100 items.** That
triggers §2a.

### 2a. Admission of GE3 held-out (D-107 b)

**Trigger.** After selection, fewer than 100 frame items (group **F**) are selected.

**Admitted group (G).** A GE3 held-out item (`split.json` `heldout_ids`, 234) is admitted only if it
passes every test below. Each exclusion is counted.
* `kind == "organic"` (GE3 held-out organic: 184) and `source == "claude"`. Codex items have no
  proxy turn.
* It is **not** flagged `p_eval_cal` and is not an E2-cal, E2-HO or E1 near duplicate. E2-cal is
  nimble's tuning set (PREREG-GT §3 leakage rule).
* It is not a near duplicate of a GE3 tune item or of an F item.
* Attach resolves its transcript record by `(session_id, timestamp)`.
* It passes the §2 eligibility rules other than the GE3 leakage row: secret guard, served tier, and
  so on. The window holds automatically, because GE3's window ends 2026-10-08T10:00Z.

**Order and selection.**
* All selected F items are accepted first.
* G items follow, in `sha256("p17d:admit:20261015:" + ge3_id)` order.
* The 0.50 session-cap walk is continued over the combined set until the total reaches
  `N_SELECT_ADMIT` = 250 or a walk accepts nothing.
* **F items refused by the cap in the F walk do not re-enter** the continued walk; only G items are
  walked in it (D-109 b). This was settled after attach run 1's counts were seen (Freeze record, D-109
  item 3).
* Each row carries `group` = F or G.

**Labels.** G items are **relabelled fresh** in the same batches, with the same rubric and the same
assemble context as F. The existing GE3 rubric labels are **not reused**: they were judged on the GE3
context (600/1500 characters), a different label basis. The calibration gate applies to the whole
run. Agreement between the fresh label and the GE3 label on G (Cohen's κ, exact) is reported as a
labeller test-retest figure and never gates.

**Feasibility gate (before any nimble or judge call, $0).** The run stops if, after admission, the
selected total is under 100 or the largest session holds more than 0.55 of the total. It then writes
a NOT INFORMATIVE gate file ("insufficient supply") and consumes the use (§10).

**Early error gate (D-109 a).** The §7 error gate is also applied before any spend:
* at `select`, on `assemble_mismatch` among the selected items. More than 10% of selected items
  writes the terminal use-log record and stops the run at $0;
* after `shadow` and before `label`, on `assemble_mismatch` + nimble errors. More than 10% writes the
  terminal record and stops at $0, before any judge call.

The full gate, adding label errors, is applied again at `score`.

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
* `git log` of `classify.py`, `claude_tiers.yaml` and `proxy/backends.py` from the corpus start
  (2026-08-01) shows wiring and feature flags, with two exceptions:
  * `57250744` (2026-08-19) is an upstream sync;
  * **`c0b0e5ca` (2026-08-31) is a hand edit of the `research` intent pattern**, made after live
    prompts were misclassified. That makes it rules tuning on real prompts. The prompts quoted in
    its commit message and the string literals of `tests/test_first_forty_task14.py` join the
    leakage set below as `rules_hand_edit`;
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
| rules_hand_edit (c0b0e5ca) | the quoted prompts plus test literals of 4 words or more | `git show c0b0e5ca`; `tests/test_first_forty_task14.py` at the freeze SHA |

Census on the corpus text, counts only: 497 of 2,658 lines hit the set. By set: GE3 held-out 221,
GE3 tune 163, E2-cal 102, E2-HO 89, E1 0. A line can hit more than one set.

**Why E2-HO and E1-HO stay out but GE3-HO may enter.** nimble's verdicts on E2-HO and E1-HO were
already computed and read in M1.9 (amend5), and each held-out set is single-use per family
(PREREGISTRATION.v2 §8; PLAN line 2611). nimble has never run on GE3-HO. rules_eff was scored on it
once for P1.6-g, and the rules were not changed afterwards.

The single exception is §2a. GE3 held-out was never used to tune nimble or the rules (§3 above). It
re-enters only through the §2a trigger, and its E2-cal overlap stays excluded. This use is not a
P1.6-g re-score: amend4 closes GE3 for P1.6-g only.

Stated limit: the exclusion is per item, not per session. A kept item from a GE3 session can carry
up to 300 characters of an excluded earlier prompt in its context. Its label and both verdicts are
still new.

## 4. Arms (both on every selected item, paired by construction)

Everything runs in a scratch `HOME`/`LLM_ROUTER_HOME` (the `g1_synthetic_evidence.scratch_env`
pattern). The router code under test is pinned at the freeze (`REPO_SHA`, draft: `64e3c84c`).
`attach` (`scripts/p17d_attach.py`) is built and independently reviewed before the freeze, and
its file sha256 is pinned (`ATTACH_SCRIPT_SHA256`). The other harness commands are built after the
freeze. Their commit SHA is logged in PROGRESS.md and in `use.log` before the first shadow row.

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
  an error. `assemble_mismatch` is also an error. That item gets no nimble call and enters as a
  fallback.
* **The final attempt counts (declared deviation from AMEND4 2(e)).** Each attempt appends its own
  record (`llm_shadow.py:177`), while `shadow_eval.score` keeps the **first** record per
  `(session_id, text_sha)` (`shadow_eval.py:170-175`). Before scoring, the harness therefore
  keeps the **last** record per key. All attempts stay in the file, and `n_attempts` is reported.
  Reason: live, the shadow never retries a failed verdict, so "first" and "only" coincide there.
  Here the retry is part of the procedure.
* **Order and crash-resume.**
  * Items run in selection order, F then G.
  * After each record the harness appends `{item_id, attempt, source}` to `shadow_meta.json`
    (fsync).
  * On resume it reconciles from `classifier_shadow.jsonl`, the authority, by key. An item with a
    final record (an `llm` or `cache` source, or a failed retry) is skipped. An item with a single
    failed attempt gets its retry. An item with no record is run.
  * Warm-up and the quiet gate are repeated on every resume.

**rules_eff.**
* `rules.tier` = `(policy.tier_for(task, cx) or policy.tier_of(requested)).name`, where
  `(task, cx) = backends._classify_class(tier_text(body), capabilities=False)`. This is the live
  `target = self.tier_for(task, cx) or req_tier` followed by `proposed = target.name`
  (`tiers.py:1031-1032`; `backends.py:463-479`; `tiers.py:725-728`). So a `None` table result falls
  back as it does live.
* The harness env pins **`LLM_ROUTER_ENGINE_MODE=shadow`**. In `decide` mode `_classify_class`
  would take the engine's Decision (`backends.py:470-472`), and the engine was tuned on GE3 tune.
* `<policy>` is the **live** tier policy (D-107 Q3). Attach resolves it read-only, in the proxy's
  own order. Nothing writes to the live install, and `doctor --fix-routing` is never run.
  1. The `--tier-policy` argument of the running proxy process. If the proxy is idle, the
     `ProgramArguments` of its launchd plist.
  2. Otherwise `LLM_ROUTER_PROXY_TIER_POLICY`: the plist `EnvironmentVariables` first, then
     `~/.llm-router/.env`. `load_dotenv_files` runs at proxy start (`server.py:1130-1131`) and the
     real environment wins. The proxy reads the key at `server.py:221` and `--tier-policy` defaults
     to it (`:1169`).
  3. Otherwise the bundled `claude_tiers.yaml` beside the **live install's** `proxy/tiers.py`
     (`DEFAULT_POLICY_PATH`, `tiers.py:148`, `:694`). The install is found from the proxy
     interpreter's `import llm_router`.

  Attach copies the file to `$RUN/tier_policy.yaml` (0600) and records the path, the basis (1, 2 or
  3), the sha256, the `route` table and `complexity_knn`. The frozen prereg pins that sha256.
  * If `complexity_knn` is true, the rules arm uses `with_complexity_knn` (`tiers.py:638`), and the
    live kNN artifact's training set joins §3 before freeze.
  * The Haiku guard override changes only `haiku_rewrite`, which does not touch `proposed_tier`.
  * Finding: at `64e3c84c`, `llm-router doctor` prints no tier-policy line (grep of `doctor.py`).
    This code-order resolution is therefore the report of truth.
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
    under-route with the same B and seed;
  * both bootstraps use the same rows as the means: the verdict rows plus the fallback rows
    (AMEND4 2(c)), last record per key. The error-excluded sensitivity reruns both;
  * with fewer than 30 sessions, both the wide and the normal interval are printed (statistics
    rule 1). Under-route rates print with Wilson 95% intervals (PREREG-shadowlab, repo, lines
    55-56).

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
`shadowlab.py` at the sha256 produced by the shadowlab pin fix. That fix lands first (its own
change, with AMEND5-shadowlab and a review), and this prereg pins its **new** sha at freeze;
`b89c7e17…` is today's sha. `shadowlab.run` is **not** used: it reads the live sidecar,
enforces the daily caps, and today refuses because its pin is the pre-amend6 gt500 sha
(`work/launchd.err.log`).

**Batching (D-107 a)** follows PREREG-GT §5:
* 8 items per call (`ITEM_TEMPLATE` × 8 + `BATCH_TAIL`);
* order by `sha256("p17d:label:20261015:" + item_id)`, with opaque ids L001… assigned in that
  order;
* a failed batch is retried up to 3 times, then its items go one at a time (`SINGLE_TAIL`). An item
  still unlabelled is a label error.

**Calibration.** The **first call is the 6 planted items** of `rubric.py` (cal-h1/h2, cal-s1/s2,
cal-o1/o2) plus the first 2 real items in label order. The order within that call is shuffled
with seed 20261015.
* Planted items get opaque ids from the same id space as real items, never `cal-*`.
* **Declared deviation:** GE3 appended the planted items to the queue and shuffled them in
  (`gt500.py:651-654`). Here they come first, so a failed labeller costs one call.
* **No reroll.** The first valid label for each planted id is final and is never asked again. That
  holds on retry and on resume, and it is enforced from `labels_planted.jsonl`. A call with no
  valid parse may be retried under the batch rules. Any wrong planted label, as soon as it is
  parsed, makes the run terminal.
* Opus is trusted only at **6/6**. Otherwise `label` writes the terminal use-log record (§10) and
  the result is NOT INFORMATIVE.

**Every selected item is labelled**, including nimble-error items, for the §7 sensitivity analysis.
F items are labelled before G items. If the spend guard stops the run, F is already complete.
`labels.jsonl` (0600) holds `{text_sha, session_id, truth}`, which is what `shadow_eval.load_labels`
reads.

## 7. Analysis and verdict

**Verdict rows (AMEND4 §2):**
* `n` = labelled rows with nimble `source` in {`llm`, `cache`}. Error rows enter the two means as
  fallback (llm = rules_eff), are counted in `n_fallback`, and do not count toward n. The sign of
  the difference is the same either way;
* one row per `(session_id, text_sha)`: the last record per key (the declared deviation in §4);
* largest-session share is computed over the n verdict rows.

**Decision rule (P1.7-d verbatim; the point-estimate reading of PREREG-shadowlab, repo, frozen
2026-10-11).** It is evaluated in this order, and the first match is the verdict.

1. **NOT INFORMATIVE (procedural):** P1.7-c has not passed (PLAN P1.7 task 3, #327); or a
   `classifier_shadow` row is dated before `frozen_utc`; or labeller calibration is below 6/6; or
   the feasibility gate (§2a) fails; or a pin sha differs.
2. **NOT INFORMATIVE (errors):** nimble errors + `assemble_mismatch` + label errors exceed **10%
   of selected items**. Its parts are also checked at `select` and after `shadow`, and a failure there
   stops the run at $0 (§2a, D-109 a).
3. **NOT INFORMATIVE (n):** n < 100, including when the spend cap stops labelling.
4. **NOT INFORMATIVE (share):** the largest session holds more than 60% of the n rows.
5. **PASS (synthetic, D-99)** if C-λ2-clamp(llm) ≤ C-λ2-clamp(rules_eff) **and**
   under(llm) ≤ under(rules_eff).
6. **FAIL** otherwise.

The comparisons in 5 use point estimates. The paired session-bootstrap CIs are printed and do not
gate (statistics rule 9).

**Groups.** When §2a fired, the verdict is computed on F ∪ G. **Every number is also printed per group
(F, G)** with its n and sessions, and the report leads with any group whose point estimate takes
the opposite side from the combined one.

**Reported, never decisive:**
* the verdict with error items excluded from both arms;
* fresh-vs-GE3 label agreement on G;
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
  3. feasibility gate and the early error gate on `assemble_mismatch`;
  4. `shadow` (every nimble and rules row), then the early error gate on mismatches + nimble errors;
  5. `label` (calibration first);
  6. `score`.
* `label` refuses to start until `shadow` has finished. `shadow` refuses if `labels.jsonl` exists.
  So no label is made before every verdict exists, and the labeller never opens the record file.
* **Calls:**
  * nimble (local, $0): 1 warm-up + up to 150 items (250 with §2a) + at most 10% retries, so at
    most 166 (276);
  * Opus at batch 8: ceil(156/8) = **20 calls**, or ceil(256/8) = **32 calls** with §2a, plus
    retries.
* **Cost estimate:** GE3 measured $6.68 / 45 batch calls = $0.149 per call ($0.019 per item). That
  gives **about $3.0-3.6** expected, or **about $4.8-5.5** with §2a. The guard below keeps the worst
  case under $10.
* **Spend guard.** The guard sums `total_cost_usd` in `$RUN/ledger.jsonl`. Before each call it
  estimates the next call at 1.25 × the most expensive call so far (first call: $0.30). It stops
  the run if spent + estimate would exceed **$10.00** (D-106). Labels already written are kept. No
  further tranche is run without a new prereg.
* **Quota pause.** Before every call the guard reads `usage.json`. It pauses (exit 3, resumable)
  at session ≥ 80%, at weekly ≥ 85%, or when the file is missing or more than 30 minutes old
  (PREREG-shadowlab §6).
  * A pause makes no call. It writes no use-log line and consumes nothing.
  * A resumed `label` continues from `ledger.jsonl`, `labels.jsonl` and `labels_planted.jsonl`. It
    never repeats a planted item or an already-labelled item.
  * **Known state at drafting:** the live weekly quota reads 100% (coordinator), so the first
    `label` call will pause until weekly falls below 85%.
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

* `score` first appends `started <UTC> prereg_sha256=<…> git_sha=<…> labels_sha256=<…>
  shadow_sha256=<…>` to `$PP/v16/p17d/use.log` (0600, append-only). It does this **before it
  parses `labels.jsonl`**: the two shas are taken over the raw bytes. It refuses if the log already
  holds a `started` or `terminal` line.
* **Terminal records.** Each of these appends `terminal <UTC> <reason>` to `use.log`, writes the
  NOT INFORMATIVE gate file, and ends the run:
  * `select` on a feasibility stop or on the early error gate;
  * `shadow` on the post-shadow error gate;
  * `label` on calibration below 6/6;
  * `label` on exit 4 (non-Opus model).

  Every command refuses once `use.log` holds a `terminal` line. So no re-selection, re-label or
  calibration reroll is possible.
* A crash during `score`, and any NOT INFORMATIVE result, consume the use. A quota pause
  (exit 3) and a spend-cap stop do not write `terminal`. After a spend-cap stop, `score` runs on
  the labels that exist.
* `status` prints counts only: line counts of labels, never truth values or a distribution of
  them. It also prints the ledger spend and the use-log state.
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
8. **The admitted group G is not fresh.**
   * Its Opus-rubric labels exist (on another context) and are known in aggregate.
   * rules_eff was scored on it once for P1.6-g, with no change to the rules afterwards.
   * It was GE3-sampled with a 35-per-session cap, so it differs in composition from F.
   * It dominates n when admitted. That is why every number is printed per group.
9. **Code version.** The prompts are from August to October 2026. The rules and the scorer are
   today's code at the pinned SHA.

## What was looked at before writing (counts only)

* The census above (a counts-only script kept outside the repo). It applied the supply.py rules and the leakage
  rule to the corpus text and printed counts only.
* The GE3 `supply.json` aggregates, `heldout_M19` latency and fallback, GE3 and shadowlab spend
  ledgers, and the shadowlab launchd log.
* No label, no verdict, no rules tier and no model output was read or computed for any corpus item.
* **Attach run 1 is void (owner decision D-109).** Its outputs were archived (renamed, not deleted) to
  `$PP/v16/p17d-run1-void/`. Attach never reads that path. Run 1 ended before any `select`: no shadow
  verdict, no label and no rules tier existed for any item. Its counts follow; they decided nothing
  except that the defect was found.
* Attach run 1 counts (`attach_counts.json`, run once 2026-10-15T14:02Z by a sub-agent, exit 0):
  * transcript files 2,048; corpus-matched records 3,030; frame records seen 3,052;
  * exclusions: not human-typed 2,473; leakage 202; research kind 98; exec session 32; G p_eval_cal 44;
    G leakage 16; after window 8; image 7; secret_scrubber 3; harness or slash 2; served Haiku 2;
  * leak hits by set: GE3 tune 94, E2-HO 78, E2-cal 48, GE3 held-out 12; for G, E2-cal 10 and E2-HO 7;
  * **F**: 46 items from 4 sessions, largest share 0.6522 (top 3: 30, 12, 3);
  * **G candidates**: 119 items from 22 sessions, largest share 0.1765 (top 3: 21, 21, 15);
  * `assemble_mismatch` 38 of 165 (F 16, G 22); capped 2; context withheld 1;
  * requested basis: ledger 5, transcript_served 160; denylist entries 0; overrides entries 1;
  * tier policy basis `1:process_argv`, `complexity_knn` false, `route.default` simple→haiku,
    moderate→sonnet, complex→opus, deep_reasoning→opus.
* After attach, a counts-only check of `items.jsonl` (no text printed): for all 38 mismatched items the
  re-assembled context differs, and each one has whitespace at a stored truncation edge (an earlier
  prompt ending in a space: 18; an assistant tail starting with a space: 22; overlap 2). Those are the
  38 false mismatches the D-109 fix removes.
* **Attach run 2 counts:** <filled at freeze from attach run 2: transcript files, exclusions, leak hits,
  F and G summaries, assemble_mismatch, capped, context withheld, requested basis, tier policy>.

```pins
LABEL=synthetic (D-99)
FRAME_SHA256=3f5d96eee34ed72b90f62ab462b8f452b6ca2d465c0beb6158dcb94b4a85b7f4
WINDOW_END_UTC=2026-10-08T20:46:00Z
SESSION_KIND=organic
SELECT_SEED=20261015
N_SELECT=150
SELECT_MAX_SHARE=0.50
ADMIT_TRIGGER_F_LT=100
ADMIT_SET=ge3_heldout,organic,claude,not_p_eval_cal
ADMIT_SEED_FORMAT=p17d:admit:20261015:{ge3_id}
N_SELECT_ADMIT=250
ADMIT_REUSE_GE3_LABELS=false
TIER_POLICY_SHA256=<filled at freeze from attach run 2>
ITEMS_SHA256=<filled at freeze from attach run 2>
ATTACH_COUNTS_SHA256=<filled at freeze from attach run 2>
ITEM_ID_FORMAT=p17d:item:{session_id}:{record_timestamp}
SHADOW_RECORD_RULE=last_per_key
ENGINE_MODE=shadow
REPO_SHA=64e3c84cbf0a3160d134a9f3ba19035bf1f1b3e8
ATTACH_SCRIPT_SHA256=<filled at freeze from attach run 2>
FEASIBILITY_MIN_SELECTED=100
FEASIBILITY_MAX_SHARE=0.55
GT500_SHA256=9cde774dfe93387d6ee3dfcbd1b5481a537f265e5961d82f85a87d29d6328959
SUPPLY_SHA256=12cd3e1eed7fc15658b4b44246d83422e2b61adc4ccb270dd9f5e729608fb0e8
SHADOWLAB_SHA256=0d2d2b58bde9cb8a5466782da866c218a244595d1485ef8d2563ac8c02fb4def
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

## Freeze record

**Fill-ins.** These are the only values filled in at the freeze:
* the attach run 2 counts (in "What was looked at");
* `TIER_POLICY_SHA256`, `ITEMS_SHA256`, `ATTACH_COUNTS_SHA256` and `ATTACH_SCRIPT_SHA256`, from attach
  run 2;
* `REPO_SHA` (router `src` under test; `64e3c84c`, unchanged since the draft);
* `SHADOWLAB_SHA256` (`0d2d2b58…`, after AMEND5-shadowlab).

**Freeze guard.** BASE is `44956ed1`. The freeze commit message records
`git diff -M 44956ed1 -- <this file>`. An independent check confirms that every changed line is one of
these:
* a fill-in above;
* the removal of the draft-only "Open before freeze" section and the draft status paragraph, which
  this status line and section replace;
* one of the **D-109 defect fixes** listed below.

N_SELECT, the share caps, the seeds, the bars and the admission criteria may not change.

**D-109 defect fixes (owner decision; these are not parameter retunes).**
1. The 301/502 storage margin, so that an edge space cannot cause a false `assemble_mismatch` (§2).
2. The early error gate at `select` and after `shadow`, stopping at $0 (§2a, §7, §8, §10).
3. Refused F items do not re-enter the §2a walk. This states a rule the draft left ambiguous.
   **Disclosure:** it was settled after attach run 1 showed a 30-item F session (0.6522 of F), so it
   was decided with those counts in view. The owner chose the stricter reading, which admits fewer
   items from that session.
4. This status line and this freeze-record section are kept in the frozen file.
5. The disclosure of void attach run 1 in "What was looked at".
