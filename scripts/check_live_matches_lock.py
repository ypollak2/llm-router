#!/usr/bin/env python3
"""Compare a Python environment's installed distributions to uv.lock.

Usage: check_live_matches_lock.py PYTHON [--lock uv.lock]

Exit 0 when every package present in BOTH the environment and the lock has
the same version; exit 1 on any mismatch; exit 2 when nothing overlapped (an
empty comparison proves nothing) or the interpreter could not be queried.
Packages only in one side (extras not installed live, transitive-only) are
reported as counts, not failures. The project itself is ignored.
Used by the deploy procedure (F10/D-164): CI tests the lock, so the live
install must run the lock's versions.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

_DUMP = (
    "import importlib.metadata as m, json, re;"
    "print(json.dumps({re.sub(r'[-_.]+','-',d.metadata['Name']).lower(): d.version"
    " for d in m.distributions()}))"
)


def installed(python: str) -> dict[str, str]:
    out = subprocess.run([python, "-I", "-c", _DUMP], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def locked(lock: Path) -> dict[str, set[str]]:
    data = tomllib.loads(lock.read_text())
    res: dict[str, set[str]] = {}
    for pkg in data["package"]:
        if "version" not in pkg or pkg.get("source", {}).get("editable"):
            continue
        name = re.sub(r"[-_.]+", "-", pkg["name"]).lower()
        res.setdefault(name, set()).add(pkg["version"])
    return res


def compare(live: dict[str, str], lock: dict[str, set[str]]) -> tuple[list[tuple[str, str, list[str]]], int]:
    both = sorted(set(live) & set(lock))
    bad = [(n, live[n], sorted(lock[n])) for n in both if live[n] not in lock[n]]
    return bad, len(both)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("python")
    ap.add_argument("--lock", default=str(Path(__file__).resolve().parent.parent / "uv.lock"))
    args = ap.parse_args(argv)
    try:
        live = installed(args.python)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"cannot query {args.python}: {exc}", file=sys.stderr)
        return 2
    lock = locked(Path(args.lock))
    bad, overlap = compare(live, lock)
    print(f"overlap={overlap} mismatches={len(bad)} live_only={len(set(live) - set(lock))} lock_only={len(set(lock) - set(live))}")
    for name, lv, kv in bad:
        print(f"MISMATCH {name}: live {lv} != lock {', '.join(kv)}")
    if overlap == 0:
        print("no packages overlap; nothing was compared", file=sys.stderr)
        return 2
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
