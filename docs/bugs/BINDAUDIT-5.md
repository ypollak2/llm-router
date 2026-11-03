---
id: BINDAUDIT-5
status: fixed in this change (BIND W3, branch `bind/audit-metrics`; audit v2)
---
## BINDAUDIT-5. Use through a tool input, and the file effect of llm_local_task, did not count as use

- **Symptom.** `~/.rsi/research/primary-plan/v16/coord/BIND-R2-mcp-unused-20261103.md` sections 1 and 8 (n=150 distinct routed_mcp calls): an `llm_text_job` commit message that went
  92% verbatim into `git commit -F -` scored `unused`; 8 of the 9 distinct `llm_local_task` edits (reviewed, tested,
  committed, merged) scored `unused`. With both counted, 10 of the 150 were used (hand count: 10).
- **Cause.** The host's output in `scripts/release/outcome_audit.py` was its text blocks plus the content of
  Edit/Write calls; the inputs of Bash, Agent, SendMessage and the like were not read. `llm_local_task` writes the
  files itself (`apply_writes`) and returns a JSON status, so comparing its text with the host's reply is meaningless.
- **Fix.** `Session.reply_after(i, inputs=True)` adds the strings of every non-router, non-read tool input
  (`input_strings`). `label_local_task` judges a local task by its effect: it wrote files (`writes_applied`, not
  `blocked`/`failed`, not `incomplete` with no changed file), and `local_task_follow_up` reads the host's later Bash
  commands and edits for 6 hours. A commit KEEPS the edit (`used / local_task_effect_kept`) only when its command names
  the workdir as a path (not as the start of a sibling such as `/work/wt-ab` or `/work/wt-a-2`, nor another tree with the
  same tail) and stages what the tool changed: `commit -a`, `add -A` or `-u` with no pathspec, or an `add` of a changed
  file or of a directory that holds one; when the tool listed no changed file only a commit of everything counts. A
  host edit of a changed file before that commit is `corrected / local_task_effect_amended`; a `git checkout` or
  `git restore` of a changed file is `unused / local_task_effect_undone`; `git stash` is not an undo, because baseline
  checks use it. The first version of this fix accepted any `git add|commit` whose command contained the workdir as a
  substring; the independent review of PR #816 (`~/.rsi/research/primary-plan/v16/coord/rv-PR816-20261103.md`, S3)
  showed that marks a sibling directory kept, an unrelated commit kept and a restored file kept. The 16.0.0 window did
  not hit it (n=9 local_task calls in 8 distinct scratch worktrees); the first call made in the main checkout would
  have turned every local_task into `used`. 8 of 8 kept edits are `used` on the 16.0.0 window
  (`scripts/release/audit_agreement.py`, n=158 hand-labelled rows, in-sample), and the one that hit max iterations is
  `unused`. The new signals are on the gate's content-bearing allowlist in `scripts/release/outcome_gate.py`.
- **Test.** `tests/test_bind_audit.py::test_a_commit_message_relayed_into_bash_is_use`,
  `::test_inputs_of_router_tools_and_read_tools_are_not_host_output`, `::test_local_task_label`,
  `::test_local_task_is_kept_when_a_commit_stages_what_the_tool_changed`,
  `::test_local_task_is_not_kept_by_a_commit_that_is_about_something_else`,
  `::test_local_task_is_undone_by_a_restore_of_its_file_and_amended_by_a_host_edit` and `::test_mcp_units_end_to_end`;
  `tests/test_bind_thresholds.py::test_a_local_task_commit_counts_for_six_hours`,
  `::test_a_sibling_directory_with_a_hyphen_is_not_the_workdir` and `::test_what_a_bare_git_add_stages`.
