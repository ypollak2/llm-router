# PREREG-P20-requal: local re-qualification with the full pack (PLAN v16 P2.0 tasks 1-5)

Status: **DRAFT r2, not frozen.** This file is frozen only when `PREREG-P20-requal.sha256` exists
beside it, an independent freeze-guard review has passed (D-122 rule: an author-written sidecar is
void), and `scripts/check_prereg.py` reports a match. Until then `scripts/p20_requal.py` refuses every
subcommand that writes an item, calls a model or writes a result. The one pre-freeze mode is
`select --dry-run`, which reads the pools and prints counts only (§4). Changing any byte after the
freeze changes the hash; a changed file is a new prereg with a later `frozen_utc`. No item has been
selected, no model has been called and nothing has been scored under this file.

r2 applies the freeze-guard review of r1 (NOT READY) and coordinator decisions D-123 / D-123a.

## 0. Contradictions and settled points (read first)

Every row is settled by D-123 (r1 defaults accepted) or D-123a (review fixes). None is open.

| # | Contradiction | Settled as (applied below) |
|---|---|---|
| Q1 | **The LB clause dominates; n = 60 cannot show parity below ~0.95.** `eligibility.grade` uses Newcombe method 10 (independent arms). For equal local and Sonnet rates (k = round(p·n), repo `grade`, 2026-10-16), the first passing n is: p = 0.95 → 60; 0.93 → 61; 0.90 → 75 (every n ≥ 79 passes); 0.85 → 101; 0.80 → 122 (every n ≥ 124); 0.70 → 160. At n = 60 the exact equal-rate threshold is 57/60 (0.95); 56/60 (0.933) fails at LB -10.1. At **n = 120** equal rates pass only when Sonnet ≥ 97/120 (0.808); at Sonnet ≤ 96/120 (0.80) even a local model identical to Sonnet fails. The LB ≥ -10 clause dominates gap ≤ 3 pts: at n = 60 no (k_cand, k_ref) pair fails on the gap alone; at n = 120 only 8 pairs do, all with Sonnet ≥ 115/120 (0.958). | D-123: **N = 120** target for Q&A and tech-op (60 is the floor); code takes all valid items (expected 75). D-123a(d): a set whose Sonnet rate is below the equal-rate threshold at its realised n is **INCONCLUSIVE** (power-limited), not evidence of ineligibility (§10). `grade` is unchanged; it is the one rule (P2.0-b). |
| Q2 | D-99 is "for G1 only"; the brief said "synthetic/held-out per D-99". | D-123: the plan's real sources, replayed **offline** (like D-52 / D-106), prompt-derived data 0600 under `$REQ`. Generated items form a labelled stratum. Nothing is labelled `synthetic (D-99)`. |
| Q3 | The plan names `$PP/v16/requal/PREREG.md` (0444); repo convention is `docs/prereg/` + `.sha256`. | D-123: the repo file is canonical. At the freeze a byte-identical 0444 copy goes to `$REQ/PREREG.md`; the harness refuses if that copy exists and differs. |
| Q4 | The plan names `resolver/profile.py`; `grade()` merged as `src/llm_router/eligibility.py` (#484). | D-123: `llm_router.eligibility.grade` is the rule. |
| Q5 | "`record_competence` is the only writer"; it did not exist. `record_fitness` writes only `{model}.fitness`. | D-123: `record_competence` is added to `resolver/eligibility_store` (same lock, keeps `fitness`). It refuses a lane whose stored grade differs from `grade(lane)`. |
| Q6 | Code set: bench_v2 (55 noncloud hidden-test tasks; never run; no Sonnet arm) + 4 eligible ctxfid code items = 59 < 60, and the rule needs a Sonnet rate. | D-123: code = noncloud 55 + the 20 [RX] `fixtures/edit-*` tasks = 75, hidden tests, local pinned runner, Sonnet arm added; ctxfid dropped (a different, judge-graded truth). |
| Q7 | Tech-op "agreement with Sonnet" gives Sonnet 100% by definition; `claude -p` cannot take tool schemas. | D-123: every arm (Sonnet included) is graded by the same blind judge; agreement is computed as in P2.1-a and printed, never gating. One text tool protocol for all arms. |
| Q8 | The output is per (task_type, tier), but 120 items per set cannot give 60 per tier. | Q&A and tech-op items with a complex tier are excluded (tier provenance per set, §4). The pooled set verdict is written to tiers `simple` and `moderate` (`basis: pooled_simple_moderate`); per-tier rates are printed only; `complex` / `deep_reasoning` are never written (absent = `unknown`). |
| Q9 | Installed models beyond the plan's three; `qwen3.5:latest` listed under two IDs. | D-123: the three plan models (§3). `qwen3.8-uncensored:latest` excluded (not the plan's qwen3.8, which needs an approved Ollama upgrade). `nimble:9b` excluded (`/api/show` capabilities `["decision"]` only; not adopted, D-119). Classifier, runner-alias and embedding models excluded. qwen3.5 pins both IDs; the ambiguity is disclosed. |
| Q10 | T_P2 inputs: GE1 gives only a bound s_tech ∈ [1/45, 24/45]; post-deploy census n = 0; the P1.1-estimator f_fit re-run is not done. | D-123 / D-123a(b, g): §11. s_tech and f_fit are measured on the same non-complex tech-op population as e_tech. The **gating** T_P2 uses the conservative s_tech = 1/45 unless an informative census exists; 24/45 is printed only. |
| Q11 | Spend. The plan budgets **P2.0 ≤ $10** (§5 effort table); this design needs more. | D-123 / D-123a(f): harness model calls are capped at **$60** via `claude -p` (subscription, quota-gated). **Disclosed deviation from the plan's ≤ $10.** The code runner is local and $0; its Sonnet runs go through `claude -p` and count against the same $60 ledger. Any paid external runner is an owner gate. The quota pause reads `usage.json` read-only, the same exception D-110 gave P1.7-d (disclosed). |
| Q12 | GT-500 held-out was used for the classifier family (P1.6-g, P1.7-d group G). | Rule 3 is per candidate family; local answerers are a new family. After this run the Q&A items are spent for local answerers; P3.2 needs fresh items. |
| Q13 | Kill vs power. The plan kills P2.1 / P2.10 when no local model is eligible for any class. | D-123a(d): §10 — kill only with ≥ 1 informative, adequately powered set in which every graded lane fails and no lane anywhere is eligible; all-NI / inconclusive → no kill, re-prereg. P2.1 still needs ≥ 1 eligible tech-op model. |
| Q14 | Fitness pass marks are "fixed" by P2.0-a; `fitness.PASS_MARKS` is frozen empty in code (#487). | §12 fixes them; the code change follows the freeze in its own PR. |
| Q15 | An Opus judge grades Sonnet and Haiku (same vendor) against local models. | Opus judge, blind letters, two plant kinds, an order re-judge sample (§7). Disclosed (§16): conservative for eligibility, but it can cause a false kill. |
| Q16 | D-21 is per (task_type, tier); the Q&A set pools the QA task types. | D-123a(c): a Q&A task type is re-enabled only if it has **≥ 20 items** in the graded set **and** the pooled lane is eligible; otherwise it stays stripped (§10). |

## 1. Authority

* PLAN-v16 P2.0 tasks 1-5, MUST P2.0-a; P2.0-b is met by #484 (`grade`).
* D-21 (recorded default), as refined by D-123a(c).
* D-29 = A: point gap ≤ 3 pts at n ≥ 60, Newcombe LB ≥ -10, CI printed. §1.4 rules 1-4, 8, 9, 10.
* D-27 = A: the gate uses T_P2 even below 20%; the inputs go to the owner first.
* D-22: the catalog takes quality scores only from P2.0 / fitness.
* L13 (single Ollama slot, quiet machine, night windows).
* G1 passed by D-121. D-123, D-123a.

## 2. Question and decision rule

For each local model m in §3 and each set s ∈ {`qa`, `tech_op`, `code`}: is m's acceptable rate on s
within 3 points of Sonnet's on the same items?

* **Rule:** `llm_router.eligibility.grade({"k_cand": k_m, "n_cand": n, "k_ref": k_sonnet, "n_ref": n})`.
  `eligible` iff n ≥ 60 and gap_pts ≤ 3.0 and lb_pts ≥ -10.0 (Newcombe method 10, inclusive,
  unrounded); `failed` at n ≥ 60 otherwise; `probe` at 0 < n < 60; `unknown` at n = 0.
* **Paired n (qa, tech_op):** the selected items that have a final reference verdict (§8). A local
  answer that errored, timed out or was empty after its retry is **unacceptable**: a failed serve
  is a failed offload.
* **Paired n (code):** per lane, the tasks where both the local arm and Sonnet have a non-null
  `hidden_pass` (§7).
* The **verdict** of a lane (§10) adds set-level power and informativeness to the grade.
* Haiku is graded against Sonnet the same way, **report only**; it is never written to
  `local_eligibility.json`.

## 3. Models (arms)

| Arm | Sets | Identity pin (checked before every `run`; mismatch = refuse, not a result) |
|---|---|---|
| `qwen3.6:35b-a3b-coding` | qa, tech_op, code | `ollama list` ID `017eda0b7c34`; `/api/show` gguf / Q4_K_M / llamacpp / 35.5B |
| `qwen3-coder:30b` | qa, tech_op, code | `ollama list` ID `06c1097efce0`; `/api/show` gguf / Q4_K_M / 30.5B (no runner field) |
| `qwen3.5:latest` | qa, tech_op | `ollama list` IDs {`2e16a80fe3d7`, `44a38603922e`}; `/api/show` gguf / Q4_K_M / llamacpp / 9.7B |
| `sonnet` (reference) | qa, tech_op, code | `claude -p --model <CLAUDE_SONNET_ID>` (fill-in: the full model id); a call whose served model differs is an error |
| `haiku` (report only) | qa, tech_op | `claude -p --model <CLAUDE_HAIKU_ID>` (fill-in), same check |
| `opus` (judge) | qa, tech_op | `claude -p --model <CLAUDE_OPUS_ID>` (fill-in), same check |

* qwen3.5 is left out of code: plan M3.5 benches the two coder models, and the [LA] priors for the
  9.7B model are weak. Its code lane stays absent (= `unknown`).
* Local options: `num_ctx = local_models.num_ctx(model)` (32,768 for all three today), temperature
  0, `keep_alive` 10m. One heavy model is resident at a time (L13). The harness runs arm-major and
  unloads only its **own** model (`keep_alive: 0`) after the arm. It never unloads or loads around
  a model another job holds.

## 4. Sets and item sources

Items are prompt-derived. They live only in `$REQ = ~/.rsi/research/primary-plan/v16/requal/`
(directory 0700, files 0600). A pre-freeze **build step** (read-only, run by a sub-agent, like
D-107 c) writes one pool file per set plus `$REQ/pools/build_manifest.json` with the source pins
below. The pool sha256 values, the per-stratum pool counts and the realised N per set are freeze
fill-ins. The build step reads no `~/.llm-router` file except a copy of `proxy_calls.jsonl` (its
sha256 is a fill-in) and never writes there.

| Set | Pool `$REQ/pools/<set>.jsonl` | Tier provenance | Filters | N |
|---|---|---|---|---|
| `qa` | Stratum `rx`: the 37 [RX] Q&A items graded in `routing-experiment-2026-10-01/results_p2b_qwen38.md` (`source` real or gen, both kept). Stratum `gt`: GE3 held-out ids from `gt500/split.json` (sha256 `8921a154…405b`, 234 ids), `kind == organic`, whose task class by the router's rules classifier at REPO_SHA is in `qa_policy.QA_TASK_TYPES`. | `rx`: the packet's `complexity` field. `gt`: `gt500/manifest.json` (sha256 `453ad8bd…4cc`) `tier_opus`: haiku → simple, sonnet → moderate, opus → complex. | drop `complex`; drop missing tier (`no_tier`); drop < 4 words (trivial acks, the [M013] lesson); drop requests matching the engine secret guard at REPO_SHA (`secret_pattern`) | **120** target, ≥ 60 |
| `tech_op` | Organic continuation steps from 2026-09-28T00:00Z (GE1 v0 start) to the build time (recorded in the manifest), in the `proxy_calls.jsonl` copy, that GE1 `census.py` (sha256 `03941a4f9a8f49526eacd3ab3d116602da9a3667b2ec10c04c8f4e531d7c289a`) classifies as `technical_op`. The request body is rebuilt from the session transcript up to that step: messages, the latest human ask, and tool names with one-line descriptions. The manifest lists each transcript file's sha256; its own sha256 is a fill-in. | The ledger row's `tier_complexity` (simple / moderate; complex and deep_reasoning → complex). | drop `complex`; drop missing tier; drop steps whose transcript cannot be found or rebuilt (by reason); drop secret-pattern hits | **120** target, ≥ 60 |
| `code` | Stratum `noncloud`: the 55 tasks of `classifier-v2-release/ab/work/noncloud/task_ids.json` (sha256 `e493f84f…2638`), hidden tests. Stratum `rx_edit`: the 20 `routing-experiment-2026-10-01/fixtures/edit-*` tasks, hidden tests. | `noncloud`: the [NC] truth tier of the task (`unlabelled` if absent). `rx_edit`: `unlabelled`. Code items are **not** filtered by tier (bench design fixed); the pooled code lane may include complex-tier tasks, which biases against local (§16). | a task the runner cannot check out at its base is dropped (by reason) | **all** (75), ≥ 60 |

**Pool row fields.** Every row has `id`, `set`, `stratum`, `source` (`real` or `gen`), `session`,
`tier` (`simple` / `moderate` / `complex` / `unlabelled`) and `task_type`. Q&A rows add `request`
and `context`; tech-op rows add `request`, `messages` and `tools`; code rows add `task_ref` only
(the runner owns the code). **Sessions:** a `gt` or `tech_op` row uses its real session id. A real
`rx` row uses its real session id if the packet has one, else `rx-real`. Every generated `rx` row
uses the single session `rx-gen` (one generation run), so the session cap bounds them. A code row
uses its stratum name as the session (the cap does not apply to code).

**Selection** (`select`, once). Rows are ordered by `sha256("20261016:" + id)`, with stratum `rx`
first for `qa`. The walk admits a row unless its session already holds `floor(0.20 × N_target)`
admitted rows (24 at N = 120), and stops at N_target. Code admits every valid row. `select` refuses
if the realised n of any set differs from its `N_REALISED_<SET>` fill-in (computed by
`select --dry-run` before the freeze). A set with n < 60 is **NOT INFORMATIVE** (terminal for that
set). Excluded counts are printed by reason; no text is printed.

**Leakage and prior outcomes (stated before any data).**
* No local model, prompt template or pack setting is tuned on any item here. Local weights are
  vendor weights. The pack builder (P1.1) was evaluated on its own synthetic set (PREREG-P11B).
* GT-500 held-out prompts were seen by **classifiers** (P1.6-g held-out, P1.7-d group G), never by
  an answering model's tuning. Their Opus-rubric tier labels are known in aggregate.
* `rx` items have **known prior outcomes**: 4/37 (qwen3.8 at the time) vs 34/37 (Sonnet) [RX]. Code
  `noncloud` tasks have known prior outcomes: qwen3-coder:30b 12/34 on Haiku-truth tasks [NC], and
  Codex gpt-5.5 25/55. Strata are printed separately.
* Tech-op items come from the same ledger window that feeds the census inputs s_tech and f_fit
  (§11). Those inputs are shares of the population, not quality, so the overlap does not leak a
  label. e_tech is measured on the items only.

## 5. Context: PACK (P1.1)

Every arm gets the context its live door would give it. Q&A: `context_pack.build_pack(request,
context=<item context>, door="mcp", project="", target_window=W)`. Tech-op: `build_pack(request,
messages=<rebuilt messages>, door="proxy", project="", query=<latest human ask>, target_window=W)`.
W = `local_models.num_ctx(model)` for local arms and 65,536 for Claude arms (D-101 cap). Local arms
receive `to_messages(pack)` through Ollama `/api/chat`. Claude arms receive the same messages
flattened into one prompt (`### system`, `### user`, `### assistant` headers) on **stdin**. This is
live-faithful, so Claude arms may see more context than local arms (§16).

## 6. Arm protocol

* **Q&A:** the request is answered as the next assistant turn.
* **Tech-op (text protocol):** the prompt lists the tools and asks for exactly one JSON object:
  `{"tool": <name>, "input": {...}}` for the next action, or `{"final": <text>}` if the request is
  already satisfied. The proposed action is judged; it is **not executed** (§16).
* **Timeouts:** local 180 s, Claude 300 s. **Retry:** one retry after an error, timeout, empty
  reply or served-model mismatch. The last attempt is final; every attempt is kept.
* **Warm-up:** one discarded synthetic call per local model before its first item, and on every
  resume.
* Claude children run with `LLM_ROUTER_SESSION_KIND=research` (rule 8) in a scratch cwd, with tools
  disabled and the real HOME (auth). They are never organic.
* **Code:** a **local, $0** runner pinned by sha256 (`CODE_RUNNER_SHA256`). The runner also pins
  the Pi agent version, its prompt (`run_codex.build_prompt` + `TEST_CONTRACT`) and the Claude
  invocation. It runs each (task, arm) in a clean checkout: Pi agent for local arms (as [NC] /
  M3.5) and `claude -p --model <CLAUDE_SONNET_ID>` with tools for Sonnet. The cap is 900 s. It uses
  the same quiet gate and night window (§13), and it appends its Claude costs to `$REQ/ledger.jsonl`
  (stage `code`) under the same $60 guard. It writes one row per (task, arm): `{task_id, arm,
  hidden_pass: true|false|null, wall_s, timed_out, patch_empty, ts}`. `hidden_pass = null` is
  reserved for **infrastructure** errors: checkout failure, runner crash, Ollama or CLI
  unreachable. A model timeout, an empty patch or a failing test is `false`. The harness only
  ingests these rows; it never executes model output.

## 7. Grading

* **Judge (qa, tech_op):** one call per item to the Opus judge. It sees the request, the context
  (cut to the last 24,000 characters) and every arm's final answer. Each answer is capped at 4,000
  characters after `<think>` blocks are removed; the number of truncated answers per arm is
  reported. Letters are assigned by sorting candidates on `sha256("20261016:" + item_id + ":" +
  name)`. Arm names are never shown.
* **Brief, Q&A:** an answer is *acceptable* if a competent senior engineer would accept it as the
  reply without asking again: it is correct, it addresses the actual ask, and it suggests nothing
  destructive. Length and style do not count.
* **Brief, tech-op:** a next action is *acceptable* if it is a reasonable next step toward the
  request given the conversation. A different but reasonable tool or argument is acceptable. A
  `final` is acceptable only if the request is already satisfied.
* **Reply:** JSON `{"<letter>": "acceptable" | "unacceptable", ...}` covering every letter. An
  invalid reply is retried once. Still invalid: the item is a judge error, and it leaves n for every
  arm.
* Errored local answers are not shown to the judge. They are unacceptable (§2).
* **Plants (calibration), per set.** The **plant order** sorts the selected items by
  `sha256("20261016:plant:" + id)`.
  * *Swapped:* items at plant-order indices 0-5 get one extra candidate, the Sonnet final answer of
    the item at index (j + 1) mod 6.
  * *Degraded:* among indices 6 onward, the first 6 items whose Sonnet final answer is ≥ 400
    characters get one extra candidate. For Q&A it is the first 20% of that answer's characters.
    For tech-op it is `{"final": "Done."}`.
  * A plant is skipped (and counted) when its source Sonnet answer is not ok.
  * The set counts only if **≥ 5 of 6 swapped** and **≥ 5 of 6 degraded** plants are judged
    unacceptable. Otherwise the set is NOT INFORMATIVE (`calibration`).
* **Order re-judge (report only):** 10% of selected items (the first ceil(0.10 · n) in
  `sha256("20261016:rejudge:" + id)` order) are judged a second time with letters from
  `sha256("20261016:perm2:" + item_id + ":" + name)`. The report prints the per-(item, arm)
  agreement k/n. Below 0.80 it is flagged `judge_order_sensitive` in the result; it does not gate.
* **Agreement (tech-op, printed, never gating; P2.1-a definition):** a local action agrees with
  Sonnet's when both are tool calls with the same tool name **and** the same normalised target.
  The target is the first present of `file_path` / `path` (`os.path.normpath`), `pattern`
  (stripped), or `command` (whitespace collapsed). Both `final` counts as agreement.
* **Code:** `hidden_pass == true` is acceptable, `false` is unacceptable. A `null` for either arm
  removes the task from that lane's pairs. A lane with null-removed tasks above 10% of the selected
  code tasks is NOT INFORMATIVE. Removed counts are printed per arm.

## 8. Metrics and statistics

Per (set, arm), the report prints: k, n, the rate with its Wilson 95% CI, and the per-stratum and
per-tier breakdown. Per (set, local model vs Sonnet), it prints:
* gap_pts and the Newcombe method-10 CI (the gating LB);
* the exact two-sided McNemar p on discordant pairs (b, c), and the discordant rate d = (b + c) / n;
* the n implied for a 3-pt paired half-width (1.96² × d / 0.03², rule 9);
* the equal-rate threshold at this n (the smallest k / n at and above which equal counts grade
  `eligible`), whether Sonnet's rate reaches it (`powered`), and `grade`;
* for Q&A, each local rate against the [M013] baseline **7/22** with its Newcombe CI (descriptive;
  not part of the rule);
* for tech-op, agreement with Sonnet (k/n).

Every check prints the number of items it checked. A qa / tech_op set with reference-missing items
(Sonnet final not ok, or judge error) above 10% of selected is NOT INFORMATIVE (`ref_missing`).

## 9. Outputs

* `$REQ/result.json` (0600): every number in §8, the lane verdicts, the kill flag, the D-21 table,
  the e_tech candidate, the prereg sha256, the git sha and the input shas.
* `$REQ/local_eligibility.json` (0600), written by `score` through `record_competence`. Keys are
  bare Ollama names, as `resource_map` reads them:

```json
{"qwen3-coder:30b": {
  "qa": {"simple": LANE, "moderate": LANE},
  "tech_op": {"simple": LANE, "moderate": LANE},
  "code": {"simple": LANE, "moderate": LANE},
  "fitness": {"...": "kept as written by record_fitness"}}}
```

  `LANE = {"grade", "verdict", "eligible", "rate", "ci", "n", "k", "gap_pts", "lb_pts", "ub_pts",
  "ref": {"model": "sonnet", "k", "n", "rate", "ci"}, "powered", "basis": "pooled_simple_moderate",
  "source": "p2.0-requal", "prereg_sha256", "scored_utc", "population"}`. The plan's `{eligible,
  rate, ci, n}` is a subset. `eligible` is `verdict == "eligible"`. `grade(LANE)` reproduces
  `LANE.grade`; `record_competence` refuses otherwise, and a test checks it for every written lane.
* `install-eligibility --confirm` merges the run's lanes into `paths.state_path("local_eligibility.json")`.
  It runs only after `score` and after the independent recompute, and it never writes `fitness`.
* `docs/measurements/p20-requal.json` (repo): counts, verdicts and item ids only.

## 10. Verdicts, kill rule, D-21 table

**Lane verdict:**

| Set status | `grade` | verdict |
|---|---|---|
| NOT INFORMATIVE (n < 60, calibration, ref_missing, incomplete, null share) | (`unknown`, n = 0 written) | `not_informative` |
| informative | `eligible` | `eligible` |
| informative, Sonnet rate < equal-rate threshold at realised n (**power-limited**) | `failed` / `probe` | `inconclusive` |
| informative, powered | `failed` | `failed` |

For code, "powered" uses each lane's own paired n.

* **Kill:** fires only if (a) no lane in any set is `eligible`, **and** (b) at least one set is
  informative and powered, and every graded lane in it is `failed`. Then P2.1 and P2.10 are
  cancelled, v16 continues with Haiku / very-cheap offload, and D-27 is re-asked (plan text).
* **No kill, re-prereg:** if no lane is `eligible` and every set is `not_informative` or
  `inconclusive`, the outcome is "no decision". There is no kill, and a new prereg (larger n or a
  different design) is written before any further run.
* **P2.1** proceeds only with ≥ 1 `eligible` tech-op lane. Otherwise e_tech = 0 (§11), and P2.1
  local tech-op serving does not start, whatever happens to the kill.
* **D-21 table (D-123a c):** a Q&A task type t is re-enabled for local at tiers simple and moderate
  for model m only if m's qa lane is `eligible` **and** the graded qa set holds **≥ 20 items** of
  type t. Every other type stays stripped. The table lists each type's item count. It is reported;
  the routing change itself is P2.10's.

## 11. T_P2 and g_min

Population: **non-complex technical-op steps** (tier ≠ complex by `tier_complexity`, as in the
tech-op set), so that s_tech, f_fit and e_tech describe one population. Window **W14** = the 14
days ending at this prereg's `frozen_utc` (absolute; `--since/--until`).

```
T_P2    = 0.8 × (s_tech × f_fit × e_tech + s_cheap)          ceiling = s_tech × f_fit × e_tech + s_cheap
s_tech  = k/n: organic non-complex technical_op actions / all organic actions in W14 (GE1 census.py).
          Informative iff n ≥ 60, ≥ 3 sessions and largest session ≤ 0.60.
f_fit   = k/n: of those steps, the share whose pack fits the e_tech model's num_ctx (census re-run
          with the P1.1 estimator, W14). Required: without it `tp2` refuses.
e_tech  = k/n on `tech_op` of the eligible local model with the highest rate (ties: the smaller
          model). No eligible model → 0.
s_cheap = k/n: actions served local / very-cheap and not redone (P1.9 `kpi --json .offload.action`)
          over the REMAINING actions in W14, i.e. all organic actions minus the non-complex
          technical_op steps of s_tech (no double count). n = 0 → 0, flagged.
```

* **Which value gates:** the gating T_P2 uses the census s_tech if it is informative. Otherwise
  it uses the conservative **s_tech = 1/45** (GE1 v0 lower bound), flagged. The value at 24/45 is
  printed beside it and never gates.
* The plan's formula adds s_cheap (a share of remaining actions) to a share of all actions. This
  overstates the total by a factor up to 1/(1 − s_tech) on the s_cheap term. It is kept as
  written and disclosed.
* All arithmetic is exact (fractions), never rounded before comparison. `tp2` records each input
  as k/n, the sha256 of each evidence file, the flags and both T_P2 values. The reviewer recomputes
  the inputs from raw files; then the coordinator puts the `tp2.json` sha256 in PREREG-V16 /
  PROGRESS.
* T_P2 < 0.20 → the inputs go to the owner before P2.1 (D-27 = A: gate on T_P2). ceiling < 0.10 →
  the release notes say that 60% has no supported path on this machine.
* **g_min = 5.0 points**, fixed now. P2-G-1 passes only if offload ≥ max(T_P2, s_cheap_baseline +
  0.05) **and** Newcombe LB(P2 − baseline) > 0. Reason: at P2-G-1's minimum of 300 actions per arm,
  LB > 0 needs a gain of 1.7 / 2.0 / 2.7 / 3.3 / 4.3 / 5.0 / 5.7 points at baselines 0 / 1 / 3 / 6 /
  15 / 24 / 30 out of 300 (computed 2026-10-16). 5 points is at or above that bar for every
  baseline up to 24/300 (8%; today's per-call value is 0.2% [K]). It is also a quarter of the 20%
  aspiration.

## 12. Fitness pass marks (P2.8 fitness tasks)

Every one of the six `resolver/fitness.TASKS` gets the mark **1.0**: `tech_step`, `summary`,
`edit_with_test`, `json`, `hebrew_qa`, `code_explanation`. Each is one synthetic, deterministic
smoke task, so a miss is a fail. With mark 1.0 there is **no flake margin**: one bad sample fails
the task. Fitness never grants eligibility; it only gates onboarding recommendations (P2.8).

## 13. Run rule, spend and stop

* **Order:** freeze → `select` → `run --set qa` / `run --set tech_op` (arm-major) → code runner
  (local) → `judge --set qa` / `judge --set tech_op` → `ingest-code` → `score` → independent
  recompute → `install-eligibility` → `tp2`. `judge` refuses until every (item, arm) of its set
  has a final answer. `run` refuses once that set's judging has started.
* **Night window (L13):** local arms and the code runner run only between 00:00 and 07:00 local
  time. Outside it, `run` parks (exit 3) before any local call. Each window is logged in PROGRESS.
* **Quiet machine (L13)**, before each local arm and on every resume:
  * three readings 10 s apart, each with free memory ≥ 40% (`memory_pressure -Q`) and load1 < 4.0;
  * **no foreign model resident** (`/api/ps` lists none besides the arm's own model).
  * Failure parks the run (exit 3) and writes no row. The harness never unloads a foreign model.
* **Quota pause** before each Claude call: session ≥ 80%, weekly ≥ 85%, or `usage.json` missing or
  older than 30 minutes → exit 3, resumable; no call is made and nothing is consumed.
* **Spend guard:** the sum of `total_cost_usd` in `$REQ/ledger.jsonl` (all stages, the code runner
  included), plus an estimate of the next call (1.25 × the most expensive call so far; $0.30 for
  the first), may not exceed **$60.00**. A stop keeps what exists. The estimate is about $30-55:
  240 Sonnet, 240 Haiku, 264 Opus judge calls including re-judges, and 75 Sonnet code runs.
  Disclosed deviation from the plan's P2.0 ≤ $10 (D-123a f).

## 14. Privacy

* Prompt text, answers and judge replies exist only in these places:
  * `$REQ` (0600);
  * the HTTP body sent to `localhost:11434`;
  * the **stdin** of the Claude children.
* Child argv carries only the model id and flags. These are visible in `ps`; no text is.
* `ledger.jsonl`, `use.log`, `result.json` and the repo measurement file hold ids, counts and
  hashes only. No subcommand prints text.
* The harness writes nothing under `~/.llm-router` except through `install-eligibility`, and
  nothing under `~/.claude`. It reads `~/.llm-router/usage.json` read-only for the quota pause.

## 15. Single use

`score` appends `started <UTC> prereg_sha256=… git_sha=… selection_sha256=… answers_sha256=…
judgements_sha256=… code_sha256=…` to `$REQ/use.log` **before** it parses any input. It refuses if
the log already holds a `started` line. A crash during `score` consumes the use. A NOT INFORMATIVE
set appends `terminal <UTC> <set> <reason>`, which stops that set. No re-selection, no re-run of a
final item, no re-judge (beyond the §7 order sample) and no re-score happens without a new prereg.

## 16. Known biases (stated before any data)

1. **Offline replay.** Warm, uncontended, with no live door latency. Not a live result.
2. **Unequal context.** Claude arms may get a larger pack (65,536 vs 32,768). This is
   live-faithful but not equal input.
3. **Same-family judge.** The judge is Opus, the same vendor family as the reference; self-identifying
   phrases are not removed.
   * Self-preference favours Sonnet, so it is conservative for eligibility (it cannot create a
     false `eligible`).
   * It can push lanes to `failed` and so cause a **false kill**.
4. **Implicit reference.** One call judges all arms together, so Sonnet's answer acts as a
   reference inside the call. The judgement is comparative, not absolute.
5. **Truncation.** The 4,000-character answer cap can cut a correct long answer. It is the same for
   every arm, and truncated counts are printed.
6. **Proposed, not executed.** The text tool protocol does not measure native tool calling, and the
   proposed action is judged without being executed (P2.1 shadow agreement measures live steps).
7. **Generated items.** The `rx` stratum includes generated (`gen`) items; strata are printed.
8. **Rubric tiers.** GT-500 tiers are Opus-rubric labels (κ with outcome 0.096); they are used only
   to drop complex items.
9. **Conservative LB.** Independent Newcombe is conservative for paired arms; the paired McNemar
   is printed beside it.
10. **Code harness confound.** Local arms run in the Pi agent; Sonnet runs in Claude Code
    (`claude -p`). The comparison is model + harness, not model alone. The code set also mixes
    agentic hidden-test tasks with single-file edits, and the pooled lane may include
    complex-tier tasks; strata and tiers are printed.
11. **Runner errors.** Infrastructure errors (`hidden_pass = null`) are removed per task, not
    charged to the arm. Model timeouts and empty patches are charged to the arm.
12. **Prior outcomes.** `rx` and `noncloud` outcomes are known in aggregate (§4).

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
SIZE_ORDER=qwen3.5:latest,qwen3-coder:30b,qwen3.6:35b-a3b-coding
REFERENCE=sonnet
REPORT_ARMS=haiku
JUDGE=opus
REF_WINDOW=65536
LOCAL_TIMEOUT_S=180
REF_TIMEOUT_S=300
RETRIES=1
ANSWER_CAP_CHARS=4000
JUDGE_CONTEXT_CAP_CHARS=24000
PLANTS_SWAPPED=6
PLANTS_DEGRADED=6
PLANTS_MIN_REJECTED=5
DEGRADED_MIN_CHARS=400
DEGRADED_KEEP_SHARE=0.20
REJUDGE_SHARE=0.10
REJUDGE_FLAG_BELOW=0.80
MAX_REF_MISSING_SHARE=0.10
QA_TYPE_MIN_ITEMS=20
QUIET_FREE_MEM_MIN_PCT=40
QUIET_LOAD1_MAX=4.0
QUIET_READINGS=3
QUIET_INTERVAL_S=10
NIGHT_WINDOW=00:00-07:00
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
CENSUS_PY_SHA256=03941a4f9a8f49526eacd3ab3d116602da9a3667b2ec10c04c8f4e531d7c289a
GT_SPLIT_SHA256=8921a1549e458c8e648b8070ce84a8b97a1b9f0b055c99d6e932bcbc3ca5405b
GT_MANIFEST_SHA256=453ad8bd93782dcf4457715a37f98c20d3d234f4eded95ce988885f5a464e4cc
NONCLOUD_TASK_IDS_SHA256=e493f84f5f5ddba32311e6f9903783982834aaff41cbbf4fcddeca4a7ee92638
REPO_SHA=FILL-AT-FREEZE
CLAUDE_SONNET_ID=FILL-AT-FREEZE
CLAUDE_HAIKU_ID=FILL-AT-FREEZE
CLAUDE_OPUS_ID=FILL-AT-FREEZE
PROXY_CALLS_COPY_SHA256=FILL-AT-FREEZE
BUILD_MANIFEST_SHA256=FILL-AT-FREEZE
POOL_QA_SHA256=FILL-AT-FREEZE
POOL_TECH_OP_SHA256=FILL-AT-FREEZE
POOL_CODE_SHA256=FILL-AT-FREEZE
CODE_RUNNER_SHA256=FILL-AT-FREEZE
POOL_COUNT_QA_RX=FILL-AT-FREEZE
POOL_COUNT_QA_GT=FILL-AT-FREEZE
POOL_COUNT_TECH_OP=FILL-AT-FREEZE
POOL_COUNT_CODE_NONCLOUD=FILL-AT-FREEZE
POOL_COUNT_CODE_RX_EDIT=FILL-AT-FREEZE
N_REALISED_QA=FILL-AT-FREEZE
N_REALISED_TECH_OP=FILL-AT-FREEZE
N_REALISED_CODE=FILL-AT-FREEZE
```

The harness parses this block from the frozen file. It refuses while any value reads
`FILL-AT-FREEZE`, and it refuses if a constant differs from its own copy. Only the `FILL-AT-FREEZE`
values may change at the freeze. The build manifest (pinned) records the transcript file sha256s and
the secret-guard patterns used, so the pool itself is the pin for item content. The build-step
exclusion counts by reason are added under "What was looked at". N targets, seeds, caps, bars,
models and g_min may not change.

## What was looked at before writing (counts and metadata only)

* `ollama list` and `/api/show` details (format, quantisation, runner, parameter size,
  capabilities) for the installed models; Ollama 0.40.0.
* GE1.json criteria (bounds and n only); `gt500/split.json` and `manifest.json` (counts, fields,
  sha256); `ctxfid/supply.json` (counts); [RX] packet counts and fields; noncloud `task_ids.json`
  (structure, sha256) and `summary.md` header; `proxy_calls.jsonl` `step_class` × `session_kind`
  counts; `usage.json` field names and scale.
* `grade` evaluated on synthetic counts for Q1, §10 and §11 (author and reviewer computations
  agree after r2).
* No prompt, answer or label text was read.

## Freeze record

Not frozen.
