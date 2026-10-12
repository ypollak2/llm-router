"""Full installation preserves writes while optionally omitting guidance."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router import host_detect, install_hooks, seats
from llm_router.commands import install

# exact CLI output (ANSI stripped) with .codex, .cursor and .windsurf present; the one
# "also detected" line is the only host-detection line.
GOLDEN = """
╔══════════════════════════════════════════╗
║   LLM Router — One-Command Install        ║
╚══════════════════════════════════════════╝

  ✓  Claude hooks installed

  Codex CLI detected — installing...
  ✓  Codex hooks installed
  also detected, not auto-wired: cursor, windsurf (wire one with `llm-router install --host <name>`)

  Seats  test seats
  no seat found — routed calls will bill an API key; log in to Claude Code, Codex, or start Ollama

✓ LLM Router installed globally.
  Every Claude Code and Codex CLI session will now auto-route tasks.
  Restart Claude Code (and Claude Desktop if installed) to activate.

  Provider keys (optional — router works without any):
  test provider keys
"""


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.chdir(tmp_path)

    calls = []

    def install_claude():
        calls.append("claude")
        (tmp_path / "claude-installed").write_text("installed")
        return ["Claude hooks installed"]

    def install_codex():
        calls.append("codex")
        (tmp_path / ".codex" / "installed").write_text("installed")
        return ["✓ Codex hooks installed"]

    detect = host_detect.detect_hosts
    monkeypatch.setattr(
        host_detect, "detect_hosts",
        lambda: detect(home=tmp_path, which=lambda binary: None),
    )
    monkeypatch.setattr(install_hooks, "install", install_claude)
    monkeypatch.setattr(install, "_install_codex_files", install_codex)
    monkeypatch.setattr(install_hooks, "claw_code_settings_path", lambda: None)
    monkeypatch.setattr(install_hooks, "check_api_keys", lambda: ["test provider keys"])
    monkeypatch.setattr(
        seats, "refresh_seats",
        lambda: SimpleNamespace(summary_line=lambda: "test seats", free_bucket=lambda: []),
    )
    monkeypatch.setattr(install, "_maybe_enable_claude_subscription", lambda found: None)
    return tmp_path, calls


@pytest.mark.parametrize("interactive", [False, True])
def test_full_install_returns_hosts_and_preserves_writes(sandbox, interactive, capsys):
    home, calls = sandbox
    (home / ".codex").mkdir()

    labels = install.run_full(interactive=interactive)

    assert isinstance(labels, list)
    assert labels == ["Claude Code", "Codex CLI"]
    assert calls == ["claude", "codex"]
    assert (home / "claude-installed").read_text() == "installed"
    assert (home / ".codex" / "installed").read_text() == "installed"
    output = capsys.readouterr().out
    assert ("Subcommands" in output) is interactive
    assert ("Try it:" in output) is interactive
    assert "test provider keys" in output


def test_detected_cursor_reports_explicit_install_command(sandbox, capsys):
    home, _ = sandbox
    (home / ".cursor").mkdir()

    assert install.run_full(interactive=False) == ["Claude Code"]

    assert (
        "also detected, not auto-wired: cursor (wire one with `llm-router install --host <name>`)"
        in capsys.readouterr().out
    )


def test_many_detected_hosts_print_one_summary_line(sandbox, capsys):
    home, _ = sandbox
    (home / ".cursor").mkdir()
    (home / ".windsurf").mkdir()

    install.run_full(interactive=False)

    lines = [ln for ln in capsys.readouterr().out.splitlines() if "not auto-wired" in ln]
    assert lines == [
        "  also detected, not auto-wired: cursor, windsurf (wire one with `llm-router install --host <name>`)"
    ]


def test_golden_output_on_a_fixture_home(sandbox, capsys):
    home, _ = sandbox
    for d in (".codex", ".cursor", ".windsurf"):
        (home / d).mkdir()

    install.run_full(interactive=False)

    out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert out == GOLDEN
def test_no_hosts_still_installs_claude(sandbox):
    home, calls = sandbox
    (home / ".codex").mkdir()

    assert install.run_full(interactive=False, flags=["--no-hosts"]) == ["Claude Code"]
    assert calls == ["claude"]
    assert (home / "claude-installed").exists()
    assert not (home / ".codex" / "installed").exists()


def test_force_and_claw_code_are_honoured_noninteractively(sandbox, monkeypatch):
    _, calls = sandbox
    settings = {"mcpServers": {"llm_router": {}, "other": {}}}
    saved = []
    monkeypatch.setattr(install_hooks, "_load_settings", lambda: settings)
    monkeypatch.setattr(install_hooks, "_save_settings", lambda value: saved.append(value))

    def install_claw():
        calls.append("claw")
        return ["claw-code hooks installed"]

    monkeypatch.setattr(install_hooks, "install_claw_code", install_claw)
    assert install.run_full(
        interactive=False, flags=["--force", "--claw-code"],
    ) == ["Claude Code"]
    assert calls == ["claude", "claw"]
    assert saved == [{"mcpServers": {"other": {}}}]


def test_cli_delegates_with_interactive_guidance(sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(
        install, "run_full",
        lambda **kwargs: calls.append(kwargs) or ["Claude Code"],
    )
    install._run_install(["--no-hosts"])
    assert calls == [{"interactive": True, "flags": ["--no-hosts"]}]
