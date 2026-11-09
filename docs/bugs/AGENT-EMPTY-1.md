---
id: AGENT-EMPTY-1
status: fixed in `fix/agent-loop-empty-backend-reply`
---
## AGENT-EMPTY-1. A failed Ollama runner's empty 200 reply was taken as the model's final answer

- **Symptom (2026-11-09, Ollama logs).** GPU out of memory: llama-server logged "Compute error" and Ollama 0.40 still
  answered HTTP 200 on `/api/chat` with `{"message":{"role":"assistant","content":""},"done":true}` and no `done_reason`
  or `eval_count`. The trace read `llm.response n_tool_calls=0 content=''` then `loop.end reason=final_text_without_any_tool_call`;
  `llm_local_task` reported an empty report with no error ("returned nothing after 11 s").
- **Cause.** `run_agent_loop` treated any reply with no tool call as final text. A real finish always carries `done_reason`;
  the failed-runner reply has neither it nor `eval_count`.
- **Fix.** `run_agent_loop` detects empty content + no tool calls + no `done_reason` + no `eval_count`, emits `llm.error`,
  retries once if the budget has more than 5 s left (`llm.retry`), then ends with `loop.end reason=backend_empty_reply` and
  records it for `agent_loop.last_end_reason()`. `llm_local_task` maps that to status `backend_error` with a clear `error`.
- **Test.** `tests/test_agent_loop_backend_empty_reply.py` (docs/BUGS.md AGENT-EMPTY-1).
