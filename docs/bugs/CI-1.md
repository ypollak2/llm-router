---
id: CI-1
status: fixed in this change (test-only; production unaffected)
---
## CI-1. `copytree` of a fixture repo raced git's background maintenance

- **Symptom.** `tests/test_verify_unit.py::test_unproven_sandbox_is_unavailable_never_pass` failed on CI
  Python 3.11 (PR #328 run 37811026623, and PR #287) with
  `shutil.Error: [('.../broken/.git/objects/maintenance.lock', ...` raised by `_patch`'s `copytree`.
- **Cause.** The fixture `_init` runs `git commit`; with auto-maintenance on, commit spawns a detached
  `git maintenance run --auto` that creates and removes `.git/objects/maintenance.lock` and repacks
  loose objects while the test copies `.git`. Reproduced locally with 4 parallel stress processes
  (400 extra files, hostile global config `gc.auto=1`, `maintenance.auto=true`): 110 copytree races
  in 240 repos (27, 28, 27, 28 of 60). Production is not affected: `sandbox.create_workspace` skips
  `.git` and copies file by file, and `verify_unit`'s `model-baseline` copytree reads that `.git`-free baseline.
- **Fix (test only).** `GIT` in `tests/test_verify_unit.py` passes `-c gc.auto=0 -c maintenance.auto=false`,
  so no background child exists. A `*.lock` ignore was rejected: it hides one file while the child still
  rewrites `objects/*` mid-copy (the stress run also failed on object directories).
- **Test.** `tests/test_git_fixture_race.py` traces git's process starts under the hostile config: 1 failed
  without the fix (maintenance child seen), passes with it. It also asserts the trace saw the commit.
- **Second cause, found when CI failed again on the repair (3.13, `verify_head_unavailable`).** The
  original `IndexError` was never the venv: `_checkout` closed the `git archive` pipe as soon as
  `tarfile` read the end-of-archive blocks, while git was still writing the record padding; git died of
  SIGPIPE, rc != 0 became `verify_head_unavailable`, and the verifier was never called. Timing-dependent,
  so it passed on a Mac and flaked on CI (either Python). Fix: drain the pipe to EOF before closing.
  Test: `test_checkout_drains_git_archive_padding_so_git_is_not_killed_by_sigpipe` (a stand-in git that
  writes the padding late; red with `_Fail: verify_head_unavailable` before the fix).


