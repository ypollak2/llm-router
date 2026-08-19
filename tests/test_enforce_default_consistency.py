"""F01 regression: the DEFAULT enforcement mode must be a consistent 'smart'
across every module.

North Star: a fresh install (no LLM_ROUTER_ENFORCE env, no routing.yaml) must actually
ENFORCE routing so offloadable work goes to cheaper models. Before this fix the
modules disagreed — enforce_config→'soft' (log-only, never blocks), repo_config→
'hard', enforce-route docstring→'smart' — and the effective out-of-box default
was 'soft', i.e. routing was advisory-only and no tokens were saved.
"""
from __future__ import annotations

import pytest

from llm_router import enforce_config, repo_config


@pytest.fixture
def no_enforce_env(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_ENFORCE", raising=False)


def test_enforce_config_default_is_smart():
    assert enforce_config.DEFAULT_ENFORCE == "smart"


def test_resolver_defaults_to_smart_with_no_env_no_config(no_enforce_env, tmp_path):
    # isolated home with no routing.yaml -> must resolve to smart, not soft
    assert enforce_config.resolve_enforce_mode(cwd=tmp_path, home=tmp_path) == "smart"


def test_repo_config_valid_enforce_includes_smart():
    # 'smart' was missing from VALID_ENFORCE, so a repo config asking for smart
    # was silently rejected and fell through to 'hard'.
    assert "smart" in repo_config.VALID_ENFORCE


def test_repo_config_default_is_smart(no_enforce_env):
    assert repo_config.RepoConfig().effective_enforce() == "smart"


def test_all_modules_agree_on_smart_default(no_enforce_env, tmp_path):
    assert enforce_config.DEFAULT_ENFORCE == "smart"
    assert enforce_config.resolve_enforce_mode(cwd=tmp_path, home=tmp_path) == "smart"
    assert repo_config.RepoConfig().effective_enforce() == "smart"
