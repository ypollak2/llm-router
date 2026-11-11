---
id: ZCE-NEGATION-1
status: fixed on `fix/zce-negated-edit` (deploy: refresh the installed package; the UserPromptSubmit hook imports `zero_claude_edit`)
---
## ZCE-NEGATION-1. "Do not modify anything yet" turns were classified as edits and taken over by zero-Claude edit

- **Symptom (2026-11-11).** `zero_claude_edit.classify_edit_prompt` returned `is_edit=True` (2 files, reason `edit verb + 2 named file(s)`) for 16 of 16 synthetic LONG-Q S-tier first turns (`~/.rsi/research/primary-plan/v16/synth-live/longq/prompts.lq1.json`, tier S, turn 1, n=16 of 54 sessions). Each turn opens "Do not modify anything yet. First run `grep -n ...`" and asks what a function returns: a read/investigate request. On a failed local edit the hook blocks the prompt (by design), so these turns would be blocked for an edit nobody asked for. M, L and L2 first turns (n=38) were rejected as "too broad" only because they name 5, 12 and 18 files.
- **Cause.** `_EDIT_VERB_RE` matched the negated "modify" (and "append" in the pasted source), and the 2 `### file:` headers satisfied the 1-3 file rule. Nothing read the negation.
- **Fix.** `_NEGATED_EDIT_RE` in `src/llm_router/zero_claude_edit.py`, checked in `classify_edit_prompt` before the verb match: "do not / don't / never <edit verb> anything|any file|any code" and "before (you) <changing|modifying|...> anything" keep `is_edit` False. "anything else" is excluded so "fix a.py, don't change anything else" is still an edit.
- **Test.** `tests/test_zero_claude_edit_scope.py::test_classify_negated_edit_is_not_an_edit` (3 cases) and `::test_classify_two_file_edit_still_an_edit` (2 cases). Against origin/main 7a31a382 the negated cases fail 3 of 3; the genuine-edit cases pass on both.
