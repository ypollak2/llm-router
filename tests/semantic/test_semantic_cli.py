"""The CLI is where you check the thing before trusting it inside a prompt.

Mostly these are smoke tests, and they are worth having for one reason: this
command reaches into every module in the semantic package, so an import error
or a signature drift anywhere shows up here as a non-zero exit rather than as a
traceback in front of a user.

Two of them are not smoke tests. `status` must print the selected arm, because
a run labelled with the wrong arm is a measurement of nothing and the terminal
is where someone notices. And an invalid arm must exit non-zero rather than
printing a reassuring page, since the entire risk of a typo'd arm is that
everything looks normal.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from llm_router.commands.semantic import cmd_semantic


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ledger.py").write_text("def post_entry(amount):\n    return amount\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True,
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True, timeout=30)
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ROOT", str(repo))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(repo)
    return repo


def test_help_is_not_an_error(capsys):
    assert cmd_semantic([]) == 0
    assert "explain" in capsys.readouterr().out


def test_an_unknown_subcommand_exits_nonzero(capsys):
    assert cmd_semantic(["frobnicate"]) == 2


def test_status_before_any_index_says_so(project, capsys):
    assert cmd_semantic(["status"]) == 0
    out = capsys.readouterr().out
    assert "not built" in out
    assert "semantic index" in out, "it says the index is missing and not how to fix it"


def test_index_then_status_reports_what_was_built(project, capsys):
    assert cmd_semantic(["index"]) == 0
    capsys.readouterr()

    assert cmd_semantic(["status"]) == 0
    out = capsys.readouterr().out
    assert "1 files" in out
    assert "entities" in out


def test_status_always_prints_the_selected_arm(project, capsys, monkeypatch):
    """A run labelled with the wrong arm is a measurement of nothing."""
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_ARM", "D")
    assert cmd_semantic(["status"]) == 0
    assert "D" in capsys.readouterr().out


def test_an_invalid_arm_exits_nonzero_rather_than_looking_fine(project, capsys,
                                                               monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_ARM", "D2")
    assert cmd_semantic(["status"]) == 1, (
        "a typo'd arm printed a normal-looking status and exited 0, which is "
        "exactly how a mislabelled experiment gets run"
    )
    assert "D2" in capsys.readouterr().out


def test_explain_needs_a_prompt(project, capsys):
    assert cmd_semantic(["explain"]) == 2


def test_explain_runs_end_to_end(project, capsys):
    cmd_semantic(["index"])
    capsys.readouterr()

    assert cmd_semantic(["explain", "fix post_entry in ledger.py"]) == 0
    out = capsys.readouterr().out
    assert "post_entry" in out
    assert "status" in out


def test_seed_then_lessons_shows_the_starter_set(project, capsys):
    assert cmd_semantic(["seed"]) == 0
    first = capsys.readouterr().out
    assert "record(s)" in first

    assert cmd_semantic(["lessons"]) == 0
    out = capsys.readouterr().out
    assert "project-scope-write-001" in out
    assert "CONFLICT" in out, (
        "the seeded contradiction is not surfaced, so a reader would take the "
        "newest of two disagreeing records as settled"
    )


def test_seeding_twice_writes_nothing_the_second_time(project, capsys):
    cmd_semantic(["seed"])
    capsys.readouterr()

    assert cmd_semantic(["seed"]) == 0
    assert "already there" in capsys.readouterr().out
