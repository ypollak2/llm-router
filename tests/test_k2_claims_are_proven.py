"""K2 — a claim with no proof fails CI.

The audit's phase 2 found `llm-router status`, the README's own first "verify
it worked" step, crashing on every clean install. That finding lived in a
document, and a document cannot fail CI.

This file is the enforcement half. It does NOT check that claims are true —
the proof tests do that. It checks that the LEDGER does not rot:

  * every claim names a proof test that exists, or explains why it cannot;
  * no claim is silently UNPROVEN;
  * a quantitative claim in the README has a ledger row.

The one UNPROVEN row is deliberate and is the most important line in the file.
"preserves task success" is the product's central claim and it is not proven:
the only mechanism that could measure it was off by default, so there is no
dataset, and neither downgrade-regret nor upgrade-waste is computed anywhere.
Marking it PROVEN would be the exact failure this ledger exists to catch, so
the suite tolerates exactly the rows listed in `KNOWN_UNPROVEN` and fails on
any other.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from claims_ledger import (  # noqa: E402
    LEDGER,
    PROVEN,
    SCOPED,
    UNPROVEN,
    WITHDRAWN,
)

REPO = pathlib.Path(__file__).resolve().parents[1]

#: Claims allowed to be UNPROVEN, each with the reason it is not simply
#: deleted. Adding to this list is a product decision, not a test fix.
KNOWN_UNPROVEN = {
    "preserves-task-success": (
        "R8's measurement half is not done: capture was off by default, so "
        "there is no dataset, and neither downgrade-regret nor upgrade-waste "
        "is computed anywhere in the repo. The two honest options remain "
        "'measure it' or 'remove the claim'."
    ),
}


def test_the_ledger_is_not_empty():
    """Anti-vacuity. Every assertion below is trivial over an empty ledger."""
    assert len(LEDGER) >= 15, f"the claims ledger holds only {len(LEDGER)} rows"


def test_claim_ids_are_unique():
    ids = [c.id for c in LEDGER]
    assert len(ids) == len(set(ids)), "duplicate claim id"


@pytest.mark.parametrize("claim", LEDGER, ids=lambda c: c.id)
def test_every_claim_has_a_valid_status(claim):
    assert claim.status in (PROVEN, SCOPED, UNPROVEN, WITHDRAWN), claim.status
    assert claim.text.strip(), f"{claim.id} has no claim text"
    assert claim.source.strip(), f"{claim.id} does not say where it is claimed"


@pytest.mark.parametrize("claim", [c for c in LEDGER if c.status == PROVEN],
                         ids=lambda c: c.id)
def test_a_proven_claim_names_a_test_that_exists(claim):
    assert claim.proof, f"{claim.id} is PROVEN but names no proof"
    path = REPO / claim.proof.split("::")[0]
    assert path.exists(), (
        f"{claim.id} is proven by {claim.proof}, which does not exist. A claim "
        "pointing at a deleted test is worse than an unproven one — it carries "
        "the appearance of evidence."
    )


@pytest.mark.parametrize("claim", [c for c in LEDGER
                                   if c.status in (PROVEN, SCOPED) and c.proof],
                         ids=lambda c: c.id)
def test_the_proof_test_actually_collects(claim):
    """A file that exists but collects nothing proves nothing."""
    target = claim.proof.split("::")[0]
    r = subprocess.run(
        [sys.executable, "-m", "pytest", target, "--collect-only", "-q",
         "-p", "no:randomly"],
        cwd=REPO, capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, (
        f"{claim.id}: its proof {target} does not collect:\n{r.stdout[-800:]}"
    )
    # `-q --collect-only` prints "<path>: <count>" per file, not the verbose
    # "N tests collected" line. Parsing the wrong format made this assertion
    # fail on every row — a check that cannot pass is as useless as one that
    # cannot fail.
    counts = [int(m) for m in re.findall(r":\s*(\d+)\s*$", r.stdout, re.M)]
    assert counts and sum(counts) > 0, (
        f"{claim.id}: {target} collected no tests.\n{r.stdout[-400:]}"
    )


@pytest.mark.parametrize("claim", [c for c in LEDGER if c.status == SCOPED],
                         ids=lambda c: c.id)
def test_a_scoped_claim_states_its_limit(claim):
    """SCOPED without the limit written down is PROVEN with extra steps.

    A SCOPED claim may have NO proof test — some limits are documentary, like
    "these are single-user observations", which is not a property of the code
    and cannot have a test. What it may never lack is the statement of the
    limit itself.
    """
    assert len(claim.note) > 60, (
        f"{claim.id} is SCOPED but its note does not say what the limit is. "
        "A scope nobody can read is not a scope."
    )


def test_no_claim_is_unproven_without_a_recorded_reason():
    unexpected = [
        c.id for c in LEDGER
        if c.status == UNPROVEN and c.id not in KNOWN_UNPROVEN
    ]
    assert not unexpected, (
        f"claim(s) marked UNPROVEN with no entry in KNOWN_UNPROVEN: "
        f"{unexpected}\n\nEither prove it, scope it, delete the claim from the "
        "product, or add it here with the reason it stays. Shipping without "
        "the claim is always available and is often right."
    )


def test_the_known_unproven_list_has_not_grown_silently():
    """A ratchet. Claims should move toward PROVEN, not accumulate excuses."""
    assert len(KNOWN_UNPROVEN) <= 1, (
        f"{len(KNOWN_UNPROVEN)} claims are excused as UNPROVEN. That list is a "
        "record of what the product asserts without evidence, and it is "
        "supposed to shrink."
    )
    for cid, reason in KNOWN_UNPROVEN.items():
        assert len(reason) > 80, f"{cid}: the excuse does not explain itself"
        row = next((c for c in LEDGER if c.id == cid), None)
        assert row is not None, f"{cid} is excused but is not in the ledger"
        # FOUND BY THIS FILE'S OWN RED-CHECK: marking the excused claim PROVEN
        # with a loosely-related test passed everything. That is the failure
        # this ledger exists to catch, one level up — silencing the
        # uncomfortable row rather than earning it.
        #
        # Promoting a KNOWN_UNPROVEN claim now requires REMOVING it from that
        # dict, which is a deliberate act with a visible diff, rather than a
        # quiet status edit.
        assert row.status == UNPROVEN, (
            f"{cid} is listed in KNOWN_UNPROVEN but its ledger status is "
            f"{row.status}. If it is genuinely proven now, delete it from "
            "KNOWN_UNPROVEN in the same commit — a claim cannot be both "
            "excused as unproven and asserted as proven."
        )


#: README lines that look quantitative but are not product claims.
_README_NOISE = re.compile(
    r"^\s*<|badge|shields\.io|mcptoplist|img src|alt=|href=|^\|?\s*-{2,}"
)

#: Lines carrying a number that is not a claim about behaviour: diagram
#: labels, prices quoted as illustration. Each is listed individually — a
#: pattern would drift into excusing real claims.
_NOT_A_CLAIM = {
    268: "ASCII architecture diagram label, not an assertion about savings",
    290: "carries its own '~' qualifier inline",
    395: "carries its own '~' qualifier inline",
}


def test_readme_quantitative_claims_have_a_ledger_row():
    """A number in the README is a claim. It needs a row, or a listed reason.

    Coverage is DECLARED (`Claim.readme_lines`), not guessed. The first version
    matched claims to lines by keyword overlap, and it failed on the per-host
    savings table: two of its four rows matched and two did not, for no reason
    anyone could act on. A ledger whose coverage is a heuristic is not a
    ledger.

    The scan is deliberately narrow — `N%` and `$N` only. Broader would fire on
    badges, image widths and version strings, get allowlisted wholesale, and
    the allowlist would become the blind spot.
    """
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    covered = {n for c in LEDGER for n in c.readme_lines}

    unmatched = []
    for i, line in enumerate(readme.splitlines(), 1):
        if _README_NOISE.search(line) or i in covered or i in _NOT_A_CLAIM:
            continue
        if not re.search(r"\b\d+(?:[\u2013-]\d+)?%|\$\d", line):
            continue
        # A line that scopes itself inline needs no row.
        if any(w in line.lower() for w in
               ("observation", "approx", "~", "single-user", "particular")):
            continue
        unmatched.append(f"README.md:{i}: {line.strip()[:90]}")

    assert not unmatched, (
        "README line(s) making a quantitative claim with no ledger row, no "
        "inline scoping, and no entry in _NOT_A_CLAIM:\n  "
        + "\n  ".join(unmatched)
        + "\n\nAdd the line number to the covering Claim.readme_lines, scope "
        "the sentence, or delete the number."
    )


def test_the_declared_readme_lines_still_carry_a_number():
    """A declared line that no longer has a claim on it means the README moved
    and the ledger did not — coverage pointing at the wrong line is worse than
    no coverage, because it reports the claim as handled."""
    lines = (REPO / "README.md").read_text(encoding="utf-8").splitlines()
    stale = []
    for c in LEDGER:
        for n in c.readme_lines:
            if n > len(lines):
                stale.append(f"{c.id}: README.md:{n} is past end of file")
                continue
            if not re.search(r"\b\d+(?:[\u2013-]\d+)?%|\$\d|secret|never",
                             lines[n - 1], re.I):
                stale.append(f"{c.id}: README.md:{n} no longer carries a claim: "
                             f"{lines[n - 1].strip()[:60]!r}")
    assert not stale, "\n  ".join([""] + stale)
