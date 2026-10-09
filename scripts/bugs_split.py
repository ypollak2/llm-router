#!/usr/bin/env python3
"""One-time migration: split docs/BUGS.md into docs/bugs/<id>.md (BUGS-1).

Each section `## <id>. <title>` becomes a file with front matter (id, status
from the old index table) and the section text byte-for-byte.  docs/BUGS.md is
then rewritten as a short pointer with no table.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bugs_index import file_name  # noqa: E402

HEAD = re.compile(r"^## (\S+?)\. ", re.M)
ROW = re.compile(r"^\| (\S+) \| .* \| (.*) \|$")

POINTER_TAIL = """
## Where the entries are

One entry per file: `docs/bugs/<id>.md`, named by milestone id (numeric ids are
zero-padded, `.` and `/` in an id become `_`; the id itself is in the front matter).
A PR adds its own file and touches no shared hunk, so concurrent PRs merge in any
order.

The index is generated, not committed:

- `python scripts/bugs_index.py` prints it (id, file, status, title).
- `python scripts/bugs_index.py --write PATH` saves it.
- `python scripts/bugs_index.py --check` is the CI job `bugs-check`: unique ids, the
  Symptom / Cause / Fix / Test headings, and every `docs/BUGS.md <id>` reference in
  `src/` and `tests/` resolves to a file.
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    root = ap.parse_args().root
    src = root / "docs" / "BUGS.md"
    text = src.read_bytes().decode("utf-8")
    heads = list(HEAD.finditer(text))
    if not heads:
        print("no entries found", file=sys.stderr)
        return 1
    preamble = text[: heads[0].start()]
    status = {}
    for ln in preamble.splitlines():
        m = ROW.match(ln)
        if m and m.group(1) != "#":
            status[m.group(1)] = m.group(2).strip()
    intro = preamble.split("\n| ", 1)[0].rstrip("\n")
    intro = re.sub(r"\A# Bugs\n+", "", intro)
    out_dir = root / "docs" / "bugs"
    ids: dict[str, str] = {}
    files: dict[str, str] = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        eid = m.group(1)
        name = file_name(eid)
        if eid in ids or name in files:
            print(f"duplicate id or file name: {eid} / {name}", file=sys.stderr)
            return 1
        ids[eid] = name
        files[name] = eid
        out_dir.mkdir(parents=True, exist_ok=True)
        front = f"---\nid: {eid}\nstatus: {status.get(eid, '')}\n---\n"
        (out_dir / name).write_bytes((front + text[m.start():end]).encode("utf-8"))
    src.write_text("# Bugs\n\n" + intro + "\n" + POINTER_TAIL)
    print(f"n={len(ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
