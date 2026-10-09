---
id: A0-FLAKE-1
status: fixed in this change (test-only; production unaffected)
---
## A0-FLAKE-1. llm_act wait=False test asserted the transient `running` state

- **Symptom.** main CI run 37917400996 (commit b4e6a340), job test (3.11):
  `tests/test_agt_a0_async_agent_loops.py::test_llm_act_wait_false_returns_job_id_and_job_polls_to_result`
  failed with `assert ('queued' == 'running' ...)`.
- **Cause.** `agent_exec.run_agent` marks the job `queued` and only flips it to `running`
  from inside the pool thread (`_in_thread`). On a loaded runner (pytest `-n auto`) the
  thread can start after the test's first poll, so `queued` is a legitimate answer there.
  The test asserted `running` at that instant. Product is correct: the job leaves `queued`
  and reaches `done`. #361 made the handle `queued`; this sibling assertion was missed.
  `test_llm_local_task_wait_false_returns_job` is not affected: it holds a 1-worker pool
  busy, asserts `queued` deterministically, and polls for terminal states.
- **Fix (test only).** First poll accepts `{queued, running}` with `result is None`; the
  test then polls to a terminal state (30 s deadline) and asserts the observed statuses
  never go backwards (queued -> running -> done). `jobs.py` and `agent_exec.py` unchanged.
- **Test.** Same test. With a pool whose workers sleep 0.5 s before starting (temporary
  pytest plugin, not committed) the old test fails with the exact CI error and the new one
  passes. See the PR for 20-run counts.
