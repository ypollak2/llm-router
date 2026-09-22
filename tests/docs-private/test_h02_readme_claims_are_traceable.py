"""H-02 — a measurement claim in the README must be traceable to its source.

The 2026-09-21 audit found the README's headline read:

    "Measured on 105 real prompts from this author's own sessions,
     76% produced a draft and 72% produced one worth relaying."

None of that triple survived checking against `docs/MEASUREMENT.md`:

  * **n=105 appears nowhere.** The run was n=115.
  * **76%** is the *baseline* draft rate, from the "before" column.
  * **72%** is the *after* acceptance rate, from a different column of a
    different row.

So a before-number and an after-number were paired as though they described one
run, under an n that no run had. The source table is honest and carries its
conditions; the summary written on top of it was not.

The sharpest part: `docs/MEASUREMENT.md` contains a section titled "A partial
rate is not a rate", written after quoting a running total as a result cost a
measurement round. The README then did a worse version of the same thing.

House rule (`~/.claude/CLAUDE.md`): *a number in a README, CHANGELOG or release
note carries its n, its window and the file it came from, or it does not ship.
Shipping without the claim is always available and is often right.*

These tests encode that rule for the README.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
README = REPO / "README.md"
MEASUREMENT = REPO / "docs" / "MEASUREMENT.md"


# Shapes that assert a measured result, as opposed to a version, a port, or a
# percentage inside a code sample.
_CLAIM_PATTERNS = [
    re.compile(r"[Mm]easured on\s+([\d,]+)\s+(?:real\s+)?prompts?"),
    re.compile(r"on\s+([\d,]+)\s+real\s+prompts?"),
    re.compile(r"(\d+)%\s+observed"),
    re.compile(r"(\d+[–-]\d+)%\s+savings"),
    re.compile(r"(\d+)%\s+produced"),
]


def _claims(text: str) -> list[tuple[str, str]]:
    out = []
    for rx in _CLAIM_PATTERNS:
        for m in rx.finditer(text):
            out.append((rx.pattern, m.group(0)))
    return out


def test_readme_carries_no_untraceable_measurement_claim():
    """Every measurement-shaped claim in the README must appear in MEASUREMENT.md.

    Withdrawing the claim always satisfies this. Restating it with its n, window
    and source also satisfies it. Inventing a number does not.
    """
    readme = README.read_text(encoding="utf-8")
    source = MEASUREMENT.read_text(encoding="utf-8")

    untraceable = []
    for pattern, claim in _claims(readme):
        number = re.search(r"[\d,]+(?:[–-]\d+)?", claim)
        if number and number.group(0) not in source:
            untraceable.append(claim)

    assert not untraceable, (
        "README states measurement claims that docs/MEASUREMENT.md does not "
        f"support: {untraceable}. Either withdraw the claim, or restate it with "
        "the n, window and conditions the source actually records."
    )


def test_the_retired_splice_does_not_return():
    """The specific defect, pinned.

    A regression here means someone re-derived the same triple, which is the
    strongest signal that the summarising step — not the measurement — is where
    this project loses accuracy.
    """
    readme = README.read_text(encoding="utf-8")
    assert "105 real prompts" not in readme
    assert "105 prompts" not in readme
    assert not re.search(r"76%\s+produced a draft", readme)


def test_claim_scanner_is_not_vacuous():
    """A scanner that finds nothing has not been shown to work (repo rule).

    The README legitimately contains no measurement claims right now, so the
    scanner must be proven against the exact text that was removed — otherwise
    `test_readme_carries_no_untraceable_measurement_claim` passes for the wrong
    reason and would keep passing if a claim came back in a shape it cannot see.
    """
    retired = (
        "Measured on 105 real prompts from this author's own sessions, "
        "76% produced a draft and 72% produced one worth relaying."
    )
    found = _claims(retired)
    assert found, "the scanner no longer recognises the very claim it was written for"
    assert any("105" in c for _, c in found)

    source = MEASUREMENT.read_text(encoding="utf-8")
    assert "105" not in source, (
        "docs/MEASUREMENT.md now contains 105, so the known-positive fixture "
        "above would no longer be detected as untraceable — pick another."
    )


@pytest.mark.parametrize("path", [README, MEASUREMENT])
def test_sources_exist_and_are_non_trivial(path):
    """Guards the denominator: an empty or missing file passes every check above."""
    assert path.exists(), f"{path} is missing"
    assert len(path.read_text(encoding="utf-8")) > 2000, f"{path} is implausibly short"
