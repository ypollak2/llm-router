# Q15 harness: the single-strong-model arm (draft, 2026-09-24)

Q15 (`architecture/EVALUATION_PLAN.md` §7): run the §28 scenario through the graph, and again as one
strong model given the same prompt and the same acceptance checks; compare verified completion and total
tokens. **The graph arm cannot run yet.** Phase 2 does not exist (no `workflows/`, no in-process
agenticgraphs integration). §28 ("Implement the plan", `TASK_GRAPH_COMPILER.md:153`) has no acceptance
checks; the only check in the design is `verdict == 'PASS'` (line 115).

So this builds the half that CAN run now: tasks with objective checks, plus the single-model arm. That
gives the baseline the graph must beat. When Phase 2 lands, the comparison becomes one more backend.

## Reuse, don't rebuild
`scripts/bench_backend_quality.py` already has what this needs:
- Task tuples `(id, kind, prompt, allowed_files, verifier)` with objective verifiers (a Python snippet that
  exits 0; `pytest_passes()`, `run()`). There is no model judge.
- A fresh sandbox per task, SHA snapshots before and after (stray-file / blast-radius check), and
  `time.monotonic`.
- A `claude` backend: `claude -p … --permission-mode acceptEdits --settings '{"hooks":{}}'
  --strict-mcp-config --mcp-config '{"mcpServers":{}}'`, with `LLM_ROUTER_ENFORCE=off` in the child env.
  llm-router hooks and MCP are off, so the arm measures the model, not the router.
- Suites: `hard` (9 cross-file tasks), `brutal`, and `easy`.

## What gets added (small)
1. **Tokens.** Switch the claude backend to `--output-format json` and record `usage` (input,
   cache-create, cache-read, output), `num_turns`, and `total_cost_usd` per task. The pilot verifies
   these fields exist. If they don't, fall back to the transcript parser already in
   `scripts/bench_review_cost.py`.
2. **A §28-shaped suite, `plan`.** 3 to 5 tasks. Each is a fixture repo with a `PLAN.md` and hidden
   acceptance tests. The prompt is literally "Implement the plan." Verified completion means the hidden
   tests pass, no files outside the allowed set change, and the repo's own tests still pass. These are
   the acceptance checks both arms will share.
3. **Repetitions.** k runs per task, reporting pass@k and mean tokens, each with its n, so one lucky run
   is not a result.
4. **Hygiene.** The sandbox stays under `/tmp/bq_*`, which `scripts/groundtruth/sources.py` already
   excludes. `LLM_ROUTER_SYNTHETIC=1` is set in the child env.

## Output
`results/q15/single_<model>.json` plus a one-table summary: task | pass@k | median tokens | median turns
| median seconds, with n and window stated.

## Validation of the harness itself
- A known-bad backend, the empty answer `noop`, must score 0/N. If it doesn't, a verifier is vacuous.
- One task is red-checked by hand: break the reference solution and the verifier must fail.
