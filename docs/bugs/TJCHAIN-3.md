---
id: TJCHAIN-3
status: fixed in `fix/text-job-chain-any-size`
---
## TJCHAIN-3. The router cut a text job's input before the local model saw it

- **Symptom.** With the chain fixed (TJCHAIN-1), accepted commit messages on large diffs described changes the diff did
  not hold. Judged sample, `~/.rsi/research/primary-plan/v16/coord/textjob-replay/JUDGE-20261104.md`, n=19, one judge, squash commits: at 16000 chars and
  over, 3 of 12 accurate and 5 wrong; under 16000, 6 of 7 accurate and 0 wrong. The acceptance check caught none of the
  5, because it only needs one changed file named.
- **Cause.** `route_and_call` ran `compact_structural` (`src/llm_router/compaction.py`) on every prompt over the
  threshold in `src/llm_router/config.py`, text jobs included. It keeps the first 20 and last 10 lines of a fenced block.
  The lane was qualified on whole inputs, and `src/llm_router/text_job_bind/gitinput.py` already fits the hook's input to
  `INPUT_BUDGET_CHARS` so that no second blind cut happens.
- **Fix.** A text job (`eligibility_exempt_reason == TEXT_JOB_REASON`) is not compacted. Every other route keeps it.
- **Test.** `tests/test_lc12c.py`: `test_text_job_prompt_reaches_the_model_whole`,
  `test_any_other_large_prompt_is_still_compacted`.
