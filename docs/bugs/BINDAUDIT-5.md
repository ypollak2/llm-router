---
id: BINDAUDIT-5
status: fixed in this change (BIND W3, branch `bind/audit-metrics`; audit v2)
---
## BINDAUDIT-5. Use through a tool input, and the file effect of llm_local_task, did not count as use

- **Symptom.** BIND-R2 sections 1 and 8: an `llm_text_job` commit message that went 92% verbatim into
  `git commit -F -` scored `unused`; 8 of the 9 distinct `llm_local_task` edits (reviewed, tested, committed, merged)
  scored `unused`. With both counted, 10 of the 150 distinct routed_mcp units were used (hand count: 10).
- **Cause.** The host's output was its text blocks plus the content of Edit/Write calls; the inputs of Bash, Agent,
  SendMessage and the like were not read. `llm_local_task` writes the files itself (`apply_writes`) and returns a JSON
  status, so comparing its text with the host's reply is meaningless.
- **Fix.** `Session.reply_after(i, inputs=True)` adds the strings of every non-router, non-read tool input
  (`input_strings`). `label_local_task` judges a local task by its effect: it wrote files (`writes_applied`, not
  `blocked`/`failed`, not `incomplete` with no changed file) and `local_task_follow_up` finds the host's later
  `git commit|add` naming its workdir (`used / local_task_effect_kept`), or an edit of one of its changed files
  first (`corrected / local_task_effect_amended`). A `git stash`/`checkout` is not read as an undo: baseline checks
  use them and the diff is not in the transcript. 8 of 8 kept edits are `used` on the 16.0.0 window, and the one
  that hit max iterations is `unused`. The new signals are on the gate's content-bearing allowlist.
- **Test.** `tests/test_bind_audit.py::test_a_commit_message_relayed_into_bash_is_use`,
  `::test_inputs_of_router_tools_and_read_tools_are_not_host_output`, `::test_local_task_label`,
  `::test_local_task_follow_up_needs_a_commit_naming_the_workdir` and `::test_mcp_units_end_to_end`.
