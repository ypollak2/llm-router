#!/usr/bin/env python3
"""Release eval files and set manifests (PLAN v16 P3.2, decision D-192).

CI-side machinery only. Nothing here runs a model, touches the network, or holds
prompt text: a set is a list of OPAQUE item ids and the pass/fail (or decision)
recorded for each. The real sets, their judges and their minimum n are frozen by
a later preregistration (PREREG-P32); this file defines the formats.

MANIFEST  release_evals/manifests/<set_id>.json  (committed)
    {"set_id", "kind", "n", "min_n", "items_sha256", "item_ids": [...]}
  kind is "performance" (binary pass per item, compared to a baseline),
  "fidelity" (PACK vs FULL within one sha) or "stability" (two decisions per item).
  items_sha256 = sha256 of the item ids, sorted, joined with "\\n". It pins the
  list: dropping an item from the list changes the hash.

EVAL FILE  release_evals/<sha>.json
    {"schema": 1, "sha": "<40 hex>", "sets": {"<set_id>": {"items": [...]}}}
  items is a LIST of records (not an object) so a duplicated id is detectable:
    performance  {"id": "...", "pass": true|false}
    fidelity     {"id": "...", "pack": true|false, "full": true|false}
    stability    {"id": "...", "a": "<decision>", "b": "<decision>"}

PIN  release_evals/PIN.json  (committed; written once at the PREREG-P32 freeze)
    {"schema": 1, "baseline_sha": "<40 hex>",
     "sets": {"<set_id>": {"manifest_sha256", "n", "min_n"}}}
  The gate refuses (NOT INFORMATIVE) if a pinned manifest is missing or extra, its
  sha256 (canonical JSON) differs, n or min_n differ from the pin, the baseline file's
  recorded sha is not baseline_sha, or manifests exist with no pin. Written by
  `release_evals.py pin --baseline release_evals/baseline.json` AFTER the manifests
  and baseline are frozen; from then on any edit needs a visible PIN.json change.

HOW THE FILE REACHES CI (chosen: release asset, no secrets)
  The tag commit cannot contain a file named after its own sha, so the file is
  NOT committed. release.sh attaches release_evals/<sha>.json to the GitHub
  Release it creates, and .github/workflows/release.yml fetches that asset with
  the workflow's own GITHUB_TOKEN. Manifests and the baseline file ARE committed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

SCHEMA = 1
KINDS = ("performance", "fidelity", "stability")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def ids_sha256(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


def make_manifest(set_id: str, kind: str, min_n: int, item_ids: list[str]) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    ids = sorted(item_ids)
    return {"set_id": set_id, "kind": kind, "n": len(ids), "min_n": int(min_n),
            "items_sha256": ids_sha256(ids), "item_ids": ids}


def manifest_problems(m: dict) -> list[str]:
    """Reasons a manifest cannot be trusted; empty list means well-formed."""
    p: list[str] = []
    for k in ("set_id", "kind", "n", "min_n", "items_sha256", "item_ids"):
        if k not in m:
            p.append(f"missing field {k}")
    if p:
        return p
    ids = m["item_ids"]
    if m["kind"] not in KINDS:
        p.append(f"unknown kind {m['kind']!r}")
    if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
        return p + ["item_ids must be a list of non-empty strings"]
    if len(set(ids)) != len(ids):
        p.append("duplicate item ids in manifest")
    if m["n"] != len(ids):
        p.append(f"n={m['n']} but {len(ids)} item ids")
    if not isinstance(m["min_n"], int) or m["min_n"] < 1:
        p.append("min_n must be a positive integer")
    elif m["n"] < m["min_n"]:
        p.append(f"n={m['n']} is below min_n={m['min_n']}")
    if m["items_sha256"] != ids_sha256(ids):
        p.append("items_sha256 does not match the item id list")
    return p


def load_manifests(directory: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in sorted(Path(directory).glob("*.json")):
        m = json.loads(f.read_text())
        if m.get("set_id") != f.stem:
            raise ValueError(f"{f.name}: set_id {m.get('set_id')!r} does not match the file name")
        out[m["set_id"]] = m
    return out


def manifest_sha256(m: dict) -> str:
    """sha256 of the manifest's canonical JSON (formatting-independent)."""
    return hashlib.sha256(json.dumps(m, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def make_pin(manifests: dict[str, dict], baseline_sha: str) -> dict:
    if not SHA_RE.match(str(baseline_sha)):
        raise ValueError(f"baseline_sha must be 40 lowercase hex chars, got {baseline_sha!r}")
    return {"schema": SCHEMA, "baseline_sha": baseline_sha,
            "sets": {sid: {"manifest_sha256": manifest_sha256(m), "n": m["n"], "min_n": m["min_n"]}
                     for sid, m in sorted(manifests.items())}}


def pin_problems(pin: dict | None, manifests: dict[str, dict], baseline: dict | None) -> list[str]:
    """Reasons the committed manifests/baseline differ from the freeze pin."""
    if pin is None:
        return ["no pin file (release_evals/PIN.json) but manifests exist: nothing proves they "
                "are the frozen sets"] if manifests else []
    if not isinstance(pin, dict) or not isinstance(pin.get("sets"), dict) \
            or not SHA_RE.match(str(pin.get("baseline_sha"))):
        return ["pin file is malformed (needs sets object and 40-hex baseline_sha)"]
    p: list[str] = []
    for sid in sorted(set(pin["sets"]) - set(manifests)):
        p.append(f"{sid}: pinned set has no manifest (deleted?)")
    for sid in sorted(set(manifests) - set(pin["sets"])):
        p.append(f"{sid}: manifest is not in the pin (added after freeze?)")
    for sid in sorted(set(pin["sets"]) & set(manifests)):
        want, m = pin["sets"][sid], manifests[sid]
        if not isinstance(want, dict):
            p.append(f"{sid}: pin entry malformed")
            continue
        if want.get("manifest_sha256") != manifest_sha256(m):
            p.append(f"{sid}: manifest sha256 differs from the pin")
        for k in ("n", "min_n"):
            if want.get(k) != m.get(k):
                p.append(f"{sid}: {k}={m.get(k)!r} differs from the pinned {want.get(k)!r}")
    if baseline is not None and baseline.get("sha") != pin["baseline_sha"]:
        p.append(f"baseline file is for sha {baseline.get('sha')!r}, pin expects {pin['baseline_sha']!r}")
    return p


def item_problems(kind: str, item: object) -> list[str]:
    """Strict field types: a truthy string or 0/1 must never count as a boolean."""
    if not isinstance(item, dict):
        return ["item is not an object"]
    if not isinstance(item.get("id"), str) or not item["id"]:
        return ["id must be a non-empty string"]
    if kind == "performance":
        fields, want = ("pass",), bool
    elif kind == "fidelity":
        fields, want = ("pack", "full"), bool
    else:
        fields, want = ("a", "b"), str
    out = []
    for f in fields:
        v = item.get(f)
        if not isinstance(v, want) or (want is str and not v):
            out.append(f"{item['id']}: {f}={v!r} must be a {'true/false boolean' if want is bool else 'non-empty string'}")
    return out


def infer_kind(items: list) -> str | None:
    first = items[0] if items and isinstance(items[0], dict) else {}
    for kind, key in (("performance", "pass"), ("fidelity", "pack"), ("stability", "a")):
        if key in first:
            return kind
    return None


def build_eval_file(sha: str, sets: dict[str, list[dict]], kinds: dict[str, str] | None = None) -> dict:
    """kinds maps set_id -> manifest kind; without it the kind is inferred from the first item."""
    if not SHA_RE.match(sha):
        raise ValueError(f"sha must be 40 lowercase hex chars, got {sha!r}")
    kinds = kinds or {}
    for sid, items in sets.items():
        if not isinstance(items, list):
            raise ValueError(f"{sid}: items must be a list")
        kind = kinds.get(sid) or infer_kind(items)
        if kind is None:
            raise ValueError(f"{sid}: cannot tell the set kind from its items")
        bad = [x for it in items for x in item_problems(kind, it)]
        if bad:
            raise ValueError(f"{sid}: invalid item values, e.g. " + "; ".join(bad[:3]))
    return {"schema": SCHEMA, "sha": sha,
            "sets": {sid: {"items": list(items)} for sid, items in sets.items()}}


def write_eval_file(out_dir: Path, sha: str, sets: dict[str, list[dict]],
                    kinds: dict[str, str] | None = None) -> Path:
    doc = build_eval_file(sha, sets, kinds)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sha}.json"
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze", help="write a set manifest from a file of item ids (one per line)")
    f.add_argument("--set-id", required=True)
    f.add_argument("--kind", required=True, choices=KINDS)
    f.add_argument("--min-n", required=True, type=int)
    f.add_argument("--ids-file", required=True)
    f.add_argument("--out-dir", default="release_evals/manifests")
    w = sub.add_parser("write", help="write release_evals/<sha>.json from a results JSON "
                                     '({"<set_id>": [item records]})')
    w.add_argument("--sha", required=True)
    w.add_argument("--results", required=True)
    w.add_argument("--out-dir", default="release_evals")
    w.add_argument("--manifests", default="release_evals/manifests",
                   help="set kinds are read from here when it exists")
    pn = sub.add_parser("pin", help="write release_evals/PIN.json from the frozen manifests and "
                                    "baseline (run once, at the PREREG-P32 freeze)")
    pn.add_argument("--manifests", default="release_evals/manifests")
    pn.add_argument("--baseline", default="release_evals/baseline.json")
    pn.add_argument("--out", default="release_evals/PIN.json")
    a = ap.parse_args(argv)

    if a.cmd == "freeze":
        ids = [ln.strip() for ln in Path(a.ids_file).read_text().splitlines() if ln.strip()]
        m = make_manifest(a.set_id, a.kind, a.min_n, ids)
        bad = manifest_problems(m)
        if bad:
            print("refusing to write manifest: " + "; ".join(bad), file=sys.stderr)
            return 1
        out = Path(a.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{a.set_id}.json"
        path.write_text(json.dumps(m, indent=1) + "\n")
        print(f"wrote {path} (n={m['n']}, min_n={m['min_n']}, sha256={m['items_sha256'][:12]})")
        return 0

    if a.cmd == "pin":
        try:
            manifests = load_manifests(Path(a.manifests)) if Path(a.manifests).is_dir() else {}
            bad = [f"{sid}: {x}" for sid, m in manifests.items() for x in manifest_problems(m)]
            if not manifests or bad:
                raise ValueError("; ".join(bad) or "no manifests to pin")
            pin = make_pin(manifests, json.loads(Path(a.baseline).read_text()).get("sha"))
        except (ValueError, OSError) as e:
            print(f"refusing to write pin: {e}", file=sys.stderr)
            return 1
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(pin, indent=1, sort_keys=True) + "\n")
        print(f"wrote {a.out} ({len(pin['sets'])} sets, baseline {pin['baseline_sha'][:12]})")
        return 0

    try:
        sets = json.loads(Path(a.results).read_text())
        kinds = ({sid: m.get("kind") for sid, m in load_manifests(Path(a.manifests)).items()}
                 if Path(a.manifests).is_dir() else {})
        path = write_eval_file(Path(a.out_dir), a.sha, sets, kinds)
    except (ValueError, OSError) as e:
        print(f"refusing to write eval file: {e}", file=sys.stderr)
        return 1
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
