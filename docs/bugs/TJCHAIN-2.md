---
id: TJCHAIN-2
status: fixed in `fix/text-job-chain-any-size` (deploy: restart the MCP server; the quality store is in the process, so a running server keeps what it recorded)
---
## TJCHAIN-2. The generic quality score threw away a local answer the text job's own check accepts, and its record made the breaker skip the local model

- **Symptom (2026-11-04, from the brief of this fix; read by the coordinator from `usage.db`, not re-read here).** With
  `LLM_ROUTER_LOCAL_TEXT_JOBS=1` (the MCP path) the local answer to a commit-message job was rejected twice as
  `low_quality:0.30` (`execution_events` `attempt_rejected`, 2026-11-04 21:47:19 and 21:48:26) and the job was then
  served by `codex/gpt-6-astra`, which spent Codex quota on a commit message. In the last 30 days 9 of 63 local
  `attempt_rejected` rows are `low_quality:0.30`.
- **Reproduced (2026-11-04, a727cb26, `tests/test_lc12c.py` fixtures, fake providers, no model; one run each).** Answer
  `feat: touch src/a.py` scores 0.30 (`quality_recorded score=0.3` in the router log). Flag `=1`, four jobs of 2000 to
  2300 chars: jobs 1 to 3 each tried the local model, then `gemini/gemini-2.5-flash`; job 4 tried
  `anthropic/claude-sonnet-5` only. Flag unset (the passed model alone), four jobs of 1000 to 1300 chars: jobs 1 to 3
  were `accepted` on the local model; job 4 was `fallback`, `local_failed`, model
  `ollama/lc8-fx:9b [quality-skipped avg=0.30 n=3]`. The bind hook is a new process per call and never reaches the
  fourth job; a long-lived MCP server does.
- **Cause.** `router._dispatch_model_loop`, P2 quality-gated escalation: every answer was scored by
  `quality_feedback.score_response`, a length and structure heuristic that gives a one-line message 0.30, under
  `LLM_ROUTER_ESCALATE_THRESHOLD` (0.4). The answer was rejected as `low_quality:<score>` and the next model tried,
  although `text_jobs.check_output` (run by `llm_text_job` on the answer afterwards) would accept it. The same block
  called `record_quality`, and `should_skip_model` skips a model whose average over three recorded calls is under 0.4
  (`_MIN_CALLS_FOR_SIGNAL`, `QUALITY_THRESHOLD`), so three text jobs in one process taught the breaker to drop the
  local model for that task and tier.
- **Fix.** A call with `eligibility_exempt_reason == TEXT_JOB_REASON` (only `llm_text_job` passes it; `llm_edit` passes
  `validated_edit`) skips the whole block: its answer is not scored, escalated or recorded and goes to `check_output`.
  Every other call keeps the escalation unchanged (tested with no exemption and with `validated_edit`).
- **Known and left alone.** `should_skip_model` also reads scores recorded by other callers
  (`tools/text.py::_record_quality` for `llm_generate`, `llm_query` and the others), so a local model those calls
  taught the breaker to skip for `generate` at `moderate` is skipped for a text job too. Text jobs do not exempt the
  breaker. Not measured.
- **Test.** `tests/test_lc12c.py`:
  `test_low_scoring_text_job_answer_is_not_escalated_and_reaches_its_own_check` (`check_output`
  called once with the local answer, `score_response` never called, nothing recorded),
  `test_the_same_score_on_any_other_call_still_escalates_once_and_is_recorded` (two cases),
  `test_repeated_text_jobs_never_get_the_local_model_quality_skipped` (four jobs, flag unset and `=1`). Mutation check:
  with the exemption removed and the `complexity_hint` kept, 3 of the 19 new tests fail; with the exemption widened to
  every exempt reason, the `validated_edit` control fails.
