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


def test_draws_depend_on_the_bank_sha(bank, copy):
    other = B.load_bank(copy)
    assert [i.id for i in other.draw(1)] == [i.id for i in bank.draw(1)]       # same sha, same draw
    other.sha256 = "f" * 64
    assert [i.id for i in other.draw(1)] != [i.id for i in bank.draw(1)]       # seeded by the sha


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
