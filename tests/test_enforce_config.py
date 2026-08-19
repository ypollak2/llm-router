"""The unified enforcement-mode resolver used by BOTH auto-route and enforce-route."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_router.enforce_config import DEFAULT_ENFORCE, resolve_enforce_mode


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_ENFORCE", raising=False)


def _routing_yaml(home: Path, value: str) -> None:
    d = home / ".llm-router"
    d.mkdir(parents=True, exist_ok=True)
    (d / "routing.yaml").write_text(f"profile: standard\nenforce: {value}\n")


def _repo_yml(cwd: Path, value: str) -> None:
    (cwd / ".llm_router.yml").write_text(f"enforce: {value}\n")


def test_default_is_smart(tmp_path, monkeypatch):
    # F01/North Star: default is "smart" — enforce routing out of the box (block
    # Q&A/reasoning tools until routed, allow code/local work) so offloadable work
    # goes to cheaper models. Relax with LLM_ROUTER_ENFORCE=soft/off.
    assert resolve_enforce_mode(cwd=tmp_path, home=tmp_path) == DEFAULT_ENFORCE == "smart"


def test_routing_yaml_is_read(tmp_path):
    _routing_yaml(tmp_path, "hard")
    assert resolve_enforce_mode(cwd=tmp_path / "empty", home=tmp_path) == "hard"


def test_repo_yml_overrides_routing_yaml(tmp_path):
    _routing_yaml(tmp_path, "hard")
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_yml(repo, "advise")
    assert resolve_enforce_mode(cwd=repo, home=tmp_path) == "advise"


def test_env_overrides_everything(tmp_path, monkeypatch):
    _routing_yaml(tmp_path, "hard")
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_yml(repo, "advise")
    monkeypatch.setenv("LLM_ROUTER_ENFORCE", "off")
    assert resolve_enforce_mode(cwd=repo, home=tmp_path) == "off"


def test_repo_yml_found_in_ancestor(tmp_path):
    _repo_yml(tmp_path, "smart")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert resolve_enforce_mode(cwd=deep, home=tmp_path / "nohome") == "smart"


def test_value_is_normalized(tmp_path):
    _routing_yaml(tmp_path, "  HARD  # inline comment")
    assert resolve_enforce_mode(cwd=tmp_path / "x", home=tmp_path) == "hard"


def test_missing_files_fall_to_default(tmp_path):
    assert resolve_enforce_mode(cwd=tmp_path / "nope", home=tmp_path / "nohome") == "smart"
