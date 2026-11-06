---
id: RELSH-3
status: fixed in `bind/release-sh-portable` (PR #815; release tooling only, production unaffected)
---
## RELSH-3. `get_version` died on macOS: the system `python3` is 3.9 and has no `tomllib`

- **Symptom (2026-11-03).** `scripts/release/release.sh` died at the first command of `main()` that runs Python, after the banner
  and before Step 1: `current_version=$(get_version)` ran `python3 -c "import tomllib; ..."`. On the maintainer's Mac `python3`
  is `/usr/bin/python3`, Python 3.9.6, and `/usr/bin/python3 -c "import tomllib"` prints
  `ModuleNotFoundError: No module named 'tomllib'` (reproduced 2026-11-03). Step 1 (`verify-version-sync.py`,
  `verify-plugin-sync.py`) and the closing hint (`verify-release.py`, which imports `tomllib` at module level) used the same bare
  `python3`.
- **Cause.** The project requires Python >= 3.11 (`requires-python` in `pyproject.toml`) and `tomllib` is standard library only from
  3.11; the script assumed `python3` was the project interpreter. `scripts/release/pre-release-verify.sh` had met the same failure and
  says so beside its `uv run python` call; `release.sh` never got the same fix.
- **Fix.** `project_python` in `scripts/release/release.sh`: `uv run --frozen python` (the interpreter the test, lint and build
  steps use); without `uv`, `python3` only when it is >= 3.11; otherwise it stops at once with a one-line message that names the
  version found and what to install (exit 1, nothing on stdout). `get_version`, Step 1 and the closing hint go through it.
  `pre-release-verify.sh` was checked and needed nothing: its `tomllib` read already uses `uv run python`, and its one bare
  `python3` call (`semver_gate.py`) prints identical output and exit status under 3.9.6 and 3.14.6. After, on the same Mac:
  `get_version` prints `16.0.0`; with `uv` off PATH it exits 1 with the message.
- **Test.** `tests/test_release_sh_portable.py::test_get_version_uses_the_project_interpreter_not_the_system_python3`
  (a stand-in `python3` that reports 3.9.6 and has no `tomllib` is never called; `uv` is called as `run --frozen python -c`),
  `::test_the_old_python_stand_in_fails_the_way_the_dry_run_did` (control: the pre-fix command dies on the stand-in),
  `::test_without_uv_an_old_python3_stops_with_a_clear_message`, `::test_without_uv_a_new_enough_python3_is_used`,
  `::test_project_python_prefers_uv_over_an_old_python3` and `::test_no_bare_python3_is_left_outside_the_interpreter_gate`.
  Mutation check: putting the bare `python3 -c` back in `get_version` fails 3 tests (run on 1f91ba4e).
