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
  produced "sdist is clean", because an empty listing matches no pattern.
- **Fix.** `SDIST_LEAK_RE`, `sdist_leaks` and `verify_sdist_clean` in `scripts/release/release.sh` (the step 4 call).
  The five names now match only document files (`.md .markdown .rst .txt .pdf .doc .docx`, also with a backup suffix such as
  `.bak`, `.orig` or `~`), wherever they sit, `src/` included. `.env`, `CLAUDE.md`, `.internal/`, `/research/`,
  `/deprecation/`, `/scripts/` and `/skills/` still block anywhere, unchanged. An unreadable archive or an empty listing
  now fails the check. After: 0 matches on the same n=1117 listing, and `verify_sdist_clean` returns 0 on the real sdist.
- **Test.** `tests/test_release_sh_portable.py::test_a_source_file_named_like_an_internal_doc_is_not_a_leak`
  (`pricing.py` and six other source or data names pass), `::test_internal_documents_still_block` and
  `::test_the_other_internal_patterns_still_block_wherever_they_appear` (the blocks that must survive),
  `::test_no_tracked_package_file_trips_the_leak_check` (every tracked file under `src/`, n=683 on 5c056309),
  `::test_verify_sdist_clean_refuses_an_archive_that_lists_nothing` and
  `::test_verify_sdist_clean_refuses_an_unreadable_archive`, and, marked `slow`,
  `::test_the_real_sdist_passes_the_leak_check` (builds the real sdist). Mutation check: putting the bare words back in place
  of the document-name alternative in `SDIST_LEAK_RE` fails 12 tests in that file (run on 1f91ba4e).
