# PREREG-P20-requal: local re-qualification with the full pack (PLAN v16 P2.0 tasks 1-5)

Status: **DRAFT, not frozen.** This file is frozen only when `PREREG-P20-requal.sha256` exists beside
it and `scripts/check_prereg.py` reports a match. Until then `scripts/p20_requal.py` refuses every
subcommand that reads items, calls a model or writes a result. Changing any byte after the freeze
changes the hash; a changed file is a new prereg with a later `frozen_utc`. No item has been
selected, no model has been called and nothing has been scored under this file.

## 0. Contradictions and open questions (read first)

The owner asked the coordinator to decide these. Each line states the default this draft already
applies ("coordinator default"). The freeze-guard review checks them before the freeze.

| # | Contradiction or question | Coordinator default (applied below) |
|---|---|---|
| Q1 | **n = 60 cannot show parity at mid rates.** `eligibility.grade` uses Newcombe method 10 (independent arms). At n = 60, a local model with *exactly* Sonnet's rate fails LB ≥ -10 unless both rates are ≥ ~0.93. Equal rates pass at n = 90 for p = 0.90, at n = 120 for p = 0.85, at n = 150 for p = 0.80, at n = 180 for p = 0.70 (computed with `grade` on equal k/n, 2026-10-16). At n = 60 and Sonnet 48/60, local needs 51/60, i.e. it must beat Sonnet. | Target **N = 120** per set for Q&A and tech-op (the MUST's "60 per set" is a minimum). N ≥ 60 is still required. Code takes every valid item (expected 75). The report prints the equal-rate pass threshold at the realised n. `grade` is not changed: it is the one rule (P2.0-b). |
| Q2 | **D-99 is "for G1 only".** P2.0 is a P2 item, and the brief says "synthetic/held-out per D-99". | The plan's real sources, replayed **offline** (like D-52 / D-106). Prompt-derived data is kept 0600 under `$REQ`. Generated items are a labelled stratum. Nothing is labelled `synthetic (D-99)` unless the owner extends D-99 to P2.0. |
| Q3 | **Prereg location.** The plan says `$PP/v16/requal/PREREG.md` (0444); repo convention is `docs/prereg/` + `.sha256`. | The repo file is canonical (`check_prereg.py`). At the freeze a byte-identical 0444 copy goes to `$REQ/PREREG.md`; the harness refuses if that copy exists and differs. |
| Q4 | **Task 6 location.** The plan names `resolver/profile.py`; `grade()` merged as `src/llm_router/eligibility.py` (#484). | `llm_router.eligibility.grade` is the rule. No `profile.py`. |
| Q5 | **"`record_competence` is the only writer."** It did not exist; `resolver/eligibility_store.record_fitness` writes only `{model}.fitness`. | `record_competence` is added to `eligibility_store` (same lock, keeps `fitness`). It refuses a lane whose stored grade differs from `grade(lane)`. |
| Q6 | **Code set is one short and has no reference arm.** Plan: bench_v2 (55 noncloud hidden-test tasks; never run; its 110 runs are 55 × 2 local models, no Sonnet) + ctxfid code items (4 eligible, `ctxfid/supply.json` 2026-10-08). 55 + 4 = 59 < 60, and the rule needs a Sonnet rate. | Code = noncloud 55 (stratum A) + the 20 [RX] Layer-C `fixtures/edit-*` tasks (stratum B) = 75, both graded by hidden tests in a pinned external runner. Sonnet is added as the reference arm. The 4 ctxfid items are left out (a different, judge-graded truth). |
| Q7 | **Tech-op reference.** "Tool-call agreement with Sonnet" gives Sonnet 100% by definition, so no gap can be computed. Sonnet is reachable only through `claude -p`, which cannot take arbitrary tool schemas. | Every arm (Sonnet included) is graded by the same blind judge: acceptable / unacceptable next action. Agreement with Sonnet is reported, not gated. All arms use one **text protocol** (tool list in the prompt, JSON reply), not native tool calls. |
| Q8 | **Tiers.** The output is per (task_type, tier), but 60 items per set cannot give 60 per tier. | Q&A and tech-op items with a complex / Opus tier label are excluded. The pooled set verdict is written to tiers `simple` and `moderate` with `basis: pooled_simple_moderate`; per-tier rates are printed only. `complex` and `deep_reasoning` are never written (absent = `unknown`). |
| Q9 | **Models.** Plan: qwen3.6, qwen3-coder:30b, qwen3.5. Installed (Ollama 0.40.0, `ollama list` 2026-10-16): also `qwen3.8-uncensored:latest` (community GGUF), `nimble:9b`, `llmr-classifier`, `llamacpp:<digest>` aliases, `nomic-embed-text`. `qwen3.5:latest` is listed twice (IDs `2e16a80fe3d7`, `44a38603922e`). | §3: the three plan models. `qwen3.8-uncensored` is excluded: it is not the plan's qwen3.8, which needs an approved Ollama upgrade. `nimble:9b` is excluded: `/api/show` capabilities are `["decision"]` only, and it was not adopted (D-119). Its identity follows `eval/PREREG-v2-amend2-errata1.md` and is irrelevant here. Classifier, runner-alias and embedding models are excluded. qwen3.5 pins **both** IDs; the ambiguity is disclosed. |
| Q10 | **T_P2 inputs are weak.** GE1 gives only a bound: s_tech ∈ [1/45, 24/45] = [0.022, 0.533] (Bash never stored before #314; post-deploy criterion n = 0). The f_fit re-run on the P1.1 estimator has not been done. s_cheap needs a P1.9 window. | §11: s_tech from a post-deploy census only if it is informative; otherwise the lower bound 1/45, flagged, and T_P2 is also printed at 24/45. Because P2-G-1 gates on max(T_P2, baseline + g_min), a low T_P2 cannot pass a regression. |
| Q11 | **Spend and quota.** No owner spend approval exists for P2.0. The quota pause needs a read of `~/.llm-router/usage.json`, which D-110 allowed for P1.7-d only. | Proposed hard cap **$60** for the harness's own calls (§13). Code-runner spend is capped by the runner at **$40** (fill-in). **Owner approval of spend is required before `run`**; this is not a coordinator default. The D-110-style read-only exception is assumed for P2.0 and is disclosed. |
| Q12 | **Held-out reuse.** GT-500 held-out was used for the classifier family (P1.6-g, P1.7-d group G). | Rule 3 is per candidate family. Local answerers are a new family, so the use is allowed. After this run the Q&A items are spent for local answerers; P3.2 frozen evals need fresh items. |
| Q13 | **Kill vs P2.1.** The plan kills P2.1/P2.10 only when no local model is eligible for any class. | Kill as written. In addition, with no eligible tech-op model, e_tech = 0, so the tech-op lever in T_P2 is 0 (printed). P2.1 then rests on its own bars. |
| Q14 | **Fitness pass marks** are "fixed" by P2.0-a, but `fitness.PASS_MARKS` is frozen empty in code (#487). | §12 fixes them here. The code change follows the freeze in its own PR, so code points at a frozen file. |
| Q15 | **Judge family.** An Opus judge grades Sonnet and Haiku (same vendor) against local models. | Opus judge, blind letters, swap-answer plants (§7). The bias is disclosed (§16). There is no second judge (cost). |

## 1. Authority

* PLAN-v16 P2.0 tasks 1-5, MUST P2.0-a; P2.0-b is met by #484 (`grade`).
* D-21 (recorded default): re-enable local Q&A per (task_type, tier) only where P2.0 passed (gap ≤ 3
  pts, LB ≥ -10, n ≥ 60).
* D-29 = A: point gap ≤ 3 pts at n ≥ 60, Newcombe LB ≥ -10, CI printed. §1.4 rules 1-4, 8, 9, 10.
* D-27 = A: the gate uses T_P2 even when it is below 20%; the inputs are reported to the owner first.
* D-22: the catalog takes quality scores only from P2.0 / fitness.
* G1 passed by D-121. D-76 freeze lifted.

## 2. Question and decision rule

For each local model m in §3 and each set s ∈ {`qa`, `tech_op`, `code`}: is m's acceptable rate on s
within 3 points of Sonnet's on the same items?

* **Rule:** `llm_router.eligibility.grade({"k_cand": k_m, "n_cand": n, "k_ref": k_sonnet, "n_ref": n})`.
  `eligible` iff n ≥ 60 and gap_pts ≤ 3.0 and lb_pts ≥ -10.0 (Newcombe method 10, inclusive,
  unrounded). `failed` at n ≥ 60 otherwise. `probe` at 0 < n < 60. `unknown` at n = 0.
* **Paired n:** the items with a final reference verdict. A local answer that errored, timed out or
  was empty after its retry is **unacceptable**: a failed serve is a failed offload.
* Haiku is graded against Sonnet the same way, **report only**. It is never written to
  `local_eligibility.json`.

## 3. Models (arms)

| Arm | Sets | Identity pin (checked before every `run`; mismatch = refuse, not a result) |
|---|---|---|
| `qwen3.6:35b-a3b-coding` | qa, tech_op, code | `ollama list` ID `017eda0b7c34`; `/api/show` gguf / Q4_K_M / llamacpp / 35.5B |
| `qwen3-coder:30b` | qa, tech_op, code | `ollama list` ID `06c1097efce0`; `/api/show` gguf / Q4_K_M / 30.5B (no runner field) |
| `qwen3.5:latest` | qa, tech_op | `ollama list` IDs {`2e16a80fe3d7`, `44a38603922e`}; `/api/show` gguf / Q4_K_M / llamacpp / 9.7B |
| `sonnet` (reference) | qa, tech_op, code | `claude -p --model sonnet`; the served model id is recorded per call |
| `haiku` (report only) | qa, tech_op | `claude -p --model haiku` |
| `opus` (judge) | qa, tech_op | `claude -p --model opus` |

* qwen3.5 is left out of code: plan M3.5 benches the two coder models; [LA] priors for the 9.7B
  model are weak. Its code lane stays absent (= `unknown`).
* Local options: `num_ctx = local_models.num_ctx(model)` (32,768 for all three today), temperature
  0, `keep_alive` 10m. One heavy model is resident at a time: the harness runs arm-major and unloads
  its **own** model (`keep_alive: 0`) before the next arm. It never touches a foreign model.

## 4. Sets and item sources

Item sources are prompt-derived. They live only in `$REQ = ~/.rsi/research/primary-plan/v16/requal/`
(directory 0700, files 0600). A pre-freeze **build step** (read-only, run by a sub-agent, like
D-107 c) writes one pool file per set. Each pool's sha256 is a freeze fill-in. The build step reads
no `~/.llm-router` file except the proxy ledger copy named below, and never writes there.

| Set | Pool `$REQ/pools/<set>.jsonl` | Filters | N |
|---|---|---|---|
| `qa` | stratum `rx`: the 37 [RX] Q&A items graded in `routing-experiment-2026-10-01/results_p2b_qwen38.md` (`source` real or gen, kept). Stratum `gt`: GE3 held-out items (`gt500/split.json` heldout ids, 234), `kind == organic`, whose task class by the router's rules classifier at REPO_SHA is in `qa_policy.QA_TASK_TYPES`. | drop tier `complex`; drop < 4 words (trivial acks, the [M013] lesson); drop secret-pattern hits | **120** target, ≥ 60 |
| `tech_op` | Organic continuation steps from 2026-09-28T00:00Z (GE1 v0 start) to the build time (recorded at the freeze), in a copy of `proxy_calls.jsonl`, that GE1 `census.py` (sha256 `03941a4f…`) classifies as `technical_op`. The request body is rebuilt from the session transcript up to that step: messages, the latest human ask, and tool names with one-line descriptions. | drop tier `complex`; drop steps whose transcript cannot be found or rebuilt (counted by reason) | **120** target, ≥ 60 |
| `code` | Stratum `noncloud`: the 55 tasks of `classifier-v2-release/ab/work/noncloud/` (hidden tests). Stratum `rx_edit`: the 20 `routing-experiment-2026-10-01/fixtures/edit-*` tasks (hidden tests). | a task the runner cannot check out at its base is dropped (counted) | **all** (75), ≥ 60 |

**Pool row fields.** Every row has `id`, `set`, `stratum`, `source` (`real` or `gen`), `session`,
`tier` (`simple` / `moderate` / `complex`) and `task_type`. Q&A rows add `request` and `context`.
Tech-op rows add `request`, `messages` and `tools`. Code rows add `task_ref` only; the runner owns
the code.

**Selection** (`select`, once). Rows are ordered by `sha256("20261016:" + id)`, with the `rx`
stratum first for `qa`. The harness walks this order and admits a row unless its session already
holds `floor(0.20 × N_target)` admitted rows (24 at N = 120). It stops at N_target. Code admits
every valid row. A set with n < 60 after selection is **NOT INFORMATIVE** (terminal for that set).
Excluded counts are printed by reason; no text is printed.

## 5. Context: PACK (P1.1)

Every arm gets the context its live door would give it. Q&A: `context_pack.build_pack(request,
context=<item context>, door="mcp", project="", target_window=W)`. Tech-op: `build_pack(request,
messages=<rebuilt messages>, door="proxy", project="", query=<latest human ask>, target_window=W)`.
W = `local_models.num_ctx(model)` for local arms and 65,536 for Claude arms (D-101 cap). Local arms
receive `to_messages(pack)` through Ollama `/api/chat`. Claude arms receive the same messages
flattened into one prompt (`### system`, `### user`, `### assistant` headers). This is
live-faithful, so Claude arms may see more context than local arms (§16).

## 6. Arm protocol

* **Q&A:** the request is answered as the next assistant turn.
* **Tech-op (text protocol, Q7):** the prompt lists the tools and asks for exactly one JSON object:
  `{"tool": <name>, "input": {...}}` for the next action, or `{"final": <text>}` if the request is
  already satisfied.
* **Timeouts:** local 180 s, Claude 300 s. **Retry:** one retry after an error, timeout or empty
  reply. The last attempt is final; every attempt is kept.
* **Warm-up:** one discarded synthetic call per local model before its first item, and on every
  resume.
* Claude children run with `LLM_ROUTER_SESSION_KIND=research` (rule 8) in a scratch cwd, with tools
  disabled and the real HOME (auth). They are never organic.
* **Code:** the pinned external runner (fill-in `CODE_RUNNER_SHA256`) runs each (task, arm) in a
  clean checkout: Pi agent for local arms (as [NC] / M3.5) and `claude -p --model sonnet` with
  tools for Sonnet. The cap is 900 s, and the runner applies the same quiet gate. It writes one JSONL
  row per (task, arm): `{task_id, arm, hidden_pass: true|false|null, wall_s, timed_out,
  patch_empty, ts}`. `hidden_pass = null` means a runner error. The harness only ingests these rows;
  it never executes model output.

## 7. Grading

* **Judge (qa, tech_op):** one `claude -p --model opus` call per item. It sees the request, the
  context (capped at 24,000 characters, oldest cut first) and every arm's final answer. Each answer
  is capped at 4,000 characters and has `<think>` blocks removed. Answers carry letters shuffled by
  `sha256("20261016:" + item_id + ":" + arm)`. Arm names are never shown.
* **Brief, Q&A:** an answer is *acceptable* if a competent senior engineer would accept it as the
  reply without asking again: it is correct, it addresses the actual ask, and it suggests nothing
  destructive. Length and style do not count.
* **Brief, tech-op:** a next action is *acceptable* if it is a reasonable next step toward the
  request given the conversation. A different but reasonable tool or argument is acceptable. A
  `final` is acceptable only if the request is already satisfied.
* **Reply:** JSON `{"<letter>": "acceptable" | "unacceptable", ...}` covering every letter. An
  invalid reply is retried once. Still invalid: the item is a judge error, and it leaves n for every
  arm.
* **Plants (calibration):** 6 items per set, chosen by the seeded order, get one extra letter: the
  Sonnet final answer of the next item in that order (a swapped answer). The set's verdicts count
  only if **≥ 5 of 6** plants are judged unacceptable. Otherwise the set is NOT INFORMATIVE.
* Errored local answers are not shown to the judge. They are unacceptable (§2).
* **Code:** `hidden_pass == true` is acceptable. `null` for a local arm is unacceptable. `null` for
  Sonnet removes the item from n.

## 8. Metrics and statistics

Per (set, arm), the report prints: k, n, the rate with its Wilson 95% CI, and the per-stratum and
per-tier breakdown. Per (set, local model vs Sonnet), it prints: gap_pts, the Newcombe method-10 CI
(the gating LB), the exact two-sided McNemar p on discordant pairs (b, c), the discordant rate
d = (b + c) / n, the n implied for a 3-pt paired half-width (1.96² × d / 0.03², rule 9), the equal-rate
pass threshold at this n, and `grade`. For Q&A it also prints each local rate against the [M013]
baseline **7/22**, with its Newcombe CI (descriptive; the baseline is not part of the rule). Every
check prints the number of items it checked. A set with reference-missing items (Sonnet final
error, Sonnet code null, judge error) above 10% of selected is NOT INFORMATIVE. Its lanes are
written as `unknown` with the reason.

## 9. Outputs

* `$REQ/result.json` (0600): every number in §8, the kill flag, the D-21 table, the prereg sha256,
  the git sha and the input shas.
* `$REQ/local_eligibility.json` (0600), written by `score` through `record_competence`. Schema
  (keys are bare Ollama names, as `resource_map` reads them):

```json
{"qwen3-coder:30b": {
  "qa": {"simple": LANE, "moderate": LANE},
  "tech_op": {"simple": LANE, "moderate": LANE},
  "code": {"simple": LANE, "moderate": LANE},
  "fitness": {"...": "kept as written by record_fitness"}}}
```

  `LANE = {"grade", "eligible", "rate", "ci", "n", "k", "gap_pts", "lb_pts", "ub_pts", "ref":
  {"model": "sonnet", "k", "n", "rate", "ci"}, "basis": "pooled_simple_moderate", "source":
  "p2.0-requal", "prereg_sha256", "scored_utc", "population"}`. The plan's `{eligible, rate, ci,
  n}` is a subset. `grade(LANE)` reproduces `LANE.grade`; `record_competence` refuses otherwise.
* `install-eligibility --confirm` merges the run's lanes into `paths.state_path("local_eligibility.json")`.
  It runs only after `score` and after the independent recompute; it never writes `fitness`.
* `docs/measurements/p20-requal.json` (repo): counts, grades and item ids only.

## 10. Kill rule and D-21 table

* **Kill:** no local model has an `eligible` lane in any set → P2.1 and P2.10 are cancelled, v16
  continues with Haiku / very-cheap offload, and D-27 is re-asked (plan text, unchanged).
* **D-21 table:** for each (model, qa lane) that is `eligible`, the action is "re-enable local for
  Q&A at tiers simple and moderate". The per-QA-type item counts are printed; a type with 0 items in
  the set stays stripped. Every other lane: "unchanged". The table is reported; the routing change
  itself is P2.10's.

## 11. T_P2 and g_min

```
T_P2    = 0.8 × (s_tech × f_fit × e_tech + s_cheap)          ceiling = s_tech × f_fit × e_tech + s_cheap
s_tech  = GE1 census, organic technical_op share, Wilson point, absolute window = the 14 days before
          the tp2 run. Informative iff n ≥ 60, ≥ 3 sessions, largest session ≤ 0.60. Otherwise
          s_tech = 1/45 (the GE1 v0 lower bound), flagged, and T_P2 is also printed at 24/45.
f_fit   = census re-run with the P1.1 estimator over the same window (pack fits the e_tech model's
          num_ctx). Required: without it `tp2` refuses (there is no proxy fallback).
e_tech  = acceptable rate (k/n point) on `tech_op` of the eligible local model with the highest
          rate (ties: the smaller model). No eligible model → 0.
s_cheap = P1.9 `llm-router kpi --json` `.offload.action` k/n over the same absolute window.
          n = 0 → 0, flagged.
```

* Computed exactly (fractions), never rounded before comparison. `tp2` records each input as k/n,
  the sha256 of each evidence file, the flags and both T_P2 values. The reviewer recomputes the
  inputs from raw files; then the coordinator puts the `tp2.json` sha256 in PREREG-V16 / PROGRESS.
* T_P2 < 0.20 → the inputs go to the owner before P2.1 (D-27 = A: gate on T_P2). ceiling < 0.10 →
  the release notes say that 60% has no supported path on this machine.
* **g_min = 5.0 points**, fixed now. P2-G-1 passes only if offload ≥ max(T_P2, s_cheap_baseline +
  0.05) **and** Newcombe LB(P2 − baseline) > 0. Reason: at P2-G-1's minimum (300 actions per arm),
  LB > 0 needs a gain of 1.7 / 2.0 / 2.7 / 3.3 / 4.3 / 5.7 points at baselines 0 / 1 / 3 / 6 / 15 /
  30 out of 300 (computed 2026-10-16). 5 points is at or above that bar for every baseline up to
  24/300 (8%; today's per-call value is 0.2% [K]), and it is a quarter of the 20% aspiration.

## 12. Fitness pass marks (P2.8 fitness tasks)

Every one of the six `resolver/fitness.TASKS` gets the mark **1.0**: `tech_step`, `summary`,
`edit_with_test`, `json`, `hebrew_qa`, `code_explanation`. Each is one synthetic, deterministic
smoke task, so a miss is a fail. Fitness never grants eligibility; it only gates onboarding
recommendations (P2.8).

## 13. Run rule, spend and stop

* **Order:** freeze → `select` → `run --set qa` / `run --set tech_op` (arm-major) → code runner
  (external) → `judge --set qa` / `judge --set tech_op` → `ingest-code` → `score` → independent
  recompute → `install-eligibility` → `tp2`. `judge` refuses until every (item, arm) of its set has
  a final answer. `run` refuses once that set's judging has started.
* **Quiet gate** before each arm and on every resume: free memory ≥ 40% (`memory_pressure -Q`) and
  load1 < 4.0. Foreign resident models are listed, never unloaded. A failure parks the run (exit 3)
  and writes no row.
* **Quota pause** before each Claude call: session ≥ 80%, weekly ≥ 85%, or `usage.json` missing or
  older than 30 minutes → exit 3, resumable; no call is made and nothing is consumed.
* **Spend guard:** the sum of `total_cost_usd` in `$REQ/ledger.jsonl`, plus an estimate of the next
  call (1.25 × the most expensive call so far; $0.30 for the first), may not exceed **$60.00**. A
  stop keeps what exists. The estimate is about $25-45: 240 Sonnet calls, 240 Haiku calls and 240
  Opus judge calls.
* Bench windows: local runs between 00:00 and 07:00 or on an idle machine; each window is logged
  in PROGRESS.

## 14. Privacy

Prompt text, answers and judge replies exist only in `$REQ` (0600), in the HTTP body sent to
`localhost:11434`, and in the argv / stdin of the Claude children. `ledger.jsonl`, `use.log`,
`result.json` and the repo measurement file hold ids, counts and hashes only. No subcommand prints
text. The harness writes nothing under `~/.llm-router` except through `install-eligibility`, and
nothing under `~/.claude`.

## 15. Single use

`score` appends `started <UTC> prereg_sha256=… git_sha=… selection_sha256=… answers_sha256=…
judgements_sha256=… code_sha256=…` to `$REQ/use.log` **before** it parses any input. It refuses if
the log already holds a `started` or `terminal` line. A crash during `score` consumes the use. A
NOT INFORMATIVE set appends `terminal <UTC> <set> <reason>` and stops that set. No re-selection,
re-run of a final item, re-judge or re-score happens without a new prereg.

## 16. Known biases (stated before any data)

1. Replayed offline: warm, uncontended, no live door latency. The result is not a live result.
2. Claude arms may get a larger pack (65,536 vs 32,768), which is live-faithful but not equal input.
3. The judge is Opus, the same vendor family as the reference arms; self-identifying phrases are not
   removed.
4. The text tool protocol does not measure native tool calling (P2.1 shadow agreement does).
5. The `rx` stratum includes generated (`gen`) items; strata are printed separately.
6. GT-500 held-out labels are Opus-rubric tiers (κ with outcome 0.096); tiers are used only to drop
   complex items.
7. Independent Newcombe is conservative for paired arms; the paired McNemar is printed beside it.
8. The code set mixes agentic hidden-test tasks with single-file edits; the strata are printed.

## Fill-ins at the freeze (the only values that may change between draft and freeze)

```text P20-CONSTANTS
LABEL=p20-requal
SELECT_SEED=20261016
N_TARGET_QA=120
N_TARGET_TECH_OP=120
N_MIN=60
SESSION_CAP_SHARE=0.20
LOCAL_MODELS=qwen3.6:35b-a3b-coding,qwen3-coder:30b,qwen3.5:latest
CODE_LOCAL_MODELS=qwen3.6:35b-a3b-coding,qwen3-coder:30b
REFERENCE=sonnet
REPORT_ARMS=haiku
JUDGE=opus
REF_WINDOW=65536
LOCAL_TIMEOUT_S=180
REF_TIMEOUT_S=300
RETRIES=1
ANSWER_CAP_CHARS=4000
JUDGE_CONTEXT_CAP_CHARS=24000
PLANTS_PER_SET=6
PLANTS_MIN_REJECTED=5
MAX_REF_MISSING_SHARE=0.10
QUIET_FREE_MEM_MIN_PCT=40
QUIET_LOAD1_MAX=4.0
QUOTA_SESSION_PAUSE_PCT=80
QUOTA_WEEKLY_PAUSE_PCT=85
QUOTA_MAX_AGE_S=1800
SPEND_CAP_USD=60.00
FIRST_CALL_EST_USD=0.30
SAFETY_FACTOR=0.8
G_MIN_PTS=5.0
TP2_REPORT_BELOW=0.20
CEILING_NOTE_BELOW=0.10
S_TECH_MIN_N=60
S_TECH_MIN_SESSIONS=3
S_TECH_MAX_SESSION_SHARE=0.60
S_TECH_FALLBACK=1/45
S_TECH_FALLBACK_HIGH=24/45
TIERS_WRITTEN=simple,moderate
QA_BASELINE=7/22
FITNESS_PASS_MARK=1.0
ID_qwen3.6:35b-a3b-coding=017eda0b7c34
ID_qwen3-coder:30b=06c1097efce0
ID_qwen3.5:latest=2e16a80fe3d7,44a38603922e
REPO_SHA=FILL-AT-FREEZE
POOL_QA_SHA256=FILL-AT-FREEZE
POOL_TECH_OP_SHA256=FILL-AT-FREEZE
POOL_CODE_SHA256=FILL-AT-FREEZE
CODE_RUNNER_SHA256=FILL-AT-FREEZE
```

The harness parses this block from the frozen file. It refuses while any value reads
`FILL-AT-FREEZE`, and it refuses if a constant differs from its own copy. The fill-ins are
`REPO_SHA` and the four sha256 values; the build-step counts (per set and stratum, excluded by
reason) are added under "What was looked at". N targets, seeds, caps, bars, models and g_min may not
change at the freeze.

## What was looked at before writing (counts and metadata only)

* `ollama list` and `/api/show` details (format, quantisation, runner, parameter size,
  capabilities) for the installed models; Ollama 0.40.0.
* GE1.json criteria (bounds and n only); `gt500/split.json` and `manifest.json` (counts, fields);
  `ctxfid/supply.json` (counts); [RX] packet counts and fields; `proxy_calls.jsonl` `step_class` ×
  `session_kind` counts. No prompt, answer or label text was read.
* `grade` evaluated on synthetic counts for Q1 and §11.

## Freeze record

Not frozen.
