#!/usr/bin/env python3
"""Numbers-provenance lint (W2-7 / UN-10, release rule 7.1.7, NFR-NUM).

Rule: every result-shaped number in README.md, docs/*.md and changelog.d/*.md
carries N or a window AND a source file. Anything without one is removed.
PR #766 cleaned the tree by hand; this keeps it clean.

What is flagged (a "result-shaped" number):
  percentages (73%), "N of M" (8 of 8), multipliers (3.5x), latencies (661 ms,
  1.2 s), dollar figures ($0.78), and token volumes (~25k tokens).

A unit (a paragraph, one list item, or one whole table) containing such a number
passes only if it also holds BOTH:
  * a source marker: a repo-style path (a/b.ext), a bare file name with a known
    extension, `PR #NNN`, or a `~/.rsi/...` path; and
  * an N/window marker: `n=`/`N=`, an ISO date, "N runs/prompts/calls/...",
    or "N of M".

Not results (never flagged, per the #766 rulings): version numbers, dates,
counts of items in a list, inventory counts, config thresholds and list prices.
The last two are recognised by a keyword on the unit (see CONFIG_WORDS); this
is a heuristic, so a miss is silenced with an inline escape:

    <!-- provenance: exempt (reason) -->

on the unit, or `<!-- provenance: exempt-file (reason) -->` anywhere in a file
whose figures all come from a named generator. The reason is mandatory. Fenced code blocks, inline code spans, URLs and HTML
comments are not scanned for numbers. CHANGELOG.md is released history and is
out of scope.

Ratchet: scripts/numbers_provenance_baseline.json lists units that were already
flagged when the lint landed. New flagged units fail; a baseline entry that no
longer matches anything also fails, so the baseline can only shrink.

Usage:
  python scripts/check_numbers_provenance.py            # check the repo
  python scripts/check_numbers_provenance.py --root DIR # check another tree
  python scripts/check_numbers_provenance.py --write-baseline
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASELINE_NAME = "scripts/numbers_provenance_baseline.json"

SCOPE_GLOBS = ("README.md", "docs/*.md", "changelog.d/*.md")

# --- result-shaped numbers -------------------------------------------------
_NUM = r"\d[\d,]*(?:\.\d+)?"
RESULT_PATTERNS = {
    "percent": re.compile(rf"(?<![\w.]){_NUM}\s?%"),
    "n-of-m": re.compile(rf"(?<![\w.]){_NUM}\s+of\s+~?{_NUM}(?![\w.]*\s*(?:tools|hosts|backends|providers|files|items|rows)\b)"),
    "multiplier": re.compile(rf"(?<![\w.]){_NUM}\s?[x×](?![\w])"),
    "latency": re.compile(rf"(?<![\w.]){_NUM}\s?(?:ms|µs)(?![\w])|(?<![\w.]){_NUM}\s(?:s|sec|seconds)(?![\w])"),
    "dollars": re.compile(rf"\$\s?(?!0(?![.\d])){_NUM}"),
    "tokens": re.compile(rf"~?{_NUM}\s?[kKmM]?\s+tokens\b"),
}

# --- markers ---------------------------------------------------------------
EXT = r"(?:py|json|jsonl|md|yaml|yml|toml|sh|txt|csv|db|ts|js|log|html)"
SOURCE_RE = re.compile(
    rf"(?:[\w.~-]+/)+[\w.-]+\.{EXT}\b"          # a/b.ext
    rf"|\b[\w-]+\.{EXT}\b"                       # bare file.ext
    r"|\bPR\s?#\d+|/pull/\d+|~/\.rsi/"
)
WINDOW_RE = re.compile(
    r"\b[nN]\s?=\s?\d"                           # n=30
    r"|\b20\d\d-\d\d-\d\d\b"                     # ISO date
    r"|\b\d[\d,]*\s+(?:runs?|prompts?|calls?|requests?|samples?|cases?|tasks?|"
    r"sessions?|events?|rows?|trials?|payloads?|probes?|days?|turns?|queries|"
    r"decisions?|iterations?)\b"
    rf"|\b{_NUM}\s+of\s+~?{_NUM}"                # 8 of 8
)

# A unit that is describing configuration or pricing, not reporting a result.
CONFIG_WORDS = re.compile(
    r"\b(?:default|defaults|threshold|thresholds|timeout|timeouts|deadline|"
    r"ttl|interval|budget|budgets|cap|caps|capped|limit|limits|"
    r"at least|at most|up to|no more than|no less than|min|max|"
    r"configurable|configure|set to|setting|env var|"
    r"list price|list prices|pricing|price|prices|per 1m|per million|"
    r"per month|per user|/mo|/month|/1m|/1k|free tier|"
    r"example|e\.g\.|for instance|illustrat\w+|hypothetical|suppose|"
    r"rule|policy|window of|rolling|quota|weights?)\b|<=|>=|≤|≥|LLM_ROUTER_\w+",
    re.IGNORECASE,
)

EXEMPT_RE = re.compile(r"<!--\s*provenance:\s*exempt\s*\(([^)]*\S[^)]*)\)\s*-->")
EXEMPT_FILE_RE = re.compile(r"<!--\s*provenance:\s*exempt-file\s*\(([^)]*\S[^)]*)\)\s*-->")
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
URL_RE = re.compile(r"https?://\S+")
LIST_START_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def split_units(text: str):
    """Yield (first_line_no, unit_text). Fenced code is skipped."""
    lines = text.splitlines()
    unit: list[str] = []
    start = 0
    in_fence = False

    def flush():
        nonlocal unit
        if unit:
            yield (start, "\n".join(unit))
        unit = []

    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith(("```", "~~~")):
            yield from flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line.strip():
            yield from flush()
            continue
        is_row = line.lstrip().startswith("|")
        if is_row:  # a whole table is one unit: its source is rarely in each row
            if not (unit and unit[-1].lstrip().startswith("|")):
                yield from flush()
                start = i
            unit.append(line)
            continue
        if unit and unit[-1].lstrip().startswith("|"):
            yield from flush()
        if LIST_START_RE.match(line) or line.lstrip().startswith("#"):
            yield from flush()
            unit, start = [line], i
            if line.lstrip().startswith("#"):
                yield from flush()
            continue
        if not unit:
            start = i
        unit.append(line)
    yield from flush()


def _norm(unit: str) -> str:
    return re.sub(r"\s+", " ", unit).strip()


def key_for(path: str, unit: str) -> str:
    digest = hashlib.sha1(_norm(unit).encode()).hexdigest()[:12]
    return f"{path}::{digest}"


def analyse_unit(unit: str):
    """Return a list of problems (empty when the unit is fine)."""
    if EXEMPT_RE.search(unit):
        return []
    # Markers are looked for in the raw text (a path usually sits in backticks).
    raw = unit
    scan = COMMENT_RE.sub(" ", unit)
    scan = URL_RE.sub(" ", scan)
    scan = CODE_SPAN_RE.sub(" ", scan)
    hits = []
    for kind, rx in RESULT_PATTERNS.items():
        for m in rx.finditer(scan):
            hits.append((kind, m.group(0).strip()))
    if not hits:
        return []
    if CONFIG_WORDS.search(raw):
        return []
    problems = []
    if not SOURCE_RE.search(raw):
        problems.append("no source (file path, PR #NNN or ~/.rsi path)")
    if not WINDOW_RE.search(raw):
        problems.append("no N or window (n=, date, 'N runs', 'N of M')")
    if problems:
        shown = ", ".join(f"{k}:{v}" for k, v in hits[:4])
        return [f"{'; '.join(problems)} [{shown}]"]
    return []


def scan_tree(root: Path):
    findings = []
    seen = set()
    for pattern in SCOPE_GLOBS:
        for path in sorted(root.glob(pattern)):
            rel = path.relative_to(root).as_posix()
            if rel in seen or not path.is_file():
                continue
            seen.add(rel)
            text = path.read_text(encoding="utf-8", errors="replace")
            if EXEMPT_FILE_RE.search(text):
                continue
            for line_no, unit in split_units(text):
                for why in analyse_unit(unit):
                    findings.append(
                        {
                            "key": key_for(rel, unit),
                            "path": rel,
                            "line": line_no,
                            "why": why,
                            "text": _norm(unit)[:160],
                        }
                    )
    return findings, len(seen)


def load_baseline(path: Path) -> dict:
    if not path.exists():
        return {}
    return {e["key"]: e for e in json.loads(path.read_text())["entries"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=REPO)
    ap.add_argument("--baseline", type=Path, default=None)
    ap.add_argument("--write-baseline", action="store_true")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    baseline_path = args.baseline or (root / BASELINE_NAME)

    findings, nfiles = scan_tree(root)
    if nfiles == 0:  # an empty scan passes everything: refuse it
        print("ERROR: no files in scope under", root, file=sys.stderr)
        return 2

    if args.write_baseline:
        entries = [
            {k: f[k] for k in ("key", "path", "line", "why", "text")} for f in findings
        ]
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps({"entries": entries}, indent=2) + "\n")
        print(f"wrote {len(entries)} baseline entries ({nfiles} files scanned)")
        return 0

    baseline = load_baseline(baseline_path)
    live = {f["key"] for f in findings}
    new = [f for f in findings if f["key"] not in baseline]
    stale = [e for k, e in baseline.items() if k not in live]

    for f in new:
        print(f"{f['path']}:{f['line']}: {f['why']}\n    {f['text']}")
    for e in stale:
        print(f"{e['path']}: STALE baseline entry (fixed? delete it from {BASELINE_NAME}): {e['text'][:100]}")
    print(
        f"numbers-provenance: {nfiles} files, {len(findings)} flagged, "
        f"{len(baseline)} baselined, {len(new)} new, {len(stale)} stale"
    )
    return 1 if (new or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
