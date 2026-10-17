# PREREG-P20T-techop: local re-qualification of technical operations (PLAN v16 P2.0-T)

Status: **FROZEN d6 (2026-10-17, d5 plus the org-first selection and the gate-window filter, D-206; d5 is kept as PREREG-P20T-techop.d5.md; d4 was d3 plus the PR #561 review repairs R1-R7, D-204; d1, d2 and d3 are kept as PREREG-P20T-techop.d1.md, .d2.md and .d3.md; d3 was d2 plus the delta freeze-guard fixes F1-F8; d2 was d1 of 16:59Z plus the fixes N1-N13 and decisions D-199/D-200; written from code and prereg text only).** This file is frozen
only when `PREREG-P20T-techop.sha256` exists beside it, an independent freeze-guard review has passed
(D-122: an author-written sidecar is void) and `scripts/check_prereg.py` reports a match. Until then
the harness refuses every subcommand that writes an item, calls a model or writes a result; the one
pre-freeze mode is `select --dry-run`, which reads the pools and prints counts only (§4). Changing
any byte after the freeze makes a new prereg with a later `frozen_utc`. Nothing has been selected,
no model has been called and nothing has been scored under this file. Bracketed `[OPEN-n]` marks are
choices the owner or coordinator settles before the freeze; each has a recommended default
(see PREREG-P20T-summary.md). The default is what the text below states.

Models on it: PREREG-P20-requal (r9) with AMEND1 and AMEND2, read from `origin/feat/p20-requal`
(f823a400 at the time of writing). Where this file is silent, P2.0's text, AMEND1 and AMEND2 stand
word for word (same harness rules, same privacy, same plants, same judge procedure).

## d6 changes (D-206, after the freeze-prep floor FAIL; counts only, no outcomes, no item text)

| S | Change | Where |
|---|---|---|
| S1 | **Org-first selection.** Every eligible `org` row is selected first, by hash order within `org`, subject to the session cap; `ho` rows then fill by hash order up to N_TARGET. Replaces the single hash order across strata (which picked `org` 2 of 120 from a pool of org 32 / ho 679). Strata are still graded separately; ORG_MIN_N and ORG_MAX_GAP_PTS are unchanged; plants are unchanged | §4, §16 item 27, H4 |
| S2 | **Gate-window filter.** Rows with `prompt_tokens_est` > GATE_WINDOW_FACTOR (0.9) x `num_ctx(LOCAL_MODEL)` (32,768 gives 29,491.2) are dropped before selection, counted per stratum as the drop reason `over_gate_window`. Mirrors the proxy local gate's forward rule (`local_gate`: `needs.tokens > WINDOW_MARGIN x window`, WINDOW_MARGIN = 0.9). `prompt_tokens_est` may itself under-count (R7) | §4, §16 item 26, H4, H12 |
| S3 | New constant `GATE_WINDOW_FACTOR=0.9` (constants block) | constants |
| S4 | The equal-rate threshold k is recomputed by the §4 rule for the realised n (unchanged rule; 97 at n = 120) | §4 |
| S5 | `select --dry-run` additionally prints `over_gate_window_by_stratum` and `gate_window_limit` | §4, H12 |

## d4 changes (PR #561 review rv-561 r1 FAIL, D-204)

| R | Change | Where |
|---|---|---|
| R1 | the end-of-run unload happens only if this process made a local call, inside the lock, never waiting for it; a held lock skips it silently | §3, §11 |
| R2 | D-199 run block (23:15-07:00 while P2.0 is unfinished) removed from text and constants: P2.0 is frozen under AMEND3 (sha256 `3b3e981a5ebd99f251381f9f94f659d3ca74ab04cb91cef854164e064c5de741`), runs at any hour under the same quiet gate and takes the same exclusive flock around every local call; both use `qwen3.8-uncensored:latest`, so they interleave at call level. The lock and the P2.0-process check remain the interlock. Resolves FIX 3 | §1, §11, §16, H5 |
| R3 | resume of a parked item continues at the next attempt number (non-final rows already present are counted); no item has more than RETRIES+1 attempt rows; the count of items over that is printed (must be 0) | §6 |
| R4 | lock, then quiet readings, then the call (AMEND3 order); a lock taken by P2.0 between two attempts parks the run | §11 |
| R5 | branch A, if ever chosen, tests tool evidence only; B stays the default and the recommended freeze value | §4 |
| R6 | `ps` that returns nothing or errors parks (fail closed) | §11 |
| R7 | `pack_over_window` under-count disclosed; dry run prints median and p90 `prompt_tokens_est`; "items run after a failed re-check" printed by `run` and tested | §4, §6, §15.7, §16 |

## d3 changes (delta freeze-guard NOT READY, 8 text fixes)

| F | Change | Where |
|---|---|---|
| F1 | plants bypass the validity and redundant pre-checks and are always shown to the judge; the pre-check would mark the repeat plant redundant | §7 plants |
| F2 | mutating Bash = `technical_ops.classify_tool_call("Bash", input)` returns no read-only tech-op class; None (unresolvable) = mutating; function verified on origin/feat/p20-requal and origin/main | §7 item 2 |
| F3 | leave-one-session-out grade with the n >= 60 clause waived (gap <= 3.0 and lb >= -10.0); `session_sensitive` iff a leave-out fails while the full set passes | §8 |
| F4 | path `.../requal/b2/use.log`; finished iff a `started` line from `score`; `pgrep -f p20_requal.py` refusal replaces the P2.0 lock claim | §11 |
| F5 | branch A only if the gate's sub-agent test runs on ledger-row / rebuilt-body fields; names verified at the freeze | §4 |
| F6 | skipped Haiku (`haiku_skipped_budget`) counts as complete for the Order rule; `redundant` added to the unacceptable list | §11, §2 |
| F7 | generic-plant calibration risk, bias 25 | §16 |
| F8 | Order line changed (skipped Haiku) listed here | §11 Order |

## d2 changes (freeze-guard CONFIRM WITH NOTES, D-199, D-200)

| N | Change | Where |
|---|---|---|
| N1 | sub-agent drop `subagent_step`: branch A (gate's own test, if REPO_SHA contains the gate) and branch B (fallback, with/without grade) | §4 Drops; constants `SUBAGENT_BRANCH`; H13, H14 |
| N2 | `org` = GE1 census-exact; `ho` = every other main-thread row (incl. row-stamp organic resolved to research, untagged); row stamp printed secondary | §4 Strata |
| N3 | edit-shaped replies judged, share and served-subset rate printed; text JSON is not the native path | §6, §7, §15 item 2 |
| N4 | deterministic pre-judge `redundant` rule (tool + full normalised input vs the full history, mutation resets): GATES, scored unacceptable for every arm like validity, not shown to judge, counts per arm; repeat rate report-only | §6, §7 item 2 and primary |
| N5 | (a) `first_object` and fence behaviour named; (b) key rule only for the six builder tools; (c) fourth plant `generic` at indices 18-23 | §6, §7 plants; constants `PLANTS_GENERIC` |
| N6 | `select --dry-run` once on final REPO_SHA and builder; later change voids it; counts decide only freeze/no-freeze; synthetic stratum is a new prereg | §4 Floor rule |
| N7 | freeze also needs `org` n >= 20; dry run prints the equal-rate threshold; `select` refuses if realised n != `N_REALISED_TECH_OP` | §4 |
| N8 | (a) 10.0-pt organic bound is a coarse screen; (b) `session_sensitive` downgrades eligible to inconclusive (D-200) | §8, §10 |
| N9 | cap $40 (D-200); judge-cost reserve before the first Sonnet call; Haiku skipped if spent + reserve would breach | §11; constants |
| N10 | no Claude call before the freeze except synthetic probes; cap fixed at the freeze; "first 5 calls" sentence removed | §11 |
| N11 | privacy wording: arms only the four named; `organic_action` / `served_model` only in the 0600 pool; measurements file hashed ids and counts only | §12, §9 |
| N12 | H11 tech_op-only mode; H12 dry-run prints; H13 merge order, REPO_SHA contains the gate and #523 | H-list |
| N13 | the 304 upper bound is the row stamp, before N1 and N2 | §0 |
| D-199 | run window replaced (block 23:15-07:00 while P2.0 unfinished; P2.0-unfinished test on `$REQ/use.log`, read-only, unreadable = unfinished; quiet gate before every local call; stop after the current item at 23:15) | §11 first bullet; constants |
| ADD-1 | Ollama lock `~/.rsi/research/primary-plan/v16/requal/ollama.lock` | §11 quiet bullet |
| ADD-2 | constants: `NIGHT_WINDOW` deleted, `RUN_BLOCK_START=23:15`, `RUN_BLOCK_END=07:00`, `RUN_BLOCK_WHILE=P2.0_UNFINISHED` added; key list updated | constants block; H5 |
| ADD-3 | §1 authority cites D-199 | §1 |
| ADD-4 | latency load-dependent and report-only; timeout charged to candidate; per-item quiet readings and failed-re-check count | §6 |
| ADD-5 | bias 24, thermal and daytime contention | §16 |

## 0. Why P2.0 could not grade tech-op, and what changes (read first)

PLAN §0.4b S2 and D-196: PREREG-P20 left `tech_op` not informative, so P2.1's precondition ("P2.0
marks >= 1 model eligible for technical_op") cannot be met. P2.0 §4 gave n = 1 (pool 15). The causes,
from P2.0's own exclusion counts (build 2, 2026-10-16) and from the code at f823a400:

| # | Cause of n = 1 | Evidence | Fix here |
|---|---|---|---|
| C1 | **Tier filter on a field the ledger does not write for continuations.** `select_sets` drops any tech-op row whose `tier` is not simple/moderate; the builder takes it from `tier_complexity`. 14 of 15 pool rows were `no_tier`. | P2.0 §4 "realised n of 1 (14 no_tier)"; `p20_requal.select_sets` (`no_tier`); builder `tier(row.get("tier_complexity"))` | **No tier filter for tech-op.** The proxy local gate maps `prev_tool_class == technical_op` to lane `tech_op` / tier `simple` without reading any tier (`proxy/local_gate.py` `lane_of`, branch feat/proxy-local-gate 8ca74fec). The population must be what the gate will face, so the tier is not a selection criterion (§4). |
| C2 | **Organic-only row stamp.** 23,372 rows `not_organic`. | P2.0 §4 build 2 exclusions | Add a labelled non-organic stratum `ho` (main-thread headless / research / harness steps), as D-131 b decided and D-133 dropped only because it was `no_tier` (§4). Organic stays its own stratum `org`. |
| C3 | **Sub-agent transcripts parsed as empty.** `transcript()` skips `isSidechain` entries, and every entry of a sub-agent transcript is a sidechain entry; 288 `msg_not_found`. | P2.0 §4 "the transcript parser drops isSidechain entries"; builder `transcript()` | Builder fix: inside a sub-agent transcript file accept sidechain entries (§4, harness list H2). |
| C4 | **Gate was built on the wrong lane contract.** P2.0 writes `simple` and `moderate` with `basis: pooled_simple_moderate`; the gate reads only `tech_op/simple`. | `p20_requal.py` `TIERS_WRITTEN`; `local_gate.lane_of` | P2.0-T writes **one lane: `tech_op` / `simple`**, `basis: all_technical_op_continuations` (§9). |
| C5 | **Sonnet agreement is 100% by definition** (P2.0 Q7), so agreement was printed and never gated. | P2.0 §0 Q7 | Agreement is defined against the **organic recorded next action** for every arm, Sonnet included (§7). Still not gating (§8, [OPEN-4]); it anchors the judge. |
| C6 | **A lenient tech-op judge could pass anything.** P2.0 §7 brief: "A different but reasonable tool or argument is acceptable." Plants were a swapped answer and `{"final":"Done."}` only. | P2.0 §7 | Add a third plant kind, a verbatim repeat of the previous tool call (§7); require protocol validity before the judge sees a candidate (§7). |

The count of what a fixed builder can reach is not known and is not claimed. A **row-stamp** upper
bound from P2.0's published counts (N13): steps stamped `session_kind == organic` that passed the class
filters and reached the transcript stage were 15 (pool) + 288 (`msg_not_found`) + 1 (`secret_pattern`)
= 304, plus 95 `not_main_thread`. That bound is **before** the sub-agent drop (N1) and the census-exact
organic rule (N2), which both shrink `org` (P2.0 §4: census-exact admitted about 1 step against about 11
by row stamp); it is not an estimate of `org`. `select --dry-run` before the freeze prints the real
numbers (counts only). **If the dry run gives realised n < 60, or `org` n < 20, or < 5 sessions, the
prereg is not frozen** (§4, Floor rule).

## 1. Authority

* PLAN-v16 P2.0-T (new, §0.4b S2), F3 (D-133a, D-196), P2.1 preconditions, P2.1-a agreement definition.
* P2.0-b: `llm_router.eligibility.grade` is the one rule (unchanged).
* D-21, D-27 = A, D-29 = A (gap <= 3 pts at n >= 60, Newcombe LB >= -10), D-123a/b, D-166 (qwen3.8),
  AMEND1 (code gate, no transcripts), AMEND2 (thinking off, foreign-arm guard).
* L13 (single Ollama slot, quiet machine); **D-199** (owner 2026-10-17: run today; the L13 night window is
  replaced for this run by the quiet gate and the shared lock of §11); **D-204** (the D-199 clock block is removed:
  P2.0 is frozen under its AMEND3, sha256 `3b3e981a5ebd99f251381f9f94f659d3ca74ab04cb91cef854164e064c5de741`).
* **P2.0-a stays PARTIAL until this run is scored.** This file closes the `tech_op` matrix for one
  local model only.

## 2. Question and decision rule

Question: on technical-operation continuation steps, is the acceptable-action rate of
`qwen3.8-uncensored:latest` (think off) within 3 points of Sonnet's on the same steps?

* **Rule:** `llm_router.eligibility.grade({"k_cand": k, "n_cand": n, "k_ref": k_s, "n_ref": n})`.
  `eligible` iff n >= 60 and gap_pts <= 3.0 and lb_pts >= -10.0 (Newcombe method 10, inclusive,
  unrounded); `failed` at n >= 60 otherwise; `probe` at 0 < n < 60; `unknown` at n = 0.
* **Paired n:** selected items with a final reference verdict (§8). A local reply that errored, timed
  out, was empty, was not valid under the protocol (§6), was **redundant** (§6) or named a tool outside the item's tool list
  is **unacceptable**: a failed serve is a failed offload (the proxy would forward it to Claude).
* **Lane written:** task_type `tech_op`, tier `simple` only (C4). Haiku is graded the same way,
  report only, never written.
* **Verdict** adds set-level power and informativeness (§10), exactly as P2.0 §10.

## 3. Arms

| Arm | Role | Identity pin (checked before every `run`; mismatch = refuse) |
|---|---|---|
| `qwen3.8-uncensored:latest` | candidate | `ollama list` ID `b3f4c17c6811`; `/api/show` gguf / Q6_K / llamacpp / 27.3B, family `qwen35`, capabilities completion, tools, thinking (AMEND2) |
| `sonnet` | reference | `claude -p --model <CLAUDE_SONNET_ID>`; served-model mismatch is an error |
| `haiku` | report only | as above |
| `opus` | judge | as above |

* `think: false` on every `/api/chat` body (AMEND2 change 2), `num_ctx = local_models.num_ctx(model)`
  = 32,768, temperature 0, `keep_alive` 10m, one resident model; the harness unloads at the end only if it made a local call itself, inside the lock and never waiting for it (R1: the sibling P2.0 harness uses the same model name, so a park before any call unloads nothing).
* Claude children: P2.0 §6 and AMEND1 exactly: `--safe-mode`, `--no-session-persistence`,
  `--settings {"disableAllHooks": true}`, no `ANTHROPIC_BASE_URL`, no `ANTHROPIC_API_KEY`,
  `LLM_ROUTER_SESSION_KIND=research`, scratch cwd, tools disabled, real HOME.
* Community Q6_K build, not vendor weights; results speak for `b3f4c17c6811` only (AMEND2 bias 15).
  The same pin binds the live gate: a different build is a different, unqualified model.

## 4. Population and item construction

**Step definition (what a tech-op item is).** One ledger row of `proxy_calls.jsonl` with
`step_class == "continuation"` and `prev_tool_class == "technical_op"`, as written by
`proxy/steps.step_fields` (`step_kind` -> `continuation`; `prev_tool_class` via
`tool_classes.step_tool_class` over the tool_use blocks the newest tool results answer, whose
Bash/Grep/Glob/Read classification is `technical_ops.classify_tool_call`). The item is the step's
request: the conversation up to that step, with the model's next action to be proposed. Rows
without a recorded `prev_tool_class` are resolved offline by GE1 `census.offline_tool_class`;
`bash_unresolved` rows stay excluded and are counted (3,613 at P2.0 build 2).

**Same privacy rules as P2.0 (no new text exposure).** P2.0 §4 and §14 already allow real
prompt-derived items, built offline by a read-only sub-agent step, stored 0600 under a 0700 run
directory, with counts only in every printed or committed artifact. This prereg adds nothing to that
surface and no source file beyond P2.0's list:
* `$REQ_T = ~/.rsi/research/primary-plan/v16/requal/t1/` (directories 0700, files 0600);
* a copy of `proxy_calls.jsonl` and the session transcripts, pinned by sha256 in
  `$REQ_T/pools/build_manifest.json` (every transcript file read, as in P2.0);
* **secret screening** on the whole rendered item (request, every message including tool results,
  the stored organic action) with the engine secret guard at REPO_SHA, by the same code path as P2.0
  (`secret_pattern` exclusion; counted, never printed);
* **never opened by the author or any reviewer:** the pools, items, answers and judgements. Review is
  by counts, shas and a harness `status` that prints counts only.

**Window.** From 2026-09-28T00:00Z (GE1 v0 start, as P2.0) to the build time recorded in the
manifest (`date -u`).

**Strata (each reported separately; pooled for the grade):**

| Stratum | Rows admitted | Why |
|---|---|---|
| `org` | **GE1 census-exact organic** (N2): the kind resolved the way `census.py` (sha256 `CENSUS_PY_SHA256`) resolves it (`KindIndex` over the backfill, the overrides and the per-session tag files), main-thread, sub-agent steps dropped (N1) | the live population |
| `ho` | **every other** main-thread admitted row (N2): non-organic by census, **including** rows whose ledger `session_kind` stamp is `organic` but census resolves to research, and untagged sessions. `exec_sessions` excluded as in P2.0 | D-131 b proxy population; labelled, never described as organic. [OPEN-2] |

The ledger's own row stamp (`session_kind`) is printed per stratum as a **secondary** field and is never
the stratum rule (P2.0's stamp rule differed from census by about 10 rows, P2.0 §4).

**Drops (counted by reason, never silent):** `not_continuation`, `not_technical_op`,
`bash_unresolved`, `not_main_thread`, `subagent_step`, `transcript_missing`, `msg_not_found`,
`no_human_ask`, `secret_pattern`, `exec_session`, `pack_over_window`, `over_gate_window`, `session_cap`.
* **Sub-agent steps (N1; conditional on what REPO_SHA contains).** The gate serves main-thread
  continuations only, and `is_main_thread` is unreliable (27 organic rows flagged true had their response
  id only in a sub-agent transcript, P2.0 §4).
  * **Branch B is the default and the recommended freeze value (R5).** If branch A is ever chosen, only the **tool evidence** is tested (the gate's test over the tool names in the rebuilt history); no file-location or parent-session evidence is used, and sub-agent steps without that tool evidence stay in the pool.
  * **Branch A (REPO_SHA contains the gate, i.e. `feat/proxy-local-gate` is merged or pushed and
    pinned, **and** the gate's sub-agent test can run on the fields a ledger row and a rebuilt request
    body hold; else branch B; the function names are verified against REPO_SHA at the freeze, F5):** the builder applies **the gate's own sub-agent test** (`proxy/steps.is_subagent_request`
    / `is_main_thread` as the gate calls them, imported, not reimplemented) to each rebuilt request
    body; a step it flags is dropped as `subagent_step`.
  * **Branch B (fallback; REPO_SHA does not contain it):** drop `is_main_thread == false` rows; report
    the count of steps found only in a sub-agent file, per stratum; print the grade **with and without**
    the steps found in sub-agent files (the with-set is the graded set; the without-set is printed so a
    difference is visible).
  * The freeze record states which branch applies and the REPO_SHA; the other branch's text is then void.
* **No tier filter** (C1). The tier, where the ledger holds one, is a printed breakdown field only.
  Reason: the gate does not read a tier for technical_op, and a selection by decision tier would
  silently drop the steps the gate will serve (and `tier_complexity` is absent on continuations).
* `pack_over_window` (**under-counts, R7**): a step whose irreducible pack exceeds 32,768 tokens is dropped, by the same
  estimator the gate uses (`local_gate` `pack_over_window`), imported, not reimplemented. Reason:
  the gate forwards such a step, so it is outside the qualified population. It is counted; the
  census share f_fit (§11 of P2.0) is a different, separate measurement. The builder's `default_fits` sees
  only the newest user turn of the rebuilt item: the system prompt and the tool schemas are absent from a
  transcript, so the estimate under-counts them; the gate itself forwards a step only when `needs.tokens`
  > 0.9 x window (`local_gate`). A kept step can therefore be larger at serve time than at build time. The
  dry run prints the median and p90 `prompt_tokens_est` (chars/4 of the rebuilt messages) of the selected items.
* Drop < 4-word asks is **not** applied (a tech-op ask may be short); an empty ask is `no_human_ask`.

**Builder fixes (H1-H3, §Harness changes).** `transcript()` accepts sidechain entries when reading a
sub-agent file; `organic_action` is stored per item (below); tier is not required.

**`organic_action` (new pool field).** The assistant message with the ledger row's `msg_id` is the
real next action. The builder stores its first tool_use as `{tool, target}` (target normalised per §7;
no input body, no text), or `{"final"}` when it has none. It is the judge-independent anchor (C5).
It is prompt-derived, so it is 0600 with the pool. The model that served it is the row's
`served_model` (metadata; counted per stratum, e.g. Opus under the pin).

**Selection (`select`, once; d6, D-206).** Rows are ordered by `sha256("20261017:" + id)` (seed
`SELECT_SEED`). Step 1, window filter: a row whose `prompt_tokens_est` is greater than
`GATE_WINDOW_FACTOR x num_ctx(LOCAL_MODEL)` (0.9 x 32,768 = 29,491.2; a row at exactly the limit is kept)
is dropped and counted per stratum as `over_gate_window`; this is the proxy local gate's own forward rule
(`local_gate`, `needs.tokens > 0.9 x window`), so a row the gate would never serve locally is not
qualified. Step 2, org-first: every remaining `org` row is admitted in hash order unless its session
already holds `floor(0.20 x N_TARGET)` admitted rows (24 at 120). Step 3: `ho` rows are admitted the same
way in hash order until N_TARGET. (P2.0's `rx`-first priority is not reused; d5's single order across
strata is replaced.) The strata are graded separately, and the §10 organic check (ORG_MIN_N,
ORG_MAX_GAP_PTS) is unchanged. Counts by reason printed; no text printed. The selection rule was changed
after seeing pool and selection stratum counts only (§16 item 27).

**N.** Target **120**, floor **60** (`grade` needs n >= 60). The equal-rate threshold k is recomputed by this rule for the realised n (97 at n = 120). [OPEN-3] At n = 120 equal rates grade
`eligible` only when Sonnet's k >= 97/120 (P2.0 Q1); below that the lane is `inconclusive`
(power-limited), by the §10 rule, not `failed`.

**Floor rule (pre-freeze, counts only; N6, N7).** `select --dry-run` is run **once, on the final
REPO_SHA and final builder** (any later change to the builder, the harness or `src/` voids the dry run
and this draft's counts). Its counts decide **only** freeze or no-freeze, never a design change. The
prereg is frozen only if the dry run gives realised n >= 60, **`org` n >= 20** and >= 5 distinct sessions
(stratum counts under census-exact and, printed beside, under the row stamp). The dry run also prints
the equal-rate threshold at the dry-run n (so power is known before the freeze). Otherwise it is not
frozen, and a synthetic stratum is a **new prereg**, not an amendment of this one (N6). The options: a synthetic stratum `syn` built from a pinned public checkout with scripted read-only
tool histories and no real prompt text (report-only, never in the grade, so it could not by itself
make a lane eligible) [OPEN-5], or accepting that tech-op stays NOT INFORMATIVE until the ledger
holds more steps (a larger window is not an option: the window is fixed above). The realised N per stratum and in total, the pool sha256 and the build manifest sha256
are fill-ins at the freeze (constants block). A set whose n < 60 at `select` is NOT INFORMATIVE and
terminal (P2.0 §13 `terminal tech_op n_min`). **`select` refuses if the realised n differs from
`N_REALISED_TECH_OP`** (P2.0 §4 rule; also per stratum for `org`).

**Informative also requires:** >= 5 distinct sessions; largest session <= 20% of n (by construction);
`org` stratum n >= 20, else the organic check in §10 is `org_too_small` and the lane cannot be
`eligible` ([OPEN-2]); the same condition is a pre-freeze requirement (N7).

**Leakage and prior outcomes (stated before any data).**
* No local model, prompt template or pack setting is tuned on any item. The pack builder (P1.1) was
  evaluated on PREREG-P11B's own synthetic set.
* P2.0 selected zero of these steps for any run (n = 1, terminal, never run), but the same ledger
  window fed GE1's census (s_tech, f_fit). Those are shares of a population, not quality; no label leaks.
* Prior local tech-op outcome: none measured. [RX] 4/37 vs 34/37 (qwen3.8 of a different build,
  AMEND2 bias 18) covered Q&A/code, not tech-op steps.
* The organic action was produced by whichever Claude served that step, so the Sonnet arm is not
  independent of the stored organic action in style; see §16.

## 5. Context: PACK (P1.1)

As P2.0 §5 for tech-op: `build_pack(request, messages=<rebuilt messages>, door="proxy", project="",
query=<latest human ask>, target_window=W)`; W = 32,768 (local), 65,536 (Claude arms, D-101 cap),
`to_messages(pack)` for local, flattened `### system / ### user / ### assistant` on **stdin** for
Claude. **Equal-window sensitivity (new, report only):** the Sonnet and Haiku answers are also
requested with W = 32,768 for the 10% re-judge sample items so the unequal-context bias (§16) has a
number; it never changes n or the grade. [OPEN-6]

## 6. Arm protocol

* Text protocol, as P2.0 §6: the prompt lists the tools (`DESCRIPTIONS` of the builder: Read, Grep,
  Glob, Bash, Edit, Write, plus any other tool named earlier in the session) and asks for exactly one
  JSON object `{"tool": <name>, "input": {...}}` or `{"final": <text>}`. The proposed action is
  judged and **never executed**.
* **Extraction (N5a).** The object is taken by `p20_requal.first_object`: the text from the **first `{`**
  to the end of the first JSON value that `json.JSONDecoder().raw_decode` accepts. Text before the first
  `{` (prose, a code fence marker) and text after the object are ignored, so a fenced object parses;
  a stray `{` in the prose before the object, a truncated object or a non-object value is invalid.
  `<think>` blocks are removed first (P2.0 §7). Only the first object counts.
* **Validity (new, deterministic, before the judge).** A reply is *valid* iff it parses (above) to one
  object with either a `final` string, or a `tool` naming a tool in the item's list and an `input`
  object. **Key rule (N5b):** for the six builder tools (Read, Grep, Glob, Bash, Edit, Write) `input`
  must hold at least one of `file_path`, `path`, `pattern`, `command` (the keys `action_key` reads); for
  any other tool named in the item's list `input` need only be an object (its concordance is then
  not computable and counts as no agreement). An invalid reply is unacceptable and is **not shown to the
  judge** (as errored answers, P2.0 §7). Validity counts per arm are printed.
* **Redundant (N4, gating; applied like validity to every arm).** A valid reply whose tool and full
  normalised input equal a call earlier in the item's FULL history (rule in §7 item 2, including the
  mutation reset) is **unacceptable** and is **not shown to the judge**. Counts per arm are printed. Reason: the live gate rejects
  an invalid tool call and forwards (P2.1 task 5, `check_applied`); an unusable reply is a failed serve.
* **Text JSON is not the native path (N3).** The proxy converts a served reply into the client's tool_use
  format; that schema conversion can fail where this text protocol parses. That failure is **unmeasured**
  here (§15).
* **Timeouts:** local 180 s, Claude 300 s; **one retry** after error, timeout, empty reply or
  served-model mismatch; the last attempt is final; every attempt kept. **Warm-up:** one discarded
  synthetic call before the first item and on every resume (AMEND2). **Resume (R3):** a parked item continues at the next attempt number, counting the non-final attempt rows already present, so no item ever has more than RETRIES+1 = 2 attempt rows; `run` prints the count of items with more (must be 0). Restarting at attempt 1 would give a candidate item up to three attempts, a candidate-favourable bias.
* **Latency** (wall ms per final attempt, local) is printed per stratum: median and p95, with the
  prompt-token count; report only. Under D-199 it is **load-dependent** (daytime runs) and is not
  comparable with P2.0's latency, which is also load-dependent. It does not gate; P2.1's live bar owns latency. A local timeout is
  charged to the candidate (§2). The harness records the quiet readings of each item's last local attempt,
  and `run` prints the count of local rows without a passing re-check (no readings, fewer than three, or any reading outside the gate), `items_run_after_failed_recheck`, which must be 0.

## 7. Outcome definition for a tech-op step

**Primary outcome: acceptable.** An arm's reply for an item is *acceptable* iff it is valid and not
redundant (§6) **and** the Opus judge marks it acceptable. Judge procedure, letters, caps, retries, blind order and the
10% order re-judge are P2.0 §7 unchanged (one call per item; the judge sees the request, the context
cut to the last 24,000 chars and every arm's reply, 4,000 chars each, arm names never shown).

*Brief, tech-op (replaces P2.0's one-liner; stated before any data):* a next action is acceptable if,
given the conversation and the tool output just returned, it is a reasonable step toward the user's
request: it uses a tool that can advance the request, its target is one the conversation points to or
a sensible neighbour of it, it does not repeat a call whose result is already in the conversation,
it does nothing destructive, and it does not stop (`final`) while the request is plainly unsatisfied.
A different but reasonable tool or argument is acceptable. A `final` is acceptable only if the request
is already satisfied.

**Edit-shaped replies (N3).** A valid reply naming Edit, Write, NotebookEdit or a mutating Bash command
is still judged (the primary outcome does not exclude it), but the **share of edit-shaped replies per arm**
is printed. The gate's served path rejects an edit-shaped reply (`edit_to_claude`, local_gate docstring),
so a lane that is eligible mostly through edit-shaped replies would over-state what would be served.
Printed beside it: the **served-subset rate**, the acceptable rate among items where the local reply is
not edit-shaped and is valid, with Wilson CI and the paired Sonnet rate on the same items. Printed, never
gated.

**Secondary outcomes (printed, with k/n and Wilson 95% CI, per stratum; never in `grade`):**
1. **Concordance with the organic action (C5).** Both are tool calls with the same tool name **and**
   the same normalised target (P2.1-a definition, P2.0 §7: first present of `file_path`/`path`
   `os.path.normpath`, `pattern` stripped, `command` whitespace-collapsed); both `final` agrees.
   Computed for every arm including Sonnet and Haiku, so the reference has its own non-trivial rate.
   Also printed: local-vs-Sonnet agreement (P2.1-a's form, exactly as in P2.0 §7).
2. **Redundant (N4; the offline re-read signal; GATING, §6).** A **deterministic pre-judge rule**,
   computed by the harness and **not shown to the judge**; a redundant reply scores unacceptable for every arm: a valid tool-call reply is *redundant* iff its tool name and
   its **full normalised input** (every input key, strings whitespace-collapsed, paths `os.path.normpath`)
   equal those of an earlier tool call in the **full** item history (all prior tool_use blocks, not a
   window), **unless** an Edit, Write, NotebookEdit or mutating Bash call intervened after that earlier
   call. **Mutating Bash** = `technical_ops.classify_tool_call("Bash", input)` does not return a read-only
   tech-op class (the function exists at `src/llm_router/technical_ops.py` on origin/feat/p20-requal and
   origin/main); an unresolvable Bash command (it returns None) is treated as mutating (the earlier result may be stale). Counts per arm (k/n, Wilson CI) and the local-minus-Sonnet
   difference (Newcombe CI) are printed. The rate itself is report-only; the unacceptable scoring is the
   gate. It is the offline analogue of P2.1 task 4 and of the gate's `redo` unit; the live post-serve re-read is P2.1-b's.
3. **Validity rate** and **timeout rate** per arm.
4. **Judge-independent anchor:** the judge-acceptable rate among items where the arm's action
   concords with the organic action (should be ~1; if < 0.90 the judge is flagged
   `judge_rejects_organic`, report only).

**Plants (calibration), per P2.0 §7 with one added kind.** The plant order sorts selected items by
`sha256("20261017:plant:" + id)`.
* *Swapped* (indices 0-5): the Sonnet reply of item (j+1) mod 6, as P2.0.
* *Degraded* (indices 6-11): `{"final": "Done."}`, as P2.0 tech-op.
* *Repeat* (indices 12-17, **new**): a valid call that re-issues the item's previous tool call
  verbatim (so the N4 rule marks it redundant). Skipped (and counted) when the item has no earlier tool call.
* *Generic* (indices 18-23, **new, N5c**): a valid, read-only call unrelated to the item (a fixed
  `{"tool": "Glob", "input": {"pattern": "**/*"}}`). It tests that the judge does not accept any valid
  call. Skipped (and counted) never; it is always placeable.
* **Pass rule per kind** (four kinds: swapped, degraded, repeat, generic), m placed: at least ceil(5/6 x m)
  judged unacceptable; the set is NOT INFORMATIVE (`calibration`) if any kind fails or places m < 4. Plants never change n and share the
  judge call (P2.0 §16 bias 13). **Plants bypass the validity and redundant pre-checks and are always
  shown to the judge** (F1); the pre-check would mark the repeat plant `redundant` (it re-issues the previous
  call), and that is why the bypass is stated: the plant tests the judge, not the pre-check. The repeat plant is the check that the judge is not rubber-stamping
  plausible-looking but pointless actions. [OPEN-4]

## 8. Metrics and statistics

Per (arm), per stratum and pooled: k, n, rate, **Wilson 95% CI**. Per local-vs-Sonnet: gap_pts and
the **Newcombe method-10 CI** (the gating LB); exact two-sided **McNemar** p on discordant pairs,
discordant rate d, the n implied for a 3-pt paired half-width (1.96^2 x d / 0.03^2); the **equal-rate
threshold** at the final paired n (smallest k in [ceil(n/2), n] such that equal counts grade eligible for
k and every larger k) and whether Sonnet's k reaches it (`powered`).

* **Session sensitivity (N8b):** leave-one-session-out min and max of gap_pts and the grade; flag
  `session_sensitive` if any leave-out flips `grade`. **The leave-out grade is computed with the n >= 60
  clause waived** (a leave-out set can fall below 60): `gap_pts <= 3.0` and `lb_pts >= -10.0` from
  `evidence_from_counts`. `session_sensitive` iff some leave-out fails those two tests while the full set
  passes them (F3). **A `session_sensitive` lane that grades `eligible`
  is downgraded to verdict `inconclusive` (reason `session_sensitive`)** (§10). Steps in one session are
  correlated, and the independent Newcombe LB ignores that; the 20% cap bounds, not removes, the effect.
* Every check prints the number of items it checked. A set whose reference-missing items (Sonnet reply
  not ok after retry, or judge error) exceed 10% of selected is NOT INFORMATIVE (`ref_missing`).
  A null/error share above 10% for the local arm is **not** a not-informative reason: it is the local
  arm's failure and is charged (§2).

## 9. Outputs

* `$REQ_T/result.json` (0600): every number in §8 and §7, the verdict, the cancel flag, the prereg /
  amend / selection / answers / judgements shas, git sha, input shas.
* `$REQ_T/local_eligibility.json` (0600): through `record_competence(model, "tech_op", {"simple": LANE})`
  only. LANE as P2.0 §9, with `basis: "all_technical_op_continuations"`, `source: "p2.0-t"`,
  `population: "replayed real steps (offline), strata org+ho"`, `strata: {org: {k,n}, ho: {k,n}}`.
  `grade(LANE)` must reproduce `LANE.grade`; a test checks every written lane. The gate additionally
  requires `eligible is True`, `verdict == "eligible"` and `grade == "eligible"`.
* `install-eligibility --confirm` merges the lane into `paths.state_path("local_eligibility.json")`
  only after `score` and an independent recompute; it never touches `fitness` or the `qa` / `code`
  lanes of the same model key (record_competence merges per task_type).
* `docs/measurements/p20t-techop.json` (repo): counts, verdicts and hashed item ids only (N11).

## 10. Verdicts and consequences

| Set status | `grade` | verdict |
|---|---|---|
| NOT INFORMATIVE: n < 60, < 5 sessions, calibration, ref_missing, incomplete | (`unknown`, n = 0 written) | `not_informative` |
| informative, `grade == eligible`, organic check passes (below) | `eligible` | `eligible` |
| informative, `grade == eligible`, organic check fails or `session_sensitive` | `eligible` -> **downgraded** | `inconclusive` (`org_disagrees`, `org_too_small` or `session_sensitive`) |
| informative, Sonnet k < equal-rate threshold at realised n (power-limited) | `failed` / `probe` | `inconclusive` |
| informative, powered, `grade == failed` | `failed` | `failed` |

* **Organic check [OPEN-2]:** with `org` n >= 20, the `org` stratum's own gap_pts (local minus
  Sonnet, same outcome) must be <= 10.0. `ho` alone cannot make a lane eligible. Reason: `ho` rows are a
  disclosed proxy population (D-131 b: "the organic stratum points the same way"). **The 10.0-pt bound
  is a coarse screen (N8a), not a parity test:** at `org` n of 20-40 its own interval is tens of points
  wide, so it can only catch a stratum that points clearly the other way; it cannot confirm parity
  within `org`, and the pooled `grade` remains the only gating rule.
* **Kill:** P2.1 is cancelled only if the set is informative and the lane is `failed` (powered).
  `not_informative` or `inconclusive` cancels nothing and gets a new prereg. `eligible` satisfies the
  P2.1 precondition (>= 1 eligible tech-op model) for this single model and this single build
  (`b3f4c17c6811`); it does not start P2.1: PREREG-V16, shadow agreement >= 90% (n >= 200) and the owner
  flip (plist) remain (PLAN §0.4b S4, D-195).
* The result feeds T_P2 (P2.0 §11): e_tech = this lane's rate on `eligible`, else 0.

## 11. Spend, windows and gates

* **Run window (D-204, replaces the D-199 clock block).** The local arm may run at **any** local time. There is
  no time-of-day block and no P2.0-unfinished test: the sibling P2.0 harness is frozen under its AMEND3
  (sha256 `3b3e981a5ebd99f251381f9f94f659d3ca74ab04cb91cef854164e064c5de741`), runs at any hour under the
  same quiet gate, and takes the **same** exclusive flock on `~/.rsi/research/primary-plan/v16/requal/ollama.lock`
  around every local call. Both harnesses use `qwen3.8-uncensored:latest`, so they interleave safely at call
  level. The quiet gate is re-checked **before every local call** (three readings 10 s apart); a failure
  parks (exit 3) and writes no row. Reason: owner instruction D-199 ("don't wait, you can do today"); the
  gate and the lock, not the clock, protect the measurement.
* **Quiet machine:** before the local arm and on every resume: three readings 10 s apart, each with
  free memory >= 40% (`memory_pressure -Q`) and load1 < 4.0, **no foreign model resident**
  (`/api/ps` lists none besides `qwen3.8-uncensored:latest`). Failure parks (exit 3), writes no row,
  never unloads a foreign model.
  * **Ollama lock (R2, R4):** the exclusive lock above is taken, non-blocking, around **every** local call
    (warm-up included), in the P2.0 AMEND3 order **lock, then quiet readings, then the call**; if it is held
    the run parks (exit 3) at that call. **P2.0-process check (fail closed, R6):** the run also parks if
    `ps` lists any `p20_requal.py` process, and if `ps` returns nothing or errors (it cannot rule P2.0 out).
    It over-parks on an editor or `grep` that names the file; that costs time, not validity.
  * **Unload (R1):** at the end of `run`, or when it parks, the harness unloads `qwen3.8-uncensored:latest`
    only if this process made a local call (the flag is set once a call has passed the gate), and does so
    inside the lock, non-blocking; if the lock is held it skips the unload silently. A park before any call
    unloads nothing.
* **Quota pause** before each Claude call: session >= 80%, weekly >= 85%, or `usage.json` missing or
  older than 30 min -> exit 3, resumable, nothing consumed (read-only read of `usage.json`, as P2.0).
* **Spend cap [OPEN-1, D-200]: $40.00** on a separate ledger `$REQ_T/ledger.jsonl` (sum of
  `total_cost_usd` + 1.25 x the most expensive call so far, $0.30 first call). A cap stop mid-set leaves the
  set incomplete, NOT INFORMATIVE (`incomplete`/`ref_missing`) by design; no partial verdict, no further
  tranche without a new prereg.
  * **Judge-cost reserve (N9b).** Before the **first Sonnet call**, the harness computes the projected
    judge cost (n judge calls + the 10% re-judge calls + plants, at 1.25 x the most expensive judge-sized
    call estimate; $0.30 per call until a real call exists) and reserves it. The Sonnet arm runs; **the Haiku
    arm is skipped** (not run, counted `haiku_skipped_budget`; a skipped Haiku arm counts as **complete** for
    the Order rule below, F6) if spent + reserve + the projected Haiku cost
    would exceed the cap. Haiku is report-only, so skipping it never changes the grade; the judge and the
    Sonnet reference are never starved to pay for it.
  * **No Claude call before the freeze except synthetic probes (N10).** The cost is **not** measured
    from the data here; the estimate is unmeasured (P2.0's $30-55 covered 240 Sonnet, 240 Haiku, 264 Opus
    judge calls and 75 code runs, and no per-call cost from it is quoted). Any pre-freeze cost probe uses
    synthetic prompts only (the AMEND1 flags). The cap is **fixed at the freeze** and does not change
    after the first real call. The separate ledger means P2.0's $60 and this cap are two caps; the
    worst-case total ($100) is disclosed, not hidden.
* **Timeouts:** §6. **Code gate (AMEND1):** `git diff --quiet <REPO_SHA> -- scripts src tests` succeeds,
  `git status --porcelain` empty, `llm_router.__file__` inside the run worktree, for every subcommand
  except `status` and `select --dry-run`; runs execute from the freeze commit.
* **Order:** freeze -> `select` -> `run --set tech_op` (arm-major: local first, then Sonnet, Haiku)
  -> `judge --set tech_op` -> `score` -> independent recompute -> `install-eligibility` -> `tp2`.
  `judge` refuses until every (item, arm) has a final answer, where a Haiku arm skipped for budget
  (`haiku_skipped_budget`) counts as having its final answers (F6, F8); `run` refuses once judging started.

## 12. Privacy

As P2.0 §14, restated only where the file differs (N11): the only arms are `qwen3.8-uncensored:latest`,
`sonnet`, `haiku` and the `opus` judge. `organic_action` and `served_model` exist **only in the 0600
pool** (and the harness's in-memory use of them); neither is printed nor written to any ledger, `use.log`,
`result.json` or the repo file, except as the counts and hashed ids named below. The repo measurement file
`docs/measurements/p20t-techop.json` holds **hashed item ids and counts only**. Prompt text, answers,
judge replies and the organic action exist only in `$REQ_T` (0600), the HTTP body to `localhost:11434` and the stdin of
Claude children. Child argv carries the model id and flags only. `ledger.jsonl`, `use.log`,
`result.json` and the repo measurement file hold ids, counts and hashes only; no subcommand prints text.
Nothing is written under `~/.llm-router` except `install-eligibility`; nothing under `~/.claude`
(AMEND1: `--no-session-persistence`). `usage.json` is read-only. The pool builder reads transcripts
under `~/.claude/projects` read-only, as P2.0's did.

## 13. Single use and the foreign-arm guard

* `score` appends `started <UTC> prereg_sha256=... amend_sha256=... git_sha=... selection_sha256=...
  answers_sha256=... judgements_sha256=...` to `$REQ_T/use.log` **before** parsing any input; it refuses
  if a `started` line exists. A crash during `score` consumes the use. A NOT INFORMATIVE set appends
  `terminal <UTC> tech_op <reason>`. No re-selection, no re-run of a final item, no re-judge (beyond the
  §7 order sample), no re-score without a new prereg.
* **Foreign-arm guard (AMEND2 §4):** the arm list is exactly `[qwen3.8-uncensored:latest, sonnet,
  haiku]`. `run`, `judge` and `score` refuse (exit 2, before any model call or write; `score` before its
  `started` line) if `answers/tech_op.jsonl` holds a row for any other arm. `status` prints
  `foreign_arm_rows` counts. Any answers from an earlier or other run directory are never copied in.
* **Judge-before-answers guard:** the judge never sees an arm's reply from another run directory.

## 14. Freeze procedure

1. Builder and harness changes merged and reviewed (§Harness changes); REPO_SHA = the last commit that
   touches `scripts/`, `src/` or `tests/`; the freeze commit touches only `docs/`.
2. Build step (read-only sub-agent, like P2.0 D-107 c) writes `$REQ_T/pools/tech_op.jsonl` and
   `build_manifest.json` (sha256 of every source and transcript read, the secret pattern list, the
   `census.py` pin, the proxy copy, the build time from `date -u`). `select --dry-run` prints counts.
3. **Floor rule (§4):** n >= 60, org n >= 20 and >= 5 sessions, else stop (not frozen).
4. Fill the `FILL-AT-FREEZE` constants (below); recompute every pin; `ollama list` ID equals `ID_*`;
   Claude model ids are probed with synthetic prompts only (AMEND1 flags).
5. Independent freeze-guard review (not the author; the verifier does not see the drafting
   conversation; it judges the file and its pins). READY TO FREEZE required.
5a. **Real-path precondition dry run (house rule, rv-561 r2).** Before the sidecar is written, the
   coordinator (not the harness author) runs, from a worktree at REPO_SHA, the harness's real
   `frozen()`, `run_gate` (including H13) and the `run` preconditions (`require_unused`,
   `require_no_foreign`, `check_set`, `items_for`, the identity check against `ollama list`, the spend
   reserve) on the real run directory `$REQ_T`, as far as and not including the first model call. It
   uses an uncommitted staging sidecar for this file with the fill-ins entered. It prints counts and
   exit codes only and makes no model, Ollama-generate or API call. `select --dry-run` is NOT this
   dry run (it takes the pre-freeze path). Any refusal voids the freeze until fixed; the staging
   sidecar is then deleted.
6. The frozen file is `docs/prereg/PREREG-P20T-techop.md` (code `PREREG_T`, `scripts/p20t_requal.py:36`), with
   its sidecar `PREREG-P20T-techop.sha256` beside it: `sha256sum PREREG-P20T-techop.md > PREREG-P20T-techop.sha256`, with `frozen_utc` from `date -u`
   taken at the freeze commit, and a byte-identical 0444 copy at `$REQ_T/PREREG.md`. `scripts/check_prereg.py`
   must report a match. `frozen()` checks the sidecar, the `frozen_utc` is later than P2.0's AMEND2
   `frozen_utc`, the constants block has exactly the keys below, and the 0444 copy matches.
7. After the freeze, any byte change is a new prereg (an AMEND file with its own sidecar, as AMEND1/2).

## 15. What this cannot show

1. **Not a live result.** Offline replay, warm, uncontended, with no door latency. Live behaviour is
   P2.1's 7-day shadow (n >= 200, agreement >= 90%) and its re-read bound; this is a precondition only.
2. **Proposed, not executed; text JSON is not the native path (N3).** The text protocol is not native
   tool calling, and the proxy's conversion of a served reply into the client's tool_use format is not
   exercised (a schema-conversion failure is unmeasured); edit-shaped replies are judged but the gate
   rejects them. The next action is judged,
   not run, so a wrong-but-plausible action's downstream cost (wasted turns, a bad edit) is not seen.
   One step only: a loop's compounding error across several local steps is not measured.
3. **"Acceptable" is not "same as Claude".** The primary outcome tolerates different reasonable actions.
   It cannot say the user's session ends as well; concordance with the organic action (secondary)
   shows exact-target agreement only and is not a quality label.
4. **One model, one build.** `b3f4c17c6811` (community Q6_K, think off). Not the vendor weights, not
   another quantisation, not thinking on (AMEND2 biases 15-17).
5. **Same-vendor judge.** Opus judges Sonnet, Haiku and a local model: self-preference can cause a false
   `failed`; it cannot cause a false `eligible` through preference, but a lenient judge could (hence
   the plants, §7). The judge's absolute leniency is only bounded by the plants.
6. **Reference is not truth.** Eligibility is "within 3 points of Sonnet", so a shared failure mode of both
   models is invisible. Sonnet at 100% would also show nothing about tasks that neither model can do.
7. **Population limits.** Sub-agent steps are dropped (branch A) or approximated (branch B, N1). Only steps whose transcript and `msg_id` were recoverable, whose bash class was
   resolvable offline, whose pack fits 32,768 tokens and that passed the secret screen. `ho` is a
   proxy population; steps lost to the unexplained `msg_id` loss (P2.0 §4: 301 untagged and 147 organic)
   are not represented and the loss may correlate with difficulty. Steps over the window are not a model
   of future usage (the user's own habits and the proxy's pin to Opus shaped what was recorded).
   `pack_over_window` under-counts (R7): `default_fits` sees only the newest user turn; the system prompt and tool
   schemas are absent; the gate forwards a step above 0.9 x window, so a kept step may be larger when served (§4, §16 item 26).
8. **Not a tier result.** One lane, `tech_op/simple`, pooled over whatever mix of steps the gate will
   see; no per-op-class (Grep vs Read vs Bash) eligibility. Per-class counts are printed, not graded
   (n too small per class for the LB clause).
9. **Power.** At n = 120 and Sonnet below 97/120 the grade cannot reach `eligible` even for an identical
   model (P2.0 Q1); that outcome is `inconclusive`, not evidence against.
10. **No condensing, no Explore lane, no re-read bound.** P2.1 tasks 3, 4, 6 and P2.1-b are separate.
11. **Item reuse.** After `score`, these items are spent for this model; later tech-op claims need fresh
    items (new prereg).

## 16. Known biases (stated before any data)

P2.0 §16 items 1-9, 13, 14 and AMEND2 items 15-19 apply (offline replay, unequal context 65,536 vs
32,768, same-family judge, implicit reference, truncation, proposed-not-executed, conservative LB,
plants share the call, hook-free Claude arms, community build, thinking off, single arm, timeout
pressure). Added:
20. **Organic action by a Claude model.** The stored organic action was produced by a Claude tier (often
    an Opus pin). Concordance favours Claude arms; it is secondary and report only.
21. **`ho` is not the organic population.** Headless and research sessions have different task mixes.
22. **Session correlation.** Steps within a session share context; the 20% cap and the leave-one-session-out
    report mitigate, not remove, the dependence.
23. **Recoverability selection.** Only steps with a findable transcript entry enter (C3 fix removes the
    known sub-agent loss, not the unexplained `msg_id` loss).
25. **Generic-plant calibration risk (F7).** The `generic` plant (a valid, unrelated read-only Glob) can look
    like a plausible next step on a request that is itself a broad file search; a judge that accepts it
    there is not necessarily lenient. The pass rule (>= 5 of 6 rejected) could fail an honest judge, and the
    set would be NOT INFORMATIVE (`calibration`) for it. That cost is accepted and disclosed.
24. **Thermal and daytime contention:** unmeasured, conservative (timeouts are charged to the candidate);
    latency is not comparable with P2.0's latency, which is also load-dependent.
26. **`pack_over_window` under-count (R7).** The builder's fit test sees the newest user turn only; the system
    prompt and tool schemas are not in a transcript, so some kept steps are larger at serve time than at
    build time, and the gate forwards (does not serve locally) a step above 0.9 x window. The qualified
    population is slightly larger-prompt than the served one is allowed to be; the direction favours the
    candidate only if long prompts are harder. Median and p90 `prompt_tokens_est` are printed by the dry run.
27. **Selection rule changed after seeing counts (d6, D-206).** The d5 rule (one hash order across strata)
    selected `org` 2 of 120 from a pool of org 32 / ho 679, below ORG_MIN_N. The rule was changed to
    org-first plus the gate-window filter after seeing pool and selection stratum counts only: no outcomes,
    no model output, no item text. Org-first over-represents `org` relative to the pool (every eligible
    org row is in, against a pool share of about 4.5%). This is intended: `org` is the population the
    gate serves. `ho` is graded separately and cannot make a lane eligible alone. The window filter drops
    rows the gate would not serve locally, but `prompt_tokens_est` (chars/4, newest-turn rebuild, no system
    prompt or tool schemas) can under-count, so a kept row can still exceed the gate's window at serve time (item 26).

## Harness changes needed (a separate build; nothing built under this draft)

Base: `scripts/p20_requal.py`, `scripts/p20_build_pools.py`, `scripts/p20_code_runner.py` (unused here)
and tests, branch `origin/feat/p20-requal`. All are code changes after the freeze-pin of REPO_SHA, so
each needs a test, an independent review and the AMEND-style pins.

| # | File | Change | Reason |
|---|---|---|---|
| H1 | `p20_build_pools.py` | **Do not require a tier for tech_op:** `tier()` falls back to `"unlabelled"`; strata `org` and `ho` (replace the `not_organic` exclusion with a stratum assignment on `session_kind`; keep `exec_session`) | C1, C2 |
| H2 | `p20_build_pools.py` `transcript()` | accept `isSidechain` entries when the file is a sub-agent transcript (under `<project>/<session>/**`); keep skipping them in the main transcript | C3 |
| H3 | `p20_build_pools.py` | store `organic_action` {tool, target} or {final}, and `served_model`; `pack_over_window` via the gate's estimator; counts by reason | §4 |
| H4 | `p20_requal.py` `select_sets` | tech_op branch: no `tier` filter, no `no_tier`/`tier_complex` reasons; d6: window filter (`over_gate_window`) then org-first, ho fills, one hash order within each stratum; >= 5 sessions informative check; `org` n >= 20 (census-exact stratum, N2); refuse if realised n != `N_REALISED_TECH_OP` | C1, §4, N7 |
| H5 | `p20_requal.py` constants | new run-dir (`t1`), delete `NIGHT_WINDOW` (d4: no run block either), the shared `ollama.lock`, per-call quiet gate and per-item quiet-reading record, `SELECT_SEED=20261017`, `TIERS_WRITTEN_TECH_OP=simple`, `ARMS = [qwen3.8-uncensored:latest, sonnet, haiku]`, `SPEND_CAP_USD=40.00`, separate ledger; `PLANTS_REPEAT=6`, `PLANTS_GENERIC=6` | §3, §7, §11 |
| H6 | `p20_requal.py` `score` / `lane` | write lane `basis: all_technical_op_continuations`, tier `simple` only for `tech_op`; `strata` and `org` check; `inconclusive` downgrade; keep `grade(lane)` self-check | C4, §10 |
| H7 | `p20_requal.py` run/judge | validity and redundant pre-checks (§6) before judge (both score unacceptable, not shown to the judge); repeat and generic plants (third and fourth kinds) and their pass rule; tech-op judge brief replaced (§7); `action_key` reused for the organic action | C5, C6 |
| H8 | `p20_requal.py` report | secondary outcomes (concordance, redundant repeat, validity, timeouts, latency), leave-one-session-out, equal-window sample (if [OPEN-6]) | §7, §8 |
| H9 | `check_prereg.py` / `frozen()` | accept this prereg's sidecar and constants block; require `frozen_utc` after P2.0 AMEND2; refuse `FILL-AT-FREEZE` | §14 |
| H10 | tests | one test per change; mutation check that each rule fails when removed; a test that an empty pool is not informative; that `grade(lane)` is reproduced for the written lane | prove-it rule |

| H11 | `p20_requal.py` / builder | **tech_op-only mode (N12):** a run directory with only the tech_op pool; missing `qa` and `code` pools count as `not_informative` and never block; `tp2` is not run, or runs from the P2.0 directory by sha, never mixing the two results | P2.0 harness assumes three sets |
| H12 | `select --dry-run` | prints per-stratum n, `org` under census-exact **and** under the row stamp, sessions, the equal-rate threshold at the dry-run n, and every drop count including `subagent_step` and `over_gate_window` per stratum (N6, N7, N12, S5) | pre-freeze decision |
| H13 | merge order | the gate branch (`feat/proxy-local-gate`) and #523 (`record_competence`) are both in before REPO_SHA is fixed; **REPO_SHA contains both** (N12). If the gate is not in, branch B of §4 (N1) applies | N1, N12 |
| H14 | builder, harness | N1 sub-agent test (branch A: import the gate's own; branch B: fallback counts with/without), N4 `redundant` pre-judge gate (scored unacceptable for every arm), N5 extraction/key rule and generic plant, N3 edit-shaped and served-subset prints, N8b downgrade, N9 reserve and Haiku skip, D-199 run window | d2 changes |

Not changed: `grade`, `record_competence` (already accepts `tech_op`/`simple`), the Claude child
hardening, the quiet and quota gates, `ollama` identity checks, the single-use log, the foreign-arm
guard (reused as is, with the arm list above).

Open items for the consumer of the lane (not this prereg's build): the gate reads
`tech_op/simple` only; it forwards when the file is missing; no plist change happens here.


## Fill-ins at the freeze (the only values that may change between draft and freeze)

```text P20T-CONSTANTS
LABEL=p20t-techop
SELECT_SEED=20261017
N_TARGET_TECH_OP=120
N_MIN=60
MIN_SESSIONS=5
SESSION_CAP_SHARE=0.20
ORG_MIN_N=20
GATE_WINDOW_FACTOR=0.9
ORG_MAX_GAP_PTS=10.0
LOCAL_MODEL=qwen3.8-uncensored:latest
ID_qwen3.8-uncensored:latest=b3f4c17c6811
REFERENCE=sonnet
REPORT_ARMS=haiku
JUDGE=opus
LOCAL_TIMEOUT_S=180
REF_TIMEOUT_S=300
RETRIES=1
REF_WINDOW=65536
ANSWER_CAP_CHARS=4000
JUDGE_CONTEXT_CAP_CHARS=24000
PLANTS_SWAPPED=6
PLANTS_DEGRADED=6
PLANTS_REPEAT=6
PLANTS_GENERIC=6
PLANTS_PASS_SHARE=5/6
PLANTS_MIN_PLACED=4
REJUDGE_SHARE=0.10
REJUDGE_FLAG_BELOW=0.80
MAX_REF_MISSING_SHARE=0.10
QUIET_FREE_MEM_MIN_PCT=40
QUIET_LOAD1_MAX=4.0
QUIET_READINGS=3
QUIET_INTERVAL_S=10
QUOTA_SESSION_PAUSE_PCT=80
QUOTA_WEEKLY_PAUSE_PCT=85
QUOTA_MAX_AGE_S=1800
SPEND_CAP_USD=40.00
JUDGE_RESERVE=1
FIRST_CALL_EST_USD=0.30
SAFETY_FACTOR_SPEND=1.25
WINDOW_SINCE=2026-09-28T00:00:00Z
TIERS_WRITTEN=simple
BASIS=all_technical_op_continuations
CENSUS_PY_SHA256=03941a4f9a8f49526eacd3ab3d116602da9a3667b2ec10c04c8f4e531d7c289a
CLAUDE_SONNET_ID=claude-sonnet-5-5
CLAUDE_HAIKU_ID=claude-haiku-5-5
CLAUDE_OPUS_ID=claude-opus-5-5
REPO_SHA=3496aea3ef3ca16d1b53fa9230956c8440bf136d
SUBAGENT_BRANCH=B
N_REALISED_TECH_OP_ROWSTAMP_ORG=22
WINDOW_UNTIL=2026-10-17T18:11:58Z
PROXY_CALLS_COPY_SHA256=8fd743bee724385f284d220934bf3e257489d2ecbf6806a1f0f3f5cbc33be3f5
BUILD_MANIFEST_SHA256=a19811e78b1e08d214e5da9f531d435489878ff61257d229d345f0076652d109
POOL_TECH_OP_SHA256=233b4ffe9f3b6456d62bd4a66caad671db4b3cd4014a0f454ac00910279b9f04
POOL_COUNT_TECH_OP_ORG=32
POOL_COUNT_TECH_OP_HO=679
N_REALISED_TECH_OP=120
N_REALISED_TECH_OP_ORG=21
N_REALISED_SESSIONS=52
```

`CENSUS_PY_SHA256`, the model ids and the AMEND2 identity pin are copied from P2.0/AMEND2 and are
re-checked at the freeze, not trusted. The harness parses this block, refuses while a value reads
`FILL-AT-FREEZE`, and refuses if a constant differs from its own copy. N targets, seeds, caps, bars and
the outcome definition may not change at the freeze.

## What was looked at before writing (code and prereg text only)

* `PREREG-P20-requal.md` (r9 with freeze fill-ins), `-AMEND1.md`, `-AMEND2.md` from `origin/feat/p20-requal`.
* PLAN-v16 §0.4b (S1-S9), P2.1 section (tasks 1-6, P2.1-a/b), D-195, D-196, D-123a/b, D-131, D-133a.
* Code: `proxy/steps.py` (`step_kind`, `step_fields`, `prev_tool_class`), `technical_ops.py`
  (`classify_tool_call`), `eligibility.py` (`grade`), `resolver/eligibility_store.py`
  (`record_competence`), `scripts/p20_requal.py` (`select_sets`, `action_key`, `agreement`, lane writer),
  `scripts/p20_build_pools.py` (tech-op branch, `transcript()`, secret screen), and `proxy/local_gate.py`
  on `feat/proxy-local-gate` (`lane_of`).
* **Not opened:** any pool, item, answer, judgement, transcript, p17d, gt500, door corpus or
  `~/.llm-router` / `~/.claude` / `~/.codex` file. No model was called, no process touched, no data
  measured. The counts in §0 are P2.0's published ones, not new measurements.
