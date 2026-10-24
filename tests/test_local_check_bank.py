"""LC-1: local-check item bank -- integrity, schema, licences, draws, plants, privacy, size, packaging."""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from llm_router.local_check import bank as B

ROOT = Path(__file__).resolve().parents[1]
DATA = B.DATA_DIR


@pytest.fixture(scope="module")
def bank():
    return B.load_bank()


@pytest.fixture()
def copy(tmp_path):
    shutil.copytree(DATA, tmp_path / "d")
    return tmp_path / "d"


def _rewrite_manifest(d: Path, **kw):
    m = json.loads((d / "manifest.json").read_text())
    m.update(kw)
    (d / "manifest.json").write_text(json.dumps(m))


# ---- integrity
def test_manifest_sha_matches_the_file_bytes():
    m = json.loads((DATA / "manifest.json").read_text())
    assert m["bank_sha256"] == hashlib.sha256((DATA / m["bank_file"]).read_bytes()).hexdigest()


def test_tampered_bank_is_refused(copy):
    p = copy / "bank-v1.jsonl.gz"
    recs = gzip.decompress(p.read_bytes()).replace(b"diff --git", b"diff --GIT", 1)
    p.write_bytes(gzip.compress(recs, mtime=0))
    with pytest.raises(B.BankIntegrityError):
        B.load_bank(copy)


def test_wrong_manifest_sha_is_refused(copy):
    _rewrite_manifest(copy, bank_sha256="0" * 64)
    with pytest.raises(B.BankIntegrityError):
        B.load_bank(copy)


def test_verifies_on_every_load_not_once(copy):
    B.load_bank(copy)
    (copy / "bank-v1.jsonl.gz").write_bytes((copy / "bank-v1.jsonl.gz").read_bytes() + b"\0")
    with pytest.raises(B.BankIntegrityError):
        B.load_bank(copy)


def test_missing_bank_is_a_bankerror(tmp_path):
    with pytest.raises(B.BankError):
        B.load_bank(tmp_path)


def test_manifest_counts_mismatch_is_refused(copy):
    m = json.loads((copy / "manifest.json").read_text())
    m["counts"]["items"] += 1
    (copy / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(B.BankError):
        B.load_bank(copy)


# ---- schema and population
def test_bank_has_the_target_population(bank):
    assert len(bank.items("commit_message")) == 70 and len(bank.items("pr_description")) == 70
    assert len(bank.plants()) == 30
    assert {k: len(bank.plants(k)) for k in B.PLANT_KINDS} == {"good": 12, "swapped": 12, "mutated": 6}
    assert bank.version == 1 and bank.manifest["counts"]["items"] == 140


def test_audited_flag_and_manifest_counts_are_true(bank):
    c = bank.manifest["counts"]
    for job in B.JOBS:
        aud = sum(i.audited for i in bank.items(job))
        assert c["items_audited_by_job"][job] == aud and c["items_unaudited_by_job"][job] == 70 - aud
    assert c["items_audited"] + c["items_unaudited"] == 140 == sum(c["items_audited_by_job"].values()) + sum(c["items_unaudited_by_job"].values())
    assert c["items_audited"] == sum(i.audited for i in bank.items()) and 0 < c["items_unaudited"] < 70


def test_audited_count_mismatch_is_refused(copy):
    m = json.loads((copy / "manifest.json").read_text())
    m["counts"]["items_audited"] += 1
    m["counts"]["items_unaudited"] -= 1
    (copy / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(B.BankError):
        B.load_bank(copy)


def test_item_schema(bank):
    ids = set()
    for i in bank.items():
        assert i.id not in ids
        ids.add(i.id)
        assert i.job in B.JOBS and re.fullmatch(r"[0-9a-f]{40}", i.sha)
        assert re.fullmatch(r"[\w.-]+/[\w.-]+", i.repo)
        assert i.url == f"https://github.com/{i.repo}/commit/{i.sha}"
        assert i.size in {"small", "medium", "large"} and i.lang
        assert i.diff and i.diff in i.task and i.files
        assert re.fullmatch(r"20\d\d-\d\d-\d\d", i.date)


def test_plant_schema(bank):
    for p in bank.plants():
        assert p.text and p.diff in p.task and re.fullmatch(r"[0-9a-f]{40}", p.sha)
        if p.kind == "swapped":
            assert p.source and p.source["sha"] != p.sha and B.licence_allowed(p.source["licence"])
        else:
            assert p.source is None


def test_strata_accessor(bank):
    s = bank.strata("commit_message")
    assert sum(s.values()) == 70 and all(re.fullmatch(r"(small|medium|large)/\w+", k) for k in s)
    k = next(iter(s))
    assert len(bank.items("commit_message", k)) == s[k]


# ---- licences
def test_every_record_is_allow_listed_and_pylint_is_gone(bank):
    for r in (*bank.items(), *bank.plants()):
        assert B.licence_allowed(r.licence), (r.id, r.licence)
        assert "pylint" not in r.repo.lower()
    assert "pylint" not in (DATA / "NOTICE").read_text().lower()
    assert not any("GPL" in k for k in bank.manifest["licences"])


def test_licence_expression_rules():
    assert B.licence_allowed("MIT") and B.licence_allowed("Apache-2.0 OR MIT")
    assert not B.licence_allowed("GPL-2.0") and not B.licence_allowed("UNKNOWN") and not B.licence_allowed("GPL-3.0 OR AGPL-3.0")


def _builder():
    spec = importlib.util.spec_from_file_location("lc_builder", ROOT / "scripts" / "build_local_check_bank.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_builder_allow_list_matches_loader_and_classifies_licences():
    b = _builder()
    assert set(b.ALLOW) == B.ALLOWED_LICENCES
    assert b.allowed("Unlicense OR MIT") and not b.allowed("GPL-2.0")
    assert b.classify_text("GNU GENERAL PUBLIC LICENSE\nVersion 2, June 1991") == "GPL-2.0"
    assert b.classify_text("Permission is hereby granted, free of charge, to any person") == "MIT"
    assert b.classify_text("Redistribution and use in source and binary forms ... Neither the name of") == "BSD-3-Clause"
    assert b.classify_text("Apache License Version 2.0, January 2004") == "Apache-2.0"
    assert b.classify_text("something proprietary") == "UNKNOWN"


MYPY_LIKE = """Mypy is licensed under the terms of the MIT license, reproduced below.

= = = = =

The MIT License

Copyright (c) 2012-2023 Jukka Lehtosalo and contributors

Permission is hereby granted, free of charge, to any person obtaining a copy of this software

= = = = =

Portions of mypy and mypyc are licensed under different licenses.
The files under stdlib-samples are licensed under the PSF 2 License, reproduced below.

PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2

Python was previously distributed under the GNU General Public License (GPL),
the more recent versions of Python are GPL-compatible, version 2.
"""


def test_classify_text_uses_the_primary_grant_not_a_later_mention():
    b = _builder()
    assert b.classify_text(MYPY_LIKE) == "MIT"
    # a real GPL text that later mentions the MIT grant stays GPL
    assert b.classify_text("GNU GENERAL PUBLIC LICENSE Version 2\n... see also: Permission is hereby granted, free of charge") == "GPL-2.0"
    assert b.classify_text("Redistribution and use in source and binary forms ... Neither the name of ... GNU General Public License version 2") == "BSD-3-Clause"
    assert not b.allowed(b.classify_text("GNU GENERAL PUBLIC LICENSE Version 2 ... Permission is hereby granted, free of charge"))


def test_builder_vendored_filter():
    b = _builder()
    def rec(*files):
        return {"files": list(files), "diff": "".join(f"diff --git a/{f} b/{f}\n+x\n" for f in files)}
    for f in ("src/pip/_vendor/certifi/cacert.pem", "vendor/x.go", "a/third_party/b.c", "web/node_modules/p/i.js", "_vendor/z.py"):
        assert b.vendored(rec("src/ok.py", f)), f
    for f in ("src/vendorize.py", "docs/vendors.md", "src/pip/_internal/x.py"):
        assert not b.vendored(rec(f)), f
    assert b.vendored({"files": ["ok.py"], "diff": "diff --git a/pkg/_vendor/q.py b/pkg/_vendor/q.py\n"})


def test_no_vendored_paths_in_the_bank(bank):
    b = _builder()
    for r in (*bank.items(), *bank.plants()):
        assert not b.vendored({"files": list(r.files), "diff": r.diff}), r.id


def test_plant_calibration_guard():
    b = _builder()
    from collections import Counter
    assert b.contradicts("good", Counter(fail=1, **{"pass": 2})) and not b.contradicts("good", Counter(**{"pass": 3}))
    assert b.contradicts("swapped", Counter(**{"pass": 1})) and b.contradicts("mutated", Counter(**{"pass": 1, "fail": 2}))
    assert not b.contradicts("swapped", Counter(fail=3)) and not b.contradicts("good", None)


def test_builder_drops_disallowed_licences(tmp_path):
    b = _builder()
    repo = tmp_path / "r"
    env = {"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(tmp_path)}
    def g(*a):
        return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, env=env).stdout.strip()
    repo.mkdir()
    g("init", "-q")
    (repo / "LICENSE").write_text("GNU GENERAL PUBLIC LICENSE\nVersion 2, June 1991")
    g("add", ".")
    g("commit", "-qm", "gpl")
    gpl = g("rev-parse", "HEAD")
    (repo / "LICENSE").write_text("Permission is hereby granted, free of charge, to any person")
    g("commit", "-qam", "mit")
    mit = g("rev-parse", "HEAD")
    assert b.licence_at(repo, gpl) == ("GPL-2.0", ["LICENSE"]) and not b.allowed(b.licence_at(repo, gpl)[0])
    assert b.licence_at(repo, mit)[0] == "MIT" and b.allowed("MIT")


def test_notice_names_every_repo_in_the_bank(bank):
    n = (DATA / "NOTICE").read_text()
    for r in {x.repo for x in (*bank.items(), *bank.plants())}:
        assert f"https://github.com/{r} " in n


# ---- draws
def test_draws_are_disjoint_balanced_and_deterministic(bank):
    d1, d2 = bank.draw(1), bank.draw(2)
    assert len(d1) == len(d2) == 70
    assert not {i.id for i in d1} & {i.id for i in d2}
    for d in (d1, d2):
        assert sum(i.job == "commit_message" for i in d) == 35 and sum(i.job == "pr_description" for i in d) == 35
    assert [i.id for i in bank.draw(1)] == [i.id for i in d1]
    assert [i.id for i in B.load_bank().draw(2)] == [i.id for i in d2]
    with pytest.raises(ValueError):
        bank.draw(3)


def test_draws_are_stratified_job_size_language(bank):
    from collections import Counter
    c1 = Counter((i.job, i.size, i.lang) for i in bank.draw(1))
    c2 = Counter((i.job, i.size, i.lang) for i in bank.draw(2))
    strata = {(i.job, i.size, i.lang) for i in bank.items()}
    assert len(strata) == 12
    assert max(abs(c1[s] - c2[s]) for s in strata) <= 1
    for s in strata:
        assert c1[s] + c2[s] == len(bank.items(s[0], f"{s[1]}/{s[2]}"))      # the two draws use the whole bank
    assert sum(c1.values()) == sum(c2.values()) == 70
    for job in B.JOBS:
        assert sum(n for (j, *_), n in c1.items() if j == job) == sum(n for (j, *_), n in c2.items() if j == job) == 35


def test_draw_1_is_fully_audited_and_balanced(bank):
    d1, d2 = bank.draw(1), bank.draw(2)
    assert sum(not i.audited for i in d1) == 0                                # membership, not ordering
    assert sum(not i.audited for i in d2) == bank.manifest["counts"]["items_unaudited"]   # every unaudited item is in draw 2
    assert sum(i.audited for i in d1) + sum(i.audited for i in d2) == bank.manifest["counts"]["items_audited"]
    for job in B.JOBS:
        assert sum(i.job == job for i in d1) == sum(i.job == job for i in d2) == 35


def test_draw_falls_back_to_unaudited_only_when_a_stratum_is_short(bank):
    # strip the audited flag from one whole stratum: draw 1 must still be a valid 35/35 split
    import dataclasses
    items = tuple(dataclasses.replace(i, audited=False) if (i.job, i.stratum) == ("commit_message", "small/py") else i for i in bank.items())
    b2 = B.Bank(bank.manifest, items, ())
    assert len(b2.draw(1)) == len(b2.draw(2)) == 70
    assert {i.id for i in b2.draw(1)}.isdisjoint(i.id for i in b2.draw(2))
    assert sum(not i.audited for i in b2.draw(1)) == len(bank.items("commit_message", "small/py")) // 2


def test_draws_depend_on_the_bank_sha(bank, copy):
    other = B.load_bank(copy)
    assert [i.id for i in other.draw(1)] == [i.id for i in bank.draw(1)]       # same sha, same draw
    other.sha256 = "f" * 64
    assert [i.id for i in other.draw(1)] != [i.id for i in bank.draw(1)]       # seeded by the sha


def _raw_records():
    return [json.loads(x) for x in gzip.decompress((DATA / "bank-v1.jsonl.gz").read_bytes()).decode().splitlines()]


def test_known_defective_plant_is_gone_and_no_plant_contradicts_calibration(bank):
    plants = _raw_records()
    plants = [r for r in plants if r["type"] == "plant"]
    # q37-98983aade6f1 (good) was judged fail by Sonnet and Haiku in calibration: a plant defect (PREREG-Q37.md s7)
    assert not any(r["id"] == "q37-98983aade6f1" for r in plants)
    for r in plants:
        v = r.get("calibration_verdicts")
        if v:
            assert v.get("fail" if r["kind"] == "good" else "pass", 0) == 0, r["id"]
    assert bank.manifest["excluded_plants_contradicting_calibration"] == ["q37-98983aade6f1#good"]


def test_plant_ids_are_content_derived_and_not_reused_with_other_content(bank):
    plants = [r for r in _raw_records() if r["type"] == "plant"]
    assert {r["provenance"] for r in plants} <= {"q37-frozen", "q37-calib1", "lc1-rebuilt"}
    assert len({(r["id"], r["kind"]) for r in plants}) == len(plants)
    rebuilt = [r for r in plants if r["provenance"] == "lc1-rebuilt"]
    assert rebuilt and len({r["id"] for r in rebuilt}) == len(rebuilt)
    for r in rebuilt:
        expect = "lc1p-" + hashlib.sha256("|".join([r["kind"], r["job"], r["sha"], r.get("source", {}).get("sha", ""), r["text"], hashlib.sha256(r["diff"].encode()).hexdigest()]).encode()).hexdigest()[:12]
        assert r["id"] == expect and r["pool_item_id"] != r["id"]
    # the plant that reused id q37-de34cc297997 with new content is no longer a q37 id
    assert all(r["provenance"] != "lc1-rebuilt" or not r["id"].startswith("q37-") for r in plants)
    assert {p.provenance for p in bank.plants()} == {r["provenance"] for r in plants}


# ---- plants vs items
def test_plant_pool_is_disjoint_from_every_item(bank):
    items = bank.items()
    ids, shas = {i.id for i in items}, {i.sha for i in items}
    diffs = {hashlib.sha256(i.diff.encode()).hexdigest() for i in items}
    for p in bank.plants():
        assert p.id not in ids and p.sha not in shas
        assert hashlib.sha256(p.diff.encode()).hexdigest() not in diffs
        if p.source:
            assert p.source["sha"] not in shas


# ---- privacy
def test_no_owner_paths_or_prompt_data(bank):
    blob = gzip.decompress((DATA / "bank-v1.jsonl.gz").read_bytes()).decode() + (DATA / "NOTICE").read_text() + (DATA / "manifest.json").read_text()
    for needle in ("/Users/", "~/.rsi", ".rsi/", "yaliandrona", "/.llm-router", "~/.claude", "/.claude/", "p17d", "door_corpus", "gt500", "CLAUDE_JOB_DIR", "/home/yali"):
        assert needle not in blob, needle
    assert not re.search(r"sk-ant-|ghp_[A-Za-z0-9]{20}|AKIA[0-9A-Z]{16}", blob)


# ---- size and packaging
def test_gzipped_bank_is_at_most_2_mb():
    assert (DATA / "bank-v1.jsonl.gz").stat().st_size <= 2 * 1024 * 1024


def test_package_data_is_not_excluded_by_build_config():
    import tomllib
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]
    assert cfg["wheel"]["packages"] == ["src/llm_router"]
    assert not any("local_check" in e or "/data" in e.replace("/src/llm_router/data", "") for e in cfg["wheel"].get("exclude", []))
    for n in ("bank-v1.jsonl.gz", "manifest.json", "NOTICE"):
        r = subprocess.run(["git", "check-ignore", "-q", str(DATA / n)], cwd=ROOT)
        assert r.returncode != 0, f"{n} is git-ignored, so hatch would drop it from the wheel"


@pytest.mark.slow
def test_built_wheel_contains_the_data(tmp_path):
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv not installed")
    r = subprocess.run([uv, "build", "--wheel", "--out-dir", str(tmp_path), str(ROOT)], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-800:]
    whl = next(tmp_path.glob("*.whl"))
    names = zipfile.ZipFile(whl).namelist()
    for n in ("bank-v1.jsonl.gz", "manifest.json", "NOTICE"):
        assert f"llm_router/local_check/data/{n}" in names
    assert "llm_router/local_check/bank.py" in names
    assert not any(n.startswith("scripts/") or "build_local_check_bank" in n for n in names)


def test_no_item_touches_a_licence_split_path(bank):
    m = bank.manifest
    assert m["licence_split_paths"] == {"celery/celery": ["docs/"]}
    for r in _raw_records():
        for pre in m["licence_split_paths"].get(r["repo"], ()):
            assert not any(f.startswith(pre) for f in r["files"]), r["id"]
            assert "diff --git a/" + pre not in r["diff"], r["id"]
    assert m["excluded_items_licence_split"] == {"celery/celery": len(m["excluded_items_licence_split_ids"])} and m["excluded_items_licence_split_ids"]


def test_split_paths_are_read_from_licence_text():
    import importlib.util
    spec = importlib.util.spec_from_file_location("lcb", Path(__file__).resolve().parents[1] / "scripts" / "build_local_check_bank.py")
    lcb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lcb)
    celery = 'BSD text.\n\nDocumentation License\n=====\n\nThe documentation portion (the rendered contents of the\n"docs" directory of a checkout) is supplied\nunder the "Creative Commons Attribution-ShareAlike 4.0" (CC BY-SA 4.0) License\n'
    assert lcb.split_paths(celery) == ["docs/"]
    assert lcb.split_paths("MIT text. The \"docs\" directory is mentioned alone.") == []
    assert lcb.split_paths("Creative Commons grant for the whole project.") == []
    assert lcb.touches_split({"_split": ["docs/"], "files": ["docs/a.rst"], "diff": ""})
    assert not lcb.touches_split({"_split": ["docs/"], "files": ["src/docs/a.rst"], "diff": "diff --git a/src/x b/src/x"})


def test_loader_enforces_every_manifest_count(copy):
    for name in ("items_by_job", "plants_by_kind", "items_by_stratum", "plants_by_provenance", "items_by_repo"):
        mp = copy / "manifest.json"
        m = json.loads(mp.read_text())
        first = next(iter(m["counts"][name]))
        m["counts"][name][first] += 1
        mp.write_text(json.dumps(m))
        with pytest.raises(B.BankError, match=name):
            B.load_bank(copy)
        m["counts"][name][first] -= 1
        mp.write_text(json.dumps(m))
    B.load_bank(copy)


def test_rebuilt_and_reused_plant_counts_are_separate(bank):
    c = bank.manifest["counts"]
    assert c["plants_topped_up"] == c["plants_by_provenance"]["lc1-rebuilt"]
    assert c["plants_reused_q37_calib"] == c["plants_by_provenance"]["q37-calib1"]


def test_shipped_release_audit(bank):
    audit = bank.manifest["release_audit"]
    assert isinstance(audit, dict)
    assert audit["bank_sha256"] == bank.manifest["bank_sha256"]
    assert audit["bank_sha256"] == hashlib.sha256((DATA / bank.manifest["bank_file"]).read_bytes()).hexdigest()
    ids = audit["audited_ids"]
    assert len(ids) == len(set(ids)) == 22
    items = {i.id: i for i in bank.items()}
    assert all(iid in items and items[iid].audited is False for iid in ids)
    in_bank = frozenset(i.id for i in bank.items() if i.audited)
    assert isinstance(bank.audited_ids, frozenset)
    assert bank.audited_ids == in_bank | frozenset(ids)
    assert len(bank.audited_ids) == 140


def test_removing_release_audit_preserves_draws(bank, copy):
    mp = copy / "manifest.json"
    manifest = json.loads(mp.read_text())
    del manifest["release_audit"]
    mp.write_text(json.dumps(manifest))
    without = B.load_bank(copy)
    for draw in (1, 2):
        assert [i.id for i in without.draw(draw)] == [i.id for i in bank.draw(draw)]
    assert without.audited_ids == frozenset(i.id for i in bank.items() if i.audited)


def test_release_audit_sha_mismatch_is_refused(copy):
    _rewrite_manifest(copy, release_audit={"bank_sha256": "0" * 64})
    with pytest.raises(B.BankError, match=r"release_audit.bank_sha256"):
        B.load_bank(copy)


def test_release_audit_unknown_id_is_refused(copy):
    _rewrite_manifest(copy, release_audit={"audited_ids": ["unknown-item"]})
    with pytest.raises(B.BankError, match="unknown item id"):
        B.load_bank(copy)


@pytest.mark.parametrize("audit", [None, [], "audit", {"audited_ids": None}, {"audited_ids": "item"}, {"audited_ids": [123]}])
def test_malformed_release_audit_is_refused(copy, audit):
    _rewrite_manifest(copy, release_audit=audit)
    with pytest.raises(B.BankError, match="release_audit"):
        B.load_bank(copy)


def test_duplicate_release_audit_ids_are_refused(bank, copy):
    iid = bank.items()[0].id
    _rewrite_manifest(copy, release_audit={"audited_ids": [iid, iid]})
    with pytest.raises(B.BankError, match="duplicates"):
        B.load_bank(copy)


def test_release_audit_optional_fields_and_unknown_keys(bank, copy):
    _rewrite_manifest(copy, release_audit={"future_field": "accepted"})
    assert B.load_bank(copy).audited_ids == frozenset(i.id for i in bank.items() if i.audited)


def test_release_audit_does_not_bypass_bank_integrity(copy):
    assert "release_audit" in json.loads((copy / "manifest.json").read_text())
    p = copy / "bank-v1.jsonl.gz"
    recs = gzip.decompress(p.read_bytes()).replace(b"diff --git", b"diff --GIT", 1)
    p.write_bytes(gzip.compress(recs, mtime=0))
    with pytest.raises(B.BankIntegrityError):
        B.load_bank(copy)
