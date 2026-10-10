"""FLAKE-LAV-1: the repo-mutation guard must ignore a live Codex's own churn in
~/.codex/config.toml and still see llm_router's installer writes there."""

from __future__ import annotations

from tests.conftest import _codex_config_slice

BASE = '[projects."/work/a"]\ntrust_level = "trusted"\n'
MCP = '[mcp_servers.llm_router]\ncommand = "llm-router"\nargs = ["serve"]\n'
TRUST = '[hooks.state."/h/.codex/hooks.json:user_prompt_submit:0:0"]\ntrusted_hash = "sha256:abc"\n'


def _slice(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return _codex_config_slice(p)


def test_missing_file_is_none(tmp_path):
    assert _codex_config_slice(tmp_path / "nope.toml") is None


def test_codex_appending_project_trust_tables_is_not_a_change(tmp_path):
    before = _slice(tmp_path, BASE + MCP)
    after = _slice(tmp_path, BASE + '[projects."/work/b"]\ntrust_level = "trusted"\n' + MCP)
    assert before == after


def test_a_new_llm_router_mcp_table_is_a_change(tmp_path):
    assert _slice(tmp_path, BASE) != _slice(tmp_path, BASE + MCP)


def test_an_edited_llm_router_mcp_table_is_a_change(tmp_path):
    assert _slice(tmp_path, MCP) != _slice(tmp_path, MCP.replace("serve", "other"))


def test_a_new_hook_trust_record_is_a_change(tmp_path):
    assert _slice(tmp_path, BASE) != _slice(tmp_path, BASE + TRUST)


def test_unparseable_toml_is_reported_not_raised(tmp_path):
    assert _slice(tmp_path, "[broken") == "<unreadable>"
