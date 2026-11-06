"""Bind commit messages and PR descriptions to the local text job (owner decision D-360).

The model writes ``git commit -m "${LLM_TEXT:?}"`` or ``gh pr create --title "<short>" --body
"${LLM_TEXT:?}"`` and the PreToolUse hook ``hooks/text-job-bind.py`` does the rest: it builds the
input from the real repository, runs the router's own ``llm_text_job`` (local model only, the
same checks), writes the checked text to a private file and rewrites the command to read it
(``-F <file>`` / ``--body-file <file>``). If the job does not produce a checked text the hook
denies the command with a short reason and the model writes the text itself. The text never
passes through the model, and a text that failed its checks is never used.

The placeholder is a shell variable on purpose: with ``:?`` the shell aborts the whole command
if nothing replaced the variable, so a hook that is not registered, was killed on its timeout
or runs on a host that ignores ``updatedInput`` can never cause the placeholder itself to be
committed or posted.

Modules: ``shellscan`` (a strict reader of the shell subset), ``plan`` (what the command asks
for, and the rewrite), ``gitinput`` (the job input from the repository), ``engine`` (the
decision), ``ledger`` (text-free rows and the report), ``limits`` (time limits).
Report: ``python -m llm_router.text_job_bind report``.

This package imports nothing heavy at import time; the router is loaded only when a model call
is actually made.
"""

PLACEHOLDER = '"${LLM_TEXT:?}"'
