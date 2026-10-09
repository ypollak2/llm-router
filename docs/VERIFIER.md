# Verifier C: pending_verify queue, detached worker, Codex marker (SHADOW, opt-in)

Source of the requirements: `VERIFIER_PLAN.md` (V1 verifier for routed code tasks, written 2026-10-06,
owner decisions 2026-10-06; kept in the owner's research notes, not in this repo) section 1 "Hook points and
latency" and section 7 "PR phasing", item 4 ("PR C: queue + verify_worker + Codex marker in agent-route.py").
PLAN-v16 P0.12 / C11 ("Verifier stack lands in shadow") puts it in the merge order #301, #272, #281, #285, #287.

## Default: OFF

`LLM_ROUTER_VERIFY` is **opt-in**. Unset, empty or any value other than `1`, `on`, `true`, `yes` means off.
Off: the Codex hook captures nothing and writes no marker; SessionStart and Stop spawn no worker. A worker
started by hand still expires and sweeps an existing queue, and verifies nothing.

Why off: PLAN-v16 P0.12 and C11 say the verifier stack "lands in shadow" and are silent on the flag's default.
D-30 (decided A) is the merge-authority decision and says nothing about it. With the plan silent, the safe
reading is that shadow means opt-in. (The previous head defaulted to on, which captured the user's source
diffs and ran a background worker with no opt-in.)

Switch on: `export LLM_ROUTER_VERIFY=on` in the environment of Claude Code (the hooks read it), then restart the
session. Switch off again: unset it or set `off`. `LLM_ROUTER_VERIFY_BUDGET_S` (default 120, cap 300) is the
per-unit budget. SHADOW also means: only `northstar.record_verify` is called; no outcome, NS, D1 or D2 changes
(PR E).

## PLAN requirements and where each is met

| VERIFIER_PLAN requirement | Evidence in this change |
|---|---|
| Codex delegation: the hook appends a marker only (cwd, HEAD sha, patch ref); 0 ms inside the turn budget | `verify_queue.enqueue_from_run`, called from `hooks/agent-route.py`; `scripts/bench_verify_hook.py` |
| Budget 120 s wall, configurable, cap 300 | `verify_worker.budget_s`; `test_budget_default_and_cap`, `test_the_unit_is_handed_the_remaining_budget_not_more_than_the_cap` |
| Max 2 concurrent workers | `verify_queue.acquire_slot` (flock slots); 2-process concurrency test in `tests/test_verify_worker.py` |
| Worker spawned detached at the next SessionStart/Stop; drains the queue | `spawn_worker_if_needed` from `session-start.py` and `session-end.py`; `test_spawn_is_detached_with_a_fixed_argv_and_no_secrets` |
| No verdict after 24 h: unknown, reason `verify_expired` | `is_expired`, `test_expired_marker_records_verify_expired_and_deletes_the_patch` |
| Patch is a recorded diff vs HEAD plus untracked files; HEAD moved: no false apply | `_capture`; baseline is `git archive <recorded head>` (`_checkout`) |
| Safety: no network, env scrubbed, user tree never written, patch deleted after the verdict | `get_delegated_env` allowlist tests, patch mode 0600 and deletion tests |
| Reason codes only, never free text | every failure path records one `unavailable/<code>`; `process` never raises |
| Advisory: never blocks the turn | hook path is fail-open, records `CHZ-FO-VERIFY-MARKER` |
| NOT in this PR (plan phases D, E and the proxy/toolkit hook points) | proxy end-of-turn spawn, toolkit-loop call, replay bars, outcome mapping |

## Queue invariants pinned by tests

| Rule | Test |
|---|---|
| Three crashed claims exhaust a unit (`MAX_ATTEMPTS` = 3) | `test_a_unit_whose_worker_dies_three_times_is_given_up_on_the_third` |
| A live worker's claim (up to 300 s old) is never stolen | `test_a_live_workers_claim_is_not_stolen_but_a_dead_ones_is` |
| A claim is touched before the rename | `test_claiming_touches_the_marker_so_the_claim_is_fresh` |
| A unit with under 5 s left is `unavailable/timeout` | `test_a_budget_under_the_minimum_never_reaches_the_verifier` |
| One run handles at most 25 units | `test_one_run_processes_at_most_max_units_per_run` |
| Patches over `MAX_PATCH_BYTES` are refused | `test_an_oversized_patch_is_refused_before_anything_is_checked_out` |
