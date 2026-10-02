"""Acceptance checks and prompt builders for the self-contained text jobs
(commit message, PR description, long-output summary). Pure logic: no model,
no disk. Every check has a passing case AND a case that must be rejected --
a checker that only ever sees good input proves nothing."""
from __future__ import annotations

import pytest

from llm_router import text_jobs as tj

DIFF = """diff --git a/src/llm_router/edit.py b/src/llm_router/edit.py
--- a/src/llm_router/edit.py
+++ b/src/llm_router/edit.py
@@ -1,2 +1,3 @@
+import re
 x = 1
"""

LOG = """FAILED tests/test_edit.py::test_apply - AssertionError
tests/test_edit.py:42: AssertionError: expected 3 got 2
src/llm_router/edit.py:118: in apply
Process exited with exit code 1
"""


def test_changed_files_from_diff_header_and_empty():
    assert tj.changed_files(DIFF) == ["src/llm_router/edit.py"]
    assert tj.changed_files("") == []
    assert tj.changed_files("not a diff at all") == []


def test_changed_files_falls_back_to_plus_lines_and_skips_devnull():
    d = "--- /dev/null\n+++ b/new_file.py\n+print(1)\n"
    assert tj.changed_files(d) == ["new_file.py"]


def test_truncate_input():
    assert tj.truncate_input("abc", 10) == ("abc", False)
    assert tj.truncate_input("a" * 20, 10) == ("a" * 10, True)


# -- commit message ---------------------------------------------------------

def test_commit_message_accepts_grounded_message():
    r = tj.check_commit_message(DIFF, "edit: import re for path matching\n\nTouches edit.py.")
    assert r.ok and r.reason is None


@pytest.mark.parametrize("msg", ["", "   \n", "WIP", "update", "Fix."])
def test_commit_message_rejects_empty_and_placeholder(msg):
    r = tj.check_commit_message(DIFF, msg)
    assert not r.ok and r.reason


def test_commit_message_rejects_ungrounded():
    r = tj.check_commit_message(DIFF, "Refactor the billing module for clarity")
    assert not r.ok
    assert "does not mention any changed file" in r.reason


def test_commit_message_rejects_overlong_subject():
    r = tj.check_commit_message(DIFF, "edit.py " + "x" * 120)
    assert not r.ok and "too long" in r.reason


def test_commit_message_fails_closed_when_diff_has_no_files():
    r = tj.check_commit_message("", "Add a thing")
    assert not r.ok
    assert "cannot verify groundedness" in r.reason


# -- PR description ---------------------------------------------------------

PR_CTX = "feat: tighten edit.py matching\nfix: ledger row in text_job_ledger.py"
GOOD_PR = "## Summary\nTightens edit.py matching.\n\n## Test plan\n- run pytest\n"


def test_pr_description_accepts_complete_body():
    assert tj.check_pr_description(PR_CTX, GOOD_PR).ok


def test_pr_description_rejects_missing_sections_and_short():
    r = tj.check_pr_description(PR_CTX, "## Summary\nedit.py changed, lots of words here to be long enough.")
    assert not r.ok and "test plan" in r.reason
    r = tj.check_pr_description(PR_CTX, "## Test plan\nrun pytest on edit.py, plenty of text to be long.")
    assert not r.ok and "Summary" in r.reason
    r = tj.check_pr_description(PR_CTX, "short")
    assert not r.ok and "too short" in r.reason


def test_pr_description_rejects_ungrounded():
    r = tj.check_pr_description(PR_CTX, "## Summary\nUnrelated prose about billing.\n\n## Test plan\nManual QA.\n")
    assert not r.ok and "does not reference" in r.reason


# -- summarize output -------------------------------------------------------

GOOD_SUMMARY = (
    "1 test FAILED: tests/test_edit.py:42 AssertionError (expected 3 got 2), "
    "raised from src/llm_router/edit.py:118; process exited with exit code 1."
)


def test_summary_accepts_when_signals_preserved():
    assert tj.check_summarize_output(LOG, GOOD_SUMMARY).ok


def test_summary_rejects_dropped_file_line():
    s = "1 test FAILED in tests/test_edit.py:42, exited with exit code 1 (error in apply)."
    r = tj.check_summarize_output(LOG, s)
    assert not r.ok and "src/llm_router/edit.py:118" in r.reason


def test_summary_rejects_dropped_exit_code():
    s = "1 test FAILED: tests/test_edit.py:42 and src/llm_router/edit.py:118 raised an AssertionError."
    r = tj.check_summarize_output(LOG, s)
    assert not r.ok and "exit-code" in r.reason


def test_summary_rejects_dropped_error_signal():
    raw = "Traceback (most recent call last)\nRuntimeError: boom\n"
    r = tj.check_summarize_output(raw, "Everything finished, nothing notable to report here.")
    assert not r.ok and "error/failure" in r.reason


def test_summary_rejects_too_short():
    assert not tj.check_summarize_output("ok", "ok").ok


def test_summary_of_clean_output_needs_no_error_word():
    assert tj.check_summarize_output("all 12 tests passed in 3s", "All twelve tests passed in three seconds.").ok


# -- dispatch / builders ----------------------------------------------------

@pytest.mark.parametrize("job", tj.JOB_NAMES)
def test_build_prompt_includes_input_hint_and_feedback(job):
    system, prompt = tj.build_prompt(job, "THE-INPUT", hint="TICKET-9", feedback="missing X")
    assert system
    assert "THE-INPUT" in prompt and "TICKET-9" in prompt and "missing X" in prompt


@pytest.mark.parametrize("job", tj.JOB_NAMES)
def test_build_prompt_omits_feedback_on_first_attempt(job):
    _, prompt = tj.build_prompt(job, "THE-INPUT")
    assert "rejected" not in prompt


def test_unknown_job_raises():
    with pytest.raises(KeyError):
        tj.build_prompt("write_poem", "x")
    with pytest.raises(KeyError):
        tj.check_output("write_poem", "x", "y")


def test_check_output_dispatches():
    assert tj.check_output(tj.JOB_SUMMARIZE_OUTPUT, LOG, GOOD_SUMMARY).ok
    assert not tj.check_output(tj.JOB_COMMIT_MESSAGE, DIFF, "WIP").ok


# -- regressions from the 2026-10-02 independent review ---------------------

def test_grounding_is_word_boundary_not_substring():
    """A short stem like 'os' (from os.py) must not be credited by appearing
    inside an unrelated word like 'across'. Independent-review repro."""
    diff = "diff --git a/os.py b/os.py\n+++ b/os.py\n+x = 1\n"
    r = tj.check_commit_message(diff, "Reorganize tests across modules")
    assert not r.ok and "does not mention" in r.reason
    # A real, word-bounded mention of the same stem still passes.
    assert tj.check_commit_message(diff, "os: fix path join on windows").ok


def test_summary_zero_failed_is_not_treated_as_an_error_signal():
    """'N passed, 0 failed' must not force a correct, error-free summary to be
    rejected for omitting error language it correctly doesn't contain.
    Independent-review repro."""
    raw = "10 passed, 0 failed in 1.23s\nExit code: 0\n"
    candidate = "All 10 tests passed. Exit code: 0."
    assert tj.check_summarize_output(raw, candidate).ok


def test_summary_real_failure_is_still_required_to_be_mentioned():
    """The zero-failure mask must not swallow a genuine non-zero failure count."""
    raw = "9 passed, 1 failed in 1.23s\nExit code: 1\n"
    candidate = "All tests passed cleanly. Exit code: 1."
    r = tj.check_summarize_output(raw, candidate)
    assert not r.ok and "error/failure" in r.reason


def test_exit_code_regex_matches_exited_with_code_phrasing():
    raw = "Process exited with code 137 (OOM killed)\n"
    lossy = "Everything looks fine, no issues."
    r = tj.check_summarize_output(raw, lossy)
    assert not r.ok and "exit-code" in r.reason


def test_file_line_regex_covers_additional_extensions():
    raw = "boom at Program.cs:42\n"
    lossy = "Something broke."
    r = tj.check_summarize_output(raw, lossy)
    assert not r.ok and "Program.cs:42" in r.reason
