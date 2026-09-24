# Plan — fixing the 2026-09-24 audit gaps

Status: **v2 — approved by owner 2026-09-24; executing.** Decisions: D2 re-enable · D3 drop · D5/D6 queue + independent judge · D7 no (point 2 parked) · banner reword no (point 15 parked). D1, D4 = owner web-UI actions.
Source: [AUDIT-2026-09-24.md](AUDIT-2026-09-24.md).
v1 was drafted by a planning pass. An independent reviewer then re-read the cited code, and four
of v1's root causes turned out wrong or incomplete; each correction below is marked **(v2)**.
The v2 corrections were re-checked by hand at `benchmarks.py:177`, `judge.py:60-66`,
`hooks/savings_logger.py:472`, `savings.py:77` and `lineage/types.py:83`.

| # | Fix | Points | Size | Risk | PR |
|---|---|---|---|---|---|
| 1 | `summary` split + labels + brand; session-end counts only realized | 6, 8, 12, 13 | M | low | 5 |
| 2 | `status`: "unverified" = not used; primary metric; `$` labelled | 8, 12 | M/L | medium | 6 |
| 3 | Judge actually writes rows; "quality" → "liveness" | 7, 9 | M | medium | 3 |
| 4 | Classifier regression gate + fail-loud answer-quality bench | 7 | M | low | 4 |
| 5 | PyPI trusted publisher; PRs enforced on `main` | 20 | S | low | owner |
| 6 | `control_plane/` | 10 | — | — | **parked by owner** |
| 7 | Benchmark fetch becomes opt-in | 5 | S | low | 2 |
| 8 | `BENCHMARKS.md` "every Monday" | 11 | S | low | 1 |
| — | Q15 negative result published | 19 | S | none | 1 |

---

## 1. `llm-router summary` and session-end

**Cause.**
- `observability/summary.py:collect()` (115-260) sums every `lineage_store.recent()` row (`savings_usd = baseline - total`, :222) with no realized filter.
- The wordmark is `"⚡ C H U Z O M ⚡"` (`summary.py:46`; also `banner_art.txt:2` → `banner.py:112`).
- Session-end's "quota preserved" (`hooks/session-end.py:716-721`) aggregates `paid_rows` (:2110 → 498-517) with no `mode` filter, so discarded `mode:"echo"` drafts count alongside realized `mode:"block"` rows.

**(v2) Trap.** `savings.is_verified_saving` compares `str(timestamp) >= "2026-09-13T17:57:16"` (`savings.py:77`), but lineage rows carry an **epoch float** (`lineage/types.py:83`). `"1…" < "2…"`, so reusing it as-is would mark *every* lineage row unverified: a silent zero, the S9 failure.

**Change.**
- Give `collect()` a lineage-aware predicate: convert the epoch to ISO UTC before the gate, and apply the same host and realized conditions.
- A row with no usable timestamp or mode is counted as **unmeasured**, separately from both verified and unverified.
- Render through `label_money` / `unverified_note`: verified first, never a bare-$ headline.
- Change the wordmark to llm-router in both files.
- In session-end, filter to `mode == "block"`.

**Tests.**
- `test_lineage_epoch_row_after_gate_can_be_verified`: a real epoch float must be able to count as verified.
  - Red-check: delete only the epoch→ISO conversion; the test must fail.
- `test_quota_preserved_excludes_echo_rows`.
  - Red-check: delete only the `mode == "block"` filter.
- `test_summary_wordmark_is_llm_router`: asserts the value, not the source text.

**Accept.**
- A live `llm-router summary` shows verified and unverified figures, each with n, and no "CHUZOM".
- The verified n equals `savings-report`'s n for the same window, or the difference is explained.

## 2. `llm-router status`

**Cause.** `dashboard_data.query_window()` (293-447) builds the verified/unverified split on `_production_pred()` (48-56), which is a test-traffic filter. Only the `savings_stats` branch (406-424) uses `savings.savings_split_sql`.

**Change.**
- `_production_pred` becomes only a drop filter. Legacy tables have no "used" column, so they count entirely as unverified.
- Verified figures come from `savings_stats` only.
- Add the primary-metric line: `verified n / eligible n` for `host='claude_code'`.
- **(v2)** Route every `$` in status through `label_money` (point 12).

**Tests.**
- `test_status_unverified_means_not_used`.
  - Red-check: restore only the line that reuses `_production_pred` as the verified axis.
- `test_status_money_is_labelled`: under a subscription, no bare `$` appears without its counterfactual label.

## 3. The judge writes no rows

**Cause (three parts; v2 adds c).**
- a. `hooks/savings_logger.py:437-459` omits `response=`, so `cost.py:1910` (`if success and response`) never fires on this path.
- b. `judge.py:104` hardcodes a paid `claude-haiku-4-5`. With no key, `call_llm` raises inside litellm, and `except Exception: pass` (118-120) swallows it.
- c. **(v2), probably dominant.** `evaluate_response_async` only *creates* a task (`judge.py:60`). The hook runs inside `asyncio.run(_persist())` (`savings_logger.py:472`), which cancels pending tasks on return. The judge is killed mid-flight whatever the model or key; the drain helper is test-only.

**Change.**
- Pass `response=`.
- Run the judge where it can finish: either await it with a bounded timeout inside `_persist()`, within the hook wall-clock budget, or queue the (prompt, response, id) and grade it later in a long-lived process. The choice is **D6**.
- Record failures via `failopen.record("CHZ-FO-JUDGE-EVAL", exc)`.
- **(v2)** The judge model must differ from the model that answered. Never let a local model grade itself; if no independent judge is available, record "ungraded", never a score (**D5**).
- Rename "quality" → "liveness" for `success_rate` and the bandit reward.

**Tests.**
- `test_judge_task_completes_under_asyncio_run`: a fake judge that awaits once must write its score.
  - Red-check: remove only the await/drain.
- `test_judge_failure_is_recorded`.
  - Red-check: delete only the `failopen.record` line.
- `test_judge_never_grades_its_own_model`.
- `test_bandit_reward_not_called_quality`: an AST/docstring check on the named symbols, not a repo-wide grep.

**Accept.** After a day of real traffic, `select count(*) from routing_decisions where judge_score is not null` is greater than 0. Report it with its n and the share graded.

## 4. Classifier regression gate + answer-quality bench (renamed in v2)

**(v2) Correction.** `classify_signals()` only labels task type and complexity. It never produces or scores an answer, so it cannot be the "quality" gate that point 7 asks for. CLAUDE.md also records that real traffic has **zero** mechanically gradable prompts, so any labelled set is hand-authored and must say so.

**Change.**
- **Mandatory classifier regression gate** (`scripts/release/classifier_gate.py`): a pinned, hand-labelled set, disclosed as such in its header, that fails below its baseline. Offline and fast.
- **Answer-quality bench** (`bench_backend_quality.py`, pinned subset): when Ollama is absent, the release **fails loudly** unless it is run with `--skip-quality "<reason>"`, and the reason is written into the release notes. It never skips silently.

**Tests.** Mutate one classify rule condition: the classifier gate fails. Run with Ollama unavailable and no flag: `pre-release-verify.sh` exits non-zero.

## 5. PyPI publish and PRs to `main`

**(v2) Correction.** CLAUDE.md says `release.sh` is not the release path, and it hard-exits at step 4. The 45 direct commits were ordinary pushes, allowed by owner bypass on `main`. Patching `release.sh` would fix nothing.

**Change.**
- **D1:** register the PyPI trusted publisher (`ypollak2/llm-router`, `publish.yml`, env `pypi`).
- **D4:** turn off admin bypass on `main`, so a PR is actually required. This blocks the rest of item 5.
- Delete `release.sh`'s dead publish step, or the whole script, so nobody relies on it.

**Accept.** The next tag's publish log shows OIDC success with no 403, and `pip install llm-routing==<new>` works. A direct `git push origin main` is rejected.

## 6. `control_plane/` — parked (owner, 2026-09-24).

## 7. Session-start benchmark fetch becomes opt-in

**(v2) Correction.** The fetched copy is **not** informational. `benchmarks.py:177` prefers `~/.llm-router/benchmarks.json` over the packaged file, and scoring reads it (`cost.py`, `dynamic_routing.py`, `profiles.py`, `scorer.py`).

**Change.**
- Guard the fetch with `LLM_ROUTER_AUTO_BENCHMARK_FETCH=1`, default off.
- Because a previously fetched file would otherwise win forever, prefer the **newer** of the installed and packaged copies by their embedded date.

**Tests.**
- With the env var unset, no `Popen` call.
  - Red-check: delete only the guard.
- With a stale installed file and a newer packaged file, the packaged file is used.

## 8. `docs/BENCHMARKS.md`

**Cause.** `benchmarks.yml` is `disabled_manually`. Its last run was 2026-07-06; all 18 runs were green. The doc claims weekly updates and reads "last updated 2026-03-30".

**Change.** **D2:** re-enable the workflow, or state the real update policy and last-run date.

## Small

- **Q15 (point 19):** add a CHANGELOG bullet and a `docs/BACKEND-QUALITY.md` line for `7b393fe`: delegated plan+hard runs passed 0/21 at 0.98× Claude tokens.

## Not covered: needs an explicit park or a fix (v2)

| Point | Gap | Proposed |
|---|---|---|
| 2 | Subscription mode needs `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true` | Installer sets it when it detects a Claude seat (S) |
| 4 | Capability registry `with_capability()` has no caller | Park; wiring it into routing is its own design |
| 15 | "🎯 routed →" stderr banner prints for every draft | Reword to "draft offered →" (S) |

## PR sequence (smallest risk first)

1. Text only: wordmark, BENCHMARKS.md (per D2), Q15 note, stderr banner wording
2. Benchmark fetch opt-in + newest-copy preference
3. Judge: `response=`, lifecycle fix, failopen, independent judge, "liveness" wording
4. Classifier gate + fail-loud quality bench
5. `summary` + session-end
6. `status` + primary metric + labelled `$`
7. Installer sets subscription mode (if D7 = yes)

D1 and D4 are owner actions and come before the next release.

## Owner decisions

| ID | Decision |
|---|---|
| D1 | Register the PyPI trusted publisher |
| D2 | `benchmarks.yml`: re-enable, or state that updates are manual |
| D3 | README "35–80%" / "87%" anecdotes: keep or drop |
| D4 | Remove admin bypass on `main` (require PRs) |
| D5 | Judge model: paid Haiku (needs a key) vs the user's cheap/local model, always different from the answering model |
| D6 | Judge timing: await inside the hook (bounded) vs queue and grade later |
| D7 | Installer auto-enables subscription mode on a detected Claude seat |
| — | Points 4 and `control_plane/`: parked |

## Execution tracker

| ID | Status | Task | Gate |
|---|---|---|---|
| PR1 | running | Docs: repo_goals files, Q15 note, drop README 35–80%/87%, BENCHMARKS.md line, banner_art brand; re-enable benchmarks.yml | `gh workflow view benchmarks.yml` state active; `grep -c "35–80\|87%" README.md` = 0; PR open |
| PR2 | running | Benchmark fetch opt-in + newest-copy preference | 2 new tests pass, red-check fails on guard removal |
| PR3 | running | Judge: response=, queue + deferred independent grading, failopen, liveness wording | new tests pass; red-checks fail as named |
| PR4 | running | Classifier gate + fail-loud quality bench in pre-release-verify.sh | gate passes on HEAD, fails on mutated rule; no-Ollama run exits non-zero |
| PR5 | running | summary split/labels/brand + session-end block filter | new tests pass; live `llm-router summary` has split with n, no CHUZOM |
| PR6 | todo (after PR5) | status: unverified=not used, primary metric, labelled $ | new tests pass; red-check fails |
| D1 | parked | Register PyPI trusted publisher | owner, PyPI web UI |
| D4 | parked | Remove admin bypass on main | owner, GitHub settings |
