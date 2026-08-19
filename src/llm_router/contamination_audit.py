# SPDX-License-Identifier: MIT
"""SHA-256 contamination audit — the load-bearing guarantee of Firewall v2.

Firewall v2 (``Docs/archive/ROUTERARENA_CLEAN_075_PLAN.md`` §2) upgrades academic-
benchmark *train/validation* splits from "banned" to "allowed **iff** a hash
audit proves 0 overlap with RouterArena's eval prompts." This module is that
audit, in a form that is (a) pure and unit-testable, (b) importable by the
centroid generator, and (c) runnable in CI on every build.

The normalization pipeline matches Nadir #159's accepted pattern:
``NFC → strip → collapse-whitespace → casefold`` before hashing, so trivially
reformatted duplicates (extra spaces, case, unicode form) still collide.

Reading RA prompt hashes *solely* to prove non-use is the accepted transparency
mechanism — it is an audit, not training. This module never returns RA prompt
text; it compares hashes and reports counts + the (capped) list of colliding
hashes so a reviewer can verify the disjointness claim without the audit itself
leaking anything usable back into a router parameter.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass

_WS_RE = re.compile(r"\s+")

# Cap on how many overlapping hashes we enumerate in the report — enough for a
# reviewer to spot-check, bounded so a pathological all-overlap run can't emit a
# multi-megabyte report.
_OVERLAP_SAMPLE_CAP = 50


def normalize(text: str) -> str:
    """Normalize a prompt for contamination comparison.

    ``NFC → strip → collapse internal whitespace → casefold``. Deterministic and
    idempotent: ``normalize(normalize(x)) == normalize(x)``.
    """
    text = unicodedata.normalize("NFC", text)
    text = _WS_RE.sub(" ", text).strip()
    return text.casefold()


def prompt_hash(text: str) -> str:
    """SHA-256 hex digest of the normalized prompt."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def hash_set(prompts: list[str]) -> set[str]:
    """Set of normalized-prompt hashes for a corpus."""
    return {prompt_hash(p) for p in prompts}


@dataclass(frozen=True)
class AuditReport:
    """Machine-checkable audit result — serialized to
    ``ROUTERARENA_CONTAMINATION_AUDIT.json`` and shipped in the PR.

    ``clean`` is the single gate the CI check and the generator both read:
    ``True`` ⇒ this training corpus may inform router parameters.
    """

    mode: str                    # "compared" | "by_construction"
    clean: bool
    total_train_prompts: int
    unique_train_hashes: int
    ra_hash_count: int
    overlap_count: int
    overlap_sample: list[str]    # up to _OVERLAP_SAMPLE_CAP colliding hashes
    normalization: str = "NFC|strip|collapse-ws|casefold"

    def as_dict(self) -> dict:
        return asdict(self)


def audit(
    train_prompts: list[str],
    *,
    ra_prompts: list[str] | None = None,
    ra_hashes: set[str] | None = None,
) -> AuditReport:
    """Audit a training corpus for overlap with RouterArena's eval prompts.

    Provide RA content one of two ways (``ra_hashes`` wins if both are given):
      * ``ra_hashes`` — precomputed normalized hashes (what CI should cache).
      * ``ra_prompts`` — raw RA eval prompts; hashed here for the comparison.

    With neither, the audit runs in **by_construction** mode: it can only assert
    the corpus is self-consistent, and ``clean`` reflects "no RA set to compare
    against" — the caller must be a purely self-generated pipeline for that to be
    a valid clean claim (state this explicitly in the PR).

    Returns an :class:`AuditReport`; never raises on overlap (the caller decides
    whether to abort), so CI and the generator can both format their own errors.
    """
    train_hashes = hash_set(train_prompts)

    if ra_hashes is None and ra_prompts is not None:
        ra_hashes = hash_set(ra_prompts)

    if ra_hashes is None:
        return AuditReport(
            mode="by_construction",
            clean=True,
            total_train_prompts=len(train_prompts),
            unique_train_hashes=len(train_hashes),
            ra_hash_count=0,
            overlap_count=0,
            overlap_sample=[],
        )

    overlap = sorted(train_hashes & ra_hashes)
    return AuditReport(
        mode="compared",
        clean=len(overlap) == 0,
        total_train_prompts=len(train_prompts),
        unique_train_hashes=len(train_hashes),
        ra_hash_count=len(ra_hashes),
        overlap_count=len(overlap),
        overlap_sample=overlap[:_OVERLAP_SAMPLE_CAP],
    )
