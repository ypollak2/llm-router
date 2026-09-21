# Technical debt

Known, deliberately unfixed. Each entry says what would make it worth fixing.

## Noisy existing-test search in verifier proposals

**Symptom.** A verifier proposal for a task naming `src/query.py` reported
**148 existing test files** as reuse candidates. The real number is one or two.

**Cause.** `propose.find_existing_tests()` matches a test file when the source
file's *stem* appears in the test's name or body. Stems like `query`, `config`,
`client` and `router` occur in most files in this repo, so the body search
matches almost everything.

**Impact.** Cosmetic today: the proposal's chosen strategy and acceptance
contract are unaffected, and `existing_sufficient` is always `False` regardless.
It costs a reviewer attention — a list of 148 files is the same as no list.

**Why not fixed.** No real candidate has been reviewed yet. Fixing it now would
be tuning a heuristic against synthetic examples, and the right threshold is
only knowable from what real proposals look like.

**Fix when.** It blocks review of an actual candidate — i.e. a reviewer says the
reuse list is unusable. Likely shape: require an import of the module under
test, or a symbol-level match, rather than a bare stem occurrence.

Found 2026-09-20 · `scripts/groundtruth/propose.py:find_existing_tests`
