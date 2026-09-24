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

## Baseline results: single-model arm (2026-09-24)

Conditions: `claude -p` 2.1.281, `--model claude-opus-5-5`, with llm-router hooks and MCP off; the
permission grant is edits plus `python3 -m pytest`; each run starts in a fresh sandbox; this machine.
Claude Code also made side calls to `claude-sonnet-5`, which are counted in the tokens below. Tokens
are input + cache-create + cache-read + output; about 90% are cache reads.

| Suite | n (tasks × runs) | Verified (correct AND clean) | Median tokens/task | Median turns | Median $ |
|---|---|---|---|---|---|
| plan (§28-shaped) | 3 × 3 | **9/9** | 235k–348k | 13–15 | 0.32–0.40 |
| hard (cross-file) | 10 × 3 | **27/30** | 108k–305k | 3–14 | 0.03–0.24 |

- `hd-strict-validate` scored 0/3 verified. It was correct 3/3, but every run edited
  `tests/test_pipeline.py`, which the task forbids. That is a real result.
- `hd-stale-index` first scored 0/3 because of a HARNESS bug: a relative `BENCH_OUT` broke the
  verifier's path. OUT_DIR is now resolved to an absolute path. A re-run with the fix scored 3/3,
  and the table uses the re-run.

What this means for Q15: on these suites one strong model is at or near the ceiling on verified
completion, so a graph can only win on tokens (the plan suite needs 235k–348k per task) or on the
cleanliness failures. To separate the arms on completion, the task set needs harder, multi-layer tasks.
Raw results: `results/q15/` (not committed).

## Local agent arm and the eligibility rule (2026-09-24)

Same harness and hidden verifiers; backend `local` = `run_agent_loop` with qwen3-coder:30b,
`AGENT_WRITES=apply` inside the sandbox, `BENCH_TIMEOUT=600`, this machine, $0.

| Suite | Local (verified, runs) | Opus 5.5 | Notes |
|---|---|---|---|
| plan (3 × 3) | 3/9 | 9/9 | passes plan-retry 3/3; ttl-cache, todo-cli 0/3 |
| hard (10 × 3) | 16/30 | 27/30 | reliable on 5 tasks, incl. strict-validate (Opus 0/3: edited the test) |
| easy (26 × 3, held-out) | 72/78 | — | |
| brutal (11 × 3, held-out) | 24/33 | — | |

Local was 2–6× faster in wall-clock where it passed.

**Eligibility rule (`scripts/local_eligibility.py`), pre-registered in 56c7764 before the held-out run:**
an edit that names its target and states the change; questions, cause-hunting and
"implement the plan" are excluded. Held-out: it admitted 10 of 37 tasks, **precision 10/10 tasks
(30/30 runs)** and recall 10/32 (the local agent was reliable on 32/37 held-out tasks). With n=10,
the true failure rate on admitted tasks could still be up to ~30% (rule of three).

**On the population it would actually face, the rule does not transfer.** Applied to 708 real
prompts from this machine's Claude Code history (deduped; benchmark sessions and sandboxes
excluded via `scripts/groundtruth/sources.py`), it admits **1 (0.1%)**, and that one is a false
admission. 60% of real prompts name no target ("keep going", "push both"), 27% are questions and
9% are cause-hunts. Real prompts are short directions to an agent that already has the context,
not self-contained edit specs. So prompt-level routing to a local agent would move ~0% of this
user's work, even though the local agent does most spec-shaped tasks well.

The plausible lever is the opposite direction: the strong model, which has the context, writes a
precise named edit and delegates it (`llm_local_task`). It is not built or measured.

## Delegation A/B: pre-registered decision rule (written 2026-09-24, BEFORE any run)

Question: does Claude **delegating** named sub-edits to a local agent (`llm_local_task`, writes applied,
an acceptance check run by the supervisor) save Claude tokens at equal verified completion?

- Arm A: backend `claude` (Opus 5.5, works directly); baseline results/q15/ (plan 9/9, hard 27/30).
- Arm B: backend `claude_delegate`: the same CLI and model, the llm-router MCP server with only
  `llm_local_task` allowed, and an appended instruction to delegate named edits with an acceptance check
  and to trust only `status=verified_complete`.
- Suites plan + hard + easy, 3 runs each, with the same hidden verifiers.
- **Ship rule:** per suite, B's verified completion ≥ A's − 1 task, AND overall B's median Claude
  tokens/task ≤ 0.8 × A's. Anything else: do not wire delegation. The report states the numbers either way.
- Claude tokens = input + cache_creation + cache_read + output from `--output-format json`. The
  cache-read share is reported separately because it prices at 0.1×.
