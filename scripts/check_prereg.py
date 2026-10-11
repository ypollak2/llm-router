#!/usr/bin/env python3
"""Check a prereg file against its recorded hash and count the shadow rows it may govern.

``check_prereg.py docs/prereg/PREREG-shadowlab.md [--shadow-log classifier_shadow.jsonl]``

Exit 1 when the file's sha256 differs from ``<name>.sha256`` (the prereg was edited after the
freeze). With ``--shadow-log`` it also prints how many records were written before
``frozen_utc`` (not eligible) and after it (eligible). Exit 0 otherwise. Prints no row content.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path


def check(prereg: Path, shadow_log: Path | None = None) -> dict:
    rec = prereg.with_suffix(".sha256").read_text(encoding="utf-8").split()
    want, frozen = rec[0], rec[rec.index("frozen_utc") + 1]
    got = hashlib.sha256(prereg.read_bytes()).hexdigest()
    cutoff = datetime.fromisoformat(frozen.replace("Z", "+00:00")).timestamp()
    out = {"sha256": got, "recorded": want, "match": got == want, "frozen_utc": frozen,
           "before_freeze": None, "after_freeze": None}
    if shadow_log is not None:
        before = after = 0
        for line in shadow_log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = r.get("ts") if isinstance(r, dict) and r.get("kind") == "classifier_shadow" else None
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    ts = None
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                before, after = (before + 1, after) if ts < cutoff else (before, after + 1)
        out.update(before_freeze=before, after_freeze=after)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prereg", type=Path)
    ap.add_argument("--shadow-log", type=Path, default=None)
    a = ap.parse_args()
    res = check(a.prereg, a.shadow_log)
    print(json.dumps(res))
    return 0 if res["match"] else 1


if __name__ == "__main__":
    sys.exit(main())
