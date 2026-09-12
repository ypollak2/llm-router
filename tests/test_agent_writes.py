"""The gate between a local model's proposed edit and the working tree.

`_resolve_path` contains every path to the project root, so the loop cannot
escape the repo. What it could do until this gate existed is rewrite any file
inside it with no record and nothing to compare against — `write_file` called
`path.write_text(...)` directly. That is the one thing standing between the
tool-loop rescue (LLM_ROUTER_LOCAL_AGENT_LOOP) and being safe to default on.

The mode must fail SAFE: a typo in the env var cannot silently grant write
access, and the gate cannot raise, because it runs inside a tool call.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_router.hooks.agent_loop import execute_tool
from llm_router.hooks import agent_writes


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("old\n", encoding="utf-8")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.delenv("LLM_ROUTER_AGENT_WRITES", raising=False)
    return tmp_path


def test_propose_is_the_default(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_AGENT_WRITES", raising=False)
    assert agent_writes.mode() == "propose"


@pytest.mark.parametrize("bad", ["", "yes", "APPLY_NOW", "1", "on", "true", "aply"])
def test_an_unrecognised_mode_does_not_grant_write_access(monkeypatch, bad):
    """Including the near-misses: `on`/`true`/`1` read as "enabled" to a human
    and must not read that way here."""
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", bad)
    assert agent_writes.mode() == "propose"


def test_propose_leaves_the_file_untouched(repo):
    out = execute_tool("edit_file",
                       {"path": "pkg/a.py", "old_string": "old", "new_string": "new"},
                       repo)
    assert (repo / "pkg" / "a.py").read_text() == "old\n"
    assert "NOT APPLIED" in out


def test_propose_returns_a_usable_patch(repo):
    out = execute_tool("edit_file",
                       {"path": "pkg/a.py", "old_string": "old", "new_string": "new"},
                       repo)
    assert "--- a/pkg/a.py" in out and "+++ b/pkg/a.py" in out
    assert "-old" in out and "+new" in out


def test_propose_tells_the_model_not_to_claim_success(repo):
    """Without this the model reports the edit in its final answer and you
    believe a file changed that did not."""
    out = execute_tool("write_file", {"path": "pkg/a.py", "content": "x"}, repo)
    assert "do not claim the file was modified" in out.lower()
    assert "do not retry" in out.lower()


def test_off_refuses_and_writes_nothing(repo, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", "off")
    out = execute_tool("write_file", {"path": "pkg/new.py", "content": "x"}, repo)
    assert "REFUSED" in out
    assert not (repo / "pkg" / "new.py").exists()


def test_apply_writes_and_journals_the_pre_image(repo, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", "apply")
    out = execute_tool("edit_file",
                       {"path": "pkg/a.py", "old_string": "old", "new_string": "new"},
                       repo)
    assert (repo / "pkg" / "a.py").read_text() == "new\n"
    assert "APPLIED" in out

    slots = list((repo / ".llm-router" / "agent_edits").iterdir())
    assert len(slots) == 1
    assert (slots[0] / "before").read_text() == "old\n", "cannot undo the edit"
    manifest = json.loads((slots[0] / "manifest.json").read_text())
    assert manifest["relative"] == "pkg/a.py"
    assert manifest["created"] is False


def test_a_new_file_is_journalled_as_created(repo, monkeypatch):
    """Reverting a created file means deleting it, not restoring empty content —
    the manifest has to say which."""
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", "apply")
    execute_tool("write_file", {"path": "pkg/new.py", "content": "x"}, repo)
    slot = next((repo / ".llm-router" / "agent_edits").iterdir())
    assert json.loads((slot / "manifest.json").read_text())["created"] is True


def test_an_unjournallable_edit_is_refused_not_applied(repo, monkeypatch):
    """An edit that cannot be undone must not happen. Applying it and hoping is
    the failure this whole module exists to prevent."""
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", "apply")
    monkeypatch.setattr(agent_writes, "journal", lambda *a, **k: None)
    out = execute_tool("edit_file",
                       {"path": "pkg/a.py", "old_string": "old", "new_string": "new"},
                       repo)
    assert "REFUSED" in out
    assert (repo / "pkg" / "a.py").read_text() == "old\n"


def test_journalling_never_raises_into_a_tool_call(repo, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", "/proc/nonexistent-and-unwritable")
    assert agent_writes.journal(repo / "pkg" / "a.py", "old\n", "new\n", repo) is None


def test_the_path_is_reported_relative_even_across_a_symlinked_root(repo):
    """_resolve_path returns a RESOLVED path; on macOS a temp root traverses
    /var -> /private/var, so an unresolved comparison prints an absolute path
    in every message."""
    out = execute_tool("edit_file",
                       {"path": "pkg/a.py", "old_string": "old", "new_string": "new"},
                       repo)
    assert "pkg/a.py" in out
    assert str(repo) not in out.split("\n")[0]


def test_a_huge_diff_is_truncated(repo, monkeypatch):
    """The diff is fed back as a tool result and carried into the final answer,
    so one edit must not be able to exhaust the context window."""
    (repo / "pkg" / "big.py").write_text("\n".join(f"line {i}" for i in range(5000)))
    out = execute_tool("write_file",
                       {"path": "pkg/big.py", "content": "\n".join(
                           f"changed {i}" for i in range(5000))}, repo)
    assert "truncated" in out
    assert len(out.splitlines()) < 260


def test_an_identical_write_is_reported_as_no_change(repo):
    out = execute_tool("write_file", {"path": "pkg/a.py", "content": "old\n"}, repo)
    assert "no change" in out.lower()


def test_the_containment_check_still_runs_first(repo):
    """The gate must not have displaced path containment."""
    out = execute_tool("write_file", {"path": "../escape.py", "content": "x"}, repo)
    assert "outside project root" in out
    assert not (repo.parent / "escape.py").exists()


# ── run_command ─────────────────────────────────────────────────────────────
#
# The write gate says nothing about commands, and a loop that cannot edit a file
# but can run any program is not gated. _BLOCKED_COMMANDS in agent_loop catches
# the catastrophic shapes (rm -rf /, mkfs, dd) and the executor avoids the shell,
# but neither constrains what a program DOES: git push, pip install and curl all
# pass as ordinary argv.

def test_the_default_command_mode_is_the_allowlist(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_AGENT_COMMANDS", raising=False)
    assert agent_writes.command_mode() == "allowlist"


@pytest.mark.parametrize("bad", ["", "yes", "ALL_OF_THEM", "1", "on"])
def test_an_unrecognised_command_mode_does_not_widen_it(monkeypatch, bad):
    monkeypatch.setenv("LLM_ROUTER_AGENT_COMMANDS", bad)
    assert agent_writes.command_mode() == "allowlist"


@pytest.mark.parametrize("cmd", [
    "ls -la", "git status", "git log --oneline", "git diff", "grep -r foo .",
    "python3 -c print(1)", "pytest -q", "wc -l x.py", "cat x.py",
])
def test_inspection_commands_are_allowed(cmd):
    allowed, _ = agent_writes.guard_command(cmd.split())
    assert allowed, f"{cmd} should be allowed"


@pytest.mark.parametrize("cmd", [
    "curl http://example.com", "wget http://example.com", "ssh host",
    "brew install x", "sudo rm x", "chmod 777 x", "docker run x", "sh -c whoami",
])
def test_programs_outside_the_allowlist_are_refused(cmd):
    allowed, msg = agent_writes.guard_command(cmd.split())
    assert not allowed
    assert "not in the inspection allowlist" in msg


@pytest.mark.parametrize("cmd", [
    "git push origin main", "git reset --hard", "git clean -fd",
    "git checkout main", "git rebase main", "git config user.name x",
    "git branch -D feature", "git stash",
])
def test_state_changing_subcommands_of_an_allowed_program_are_refused(cmd):
    """`git` is allowed for status/log/diff. It is also how you push and reset."""
    allowed, msg = agent_writes.guard_command(cmd.split())
    assert not allowed, f"{cmd} should be refused"
    assert "changes state" in msg


def test_a_flag_before_the_subcommand_does_not_smuggle_it_through():
    allowed, _ = agent_writes.guard_command(["git", "-C", "/tmp", "push"])
    assert not allowed


@pytest.mark.parametrize("module", ["pip", "ensurepip", "venv", "http.server"])
def test_python_m_cannot_reach_the_network_or_mutate_the_env(module):
    allowed, msg = agent_writes.guard_command(["python3", "-m", module, "install", "x"])
    assert not allowed
    assert "mutates the environment" in msg


def test_python_m_pytest_is_still_fine():
    allowed, _ = agent_writes.guard_command(["python3", "-m", "pytest", "-q"])
    assert allowed


def test_off_refuses_everything(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_AGENT_COMMANDS", "off")
    allowed, msg = agent_writes.guard_command(["ls"])
    assert not allowed and "disabled" in msg


def test_all_permits_what_the_allowlist_would_refuse(monkeypatch):
    """The escape hatch has to actually work, or people edit the source instead."""
    monkeypatch.setenv("LLM_ROUTER_AGENT_COMMANDS", "all")
    assert agent_writes.guard_command(["curl", "http://example.com"])[0]


def test_a_refused_command_does_not_execute(repo, tmp_path):
    marker = tmp_path / "ran"
    out = execute_tool("run_command",
                       {"command": f"touch {marker}"}, repo)
    assert "REFUSED" in out
    assert not marker.exists(), "a refused command still ran"


def test_the_catastrophic_blocklist_still_runs_first(repo):
    out = execute_tool("run_command", {"command": "rm -rf /"}, repo)
    assert "blocked for safety" in out.lower()
