"""The Codex writer: config.toml MCP table, trusted hooks, AGENTS.md, legacy cleanup.

Codex 0.153 reads `~/.codex/config.toml` for MCP servers and silently skips any
hooks.json hook without a matching `[hooks.state."…"] trusted_hash` record. The
previous installers wrote config.yaml / config.json / rules/*.md / instructions.md,
none of which Codex reads, so Codex -> llm-router never worked for anyone.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from llm_router import codex_host, install_manifest
from llm_router.commands import install


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "llm_router.install_hooks._build_mcp_entry",
        lambda: ({"command": "/opt/llm/bin/llm-router", "args": []}, []),
    )
    monkeypatch.setattr("llm_router.install_hooks._python_exe", lambda: "/opt/py/bin/python3")
    return tmp_path


def _toml(home):
    return (home / ".codex" / "config.toml").read_text()


def _hooks(home):
    return json.loads((home / ".codex" / "hooks.json").read_text())


# ── MCP ─────────────────────────────────────────────────────────────────────

def test_mcp_server_lands_in_config_toml_with_absolute_command(home):
    install._install_codex_files()
    entry = codex_host.read_mcp_server(_toml(home))
    assert entry["command"] == "/opt/llm/bin/llm-router" and entry["args"] == []
    assert not (home / ".codex" / "config.yaml").exists()
    assert not (home / ".codex" / "config.json").exists()


def test_routing_doors_are_auto_approved_for_codex_exec(home):
    """`codex exec` runs with approval policy "never"; an unapproved MCP tool fails
    with 'MCP tool call requires approval' instead of routing (observed live)."""
    import tomllib
    install._install_codex_files()
    tools = tomllib.loads(_toml(home))["mcp_servers"]["llm_router"]["tools"]
    assert tools["llm"] == {"approval_mode": "approve"}
    assert tools["llm_act"] == {"approval_mode": "approve"}
    assert "llm_router_admin" not in tools, "admin tools keep prompting"


def test_hand_edited_config_toml_survives_byte_for_byte(home):
    user = (
        '# mine\nmodel = "gpt-5.5"\nmodel_reasoning_effort = "medium"\n\n'
        '[model_providers.gemini]\nname = "Gemini"\nbase_url = "https://x/v1"\n\n'
        '[projects."/Users/me/proj"]\ntrust_level = "trusted"\n\n'
        '[mcp_servers.other_mcp]\ncommand = "/x/other_mcp"\nargs = []\n'
    )
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(user)
    install._install_codex_files()
    text = _toml(home)
    assert text.startswith(user)
    assert codex_host.read_mcp_server(text)["command"] == "/opt/llm/bin/llm-router"
    # the other MCP server is still there and still parses
    import tomllib
    assert tomllib.loads(text)["mcp_servers"]["other_mcp"]["command"] == "/x/other_mcp"


def test_missing_router_binary_warns_and_installs_nothing_unrunnable(home, monkeypatch):
    monkeypatch.setattr("llm_router.install_hooks._build_mcp_entry", lambda: (None, ["WARN no binary"]))
    actions = install._install_codex_files()
    assert any("WARN no binary" in a for a in actions)
    assert codex_host.read_mcp_server(_toml(home)) is None


# ── Hooks + trust ───────────────────────────────────────────────────────────

def test_hooks_are_registered_and_trusted(home):
    install._install_codex_files()
    doc = _hooks(home)
    route_cmd = f"/opt/py/bin/python3 {home}/.llm-router/hooks/codex-auto-route.py"
    post_cmd = f"{home}/.llm-router/hooks/codex-post-tool.py"
    assert doc["hooks"]["UserPromptSubmit"] == [{"hooks": [{"type": "command", "command": route_cmd}]}]
    assert doc["hooks"]["PostToolUse"] == [{"matcher": "Bash", "hooks": [{"type": "command", "command": post_cmd}]}]
    assert (home / ".llm-router" / "hooks" / "codex-auto-route.py").exists()
    assert (home / ".llm-router" / "hooks" / "llm_router_tool_surface.py").exists()

    hooks_json = home / ".codex" / "hooks.json"
    records = codex_host.read_trust_records(_toml(home))
    expected = codex_host.trust_records(hooks_json, doc)
    assert records == expected and len(records) == 2, "every hook we wrote must carry its trust record"
    assert records[f"{hooks_json}:user_prompt_submit:0:0"] == codex_host.hook_trust_hash(
        "UserPromptSubmit", {"type": "command", "command": route_cmd})


def test_existing_user_hooks_keep_their_index_and_are_not_trusted_by_us(home):
    (home / ".codex").mkdir()
    (home / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/theirs.sh"}]}],
        "PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "/theirs2.sh"}]}],
    }}))
    install._install_codex_files()
    doc = _hooks(home)
    assert doc["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "/theirs.sh"
    assert "codex-auto-route.py" in doc["hooks"]["UserPromptSubmit"][1]["hooks"][0]["command"]
    records = codex_host.read_trust_records(_toml(home))
    hooks_json = home / ".codex" / "hooks.json"
    assert f"{hooks_json}:user_prompt_submit:1:0" in records
    assert f"{hooks_json}:post_tool_use:1:0" in records
    assert f"{hooks_json}:user_prompt_submit:0:0" not in records, "we must not vouch for someone else's hook"


def test_rerun_is_idempotent(home):
    install._install_codex_files()
    first_toml, first_hooks = _toml(home), _hooks(home)
    actions = install._install_codex_files()
    assert _toml(home) == first_toml
    assert _hooks(home) == first_hooks
    assert any("already" in a.lower() for a in actions)


def test_moved_python_updates_the_trust_record(home, monkeypatch):
    install._install_codex_files()
    old = codex_host.read_trust_records(_toml(home))
    monkeypatch.setattr("llm_router.install_hooks._python_exe", lambda: "/new/python3")
    install._install_codex_files()
    doc = _hooks(home)
    cmds = [h["command"] for g in doc["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert len(cmds) == 2, "a new interpreter path is a new hook entry, the old one is left for the user"
    new = codex_host.read_trust_records(_toml(home))
    assert set(old) < set(new)


def test_corrupt_hooks_json_is_left_alone(home):
    (home / ".codex").mkdir()
    (home / ".codex" / "hooks.json").write_text("{not json")
    actions = install._install_codex_files()
    assert (home / ".codex" / "hooks.json").read_text() == "{not json"
    assert any("not valid JSON" in a for a in actions)
    assert codex_host.read_trust_records(_toml(home)) == {}


# ── AGENTS.md ───────────────────────────────────────────────────────────────

def test_agents_md_block_is_upserted_and_user_text_preserved(home):
    (home / ".codex").mkdir()
    (home / ".codex" / "AGENTS.md").write_text("# House rules\n\nBe brief.\n")
    install._install_codex_files()
    text = (home / ".codex" / "AGENTS.md").read_text()
    assert text.startswith("# House rules\n\nBe brief.\n")
    assert codex_host.AGENTS_BLOCK_START in text and codex_host.AGENTS_BLOCK_END in text
    assert "llm_router" in text.lower() or "llm-router" in text.lower()
    assert not (home / ".codex" / "instructions.md").exists()
    assert not (home / ".codex" / "rules" / "llm_router.md").exists()
    install._install_codex_files()
    assert (home / ".codex" / "AGENTS.md").read_text().count(codex_host.AGENTS_BLOCK_START) == 1


# ── Legacy cleanup ──────────────────────────────────────────────────────────

def test_legacy_files_codex_never_read_are_cleaned(home):
    codex = home / ".codex"
    codex.mkdir()
    (codex / "config.yaml").write_text("other: 1\n" + install._CODEX_LEGACY_YAML_BLOCK)
    (codex / "config.json").write_text(json.dumps({"mcpServers": {
        "other_mcp": {"command": "other_mcp", "args": []},
        "llm-router": {"command": "uvx", "args": ["claude-code-llm-router"]},
        "llm_router": {"command": "llm-router", "args": []},
    }}))
    (codex / "rules").mkdir()
    (codex / "rules" / "llm_router.md").write_text("<!-- llm_router-rules-version: 2 -->\n# rules\n")
    (codex / "rules" / "mine.md").write_text("keep\n")
    (codex / "instructions.md").write_text("<!-- llm_router-rules-version: 2 -->\n# rules\n")
    install_manifest.record("created_file", codex / "instructions.md")

    actions = install._install_codex_files()
    assert (codex / "config.yaml").read_text() == "other: 1\n"
    assert json.loads((codex / "config.json").read_text()) == {"mcpServers": {"other_mcp": {"command": "other_mcp", "args": []}}}
    assert not (codex / "rules" / "llm_router.md").exists()
    assert (codex / "rules" / "mine.md").exists()
    assert not (codex / "instructions.md").exists()
    assert sum(a.startswith("✓ Removed legacy") for a in actions) == 4


def test_legacy_cleanup_leaves_files_that_are_not_ours(home):
    codex = home / ".codex"
    codex.mkdir()
    (codex / "config.yaml").write_text("mcp:\n  servers:\n    llm_router:\n      command: /custom\n")
    (codex / "instructions.md").write_text("# my own instructions mentioning llm_router\n")
    (codex / "rules").mkdir()
    (codex / "rules" / "llm_router.md").write_text("# hand-written\n")
    install._install_codex_files()
    assert "/custom" in (codex / "config.yaml").read_text()
    assert (codex / "instructions.md").read_text() == "# my own instructions mentioning llm_router\n"
    assert (codex / "rules" / "llm_router.md").read_text() == "# hand-written\n"


# ── Uninstall ───────────────────────────────────────────────────────────────

def test_uninstall_removes_only_what_we_wrote(home):
    codex = home / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text('model = "gpt-5.5"\n\n[mcp_servers.other_mcp]\ncommand = "/x"\n')
    (codex / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/theirs"}]}]}}))
    (codex / "AGENTS.md").write_text("# mine\n")
    install._install_codex_files()
    install_manifest.apply_uninstall()

    text = _toml(home)
    assert 'model = "gpt-5.5"' in text and "[mcp_servers.other_mcp]" in text
    assert codex_host.read_mcp_server(text) is None
    assert codex_host.read_trust_records(text) == {}
    doc = _hooks(home)
    assert doc["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "/theirs"}]}]
    assert doc["hooks"].get("UserPromptSubmit", []) == []
    assert (codex / "AGENTS.md").read_text() == "# mine\n"
    assert not (home / ".llm-router" / "hooks" / "codex-auto-route.py").exists()


# ── Autodetect from the plain install ──────────────────────────────────────

def test_plain_install_wires_codex_when_detected(home, monkeypatch, capsys):
    from llm_router import host_detect, seats as S
    monkeypatch.setattr("llm_router.install_hooks.install", lambda force=False: ["claude ok"])
    monkeypatch.setattr("llm_router.install_hooks.install_claw_code", lambda: [])
    monkeypatch.setattr("llm_router.install_hooks.claw_code_settings_path", lambda: None)
    monkeypatch.setattr("llm_router.install_hooks.check_api_keys", lambda: [])
    monkeypatch.setattr(host_detect, "detect_hosts", lambda **kw: {
        "claude-code": host_detect.HostInfo("claude-code", "/bin/claude", None),
        "codex": host_detect.HostInfo("codex", "/bin/codex", None),
        "gemini-cli": host_detect.HostInfo("gemini-cli", None, None),
    })
    fake = S.Seats(claude=S.Seat(kind="claude.ai", plan="max"), codex=S.Seat(kind="chatgpt", plan="pro"),
                   detected_at="2026-09-04T00:00:00+00:00")
    monkeypatch.setattr(S, "refresh_seats", lambda **kw: fake)

    install._run_install([])
    out = capsys.readouterr().out
    assert "Codex CLI detected" in out
    assert codex_host.read_mcp_server(_toml(home)) is not None
    assert "Seats" in out and "claude.ai(max)" in out and "free bucket: claude, codex" in out
    assert "Every Claude Code and Codex CLI session" in out


def test_plain_install_skips_codex_with_no_hosts_or_when_absent(home, monkeypatch, capsys):
    from llm_router import host_detect
    monkeypatch.setattr("llm_router.install_hooks.install", lambda force=False: [])
    monkeypatch.setattr("llm_router.install_hooks.install_claw_code", lambda: [])
    monkeypatch.setattr("llm_router.install_hooks.claw_code_settings_path", lambda: None)
    monkeypatch.setattr("llm_router.install_hooks.check_api_keys", lambda: [])
    monkeypatch.setattr("llm_router.seats.refresh_seats", lambda **kw: (_ for _ in ()).throw(OSError("no")))
    present = {"codex": host_detect.HostInfo("codex", "/bin/codex", None),
               "claude-code": host_detect.HostInfo("claude-code", None, None),
               "gemini-cli": host_detect.HostInfo("gemini-cli", None, None)}
    monkeypatch.setattr(host_detect, "detect_hosts", lambda **kw: present)
    install._run_install(["--no-hosts"])
    assert not (home / ".codex" / "config.toml").exists()

    absent = {k: host_detect.HostInfo(k, None, None) for k in present}
    monkeypatch.setattr(host_detect, "detect_hosts", lambda **kw: absent)
    install._run_install([])
    assert not (home / ".codex" / "config.toml").exists()
    assert "Codex CLI detected" not in capsys.readouterr().out
