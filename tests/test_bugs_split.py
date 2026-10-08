"""BUGS-a: one file per bug entry (plan v16 BUGS-1, D-R8-1 = A).

The acceptance test for the migration of docs/BUGS.md into docs/bugs/<id>.md.
It pins: the id set survives (n printed), entry text is byte-preserved, the
generated index lists every id once, concurrent PRs that add different entries
merge in both orders without a conflict (and two that add the same id DO
conflict, so the test can fail), and `bugs_index.py --check` is red on an entry
without a Test heading, on a duplicate id, on an unresolved reference and on a
stale index.

`tests/fixtures/bugs_legacy.md` is docs/BUGS.md as it stood on origin/main
09fece62, the file the migration split.
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
LEGACY = ROOT / "tests" / "fixtures" / "bugs_legacy.md"
HEAD_RE = re.compile(r"^## (\S+?)\. ", re.M)


def _load(name: str):
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(SCRIPTS))


def _run(script: str, *args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPTS / script), *args],
        cwd=cwd, capture_output=True, text=True,
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
         "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args],
        cwd=cwd, capture_output=True, text=True,
    )


def _entry(eid: str, body_extra: str = "- **Test.** t\n") -> str:
    return (
        f"---\nid: {eid}\nstatus: open\n---\n"
        f"## {eid}. Title of {eid}\n\n- **Symptom.** s\n- **Cause.** c\n- **Fix.** f\n{body_extra}\n"
    )


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A repo-shaped tree: the legacy file split into docs/bugs/."""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "tests").mkdir()
    shutil.copy(LEGACY, root / "docs" / "BUGS.md")
    r = _run("bugs_split.py", "--root", str(root))
    assert r.returncode == 0, r.stderr
    return root


def test_id_set_preserved_and_text_byte_identical(tree: Path) -> None:
    legacy = LEGACY.read_text()
    before = HEAD_RE.findall(legacy)
    assert len(before) == len(set(before)), "legacy ids are not unique"
    files = sorted((tree / "docs" / "bugs").glob("*.md"))
    mod = _load("bugs_index")
    entries = [mod.parse_entry(p) for p in files]
    after = [e.id for e in entries]
    n = len(before)
    print(f"BUGS-a ids before={n} after={len(after)}")
    assert n >= 50, "empty/short legacy set would pass vacuously"
    assert set(before) == set(after) and len(after) == n
    # byte preservation: the bodies, in legacy order, are the legacy sections.
    by_id = {e.id: e.body for e in entries}
    first = legacy.index("## ")
    assert "".join(by_id[i] for i in before) == legacy[first:]
    # ids keep their exact spelling
    for must in ("P03-1", "AB-1", "CI-1", "LC-1", "DT-1", "P010-2", "P0.14-a", "A.0-3", "1", "29"):
        assert must in after, must


def test_file_names_are_filesystem_safe_and_mapped(tree: Path) -> None:
    mod = _load("bugs_index")
    names = [p.name for p in (tree / "docs" / "bugs").glob("*.md")]
    assert len(names) == len(set(n.lower() for n in names))
    for n in names:
        assert re.fullmatch(r"[A-Za-z0-9_-]+\.md", n), n
    assert "0001.md" in names and "0029.md" in names
    assert "P0_14-a.md" in names and "A_0-3.md" in names
    assert mod.file_name("P0.14-a") == "P0_14-a.md"
    assert mod.file_name("a/b") == "a_b.md"
    r = _run("bugs_index.py", "--root", str(tree))
    assert "P0_14-a.md" in r.stdout and "P0.14-a" in r.stdout  # mapping is recorded


def test_pointer_has_no_table_and_index_lists_every_id_once(tree: Path) -> None:
    pointer = (tree / "docs" / "BUGS.md").read_text()
    assert not re.search(r"^\|", pointer, re.M), "pointer must not carry an index table"
    assert "docs/bugs/" in pointer
    r = _run("bugs_index.py", "--root", str(tree))
    assert r.returncode == 0, r.stderr
    rows = [ln for ln in r.stdout.splitlines() if ln.startswith("| ") and not ln.startswith("| id ")
            and not ln.startswith("|---")]
    ids = [ln.split("|")[1].strip() for ln in rows]
    expect = HEAD_RE.findall(LEGACY.read_text())
    assert sorted(ids) == sorted(expect) and len(ids) == len(set(ids))
    print(f"BUGS-a index rows={len(ids)}")


def test_two_branches_adding_different_entries_merge_in_both_orders(tree: Path, tmp_path: Path) -> None:
    repo = tmp_path / "scratch"
    shutil.copytree(tree, repo)
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    assert _git(repo, "add", "-A").returncode == 0
    assert _git(repo, "commit", "-qm", "base").returncode == 0
    for name, eid in (("a", "ZZ-1"), ("b", "ZZ-2")):
        assert _git(repo, "checkout", "-q", "-b", name, "main").returncode == 0
        (repo / "docs" / "bugs" / f"{eid}.md").write_text(_entry(eid))
        assert _git(repo, "add", "-A").returncode == 0
        assert _git(repo, "commit", "-qm", eid).returncode == 0
    indexes = []
    for first, second in (("a", "b"), ("b", "a")):
        assert _git(repo, "checkout", "-q", "-B", f"m-{first}", "main").returncode == 0
        for other in (first, second):  # 2 merges per order, 4 in all
            m = _git(repo, "merge", "--no-ff", "-m", f"merge {other}", other)
            assert m.returncode == 0, f"order {first}{second}: merge of {other} conflicted:\n{m.stdout}{m.stderr}"
        idx = _run("bugs_index.py", "--root", str(repo))
        assert idx.returncode == 0, idx.stderr
        assert "ZZ-1" in idx.stdout and "ZZ-2" in idx.stdout
        assert _run("bugs_index.py", "--root", str(repo), "--check").returncode == 0
        indexes.append(idx.stdout)
    assert indexes[0] == indexes[1], "index differs between merge orders"
    n = len(HEAD_RE.findall(LEGACY.read_text())) + 2
    print(f"BUGS-a merges: 4 clean (both orders), index rows {n}")


def test_two_branches_adding_the_same_id_conflict(tree: Path, tmp_path: Path) -> None:
    """Control: the merge test can fail."""
    repo = tmp_path / "scratch"
    shutil.copytree(tree, repo)
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    assert _git(repo, "add", "-A").returncode == 0
    assert _git(repo, "commit", "-qm", "base").returncode == 0
    for name, extra in (("a", "- **Test.** one\n"), ("b", "- **Test.** two\n")):
        assert _git(repo, "checkout", "-q", "-b", name, "main").returncode == 0
        (repo / "docs" / "bugs" / "ZZ-9.md").write_text(_entry("ZZ-9", extra))
        assert _git(repo, "add", "-A").returncode == 0
        assert _git(repo, "commit", "-qm", name).returncode == 0
    assert _git(repo, "checkout", "-q", "a").returncode == 0
    assert _git(repo, "merge", "b").returncode != 0


def test_check_is_green_on_the_split_tree(tree: Path) -> None:
    r = _run("bugs_index.py", "--root", str(tree), "--check")
    assert r.returncode == 0, r.stdout + r.stderr


def test_check_red_on_entry_missing_test_heading(tree: Path) -> None:
    (tree / "docs" / "bugs" / "ZZ-3.md").write_text(_entry("ZZ-3", body_extra=""))
    r = _run("bugs_index.py", "--root", str(tree), "--check")
    assert r.returncode != 0 and "ZZ-3" in (r.stdout + r.stderr) and "Test" in (r.stdout + r.stderr)


def test_check_red_on_duplicate_id_and_bad_file_name(tree: Path) -> None:
    (tree / "docs" / "bugs" / "dup.md").write_text(_entry("AB-1"))
    r = _run("bugs_index.py", "--root", str(tree), "--check")
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "AB-1" in out


def test_check_red_on_unresolved_reference_and_green_on_prose(tree: Path) -> None:
    (tree / "src" / "x.py").write_text("# prose: BUGS.md has the entry; real: BUGS.md P010-1)\n")
    assert _run("bugs_index.py", "--root", str(tree), "--check").returncode == 0
    (tree / "src" / "y.py").write_text("# see docs/BUGS.md NOPE-9 for the story\n")
    r = _run("bugs_index.py", "--root", str(tree), "--check")
    assert r.returncode != 0 and "NOPE-9" in (r.stdout + r.stderr)


def test_check_red_on_stale_index(tree: Path, tmp_path: Path) -> None:
    idx = tmp_path / "INDEX.md"
    assert _run("bugs_index.py", "--root", str(tree), "--write", str(idx)).returncode == 0
    ok = _run("bugs_index.py", "--root", str(tree), "--check", "--index", str(idx))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    (tree / "docs" / "bugs" / "ZZ-4.md").write_text(_entry("ZZ-4"))  # entry added, index not regenerated
    stale = _run("bugs_index.py", "--root", str(tree), "--check", "--index", str(idx))
    assert stale.returncode != 0 and "stale" in (stale.stdout + stale.stderr).lower()


def test_repo_does_not_gitignore_bug_entries() -> None:
    """/docs/* is ignore-by-default; a new docs/bugs/<id>.md must not be silently dropped."""
    for name in ("0001.md", "ZZ-1.md"):
        r = _git(ROOT, "check-ignore", "-q", f"docs/bugs/{name}")
        assert r.returncode == 1, f"docs/bugs/{name} is gitignored"
