"""Regression / guard: CHZ-AUD-010 — fabricated public claims must not reappear.

The audit found unbacked magnitude claims in the PyPI description and README
("3× longer sessions", "60–90% token savings") and unqualified absolutes
("every prompt flows", "no cloud", "always routes"). This guard scans the
user-facing surfaces the claim-linter previously never checked (pyproject
description + README) and fails if a fabricated/unqualified claim returns. It is
the seed of the G6 claims gate.
"""
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Patterns that must NOT appear unqualified in public marketing surfaces.
FORBIDDEN = [
    re.compile(r"3[×x]\s*longer", re.I),
    re.compile(r"60[–-]?90%", re.I),
    re.compile(r"every prompt flows to the model that fits it", re.I),
    re.compile(r"\bno cloud\b", re.I),
    re.compile(r"zero data leaves your machine", re.I),
]


def _pyproject_description() -> str:
    return tomllib.load(open(ROOT / "pyproject.toml", "rb"))["project"]["description"]


def test_pyproject_description_has_no_fabricated_claims():
    desc = _pyproject_description()
    for pat in FORBIDDEN:
        assert not pat.search(desc), f"fabricated claim in PyPI description: {pat.pattern}"


def test_readme_headline_has_no_fabricated_claims():
    # Scan the first 60 lines (title + hero) where the least-hedged claims lived.
    head = "\n".join((ROOT / "README.md").read_text().splitlines()[:60])
    for pat in FORBIDDEN:
        assert not pat.search(head), f"fabricated claim in README hero: {pat.pattern}"


# RED2-05: the magnitude claim ("60-90%") was still baked into IDE-config
# templates in install_hooks.py, which the two hand-picked scans above never saw.
# The MAGNITUDE claims must not appear ANYWHERE the product writes to a user's
# machine. We scan all shipped source (.py templates + generated .md/.json), not
# just marketing surfaces. (Absolutes like "no cloud" are context-dependent and
# stay scoped to the marketing surfaces above.)
MAGNITUDE_FORBIDDEN = [
    re.compile(r"3[×x]\s*longer", re.I),
    re.compile(r"60[–-]?90\s*%", re.I),
    # RED2-3-02: any unqualified "NN-NNx"/"NNx" cost/speed multiplier claim
    # (e.g. "50–100x less", "100x cheaper"). The guard previously only knew the
    # two specific phrasings above, so "50–100x" shipped into installed rules.
    re.compile(r"\d+\s*[–-]\s*\d+\s*[x×]\b", re.I),
    re.compile(r"\b\d{2,}\s*[x×]\s*(?:less|cheaper|faster|savings?)\b", re.I),
]


def test_no_fabricated_magnitude_claims_anywhere_in_src():
    offenders = []
    # Scan shipped code AND every surface the product ships to a user: src code,
    # bundled rules, and the repo-root skills/ dir (RED2-4-03 — a skill file had a
    # live "50× cheaper" claim the src-only scan missed).
    roots_and_globs = [
        (ROOT / "src" / "llm_router", ("**/*.py", "**/*.md", "**/*.mdc")),
        (ROOT / "skills", ("**/*.md", "**/*.mdc")),
    ]
    for base, globs in roots_and_globs:
        if not base.exists():
            continue
        for g in globs:
            for path in base.rglob(g.replace("**/", "")):
                # skip training-data JSONL and binary
                try:
                    text = path.read_text()
                except OSError:
                    continue
                for pat in MAGNITUDE_FORBIDDEN:
                    if pat.search(text):
                        offenders.append(f"{path.relative_to(ROOT)} :: {pat.pattern}")
    assert not offenders, (
        "fabricated magnitude claim(s) in shipped source (RED2-05/RED2-3-02/RED2-4-03): "
        + "; ".join(offenders)
    )


# RED2-5-04: the README is the single most user-facing surface, yet the guard
# only scanned its first 60 lines (FORBIDDEN phrases) and never ran the generic
# NN×/NN-NN× MAGNITUDE_FORBIDDEN patterns against it at all — so an unqualified
# hero multiplier ("3–5× Longer Sessions") shipped un-guarded. This scans the
# WHOLE README with the magnitude patterns. The one explicitly-disclaimed
# "Estimated savings by workload" block (illustrative/directional estimates with
# an audited-ratio methodology) is carved out — but ONLY when its disclaimer is
# actually present, so the carve-out can't be abused to smuggle in a bare claim.
_ESTIMATES_DISCLAIMER = "illustrative estimates — directional, not measured"


def _scannable_from_lines(lines: list[str]) -> str:
    """Pure carve-out logic (testable without touching the real README)."""
    heading = next(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("### Estimated savings by workload")),
        None,
    )
    if heading is None:
        return "\n".join(lines)
    # End of the section = next h2/h3 heading after it, else EOF.
    end = next(
        (j for j in range(heading + 1, len(lines))
         if lines[j].startswith("## ") or lines[j].startswith("### ")),
        len(lines),
    )
    section = lines[heading:end]
    if _ESTIMATES_DISCLAIMER in "\n".join(section):
        # RED2-7-02: exempt ONLY the disclaimed table-DATA rows (Markdown `|`
        # lines) — the specific illustrative figures the disclaimer qualifies —
        # NOT arbitrary prose. A section-wide carve-out let an unrelated,
        # unqualified prose claim be smuggled in next to the real disclaimer and
        # pass the guard. Prose lines in the block are still scanned.
        kept_section = [ln for ln in section if not ln.lstrip().startswith("|")]
        return "\n".join(lines[:heading] + kept_section + lines[end:])
    # Disclaimer missing → the block is no longer qualified; scan everything.
    return "\n".join(lines)


def _readme_scannable_text() -> str:
    return _scannable_from_lines((ROOT / "README.md").read_text().splitlines())


def test_readme_full_has_no_unqualified_magnitude_claims():
    text = _readme_scannable_text()
    offenders = []
    for pat in MAGNITUDE_FORBIDDEN:
        for m in pat.finditer(text):
            offenders.append(f"{pat.pattern} :: ...{text[max(0, m.start()-30):m.end()+10]!r}")
    assert not offenders, (
        "RED2-5-04: unqualified magnitude claim(s) in README outside the "
        "disclaimed estimates block: " + "; ".join(offenders)
    )


def test_readme_carveout_does_not_exempt_prose_in_the_disclaimed_block():
    """RED2-7-02: the carve-out must exempt only the disclaimed table-DATA rows,
    not arbitrary prose. A fabricated prose claim inserted into the disclaimed
    block must still be caught (tested via the pure helper, no file mutation)."""
    synthetic = [
        "# README",
        "### Estimated savings by workload",
        "> These are illustrative estimates — directional, not measured.",
        "Independent benchmarks show LLM Router is 100x cheaper in every case.",  # smuggled prose
        "| Workload | runway |",
        "| dev | ~2–3× more |",  # a legit disclaimed table figure
        "## Next section",
    ]
    scannable = _scannable_from_lines(synthetic)
    # The smuggled prose claim must survive into the scannable text and be caught…
    assert any(p.search(scannable) for p in MAGNITUDE_FORBIDDEN), (
        "RED2-7-02: prose claim smuggled into the disclaimed block was NOT caught"
    )
    # …while the legit table figure on a `|` row is exempted (not in scannable text).
    assert "~2–3× more" not in scannable, "disclaimed table figure should stay exempt"


def test_readme_estimates_carveout_requires_its_disclaimer():
    """The carve-out is only valid while the disclaimer is present — guard the guard."""
    readme = (ROOT / "README.md").read_text()
    if "### Estimated savings by workload" in readme:
        assert _ESTIMATES_DISCLAIMER in readme, (
            "the Estimated-savings block must keep its 'illustrative estimates — "
            "directional, not measured' disclaimer, or its magnitude figures are unqualified"
        )


def test_rules_version_bumped_when_content_changes():
    """RED2-4-02: llm_router.md content changes must bump llm_router-rules-version so
    check_and_update_rules() actually propagates them to installed users."""
    import re as _re
    rules = (ROOT / "src" / "llm_router" / "rules" / "llm_router.md").read_text()
    m = _re.search(r"llm_router-rules-version:\s*(\d+)", rules)
    assert m and int(m.group(1)) >= 7, (
        "rules-version must be >=7 after the iteration-3/4 content reword "
        "(RED2-4-02: a content change without a version bump does not auto-update)"
    )
