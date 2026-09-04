"""`llm-router install --project`: one rules file both agents read."""
from __future__ import annotations

import os
import pathlib

import pytest

from llm_router import codex_host, install_manifest
from llm_router.commands import install


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("HOME", str(h))
    return h


def _block_count(path):
    return path.read_text().count(codex_host.AGENTS_BLOCK_START)


def test_fresh_repo_gets_agents_md_and_a_relative_claude_link(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    actions = install._install_project_files(repo)
    agents, claude = repo / "AGENTS.md", repo / "CLAUDE.md"
    assert _block_count(agents) == 1
    assert "llm_query" in agents.read_text() or "llm(" in agents.read_text()
    assert claude.is_symlink() and os.readlink(claude) == "AGENTS.md"
    assert claude.read_text() == agents.read_text()
    assert any("Created" in a for a in actions) and any("Linked" in a for a in actions)


def test_existing_agents_md_is_preserved_and_rerun_is_idempotent(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# Team conventions\n\nUse uv.\n")
    install._install_project_files(repo)
    text = (repo / "AGENTS.md").read_text()
    assert text.startswith("# Team conventions\n\nUse uv.\n")
    assert _block_count(repo / "AGENTS.md") == 1
    actions = install._install_project_files(repo)
    assert _block_count(repo / "AGENTS.md") == 1
    assert (repo / "AGENTS.md").read_text() == text
    assert all("skipped" in a for a in actions)


def test_existing_claude_md_file_is_kept_and_gets_the_block(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("# Claude notes\n")
    install._install_project_files(repo)
    claude = repo / "CLAUDE.md"
    assert not claude.is_symlink()
    assert claude.read_text().startswith("# Claude notes\n")
    assert _block_count(claude) == 1
    assert _block_count(repo / "AGENTS.md") == 1


def test_foreign_claude_link_is_left_alone(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "other.md").write_text("x\n")
    (repo / "CLAUDE.md").symlink_to("other.md")
    actions = install._install_project_files(repo)
    assert os.readlink(repo / "CLAUDE.md") == "other.md"
    assert any("left alone" in a for a in actions)


def test_windows_copies_instead_of_linking(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    install._install_project_files(repo, use_symlink=False)
    claude = repo / "CLAUDE.md"
    assert not claude.is_symlink()
    assert claude.read_text() == (repo / "AGENTS.md").read_text()


def test_uninstall_removes_the_block_and_our_link_only(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# mine\n")
    install._install_project_files(repo)
    install_manifest.apply_uninstall()
    assert (repo / "AGENTS.md").read_text() == "# mine\n"
    assert not (repo / "CLAUDE.md").exists() and not (repo / "CLAUDE.md").is_symlink()


def test_run_install_project_flag_uses_cwd_and_touches_nothing_global(home, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    install._run_install(["--project"])
    assert (repo / "AGENTS.md").exists() and (repo / "CLAUDE.md").is_symlink()
    assert not (home / ".claude").exists() and not (home / ".codex").exists()
    assert "AGENTS.md" in capsys.readouterr().out
