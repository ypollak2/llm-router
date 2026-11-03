---
id: RELSH-1
status: fixed in `bind/release-sh-portable` (PR #815; release tooling only, production unaffected)
---
## RELSH-1. The sdist leak check blocked the release on the legitimate module `src/llm_router/pricing.py`

- **Symptom (2026-11-03).** The dry run of `scripts/release/release.sh` (every step up to, not including, the tag) was
  BLOCKED at step 4 by the sdist leak check. Measured on the 16.0.0 sdist built from origin/main 5c056309
  (`uv build --sdist`, then `tar tzf`; n=1117 entries): the old pattern matched exactly 1 entry,
  `llm_routing-16.0.0/src/llm_router/pricing.py`. Nothing private was in the archive.
- **Cause.** The check was `tar tzf "$SDIST_FILE" | grep -i -E '...|MONETIZATION|ROADMAP|STRATEGY|PRICING|PHASE_COMPLETION|...'`.
  The five internal-document names were unanchored and `-i` made them case-insensitive, so `PRICING` matched the file name
  `pricing.py`. The names stand for documents (`pyproject.toml` excludes `PRICING.md`, `ROADMAP.md` and the others under
  "Private / internal docs - never publish"), not for modules; a module named `strategy.py` or `roadmap.py` would have
  blocked a release the same way. A second weakness sat in the same lines: a `tar tzf` that failed, or listed nothing,
  produced "sdist is clean", because an empty listing matches no pattern, and so did a `grep` that failed (exit status 2:
  the `|| true` that tolerated "no match" tolerated that too).
- **Fix.** `SDIST_BLOCK_RE`, `SDIST_NAMES_RE`, `SDIST_CODE_RE`, `sdist_leaks` and `verify_sdist_clean` in
  `scripts/release/release.sh` (the step 4 call). `.env`, `CLAUDE.md`, `.internal/`, `/research/`, `/deprecation/`,
  `/scripts/` and `/skills/` still block anywhere, unchanged. The five names block in any path that carries one,
  whatever the file type or none, so a document type nobody listed cannot slip through. The one exemption, from the names only,
  is Python source under the package tree (`<root>/src/**/*.py` and `*.pyi`); a module in one of the blocked folders
  (`src/llm_router/skills/x.py`) still blocks. A non-code file under `src/` named for one of them
  (`src/llm_router/data/pricing.json`) blocks too: that wants a decision, not a pass. The check fails closed: an unreadable
  archive, an empty listing, and a failing `grep` or `awk` all stop the release instead of printing "sdist is clean".
  After: 0 matches on the same n=1117 listing, 0 on the n=683 tracked files under `src/`, and `verify_sdist_clean` returns 0 on
  the real sdist. A first version of this fix (b01733bb) allowed only seven document extensions to carry the names; the
  independent review of PR #815 found that it passed 44 of its 91 probe paths that the old check blocked and that are not
  Python source (extension-less `ROADMAP`, `.xlsx`, `.rtf`, `.json`, `.zip`, a folder `strategy/`), so the rule was inverted to
  fail closed. On the same 91 probes the final rule differs from the old check only on the 3 legitimate modules
  `src/llm_router/pricing.py`, `pricing.pyi` and `strategy.py`.
- **Test.** `tests/test_release_sh_portable.py::test_a_source_file_named_like_an_internal_doc_is_not_a_leak`
  (`pricing.py` and 8 other Python-source names pass), `::test_internal_documents_still_block` (every extension the first
  version listed, `.rst`, `.markdown`, `.doc` and `.docx` included), `::test_the_names_block_in_any_file_type_or_none`
  (no extension, `.xlsx`, `.rtf`, `.json`, archives, odd backups, a folder named for one of them, non-code under `src/`),
  `::test_the_exemption_is_python_source_under_src_and_nothing_else`,
  `::test_the_other_internal_patterns_still_block_wherever_they_appear`,
  `::test_only_the_offending_lines_are_reported_and_each_once`,
  `::test_no_tracked_package_file_trips_the_leak_check` (every tracked file under `src/`, n=683 on 5c056309),
  `::test_verify_sdist_clean_refuses_an_archive_that_lists_nothing`, `::test_verify_sdist_clean_refuses_an_unreadable_archive`,
  `::test_a_leak_check_that_fails_is_not_a_clean_sdist` and `::test_sdist_leaks_fails_when_a_pattern_is_broken` (a broken
  pattern must not print "sdist is clean" for an archive holding `.env` and `PRICING.md`),
  `::test_step_four_runs_verify_sdist_clean_and_stops_the_release_when_it_fails` (the wiring of the fix: reverting Step 4 to an
  inline grep, or dropping its `|| exit 1`, fails it), and, marked `slow`, `::test_the_real_sdist_passes_the_leak_check`
  (builds the real sdist). Mutation check (scratch harness, not committed): 38 small edits to `release.sh` (this check, its
  Step 4 wiring, the timeout wrapper, `project_python`, the test-step default), each followed by a run of that test file, and
  every one makes at least one test fail. For this check they are: each alternative of the three patterns; the exemption
  removed, widened to any `.py` anywhere, widened to everything under `src/`, narrowed to `.py` without `.pyi`, or not
  anchored to the root `src/`; the names restricted to `.md` (the shape of b01733bb); `-i` dropped; duplicates not removed;
  `_grep_hits` swallowing errors; `verify_sdist_clean` ignoring a failed check; its two guards; and Step 4 back to an inline
  grep or without its `|| exit 1`. One edit survived the first pass, the unanchored `src/`; the cases `docs/src/pricing.py`
  and `integrations/pi/src/strategy.py` in `::test_the_exemption_is_python_source_under_src_and_nothing_else` now catch it.
  Fixed in 231e468e after the independent review of b01733bb.
