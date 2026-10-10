#!/usr/bin/env python3
"""CHANGELOG fragments (owner decision D-49, 2026-10-10).

A PR adds `changelog.d/<id>.<type>.md` instead of editing CHANGELOG.md, so two
PRs never touch the same lines (7+ forced re-merges: #364 #378 #382 #384 #390
#398 #404).

  assemble [--version V --date YYYY-MM-DD]  fold fragments into ## [Unreleased]
                                            (and cut it to ## [V] - date), delete them
  check --base REF [--strict]               CI: validate fragment names; flag a direct
                                            edit of CHANGELOG.md's Unreleased section
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRAG_DIR = ROOT / "changelog.d"
CHANGELOG = ROOT / "CHANGELOG.md"
# Fragment type -> CHANGELOG section title; the tuple order is the output order.
SECTIONS = {
    "added": "Added", "changed": "Changed", "deprecated": "Deprecated",
    "removed": "Removed", "fixed": "Fixed", "security": "Security",
    "docs": "Docs", "internal": "Internal",
}
NAME_RE = re.compile(r"^(?P<id>[A-Za-z0-9][A-Za-z0-9_.-]*)\.(?P<type>[a-z]+)\.md$")
UNRELEASED = "## [Unreleased]"


def parse_name(name: str) -> tuple[str, str] | None:
    m = NAME_RE.match(name)
    if not m or m["type"] not in SECTIONS:
        return None
    return m["id"], m["type"]


def natural_key(name: str) -> list:
    """Digit runs compare as numbers: 9.fixed.md before 10.fixed.md."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower()) for t in re.split(r"(\d+)", name)]


def fragments(frag_dir: Path) -> list[tuple[str, str, Path]]:
    out = []
    seen: set[tuple[str, str]] = set()
    for p in sorted(frag_dir.glob("*.md"), key=lambda q: natural_key(q.name)):
        if p.name == "README.md":
            continue
        parsed = parse_name(p.name)
        if parsed is None:
            raise ValueError(f"bad fragment name {p.name!r}: want <id>.<type>.md, type in {sorted(SECTIONS)}")
        if not p.read_text(encoding="utf-8").strip():
            raise ValueError(f"empty fragment {p.name!r}")
        key = (parsed[0].lower(), parsed[1])
        if key in seen:
            raise ValueError(f"duplicate fragment id/type {p.name!r} (names differ only by case)")
        seen.add(key)
        out.append((parsed[0], parsed[1], p))
    return out


def bullet(text: str) -> str:
    text = text.strip("\n").rstrip()
    if text.lstrip().startswith("- "):
        return text.lstrip()
    lines = text.split("\n")
    return "\n".join(["- " + lines[0]] + [("  " + x if x.strip() else x) for x in lines[1:]])


def assemble(changelog: Path = CHANGELOG, frag_dir: Path = FRAG_DIR,
             version: str | None = None, date: str | None = None) -> int:
    """Fold fragments into Unreleased; return the number folded. No fragments: no-op."""
    frags = fragments(frag_dir)
    if not frags:
        return 0
    text = changelog.read_text(encoding="utf-8")
    start = text.find(UNRELEASED)
    if start < 0:
        raise ValueError("CHANGELOG.md has no '## [Unreleased]' heading")
    body_start = start + len(UNRELEASED)
    nxt = re.search(r"(?m)^## ", text[body_start:])
    end = body_start + nxt.start() if nxt else len(text)
    # Split the section into (heading-or-None, lines) blocks.
    blocks: list[list] = [[None, []]]
    for line in text[body_start:end].strip("\n").split("\n"):
        if line.startswith("### "):
            blocks.append([line[4:].strip(), []])
        else:
            blocks[-1][1].append(line)
    for b in blocks:
        while b[1] and not b[1][-1].strip():
            b[1].pop()
    for typ in SECTIONS:
        new = [bullet(p.read_text(encoding="utf-8")) for _, t, p in frags if t == typ]
        if not new:
            continue
        title = SECTIONS[typ]
        block = next((b for b in blocks if b[0] == title), None)
        if block is None:
            block = [title, []]
            # Insert before the first existing block that belongs later in SECTIONS order.
            order = list(SECTIONS.values())
            later = [i for i, b in enumerate(blocks)
                     if b[0] in order and order.index(b[0]) > order.index(title)]
            blocks.insert(later[0], block) if later else blocks.append(block)
        block[1].extend(new)
    parts = []
    for title, lines in blocks:
        chunk = ([f"### {title}"] if title else []) + list(lines)
        if any(x.strip() for x in chunk):
            parts.append("\n".join(chunk).strip("\n"))
    section = "\n\n".join(parts)
    head = UNRELEASED
    tail = text[end:]
    if version:
        head = f"{UNRELEASED}\n\n## [{version}]" + (f" - {date}" if date else "")
    new_text = text[:start] + head + "\n\n" + section + "\n\n" + tail.lstrip("\n")
    changelog.write_text(new_text.rstrip("\n") + "\n", encoding="utf-8")
    for _, _, p in frags:
        p.unlink()
    return len(frags)


def unreleased_section(text: str) -> str:
    start = text.find(UNRELEASED)
    if start < 0:
        return ""
    rest = text[start + len(UNRELEASED):]
    nxt = re.search(r"(?m)^## ", rest)
    return rest[: nxt.start()] if nxt else rest


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def check(base: str, strict: bool = False, repo: Path = ROOT) -> int:
    """Exit 1 on a malformed/empty fragment; a direct Unreleased edit warns (fails if strict)."""
    problems, warnings = [], []
    added = _git("diff", "--name-only", "--diff-filter=AM", f"{base}...HEAD", "--", "changelog.d", cwd=repo).split()
    seen: set[tuple[str, str]] = set()
    for f in sorted((repo / "changelog.d").glob("*.md")):
        parsed = parse_name(f.name)
        if parsed and (parsed[0].lower(), parsed[1]) in seen:
            problems.append(f"{f.name}: duplicate id/type (differs only by case)")
        elif parsed:
            seen.add((parsed[0].lower(), parsed[1]))
    for f in added:
        name = Path(f).name
        if name == "README.md":
            continue
        if parse_name(name) is None:
            problems.append(f"{f}: name must be <id>.<type>.md, type in {sorted(SECTIONS)}")
        elif not (repo / f).read_text(encoding="utf-8").strip():
            problems.append(f"{f}: empty fragment")
    old = _git("show", f"{base}:CHANGELOG.md", cwd=repo)
    new = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    if unreleased_section(old) != unreleased_section(new):
        msg = ("CHANGELOG.md [Unreleased] was edited directly; add changelog.d/<id>.<type>.md "
               "instead (D-49)")
        (problems if strict else warnings).append(msg)
    for w in warnings:
        print(f"::warning file=CHANGELOG.md::{w}")
    for p in problems:
        print(f"::error::{p}")
    print(f"changelog check: {len(added)} fragment file(s) changed, {len(warnings)} warning(s), {len(problems)} error(s)")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("assemble")
    a.add_argument("--version")
    a.add_argument("--date")
    c = sub.add_parser("check")
    c.add_argument("--base", required=True)
    c.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "assemble":
            n = assemble(version=args.version, date=args.date)
            print(f"folded {n} fragment(s)")
            return 0
        return check(args.base, args.strict)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
