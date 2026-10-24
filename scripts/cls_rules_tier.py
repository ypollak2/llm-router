#!/usr/bin/env python3
"""CLS-v16 B-R: the rules_tier.jsonl producer for PREREG-CLS-DEV section 2.2 (reference arms R-E and R-U).

Resolves every manifest item's rules tier through the P17D section 4 resolver (p17d_synthetic.rules_tier,
the live tier policy; the rules engine is NOT reimplemented here) and writes rules_tier.jsonl:
header {"schema": "rules_tier/1", resolver_commit, produced_utc}, then one row per item
{item_id, rules_tier, record_utc, haiku_live}. Real rows carry their record timestamp and
haiku_live = record_utc >= 2026-10-09T00:00:00Z; DEV-SYN rows have record_utc null and haiku_live true.

Reads item TEXT (0600 items files, through the custody guards) but prints COUNTS ONLY on stdout and writes
no text anywhere. The output is created exclusively at mode 0600. All paths are absolute flags.

Exit: 0 done, 2 output file exists (never overwritten), 5 refused input, 10 custody.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cls_dev_lib as D  # noqa: E402
import cls_pins as P  # noqa: E402
import cls_send_lib as S  # noqa: E402

EXIT_EXISTS = 2
TIERS = ("haiku", "sonnet", "opus")


def load_policy(path: str, want_sha: str):
    """The live tier policy from the very bytes whose sha256 was checked (as p17d_synthetic.load_policy)."""
    import yaml
    from llm_router.proxy.tiers import ClaudeTierPolicy

    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != want_sha:
        raise S.CLSExit(S.EXIT_REFUSE, "tier_policy sha256 differs from the expected pin")
    data = yaml.safe_load(raw) or {}
    if data.get("complexity_knn"):
        raise S.CLSExit(S.EXIT_REFUSE, "complexity_knn is true; refusing")
    return ClaudeTierPolicy.from_dict(data)


def read_texts(cls_root: str, paths: list) -> dict:
    out = {}
    for p in paths:
        for line in S.read_guarded_text(p, cls_root).splitlines():
            if line.strip():
                it = json.loads(line)
                out[it["item_id"]] = it
    return out


def repo_commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(HERE), capture_output=True, text=True, check=True)
        return r.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def build_rows(manifest: dict, texts: dict, policy, rules_tier, min_body) -> list[dict]:
    live_from = D.parse_utc(D.HAIKU_LIVE_FROM)
    rows = []
    for m in sorted(manifest["items"], key=lambda x: x["item_id"]):
        iid = m["item_id"]
        it = texts.get(iid)
        if it is None or not isinstance(it.get("text"), str):
            raise S.CLSExit(S.EXIT_REFUSE, "no item text for %s" % iid)
        body = min_body("", [], "", it["text"], model=None)
        tier = rules_tier(body, policy, None)[2]
        if tier is None and m.get("requested") in policy.by_name:
            tier = policy.by_name[m["requested"]].name
        if tier not in TIERS:
            raise S.CLSExit(S.EXIT_REFUSE, "no resolvable tier for %s" % iid)
        if m.get("kind") == "syn":
            rec, live = None, True
        else:
            ts = D.parse_utc(it.get("timestamp"))
            if ts is None:
                raise S.CLSExit(S.EXIT_REFUSE, "no parseable record timestamp for %s" % iid)
            rec, live = ts.strftime("%Y-%m-%dT%H:%M:%SZ"), ts >= live_from
        rows.append({"item_id": iid, "rules_tier": tier, "record_utc": rec, "haiku_live": live,
                     "_kind": m.get("kind")})
    return rows


def run(a) -> dict:
    out = Path(a.out)
    if out.exists():
        raise S.CLSExit(EXIT_EXISTS, "output exists; refusing to overwrite")
    import p17d_synthetic as P17

    commit = a.resolver_commit or repo_commit()
    if not commit:
        raise S.CLSExit(S.EXIT_REFUSE, "no resolver commit")
    policy = load_policy(a.tier_policy, a.policy_sha256 or P17.TIER_POLICY_SHA256)
    manifest = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    texts = read_texts(a.cls_root, [p for p in (a.items, a.syn_items) if p])
    rows = build_rows(manifest, texts, policy, P17.rules_tier, P17.pa.min_body)
    lines = [json.dumps({"schema": P.RULES_TIER_SCHEMA, "resolver_commit": commit, "produced_utc": S.utc_now()},
                        sort_keys=True)]
    lines += [json.dumps({k: v for k, v in r.items() if k != "_kind"}, sort_keys=True) for r in rows]
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise S.CLSExit(EXIT_EXISTS, "output exists; refusing to overwrite") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(str(out), 0o600)
    tiers = {t: sum(1 for r in rows if r["rules_tier"] == t) for t in TIERS}
    return {"n": len(rows), "n_real": sum(1 for r in rows if r["_kind"] != "syn"),
            "n_syn": sum(1 for r in rows if r["_kind"] == "syn"), "tiers": tiers,
            "n_haiku_live": sum(1 for r in rows if r["haiku_live"])}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="rules_tier.jsonl producer (counts only on stdout)")
    p.add_argument("--cls-root", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--items", required=True)
    p.add_argument("--syn-items")
    p.add_argument("--tier-policy", required=True)
    p.add_argument("--policy-sha256", help="default: the P17D pin (p17d_synthetic.TIER_POLICY_SHA256)")
    p.add_argument("--resolver-commit", help="default: git rev-parse HEAD of this repo")
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    try:
        print(json.dumps(run(a), sort_keys=True))
        return 0
    except S.CLSExit as e:
        print("refused: %s" % e, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
