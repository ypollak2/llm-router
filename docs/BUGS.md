# Bugs

One entry per bug: symptom, cause, fix, and the test that keeps it closed. A bug that is
found but not fixed yet is listed with its **Status** and the task that fixes it; its test
line then names the test that task must add, not one that exists. Numbers carry their n and
source. Counts from the owner's machine are read from `~/.llm-router` and the 2026-10-06
`llm-router kpi --days 7 --json` run on c2ed278, unless a line says otherwise.

## Where the entries are

One entry per file: `docs/bugs/<id>.md`, named by milestone id (numeric ids are
zero-padded, `.` and `/` in an id become `_`; the id itself is in the front matter).
A PR adds its own file and touches no shared hunk, so concurrent PRs merge in any
order.

The index is generated, not committed:

- `python scripts/bugs_index.py` prints it (id, file, status, title).
- `python scripts/bugs_index.py --write PATH` saves it.
- `python scripts/bugs_index.py --check` is the CI job `bugs-check`: unique ids, the
  Symptom / Cause / Fix / Test headings, and every `docs/BUGS.md <id>` reference in
  `src/` and `tests/` resolves to a file.
