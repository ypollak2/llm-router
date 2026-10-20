"""Zero-call self-validity cases for ``grader.grade_text`` on SYNTHETIC fixtures (ported from PREREG-Q37; no real item is used).

Each case is ``(name, job, fixture, text, expect_ok, expected_reason_or_None)``. A case behaves as pre-registered iff ``ok`` matches and (for a
rejection) the named reason is among the reasons. ``run()`` returns the unexpected cases; an empty list means the grader is valid.
"""
from __future__ import annotations

from typing import Any

from . import grader as G

DIFF_PY = """diff --git a/src/pkg/retry.py b/src/pkg/retry.py
index 1111111..2222222 100644
--- a/src/pkg/retry.py
+++ b/src/pkg/retry.py
@@ -10,12 +10,18 @@ import time
-def retry_call(fn, attempts=3, delay=1.0):
+def retry_call(fn, attempts=3, delay=1.0, max_delay=30.0, backoff_factor=2.0):
     last = None
     for i in range(attempts):
         try:
             return fn()
         except Exception as exc:
             last = exc
-            time.sleep(delay)
+            time.sleep(min(delay * backoff_factor ** i, max_delay))
     raise last
diff --git a/tests/test_retry.py b/tests/test_retry.py
--- a/tests/test_retry.py
+++ b/tests/test_retry.py
@@ -1,3 +1,8 @@
+def test_retry_call_caps_delay(monkeypatch):
+    assert True
"""
DIFF_TS = """diff --git a/packages/core/src/parse.ts b/packages/core/src/parse.ts
--- a/packages/core/src/parse.ts
+++ b/packages/core/src/parse.ts
@@ -4,6 +4,9 @@ export function parseQuery(input: string): Query {
-  const parts = input.split('&')
+  const parts = input.split('&').filter(Boolean)
+  if (parts.length === 0) return { $empty: true }
   return build(parts)
"""
DIFF_GO = """// see #12 for the nil case
diff --git a/internal/cache/lru.go b/internal/cache/lru.go
--- a/internal/cache/lru.go
+++ b/internal/cache/lru.go
@@ -20,7 +20,10 @@ func (c *Cache) Evict() {
-	delete(c.items, c.oldest.key)
+	if c.oldest != nil {
+		delete(c.items, c.oldest.key)
+	}
"""
FIX: dict[str, tuple[str, list[str]]] = {"py": (DIFF_PY, ["src/pkg/retry.py", "tests/test_retry.py"]), "ts": (DIFF_TS, ["packages/core/src/parse.ts"]), "go": (DIFF_GO, ["internal/cache/lru.go"])}

PR_OK = ("## Summary\nAdd a `max_delay` cap and a `backoff_factor` to `retry_call` so that the sleep between attempts grows exponentially but never exceeds the cap. "
         "The previous fixed delay is the default behaviour when the factor is 1.\n\n## Test plan\n- Run `pytest tests/test_retry.py`\n- Call `retry_call` with a failing function and check the sleep times\n")
WORDS = " ".join(["word"] * 121)

# (name, job, fixture, text, expect_ok, reason)
CASES: list[tuple[str, str, str, str, bool, str | None]] = [
    # ---------- commit: must be ACCEPTED (valid answers in other formats)
    ("c_plain", "commit_message", "py", "Add max_delay cap to retry_call", True, None),
    ("c_body", "commit_message", "py", "Add `max_delay` option to `retry_call`\n\nThe backoff delay is now capped by `max_delay`.", True, None),
    ("c_conventional", "commit_message", "py", "fix(retry): cap backoff delay", True, None),
    ("c_conventional_bang", "commit_message", "py", "feat!: add max_delay to retry_call", True, None),
    ("c_gitmoji", "commit_message", "py", "✨ Add max delay to retry_call", True, None),
    ("c_gitmoji_code", "commit_message", "py", ":bug: Fix delay cap in retry_call", True, None),
    ("c_module_prefix", "commit_message", "py", "retry: cap the delay", True, None),
    ("c_bracket_tag", "commit_message", "py", "[retry] Cap the delay", True, None),
    ("c_outer_fence", "commit_message", "py", "```\nAdd max_delay cap\n```", True, None),
    ("c_outer_fence_lang", "commit_message", "py", "```text\nAdd max_delay cap\n\nBody line.\n```", True, None),
    ("c_think_stripped", "commit_message", "py", "<think>plan</think>\nAdd max_delay cap", True, None),
    ("c_test_path_in_diff", "commit_message", "py", "Cover retry_call cap in `tests/test_retry.py`", True, None),
    ("c_dir_path_ok", "commit_message", "py", "Cap delay in `src/pkg/` retries", True, None),
    ("c_basename_in_diff", "commit_message", "py", "Cap delay in `retry.py`", True, None),
    ("c_process_ss", "commit_message", "py", "Process retries with a capped delay", True, None),
    ("c_address", "commit_message", "py", "Address unbounded sleep in retry_call", True, None),
    ("c_use", "commit_message", "py", "Use exponential backoff in retry_call", True, None),
    ("c_bypass", "commit_message", "py", "Bypass the delay when max_delay is zero", True, None),
    ("c_embed", "commit_message", "py", "Embed max_delay in retry_call", True, None),
    ("c_string_noun", "commit_message", "py", "String-format the retry delay", True, None),
    ("c_ensure_tests_pass_goal", "commit_message", "py", "Make tests pass with a capped delay", True, None),
    ("c_so_tests_pass", "commit_message", "py", "Cap delay\n\nCap the delay so tests pass on slow machines.", True, None),
    ("c_fix_tests_to_pass", "commit_message", "py", "Cap delay\n\nThis lets the flaky tests to pass reliably.", True, None),
    ("c_builtin_ident", "commit_message", "py", "Raise `ValueError` for `None` delay in `retry_call`", True, None),
    ("c_dotted_ident", "commit_message", "py", "Use `time.sleep` with `min` in `retry_call`", True, None),
    ("c_call_parens", "commit_message", "py", "Cap `retry_call()` delay", True, None),
    ("c_tool_command", "commit_message", "py", "Cap delay\n\nRun `pytest` to see it.", True, None),
    ("c_attr_json", "commit_message", "py", "Cap delay before `response.json` is read", True, None),
    ("c_version_number", "commit_message", "py", "Cap delay at 30.0 seconds", True, None),
    ("c_150_words", "commit_message", "py", "Cap delay\n\n" + " ".join(["word"] * 150), True, None),
    ("c_72_chars", "commit_message", "py", "Cap delay " + "x" * 62, True, None),
    ("c_ts_ident", "commit_message", "ts", "Filter empty parts in `parseQuery`", True, None),
    ("c_ts_dollar", "commit_message", "ts", "Return `$empty` for empty input in parse.ts", True, None),
    ("c_go_ident", "commit_message", "go", "Guard nil `oldest` in `Cache.Evict`", True, None),
    ("c_go_path", "commit_message", "go", "Fix nil panic in `internal/cache/lru.go`", True, None),
    # ---------- commit: must be REJECTED
    ("c_past", "commit_message", "py", "Added max_delay to retry_call", False, "not_imperative"),
    ("c_third_person", "commit_message", "py", "Adds max_delay to retry_call", False, "not_imperative"),
    ("c_third_person_es", "commit_message", "py", "Fixes the delay cap", False, "not_imperative"),
    ("c_gerund", "commit_message", "py", "Adding max_delay to retry_call", False, "not_imperative"),
    ("c_past_irregular", "commit_message", "py", "Made the delay capped", False, "not_imperative"),
    ("c_past_after_prefix", "commit_message", "py", "fix(retry): capped the delay", False, "not_imperative"),
    ("c_73_chars", "commit_message", "py", "Cap delay " + "x" * 63, False, "subject_too_long"),
    ("c_preamble_here", "commit_message", "py", "Here is the commit message:\n\nAdd max_delay", False, "preamble"),
    ("c_preamble_sure", "commit_message", "py", "Sure! Add max_delay", False, "preamble"),
    ("c_no_blank", "commit_message", "py", "Add max_delay\nbody directly below", False, "no_blank_after_subject"),
    ("c_body_151", "commit_message", "py", "Cap delay\n\n" + " ".join(["word"] * 151), False, "body_too_long"),
    ("c_fence_inner", "commit_message", "py", "Cap delay\n\n```py\nx = 1\n```", False, "fence"),
    ("c_empty", "commit_message", "py", "   ", False, "empty"),
    ("c_think_only", "commit_message", "py", "<think>only thinking</think>", False, "empty"),
    ("c_fab_path_backtick", "commit_message", "py", "Add `utils/helpers.py` support", False, "ungrounded_path"),
    ("c_fab_path_plain", "commit_message", "py", "Update src/other/foo.py handling", False, "ungrounded_path"),
    ("c_fab_bare_ext", "commit_message", "py", "Update `settings.json` loading", False, "ungrounded_path"),
    ("c_fab_symbol", "commit_message", "py", "Add `max_wait` option to retry_call", False, "ungrounded_symbol"),
    ("c_dotted_plain_component_lenient", "commit_message", "py", "Use `retry_call.timeout` for the cap", True, None),
    ("c_fab_flag", "commit_message", "py", "Add `--turbo` flag to retry_call", False, "ungrounded_flag"),
    ("c_claim_all_pass", "commit_message", "py", "Add max_delay\n\nAll tests pass.", False, "test_claim"),
    ("c_claim_n_passed", "commit_message", "py", "Add max_delay\n\n12 passed locally.", False, "test_claim"),
    ("c_claim_tested_locally", "commit_message", "py", "Add max_delay\n\nTested locally.", False, "test_claim"),
    ("c_claim_i_ran", "commit_message", "py", "Add max_delay\n\nI ran the test suite.", False, "test_claim"),
    ("c_claim_ci_green", "commit_message", "py", "Add max_delay\n\nCI is green.", False, "test_claim"),
    ("c_claim_tests_line", "commit_message", "py", "Add max_delay\n\nTests: 12 passed, 0 failed", False, "test_claim"),
    ("c_ts_fab_symbol", "commit_message", "ts", "Filter empty parts in `parseUrl`", False, "ungrounded_symbol"),
    ("c_go_fab_path", "commit_message", "go", "Fix panic in `internal/store/lru.go`", False, "ungrounded_path"),
    ("c_plain_value_ok", "commit_message", "py", "Cap delay on `linux`, `darwin` and `win32`", True, None),
    ("c_plain_word_ok", "commit_message", "py", "Cap `delay` for `retry` calls", True, None),
    ("c_camel_fab", "commit_message", "ts", "Filter empty parts in `parseQueryString`", False, "ungrounded_symbol"),
    ("c_pascal_fab", "commit_message", "go", "Guard nil `oldest` in `CacheManager.Evict`", False, "ungrounded_symbol"),
    ("c_dotted_fab_component", "commit_message", "py", "Cap `retry.maxDelay` for the loop", False, "ungrounded_symbol"),
    ("c_fab_issue_ref", "commit_message", "py", "Cap delay\n\nFixes #4821.", False, "ungrounded_issue_ref"),
    ("c_fab_url", "commit_message", "py", "Cap delay\n\nSee " + "https:" + "//example.com/retry for background.", False, "ungrounded_url"),
    ("c_issue_ref_in_diff", "commit_message", "go", "Guard nil `oldest` in `Cache.Evict`\n\nRefs #12.", True, None),
    ("c_hash_number_not_in_diff", "commit_message", "py", "Cap delay\n\nSee section #10 of the docs.", False, "ungrounded_issue_ref"),
    # ---------- pr: must be ACCEPTED
    ("p_ok", "pr_description", "py", PR_OK, True, None),
    ("p_outer_fence", "pr_description", "py", "```markdown\n" + PR_OK + "```", True, None),
    ("p_star_bullets", "pr_description", "py", PR_OK.replace("- Run", "* Run").replace("- Call", "* Call"), True, None),
    ("p_numbered", "pr_description", "py", PR_OK.replace("- Run", "1. Run").replace("- Call", "2. Call"), True, None),
    ("p_checkboxes", "pr_description", "py", PR_OK.replace("- Run", "- [ ] Run").replace("- Call", "- [ ] Call"), True, None),
    ("p_heading_case", "pr_description", "py", PR_OK.replace("## Test plan", "## Test Plan"), True, None),
    ("p_heading_colon", "pr_description", "py", PR_OK.replace("## Test plan", "## Test plan:"), True, None),
    ("p_plan_verify_tests_pass", "pr_description", "py", PR_OK.replace("- Call", "- Verify that the tests pass"), True, None),
    ("p_plan_ensure_pass", "pr_description", "py", PR_OK.replace("- Call", "- Ensure all tests pass"), True, None),
    ("p_plan_pytest_k", "pr_description", "py", PR_OK.replace("- Call `retry_call` with a failing function and check the sleep times", "- Run `pytest tests/test_retry.py::test_retry_call_caps_delay -x`"), True, None),
    ("p_120_words", "pr_description", "py", "## Summary\n" + " ".join(["word"] * 120) + "\n\n## Test plan\n- Run the tests\n", True, None),
    ("p_8_bullets", "pr_description", "py", "## Summary\n" + " ".join(["word"] * 20) + "\n\n## Test plan\n" + "".join(f"- step {i}\n" for i in range(8)), True, None),
    ("p_ts", "pr_description", "ts", "## Summary\nFilter empty segments in `parseQuery` and return `$empty` when no segment remains, so empty input no longer builds an empty query object.\n\n## Test plan\n- Call `parseQuery('')` and check the result\n", True, None),
    ("p_code_block_in_plan", "pr_description", "py", "## Summary\nAdd a `max_delay` cap to `retry_call` so the sleep between attempts stays bounded for long chains of retries.\n\n## Test plan\n- Run:\n```\npytest tests/test_retry.py\n```\n- Check the delay\n", True, None),
    # ---------- pr: must be REJECTED
    ("p_no_summary", "pr_description", "py", "## Test plan\n- Run the tests\n", False, "sections_not_exactly_summary_test_plan"),
    ("p_no_plan", "pr_description", "py", "## Summary\n" + " ".join(["word"] * 20) + "\n", False, "sections_not_exactly_summary_test_plan"),
    ("p_extra_section", "pr_description", "py", PR_OK + "\n## Notes\nSomething.\n", False, "sections_not_exactly_summary_test_plan"),
    ("p_title_heading", "pr_description", "py", "# Cap retry delay\n\n" + PR_OK, False, "sections_not_exactly_summary_test_plan"),
    ("p_wrong_order", "pr_description", "py", "## Test plan\n- Run the tests\n\n## Summary\n" + " ".join(["word"] * 20) + "\n", False, "sections_not_exactly_summary_test_plan"),
    ("p_h3_headings", "pr_description", "py", PR_OK.replace("## ", "### "), False, "sections_not_exactly_summary_test_plan"),
    ("p_summary_121", "pr_description", "py", "## Summary\n" + WORDS + "\n\n## Test plan\n- Run the tests\n", False, "summary_too_long"),
    ("p_summary_short", "pr_description", "py", "## Summary\nCap delay.\n\n## Test plan\n- Run the tests\n", False, "summary_too_short"),
    ("p_no_bullets", "pr_description", "py", "## Summary\n" + " ".join(["word"] * 20) + "\n\n## Test plan\nRun the tests and check.\n", False, "no_plan_bullets"),
    ("p_9_bullets", "pr_description", "py", "## Summary\n" + " ".join(["word"] * 20) + "\n\n## Test plan\n" + "".join(f"- step {i}\n" for i in range(9)), False, "too_many_plan_bullets"),
    ("p_text_before", "pr_description", "py", "Here you go:\n\n" + PR_OK, False, "text_before_summary"),
    ("p_claim_summary", "pr_description", "py", PR_OK.replace("The previous fixed", "All tests pass. The previous fixed"), False, "test_claim"),
    ("p_claim_manual", "pr_description", "py", PR_OK.replace("The previous fixed", "I tested this manually. The previous fixed"), False, "test_claim"),
    ("p_fab_symbol_plan", "pr_description", "py", PR_OK.replace("`retry_call` with a failing", "`retry_forever` with a failing"), False, "ungrounded_symbol"),
    ("p_fab_path_plan", "pr_description", "py", PR_OK.replace("tests/test_retry.py", "tests/test_backoff.py"), False, "ungrounded_path"),
    ("p_fab_flag_plan", "pr_description", "py", PR_OK.replace("`pytest tests/test_retry.py`", "`pytest --turbo-mode tests/test_retry.py`"), False, "ungrounded_flag"),
    ("p_empty", "pr_description", "py", "", False, "empty"),
]


def run() -> dict[str, Any]:
    """Grade every case; ``unexpected`` lists the cases that did not behave as pre-registered."""
    unexpected: list[dict[str, Any]] = []
    for name, job, fx, text, ok, reason in CASES:
        diff, files = FIX[fx]
        g = G.grade_text({"stratum": job, "diff": diff, "files": files}, text)
        good = g["ok"] == ok and (ok or reason in g["reasons"])
        if not good:
            unexpected.append({"case": name, "expected_ok": ok, "expected_reason": reason, "got_ok": g["ok"], "got": g["reasons"]})
    return {"n": len(CASES), "unexpected": unexpected, "n_accept": sum(1 for c in CASES if c[4]), "n_reject": sum(1 for c in CASES if not c[4])}
