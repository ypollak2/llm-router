"""M-12 — the README documented commands that did not exist.

    llm-router health    documented twice; no such command
    llm-router gain      `commands/gain.py` is a COMPLETE implementation
                         (`show_gain`) that was never wired into the CLI, while
                         `commands/demo.py` told users to run it

The `gain` case is the worse of the two. A command that is advertised and
unreachable teaches the user that the tool is broken, rather than that the docs
are wrong — and here the code to serve it had been sitting in the tree all along.

`docs/RESEARCH_FIRSTRUN.md` had already recorded that `llm-router health` does
not exist. The finding survived being written down, which is its own lesson:
prose about a defect does not close it.

**A claim that did NOT reproduce.** The audit also reported "two documented host
integrations are rejected by the installer". Tested directly, all six documented
`--host` values (`codex`, `gemini-cli`, `vscode`, `cursor`, `claude-code`,
`claude-desktop`) exit 0. That half of M-12 is not a defect, and this test pins
it so a real regression there would be visible.
"""

from __future__ import annotations

import pathlib
import re
import os
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
README = REPO / "README.md"
CLI = REPO / "src" / "llm_router" / "cli.py"


def _documented_commands() -> set[str]:
    """Subcommands the README tells a user to RUN.

    Only inside ```bash fences. A bare line-start match also catches prose --
    "llm-router runs entirely on your machine" is a sentence, and treating it as
    an invocation would make this gate cry wolf, which is how a gate gets muted.
    """
    text = README.read_text(encoding="utf-8")
    found = set()
    for block in re.findall(r"```(?:bash|sh|console)\n(.*?)```", text, re.S):
        for line in block.splitlines():
            m = re.match(r"\s*\$?\s*llm-router ([a-z][a-z0-9-]*)", line)
            if m:
                found.add(m.group(1))
    # `install` takes --host and is covered separately
    return found - {"install"}


def _documented_hosts() -> set[str]:
    text = README.read_text(encoding="utf-8")
    return set(re.findall(r"llm-router install --host ([a-z][a-z0-9-]*)", text))


def test_the_readme_actually_documents_commands():
    """Denominator guard: an empty set passes every assertion below."""
    cmds = _documented_commands()
    assert len(cmds) >= 5, f"only parsed {len(cmds)} documented commands: {cmds}"
    assert "status" in cmds and "doctor" in cmds


def test_every_documented_command_is_known_to_the_cli():
    """The defect, generalised: a command in the docs must be dispatchable."""
    cli_src = CLI.read_text(encoding="utf-8")
    missing = [c for c in sorted(_documented_commands()) if f'"{c}"' not in cli_src]
    assert not missing, (
        f"documented but not present in the CLI: {missing}. Either implement it, "
        f"or remove it from the README — an advertised command that does not "
        f"exist reads as a broken tool."
    )


def test_health_is_gone_from_the_readme():
    """The specific claim, pinned. `doctor` is the command that does this job."""
    text = README.read_text(encoding="utf-8")
    assert "llm-router health" not in text, (
        "`llm-router health` is documented again and still does not exist"
    )


def test_gain_is_reachable():
    """It was a complete implementation with no route to it."""
    from llm_router.commands.gain import show_gain

    assert callable(show_gain)
    assert '"gain"' in CLI.read_text(encoding="utf-8"), "gain is not dispatched"


@pytest.mark.parametrize("period", ["today", "week", "month", "all"])
def test_gain_runs_for_each_period(period):
    r = subprocess.run(
        [sys.executable, "-m", "llm_router.cli", "gain", period],
        capture_output=True, text=True, timeout=90, cwd=str(REPO),
    )
    assert r.returncode == 0, f"`llm-router gain {period}` exited {r.returncode}: {r.stderr[:300]}"
    assert r.stdout.strip(), "gain produced no output"


def test_an_unknown_period_is_refused_clearly():
    """Anti-over-correction: wiring a command must not accept anything at all."""
    r = subprocess.run(
        [sys.executable, "-m", "llm_router.cli", "gain", "yesteryear"],
        capture_output=True, text=True, timeout=90, cwd=str(REPO),
    )
    assert r.returncode != 0
    assert "period" in (r.stdout + r.stderr).lower()


def test_nothing_suggests_a_command_that_cannot_be_run():
    """`demo.py` printed `llm_router gain` — right command, wrong spelling.

    The CLI is `llm-router`; a suggestion a user cannot copy-paste is the same
    defect in a smaller form.
    """
    src = REPO / "src" / "llm_router"
    offenders = []
    # Only an INVOCATION counts. "llm_router health check" is a banner heading
    # describing what is running, not something a user types, and rewriting it
    # would be the over-correction this suite warns about elsewhere: a gate that
    # cries wolf is a gate that gets muted.
    invocation = re.compile(
        r"llm_router (gain|status|doctor|soak|health)\b(?! *(check|checks|module|package|ledger))"
    )
    for f in src.rglob("*.py"):
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in invocation.finditer(text):
            offenders.append(f"{f.relative_to(src)}: {m.group(0)}")
    assert not offenders, (
        f"suggestions using the underscore spelling, which is not the CLI name: "
        f"{offenders}"
    )


@pytest.mark.parametrize("host", sorted(_documented_hosts()))
def test_every_documented_host_is_accepted(host, tmp_path):
    """The half of M-12 that did not reproduce, pinned so a regression shows.

    The installer writes into ``Path.home()`` — `~/.cursor/mcp.json` for cursor,
    `~/.gemini/settings.json` for gemini-cli. This ran it against the REAL home,
    and the sandbox guard failed the test in teardown for writing outside its
    sandbox. It passed locally only because those files already existed on this
    machine, so nothing was newly created; on a clean runner they are, and CI was
    red on it from 15.0.1 through 15.1.0.

    The installer runs in a SUBPROCESS, so `monkeypatch.setenv` in this process
    does not reach it and a patched `Path.home` does not either — the child reads
    its own environment. The home has to be handed over as an env var, which is
    what the guard's advice ("isolate BOTH") means on this side of a fork.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    # Point the router's own state at the sandbox too, so the install does not
    # touch the operator's real ledger while proving it accepts the host.
    env["LLM_ROUTER_HOME"] = str(home / ".llm-router")
    r = subprocess.run(
        [sys.executable, "-m", "llm_router.cli", "install", "--host", host],
        capture_output=True, text=True, timeout=90, cwd=str(tmp_path), env=env,
    )
    assert r.returncode == 0, (
        f"`install --host {host}` is documented but exited {r.returncode}: "
        f"{(r.stderr or r.stdout)[:300]}"
    )


def test_the_host_list_was_actually_parsed():
    hosts = _documented_hosts()
    assert len(hosts) >= 4, f"only parsed {len(hosts)} documented hosts: {hosts}"
