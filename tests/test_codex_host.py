"""Codex-specific pure helpers: trust hash, state keys, TOML and AGENTS.md surgery."""
from __future__ import annotations

import pytest

from llm_router import codex_host as C

# Captured 2026-09-04: with exactly this record in config.toml, Codex 0.153.2 ran the
# hook; without it the hook was silently skipped. Pins the recipe end to end.
GOLDEN_CMD = "/private/tmp/claude-501/-Users-yaliandrona/aafedb09-c181-4dcc-8615-1a3f442a817f/scratchpad/dump_hook.sh"
GOLDEN_HASH = "sha256:e370c6859c4e6f39a52fdb18d2524fa9d67815369422717a1e7b6dcb65337497"


def test_trust_hash_matches_what_codex_accepted():
    assert C.hook_trust_hash("UserPromptSubmit", {"type": "command", "command": GOLDEN_CMD}) == GOLDEN_HASH


def test_user_prompt_submit_drops_the_matcher_before_hashing():
    """Codex normalizes the matcher to None for UserPromptSubmit/Stop, so a
    hooks.json that says matcher "" hashes identically to one that omits it."""
    h = {"type": "command", "command": GOLDEN_CMD}
    assert C.hook_trust_hash("UserPromptSubmit", h, matcher="") == GOLDEN_HASH
    assert C.hook_trust_hash("UserPromptSubmit", h, matcher="Bash") == GOLDEN_HASH


def test_tool_events_keep_the_matcher():
    h = {"type": "command", "command": "/x"}
    assert C.hook_trust_hash("PostToolUse", h, matcher="Bash") != C.hook_trust_hash("PostToolUse", h, matcher=None)


def test_default_timeout_is_600_and_explicit_600_is_identical():
    h = {"type": "command", "command": "/x"}
    assert C.hook_trust_hash("UserPromptSubmit", h) == C.hook_trust_hash("UserPromptSubmit", {**h, "timeout": 600})
    assert C.hook_trust_hash("UserPromptSubmit", h) != C.hook_trust_hash("UserPromptSubmit", {**h, "timeout": 30})


def test_optional_fields_only_enter_when_set():
    h = {"type": "command", "command": "/x"}
    base = C.hook_trust_hash("UserPromptSubmit", h)
    assert C.hook_trust_hash("UserPromptSubmit", {**h, "async": False}) == base
    assert C.hook_trust_hash("UserPromptSubmit", {**h, "async": True}) != base
    assert C.hook_trust_hash("UserPromptSubmit", {**h, "additionalContextLimit": 2500}) == base
    assert C.hook_trust_hash("UserPromptSubmit", {**h, "additionalContextLimit": 100}) != base
    assert C.hook_trust_hash("UserPromptSubmit", {**h, "statusMessage": "hi"}) != base


def test_unknown_event_or_non_command_rejected():
    with pytest.raises(ValueError):
        C.hook_trust_hash("Nope", {"type": "command", "command": "/x"})
    with pytest.raises(ValueError):
        C.hook_trust_hash("Stop", {"type": "mcp_tool", "server": "s", "tool": "t"})


def test_state_key_format():
    assert C.hook_state_key("/h/.codex/hooks.json", "UserPromptSubmit", 0, 1) == "/h/.codex/hooks.json:user_prompt_submit:0:1"
    assert C.hook_state_table("a:b:0:0") == 'hooks.state."a:b:0:0"'


def test_trust_records_walk_every_command_hook_and_can_be_restricted():
    doc = {"hooks": {
        "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/theirs"}]}],
        "UserPromptSubmit": [
            {"hooks": [{"type": "mcp_tool", "server": "s", "tool": "t"}]},
            {"hooks": [{"type": "command", "command": GOLDEN_CMD}]},
        ],
        "Bogus": [{"hooks": [{"type": "command", "command": "/x"}]}],
    }}
    recs = C.trust_records("/h/hooks.json", doc)
    assert recs["/h/hooks.json:user_prompt_submit:1:0"] == GOLDEN_HASH
    assert "/h/hooks.json:post_tool_use:0:0" in recs
    assert len(recs) == 2
    mine = C.trust_records("/h/hooks.json", doc, only_commands={GOLDEN_CMD})
    assert list(mine) == ["/h/hooks.json:user_prompt_submit:1:0"]


# ── TOML surgery ───────────────────────────────────────────────────────────

USER_TOML = (
    '# my config\nmodel = "gpt-5.5"\n\n'
    '[model_providers.gemini]\nname = "Gemini"\n\n'
    '[projects."/Users/me"]\ntrust_level = "trusted"\n'
)


def test_upsert_appends_table_and_preserves_user_text():
    out = C.upsert_toml_table(USER_TOML, C.MCP_TABLE, C.mcp_table_body("/bin/llm-router", []))
    assert out.startswith(USER_TOML)
    assert out.endswith('[mcp_servers.llm_router]\ncommand = "/bin/llm-router"\nargs = []\n')
    assert C.read_mcp_server(out) == {"command": "/bin/llm-router", "args": []}


def test_upsert_replaces_existing_table_in_place_only():
    text = USER_TOML + '\n[mcp_servers.llm_router]\ncommand = "old"\n\n[mcp_servers.other]\ncommand = "keep"\n'
    out = C.upsert_toml_table(text, C.MCP_TABLE, C.mcp_table_body("/new", ["--x"]))
    assert 'command = "old"' not in out
    assert '[mcp_servers.other]\ncommand = "keep"' in out
    assert out.startswith(USER_TOML)
    assert C.read_mcp_server(out) == {"command": "/new", "args": ["--x"]}


def test_upsert_into_empty_file():
    assert C.upsert_toml_table("", "a.b", "k = 1") == "[a.b]\nk = 1\n"


def test_remove_table_leaves_neighbours():
    text = '[a]\nx = 1\n\n[mcp_servers.llm_router]\ncommand = "c"\n\n[b]\ny = 2\n'
    out = C.remove_toml_table(text, C.MCP_TABLE)
    assert "[a]\nx = 1" in out and "[b]\ny = 2" in out and "llm_router" not in out


def test_quoted_state_table_roundtrips_through_tomllib():
    key = "/Users/me/.codex/hooks.json:user_prompt_submit:0:0"
    out = C.upsert_toml_table("[hooks.state]\n", C.hook_state_table(key), 'trusted_hash = "sha256:ab"')
    assert C.read_trust_records(out) == {key: "sha256:ab"}
    out2 = C.upsert_toml_table(out, C.hook_state_table(key), 'trusted_hash = "sha256:cd"')
    assert C.read_trust_records(out2) == {key: "sha256:cd"}
    assert out2.count("hooks.state.") == 1


def test_read_helpers_tolerate_broken_toml():
    assert C.read_mcp_server("[broken") is None
    assert C.read_trust_records("[broken") == {}


def test_toml_string_escapes():
    assert C.toml_string('a"b\\c') == '"a\\"b\\\\c"'


# ── AGENTS.md ──────────────────────────────────────────────────────────────

def test_marked_block_insert_replace_remove_preserve_user_text():
    user = "# My agents file\n\nDo things.\n"
    v1 = C.upsert_marked_block(user, "rules v1")
    assert v1.startswith(user)
    assert v1.endswith(f"{C.AGENTS_BLOCK_START}\nrules v1\n{C.AGENTS_BLOCK_END}\n")
    v2 = C.upsert_marked_block(v1 + "\n## Later section\n", "rules v2")
    assert "rules v1" not in v2 and "rules v2" in v2
    assert v2.startswith(user) and v2.rstrip().endswith("## Later section")
    assert C.remove_marked_block(v2).strip() == (user + "\n## Later section").strip()
    assert C.upsert_marked_block("", "x") == f"{C.AGENTS_BLOCK_START}\nx\n{C.AGENTS_BLOCK_END}\n"
