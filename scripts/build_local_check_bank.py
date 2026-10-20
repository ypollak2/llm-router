#!/usr/bin/env python3
"""Reproducible builder for the local-check item bank (LC-1).

Not shipped in the wheel (scripts/ is excluded from the sdist and the wheel only
packages src/llm_router). It turns the audited public Q37 material plus its
generator tooling into three files under src/llm_router/local_check/data/:

    bank-v1.jsonl.gz   one JSON record per line: {"type": "item"|"plant", ...}
    manifest.json      bank_version, bank_sha256 (of the .gz bytes), counts, strata, licences
    NOTICE             generated per-repo attribution

Every record carries repo, sha, SPDX licence and URL. The licence is read from the
LICENSE file at the record's own sha in a local clone, never from memory. Records
whose licence is not on ALLOW are dropped (and counted in the manifest).

Usage:
    python scripts/build_local_check_bank.py --q37 DIR --repos DIR [--out DIR]

--q37    the prereg-q37 directory (frozen/, run/, calib1-3/, poolc/, poolf/, plants_q37.py)
--repos  directory holding one git clone per repo short name (as the Q37 generator made).
         It must hold every clone the items AND the plant pool draw on: the 16 allow-listed repos
         (aiohttp, axum, celery, clap, django, gin, pip, poetry, prettier, pydantic, ripgrep, scrapy,
         sqlalchemy, uv, vite, zod) plus the plant-pool clones express, mypy and pylint (mypy is
         MIT and enters the bank; pylint is GPL-2.0 and is excluded, but its LICENSE is still read).
Deterministic: same inputs -> byte-identical outputs (gzip mtime fixed to 0).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

BANK_VERSION = 1
JUDGE_ID = "claude-sonnet-5-5"
ALLOW = ("MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "Unlicense")
TARGET_PER_JOB = 70
PLANT_TARGET = {"good": 12, "swapped": 12, "mutated": 6}
ITEM_DIRS = ("frozen", "calib1", "calib2", "calib3")     # audited Q37 items
EXTRA_ITEM_DIR = "poolc"                                 # additions to reach 70 per job
PLANT_POOL_DIR = "poolf"                                 # plants come only from here
# item dir -> the Q37 results dir holding its two arms' answers, grader and judge verdicts
RESULTS_DIR = {"frozen": "run", "calib1": "calib1", "calib2": "calib2", "calib3": "calib3"}
# plant set (a dir with plants.jsonl) -> results dirs whose judge*.jsonl judged exactly those plants
PLANT_SETS = {"frozen": ("poolf", "run"), "calib1": ("calib1", "calib2")}
# diffs touching vendored third-party code carry that code's own licence, which is not checked here
VENDORED = re.compile(r"(^|/)(_vendor|vendor|third_party|node_modules)/")
LICENCE_FILE = re.compile(r"^(LICENSE(\.txt|\.md)?|LICENSE-[A-Z]+|UNLICENSE)$")

# (SPDX id, regexes that must all match the normalised licence text)
SIGNATURES = (
    ("Unlicense", (r"free and unencumbered software released into the public domain",)),
    ("Apache-2.0", (r"apache license", r"version 2\.0")),
    ("GPL-2.0", (r"gnu general public license", r"version 2")),
    ("GPL-3.0", (r"gnu general public license", r"version 3")),
    ("MIT", (r"permission is hereby granted, free of charge",)),
    ("ISC", (r"permission to use, copy, modify, and/or distribute this software",)),
    ("BSD-3-Clause", (r"redistribution and use in source and binary forms", r"neither the name")),
    ("BSD-2-Clause", (r"redistribution and use in source and binary forms",)),
)


def classify_text(text: str) -> str:
    """SPDX id of the primary grant in a licence text.

    A LICENSE may quote other licences for bundled parts (mypy: MIT, then PSF text that mentions the
    GPL). The primary licence is the one whose header comes first in the text; ties (BSD-3 contains
    BSD-2) go to the earlier, more specific SIGNATURES entry.
    """
    t = re.sub(r"\s+", " ", text.lower())
    best: tuple[int, int, str] | None = None
    for rank, (spdx, pats) in enumerate(SIGNATURES):
        if all(re.search(p, t) for p in pats):
            cand = (re.search(pats[0], t).start(), rank, spdx)  # type: ignore[union-attr]
            if best is None or cand < best:
                best = cand
    return best[2] if best else "UNKNOWN"


def vendored(rec: dict) -> bool:
    """True if the record's diff or file list touches a vendored third-party path."""
    paths = list(rec["files"]) + re.findall(r"^diff --git a/(\S+)", rec["diff"], flags=re.M)
    return any(VENDORED.search(x) for x in paths)


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {r.stderr.strip()}")
    return r.stdout


def licence_at(repo: Path, sha: str) -> tuple[str, list[str]]:
    """SPDX expression + the licence files it was read from, at `sha`."""
    names = [n for n in git(repo, "ls-tree", "--name-only", sha).split("\n") if LICENCE_FILE.match(n)]
    if not names:
        return "UNKNOWN", []
    ids = []
    for n in sorted(names):
        i = classify_text(git(repo, "show", f"{sha}:{n}"))
        if i not in ids:
            ids.append(i)
    return " OR ".join(ids), sorted(names)


def allowed(expr: str) -> bool:
    """OR-expression: usable if at least one alternative is on the allow-list."""
    return any(p.strip() in ALLOW for p in expr.split(" OR "))


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class Resolver:
    def __init__(self, repos: Path):
        self.repos = repos
        self.slug: dict[str, str] = {}
        self.cache: dict[tuple[str, str], tuple[str, list[str]]] = {}

    def repo_slug(self, short: str) -> str:
        if short not in self.slug:
            url = git(self.repos / short, "remote", "get-url", "origin").strip()
            m = re.match(r"https://github\.com/([^/]+/[^/]+?)(\.git)?$", url)
            if not m:
                raise RuntimeError(f"unexpected origin for {short}: {url}")
            self.slug[short] = m.group(1)
        return self.slug[short]

    def meta(self, short: str, sha: str) -> dict:
        if (short, sha) not in self.cache:
            self.cache[(short, sha)] = licence_at(self.repos / short, sha)
        lic, files = self.cache[(short, sha)]
        slug = self.repo_slug(short)
        return {"repo": slug, "sha": sha, "licence": lic, "licence_files": files,
                "url": f"https://github.com/{slug}/commit/{sha}"}


def item_record(it: dict, res: Resolver, audited: bool) -> dict:
    return {"type": "item", "id": it["id"], "job": it["stratum"], **res.meta(it["repo"], it["commit"]),
            "date": it["commit_date"], "task": it["messages"][0]["content"], "diff": it["diff"],
            "files": it["files"], "strata": {"size": it["size_stratum"], "lang": it["lang"]}, "audited": audited}


def plant_record(p: dict, pool: dict[str, dict], res: Resolver) -> dict:
    it = pool[p["id"]]
    rec = {"type": "plant", "id": p["id"], "kind": p["kind"], "job": p["job"], **res.meta(it["repo"], it["commit"]),
           "task": p["messages"][0]["content"], "diff": p["diff"], "files": p["files"], "text": p["text"]}
    if p.get("source"):
        s = pool[p["source"]]
        rec["source"] = {"id": p["source"], **res.meta(s["repo"], s["commit"])}
    if p.get("original_subject"):
        rec["original_subject"] = p["original_subject"]
    return rec


def audited_item_ids(q37: Path) -> set[str]:
    """Items answered by BOTH arms in a Q37 run/calibration slice with grader+judge results behind them.

    Per arm: a judge verdict exists, or the answer failed the programmatic grader and the item is in the
    G4 audit set (run/audit_set.jsonl). Every calibration failure was hand-audited (PREREG-Q37.md s11).
    """
    in_audit_set = {x["item_id"] for x in read_jsonl(q37 / "run" / "audit_set.jsonl")}
    out: set[str] = set()
    for item_dir, rdir in RESULTS_DIR.items():
        res = q37 / rdir / "results"
        arms = {"qwen": {x["item_id"]: x for x in read_jsonl(res / "qwen3.6_35b-a3b-coding.jsonl")},
                "sonnet": {x["item_id"]: x for x in read_jsonl(res / "sonnet.jsonl")}}
        judged = {a: {x["item_id"] for x in read_jsonl(res / "judge.jsonl") if x["arm"] == a} for a in arms}
        for it in read_jsonl(q37 / item_dir / "items.jsonl"):
            i = it["id"]
            ok = all(i in arms[a] and not arms[a][i].get("err") and (i in judged[a] or i in in_audit_set) for a in arms)
            if ok:
                out.add(i)
    return out


def plant_verdicts(q37: Path) -> tuple[dict[tuple[str, str, str], Counter], dict[tuple[str, str], str]]:
    """Judge verdicts per (id, kind, sha256(text)), and which Q37 plant set each (id, kind, text) came from.

    A verdict only counts for the exact text it judged: Q37 reused ids across plant sets with different text.
    """
    verdicts: dict[tuple[str, str, str], Counter] = {}
    origin: dict[tuple[str, str], str] = {}
    for pdir, rdirs in PLANT_SETS.items():
        texts = {(x["id"], x["kind"]): h(x["text"]) for x in read_jsonl(q37 / pdir / "plants.jsonl")}
        for (i, k), th in texts.items():
            origin.setdefault((i, k, th), pdir)
        for rdir in rdirs:
            for f in sorted((q37 / rdir / "results").glob("judge*.jsonl")):
                for x in read_jsonl(f):
                    if x["arm"] != "plant":
                        continue
                    i, k = x["item_id"].split("#")
                    if (i, k) in texts:
                        verdicts.setdefault((i, k, texts[(i, k)]), Counter())[x["verdict"]] += 1
    return verdicts, origin  # type: ignore[return-value]


def contradicts(kind: str, v: Counter | None) -> bool:
    """A good plant must be judged pass; a swapped or mutated plant must be judged fail."""
    return bool(v) and v["fail" if kind == "good" else "pass"] > 0  # type: ignore[index]


def build(q37: Path, repos: Path) -> tuple[list[dict], dict]:
    sys.path.insert(0, str(q37))
    import plants_q37  # the Q37 plant tooling (mechanical, no model call)

    res = Resolver(repos)
    excluded: Counter = Counter()
    excluded_vendored: Counter = Counter()
    audited_ids = audited_item_ids(q37)

    def keep(rec: dict) -> bool:
        if not allowed(rec["licence"]):
            excluded[rec["licence"]] += 1
            return False
        if vendored(rec):
            excluded_vendored[rec["job"]] += 1
            return False
        return True

    # ---- items: Q37 items answered by both arms first (audited), then unaudited poolc fill up to 70 per job
    items: list[dict] = []
    seen: set[str] = set()
    for d in RESULTS_DIR:
        for it in read_jsonl(q37 / d / "items.jsonl"):
            r = item_record(it, res, it["id"] in audited_ids)
            if r["id"] not in seen and keep(r):
                items.append(r)
                seen.add(r["id"])
    assert all(r["audited"] for r in items), "an item dir holds an item never answered by both arms"
    extra = read_jsonl(q37 / "poolc" / "items.jsonl")
    per = Counter(r["job"] for r in items)
    for job in ("commit_message", "pr_description"):
        assert per[job] <= TARGET_PER_JOB, f"{per[job]} audited {job} items exceed {TARGET_PER_JOB}"
        need = TARGET_PER_JOB - per[job]
        for it in sorted((x for x in extra if x["stratum"] == job), key=lambda x: h("lc1-extra" + x["id"])):
            if need <= 0:
                break
            r = item_record(it, res, False)
            if r["id"] in seen or not keep(r):
                continue
            items.append(r)
            seen.add(r["id"])
            need -= 1
    # an item must not share a commit or a diff with another item
    shas, diffs = set(), set()
    for r in items:
        assert r["sha"] not in shas and h(r["diff"]) not in diffs, f"duplicate item {r['id']}"
        shas.add(r["sha"])
        diffs.add(h(r["diff"]))

    # ---- plants: Q37 plants first, then mechanical rebuilds from the plant pool.
    # pool = poolf (the audited plant pool) plus whatever of poolc was NOT selected as an item
    pool_items = {x["id"]: x for x in read_jsonl(q37 / "poolf" / "items.jsonl")}
    pool_src = read_jsonl(q37 / "poolf" / "plants_src.jsonl")
    for x, s in zip(extra, read_jsonl(q37 / "poolc" / "plants_src.jsonl")):
        assert x["id"] == s["id"]
        if x["id"] not in seen and x["commit"] not in shas:
            pool_items[x["id"]] = x
            pool_src.append(s)
    verdicts, origin = plant_verdicts(q37)
    plant_excluded: Counter = Counter()
    plant_vendored = 0
    plant_contradicted: list[str] = []

    def plant_ok(p: dict) -> dict | None:
        nonlocal plant_vendored
        rec = plant_record(p, pool_items, res)
        lics = [rec["licence"]] + ([rec["source"]["licence"]] if "source" in rec else [])
        if not all(allowed(x) for x in lics):
            for x in lics:
                if not allowed(x):
                    plant_excluded[x] += 1
            return None
        if vendored(rec):
            plant_vendored += 1
            return None
        if rec["sha"] in shas or h(rec["diff"]) in diffs or p["id"] in seen:
            return None                                            # plants stay disjoint from every item
        th = h(rec["text"])
        v = verdicts.get((p["id"], p["kind"], th))
        if contradicts(p["kind"], v):
            plant_contradicted.append(f'{p["id"]}#{p["kind"]}')    # the calibration judge read it differently from its label
            return None
        if (p["id"], p["kind"], th) in origin:                     # byte-identical to a Q37 plant: keep its id
            rec["provenance"] = f"q37-{origin[(p['id'], p['kind'], th)]}"
        else:                                                      # rebuilt here: a new, content-derived id
            rec["pool_item_id"] = p["id"]
            rec["id"] = "lc1p-" + h("|".join([p["kind"], p["job"], rec["sha"], rec.get("source", {}).get("sha", ""), rec["text"], h(rec["diff"])]))[:12]
            rec["provenance"] = "lc1-rebuilt"
        if v:
            rec["calibration_verdicts"] = {k: v[k] for k in sorted(v)}
        return rec

    frozen_plants = read_jsonl(q37 / "frozen" / "plants.jsonl")
    plants = [r for r in (plant_ok(p) for p in frozen_plants) if r]
    kept_q37 = len(plants)
    have = Counter((p["kind"], p["job"]) for p in plants)
    want = Counter((p["kind"], p["job"]) for p in frozen_plants)
    clean_pool = [x for x in pool_items.values() if allowed(res.meta(x["repo"], x["commit"])["licence"])]
    clean_ids = {x["id"] for x in clean_pool}
    cand = plants_q37.build(clean_pool, [s for s in pool_src if s["id"] in clean_ids], n_good=60, n_swapped=60, n_mut=30)
    used = {(p["id"], p["kind"]) for p in plants}                  # kept Q37 plants keep their Q37 id
    for c in cand:
        key = (c["kind"], c["job"])
        if have[key] >= want[key] or (c["id"], c["kind"]) in used:
            continue
        rec = plant_ok(c)
        if rec:
            plants.append(rec)
            used.add((c["id"], c["kind"]))
            have[key] += 1
    # Q37 keys its plants by (id, kind), so one pool item may carry a good and a swapped plant; rebuilt ids are unique outright
    assert len({(p["id"], p["kind"]) for p in plants}) == len(plants), "plant (id, kind) must be unique"
    assert len({p["id"] for p in plants if p["provenance"] == "lc1-rebuilt"}) == sum(p["provenance"] == "lc1-rebuilt" for p in plants)
    plants.sort(key=lambda p: (p["kind"], p["job"], p["id"]))
    rebuilt = len(plants) - kept_q37

    by_job = lambda rs, a: dict(sorted(Counter(r["job"] for r in rs if r["audited"] is a).items()))  # noqa: E731
    manifest = {
        "bank_version": BANK_VERSION,
        "judge_id": JUDGE_ID,
        "licence_allow_list": list(ALLOW),
        "source": "prereg-q37 (frozen/run, calib1-3: audited; poolc: unaudited fill; plants: Q37 plants, then mechanical rebuilds from poolf+poolc)",
        "counts": {
            "items": len(items),
            "items_by_job": dict(Counter(r["job"] for r in items)),
            "items_audited": sum(r["audited"] for r in items),
            "items_unaudited": sum(not r["audited"] for r in items),
            "items_audited_by_job": by_job(items, True),
            "items_unaudited_by_job": by_job(items, False),
            "items_by_stratum": dict(sorted(Counter(f'{r["job"]}/{r["strata"]["size"]}/{r["strata"]["lang"]}' for r in items).items())),
            "items_by_repo": dict(sorted(Counter(r["repo"] for r in items).items())),
            "plants": len(plants),
            "plants_by_kind": dict(Counter(p["kind"] for p in plants)),
            "plants_by_provenance": dict(sorted(Counter(p["provenance"] for p in plants).items())),
            "plants_audited_kept": kept_q37,
            "plants_topped_up": rebuilt,
        },
        "licences": dict(sorted(Counter(r["licence"] for r in items + plants).items())),
        "repo_licences": dict(sorted({r["repo"]: r["licence"] for r in items + plants}.items())),
        "excluded_items_by_licence": dict(sorted(excluded.items())),
        "excluded_items_vendored_by_job": dict(sorted(excluded_vendored.items())),
        "excluded_plant_candidates_by_licence": dict(sorted(plant_excluded.items())),
        "excluded_plant_candidates_vendored": plant_vendored,
        "excluded_plants_contradicting_calibration": sorted(plant_contradicted),
    }
    return items + plants, manifest


def notice(records: list[dict], texts: dict[str, str]) -> str:
    rows: dict[str, tuple[str, set]] = {}
    first_sha: dict[str, str] = {}
    for r in records:
        for x in [r] + ([r["source"]] if "source" in r else []):
            rows.setdefault(x["repo"], (x["licence"], set()))[1].add(x["sha"])
            first_sha.setdefault(x["repo"], min(rows[x["repo"]][1]))
    out = ["NOTICE for llm_router.local_check bank data",
           "",
           "bank-v1.jsonl.gz holds text taken from public commits of the open-source projects below",
           "(diffs, file lists and, for the judge-validity plants, the upstream commit or pull-request",
           "message). Each record names its repo, commit sha, SPDX licence and URL. Every project is",
           "used under a licence on this allow-list: " + ", ".join(ALLOW) + ".",
           "Copyright remains with each project's authors; the licence text of each project is in its",
           "repository at the commit linked from the record. This file is generated by",
           "scripts/build_local_check_bank.py.",
           ""]
    for repo in sorted(rows):
        lic, shas = rows[repo]
        out.append(f"- https://github.com/{repo}  licence: {lic}  ({len(shas)} commits)")
    out += ["", "Licence texts, copied from each repository at the first commit used from it:"]
    for repo in sorted(rows):
        out += ["", "=" * 78, f"{repo} @ {first_sha[repo]}", "=" * 78, texts[repo].rstrip()]
    return "\n".join(out) + "\n"


def licence_texts(records: list[dict], repos: Path) -> dict[str, str]:
    """The licence file text(s) of each repo, at the first (sorted) sha used from it."""
    first: dict[str, dict] = {}
    for r in records:
        for x in [r] + ([r["source"]] if "source" in r else []):
            if x["repo"] not in first or x["sha"] < first[x["repo"]]["sha"]:
                first[x["repo"]] = x
    out = {}
    for repo, x in first.items():
        clone = repos / repo.split("/")[1].lower() if (repos / repo.split("/")[1].lower()).is_dir() else None
        if clone is None:
            raise RuntimeError(f"no clone for {repo}")
        out[repo] = "\n\n".join(f"--- {n} ---\n" + git(clone, "show", f"{x['sha']}:{n}") for n in x["licence_files"])
    return out


def write(records: list[dict], manifest: dict, out: Path, texts: dict[str, str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n" for r in records).encode()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(body)
    gz_bytes = buf.getvalue()
    manifest = {**manifest, "bank_sha256": hashlib.sha256(gz_bytes).hexdigest(), "bank_file": "bank-v1.jsonl.gz",
                "bank_bytes_gz": len(gz_bytes), "records": len(records)}
    (out / "bank-v1.jsonl.gz").write_bytes(gz_bytes)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out / "NOTICE").write_text(notice(records, texts))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--q37", type=Path, required=True)
    ap.add_argument("--repos", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "src/llm_router/local_check/data")
    a = ap.parse_args()
    records, manifest = build(a.q37, a.repos)
    write(records, manifest, a.out, licence_texts(records, a.repos))
    print(json.dumps(manifest["counts"], indent=1))
    print("excluded items:", manifest["excluded_items_by_licence"], "plants:", manifest["excluded_plant_candidates_by_licence"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
