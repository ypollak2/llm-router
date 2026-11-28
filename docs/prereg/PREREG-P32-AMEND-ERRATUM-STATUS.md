# PREREG-P32 erratum: the Status line of AMEND1 and AMEND2 is stale

Status: **NOTE, not a frozen document and not an amendment.** It changes no byte of any frozen file.

`docs/prereg/PREREG-P32-AMEND1.md` (line 3) and `docs/prereg/PREREG-P32-AMEND2.md` (line 3) still read
"Status: **DRAFT, not frozen.**" That was true when each was written. Each now has a sidecar beside it:

| file | sha256 recomputed on origin/main 39098a3e | sidecar |
|---|---|---|
| `PREREG-P32-AMEND1.md` | `76879f9830cf03a3f85d680ac3c8fbaa8a18a62a30f619dd8bb7687d74ae2afe` | `PREREG-P32-AMEND1.sha256`, same hash, `frozen_utc 2026-10-27T17:19:48Z` |
| `PREREG-P32-AMEND2.md` | `32019a21f42d5ff6655fa5bde8be1115d2015aa9f251dbd67be373ef5dd1b63c` | `PREREG-P32-AMEND2.sha256`, same hash, `frozen_utc 2026-11-02T22:49:49Z` |

Source: `git show origin/main:<file> | sha256sum` and `cat <sidecar>`, 2026-11-25.

The Status line cannot be corrected in place: editing either file changes its hash and voids its sidecar. The frozen
state is defined by the sidecar plus a passing `scripts/check_prereg.py`, not by the Status line. This note does not
claim that the D-122 independent freeze-guard review took place for either amendment; the sidecars exist, and the
review record was not checked by this note. The coordinator should confirm that record before relying on it.
