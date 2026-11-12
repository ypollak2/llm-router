"""UN-10 / W2-7: numbers-provenance lint (release rule 7.1.7)."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_numbers_provenance.py"
spec = importlib.util.spec_from_file_location("check_numbers_provenance", SCRIPT)
np_ = importlib.util.module_from_spec(spec)
spec.loader.exec_module(np_)  # not registered in sys.modules (neighbour lint)


def run(root: Path, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True, text=True, env=env,
    )


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "bench.py").write_text("")
    (tmp_path / "bench").mkdir()
    (tmp_path / "bench" / "out.json").write_text("{}")
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("# Project\n\nIt routes prompts.\n")
    return tmp_path


def test_positive_control_unsourced_readme_is_red(tree):
    (tree / "README.md").write_text("# Project\n\nOur router is 73% faster.\n")
    r = run(tree)
    print(r.stdout)  # shown with -s: the RED evidence
    assert r.returncode == 1
    assert "README.md:3" in r.stdout and "percent:73%" in r.stdout


def test_positive_control_unsourced_fragment_is_red(tree):
    (tree / "changelog.d" / "X.fixed.md").write_text("Latency fell to 12 ms for every call.\n")
    r = run(tree)
    assert r.returncode == 1 and "changelog.d/X.fixed.md:1" in r.stdout


@pytest.mark.parametrize("text", [
    # sourced: path + n + window
    "Measured 2026-10-18, n=30 runs: 73% faster (`scripts/bench.py`, PR #589).",
    # source and N present but via PR number and 'N of M'
    "8 of 8 calls passed, per PR #589.",
    # config threshold
    "The default timeout is 30 s and the cap is 80%.",
    # list price
    "Opus list price is $15 per 1M input tokens.",
    # version number and date
    "Released v15.4.0 on 2026-10-18.",
    # explicit escape with a reason
    "A 73% figure we cannot source. <!-- provenance: exempt (illustrative, not a result) -->",
])
def test_negative_controls_are_green(tree, text):
    (tree / "README.md").write_text(f"# P\n\n{text}\n")
    r = run(tree)
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("text", [
    "Savings were 73% in the example run.",                      # 'example' no longer silences
    "We cut cost by 73% under the new rule.",                    # 'rule' no longer silences
    "Accuracy rose to 73% (see README.md, 2026-10-01).",         # bare name + date
    "73% on 5 of 5 runs, measured with bench.py.",               # bare name
    "73% on 5 of 5 runs, measured with scripts/missing.py.",     # path does not exist
    "Accuracy rose to 73% (`scripts/bench.py`, 2026-10-01).",    # date is not N
    "Accuracy was 73 percent on a good day.",                    # 'percent' word
    "It is 4 times cheaper than before.",                        # 'times cheaper'
    "Savings $5.00 here, cap 3 of them.",                        # 'cap' no longer silences
])
def test_tightened_probes_are_red(tree, text):
    (tree / "README.md").write_text(f"# P\n\n{text}\n")
    assert run(tree).returncode == 1, text


def test_existing_path_and_n_is_green(tree):
    (tree / "README.md").write_text("# P\n\n73% on 5 of 5 runs (`scripts/bench.py`).\n")
    assert run(tree).returncode == 0


def test_docs_subdirectories_are_scanned(tree):
    (tree / "docs" / "sub").mkdir()
    (tree / "docs" / "sub" / "a.md").write_text("73% faster.\n")
    assert "docs/sub/a.md" in run(tree).stdout


def test_exempt_file_only_honoured_under_docs(tree):
    tag = "<!-- provenance: exempt-file (generated) -->\n"
    (tree / "docs" / "g.md").write_text(tag + "73% faster.\n")
    assert run(tree).returncode == 0
    (tree / "README.md").write_text(tag + "73% faster.\n")
    assert run(tree).returncode == 1
    (tree / "README.md").write_text("ok\n")
    (tree / "changelog.d" / "A.fixed.md").write_text(tag + "73% faster.\n")
    assert run(tree).returncode == 1


def test_pasted_copy_of_baselined_unit_is_new(tree):
    (tree / "README.md").write_text("Old: 73% faster.\n")
    subprocess.run([sys.executable, str(SCRIPT), "--root", str(tree), "--write-baseline"],
                   capture_output=True, text=True)
    assert run(tree).returncode == 0
    (tree / "README.md").write_text("Old: 73% faster.\n\nOld: 73% faster.\n")
    out = run(tree)
    assert out.returncode == 1 and "1 new" in out.stdout


def test_escape_needs_a_reason(tree):
    (tree / "README.md").write_text("73% faster. <!-- provenance: exempt () -->\n")
    assert run(tree).returncode == 1


def test_code_blocks_and_code_spans_are_not_scanned(tree):
    (tree / "README.md").write_text("```\nlatency 99 ms, 73%\n```\n\nUse `--max 73%`.\n")
    assert run(tree).returncode == 0


def test_table_is_one_unit_so_its_source_row_counts(tree):
    (tree / "README.md").write_text("| run | pass |\n|---|---|\n| a | 73% |\n| b | 80% |\n")
    assert run(tree).returncode == 1
    (tree / "README.md").write_text(
        "| run | pass | source |\n|---|---|---|\n"
        "| a | 73% | `bench/out.json` n=30 |\n| b | 80% | |\n"
    )
    assert run(tree).returncode == 0


def test_baseline_ratchet_new_fails_stale_fails(tree):
    (tree / "README.md").write_text("Old claim: 73% faster.\n")
    assert run(tree).returncode == 1
    # record it
    r = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tree), "--write-baseline"],
                       capture_output=True, text=True)
    assert "1 baseline entries" in r.stdout
    assert run(tree).returncode == 0                      # baselined: green
    (tree / "docs" / "new.md").write_text("Fresh claim: 90% better.\n")
    assert run(tree).returncode == 1                      # new: red
    (tree / "docs" / "new.md").unlink()
    (tree / "README.md").write_text("Claim removed.\n")
    out = run(tree)
    assert out.returncode == 1 and "STALE" in out.stdout  # fixed: baseline must shrink


def test_empty_scope_is_refused(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 2


def test_real_repo_at_head_is_green_and_scans_something():
    r = run(ROOT)
    assert r.returncode == 0, r.stdout
    findings, nfiles = np_.scan_tree(ROOT)
    assert nfiles > 100                                   # README + docs + fragments
    baseline = json.loads((ROOT / np_.BASELINE_NAME).read_text())["entries"]
    assert len(baseline) == len(findings) > 0             # the scan finds things


def test_this_changelog_fragment_passes_on_its_own():
    # The fragment was folded into CHANGELOG.md at the 16.0.0 release prep; a verbatim copy is kept as a fixture.
    frag = ROOT / "tests" / "fixtures" / "numbers_provenance" / "UN-10.added.md"
    assert frag.exists()
    assert not any(np_.analyse_unit(u) for _, u in np_.split_units(frag.read_text()))


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@needs_git
def test_git_ignored_generated_doc_is_not_scanned(tree):
    """PRV RC6 2026-11-07: the suite writes docs/SCENARIO_REPORT.md (git-ignored); the real-repo scan
    then flagged its 35 numbers. A file git ignores is generated, not a published claim."""
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    (tree / ".gitignore").write_text("/docs/generated.md\n")
    (tree / "docs" / "generated.md").write_text("Fresh claim: 90% better.\n")
    assert run(tree).returncode == 0
    (tree / "docs" / "tracked.md").write_text("Fresh claim: 90% better.\n")
    assert run(tree).returncode == 1                      # a non-ignored file is still scanned


@needs_git
def test_git_ignored_non_ascii_name_matches_exactly(tree):
    """Review of #851: without -z, git C-quotes non-ASCII names, so an ignored file was scanned anyway."""
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    (tree / ".gitignore").write_text("/docs/*-generated.md\n")
    (tree / "docs" / "caf\u00e9-generated.md").write_text("Fresh claim: 90% better.\n")
    assert run(tree).returncode == 0


@needs_git
def test_git_ignored_newline_name_matches_exactly(tree):
    """-z also keeps a name containing a newline as one path."""
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    (tree / ".gitignore").write_text("/docs/*-generated.md\n")
    (tree / "docs" / "a\nb-generated.md").write_text("Fresh claim: 90% better.\n")
    assert run(tree).returncode == 0


@needs_git
def test_non_ascii_name_under_c_locale_does_not_crash(tree):
    """text=True decoded git's output with the locale codec; under an ASCII locale an ignored non-ASCII
    name raised UnicodeDecodeError. Coercion to UTF-8 is switched off so the locale really is ASCII."""
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    (tree / ".gitignore").write_text("/docs/*-generated.md\n")
    (tree / "docs" / "caf\u00e9-generated.md").write_text("Fresh claim: 90% better.\n")
    env = {**os.environ, "LANG": "C", "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}
    r = run(tree, env)
    assert "Traceback" not in r.stderr
    assert r.returncode == 0


def test_non_git_root_notes_on_stderr_and_still_scans(tree):
    """Outside a git work tree nothing is ignored (fail-safe) and the scan says so on stderr."""
    (tree / "docs" / "generated.md").write_text("Fresh claim: 90% better.\n")
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(tree.parent)}  # tmp_path may sit inside a git tree
    r = run(tree, env)
    assert r.returncode == 1
    assert "git-ignored files are scanned too" in r.stderr


def test_git_missing_notes_on_stderr(monkeypatch, capsys, tmp_path):
    def no_git(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(np_.subprocess, "run", no_git)
    assert np_.git_ignored(tmp_path, ["docs/a.md"]) == set()
    assert "git check-ignore unavailable (FileNotFoundError)" in capsys.readouterr().err
