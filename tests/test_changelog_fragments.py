"""D-49: changelog fragments (assembler ordering, sections, idempotence; CI check)."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "changelog_fragments.py"
spec = importlib.util.spec_from_file_location("changelog_fragments", SCRIPT)
cf = importlib.util.module_from_spec(spec)
sys.modules["changelog_fragments"] = cf
spec.loader.exec_module(cf)

BASE = """# Changelog

## [Unreleased]

### Added
- old added

### Fixed
- old fixed

## [1.0.0] - 2026-01-01

### Added
- first
"""


@pytest.fixture
def env(tmp_path):
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "CHANGELOG.md").write_text(BASE)
    return tmp_path


def frag(env, name, body):
    (env / "changelog.d" / name).write_text(body)


def run_assemble(env, **kw):
    return cf.assemble(env / "CHANGELOG.md", env / "changelog.d", **kw)


def test_sections_and_ordering(env):
    frag(env, "B2.fixed.md", "second fix\nmore")
    frag(env, "A1.fixed.md", "first fix")
    frag(env, "C3.security.md", "- sec item")
    assert run_assemble(env) == 3
    text = (env / "CHANGELOG.md").read_text()
    unrel = text.split("## [1.0.0]")[0]
    assert unrel.index("- old fixed") < unrel.index("- first fix") < unrel.index("- second fix\n  more")
    assert unrel.index("### Fixed") < unrel.index("### Security") and "- sec item" in unrel
    assert "- first\n" in text.split("## [1.0.0]")[1]  # released history untouched
    assert not list((env / "changelog.d").glob("*.md"))


def test_idempotent(env):
    frag(env, "A1.added.md", "new")
    assert run_assemble(env) == 1
    once = (env / "CHANGELOG.md").read_text()
    assert run_assemble(env) == 0
    assert (env / "CHANGELOG.md").read_text() == once


def test_new_section_created_and_release_cut(env):
    frag(env, "D1.changed.md", "changed thing")
    run_assemble(env, version="1.1.0", date="2026-02-02")
    text = (env / "CHANGELOG.md").read_text()
    assert "## [Unreleased]\n\n## [1.1.0] - 2026-02-02\n\n### Added\n- old added" in text
    assert "### Changed\n- changed thing" in text
    assert text.index("## [1.1.0]") < text.index("## [1.0.0]")


def test_readme_ignored_bad_name_and_empty_rejected(env):
    frag(env, "README.md", "docs")
    assert run_assemble(env) == 0
    frag(env, "oops.md", "x")
    with pytest.raises(ValueError):
        run_assemble(env)
    (env / "changelog.d" / "oops.md").unlink()
    frag(env, "E1.fixed.md", "  \n")
    with pytest.raises(ValueError):
        run_assemble(env)


def test_parse_name():
    assert cf.parse_name("N21.fixed.md") == ("N21", "fixed")
    assert cf.parse_name("HAIKU55-TIER-1.added.md") == ("HAIKU55-TIER-1", "added")
    assert cf.parse_name("a.b.changed.md") == ("a.b", "changed")
    assert cf.parse_name("x.bogus.md") is None
    assert cf.parse_name("x.md") is None


def git(repo, *a):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=repo, check=True,
                   capture_output=True)


@pytest.fixture
def repo(env):
    git(env, "init", "-q", "-b", "main")
    git(env, "add", "-A")
    git(env, "commit", "-q", "-m", "base")
    git(env, "checkout", "-q", "-b", "pr")
    return env


def commit(repo):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pr")


def test_check_fragment_passes(repo, capsys):
    frag(repo, "N1.fixed.md", "ok")
    commit(repo)
    assert cf.check("main", repo=repo) == 0
    assert "1 fragment file(s) changed, 0 warning(s), 0 error(s)" in capsys.readouterr().out


def test_check_direct_edit_warns_not_fails_then_strict_fails(repo, capsys):
    p = repo / "CHANGELOG.md"
    p.write_text(p.read_text().replace("- old added", "- old added\n- sneaky"))
    commit(repo)
    assert cf.check("main", repo=repo) == 0
    assert "::warning file=CHANGELOG.md::" in capsys.readouterr().out
    assert cf.check("main", strict=True, repo=repo) == 1


def test_check_release_section_edit_is_not_flagged(repo, capsys):
    p = repo / "CHANGELOG.md"
    p.write_text(p.read_text().replace("- first", "- first (typo fix)"))
    commit(repo)
    assert cf.check("main", repo=repo) == 0
    assert "0 warning(s)" in capsys.readouterr().out


def test_check_bad_fragment_name_fails(repo):
    frag(repo, "bad.md", "x")
    commit(repo)
    assert cf.check("main", repo=repo) == 1
