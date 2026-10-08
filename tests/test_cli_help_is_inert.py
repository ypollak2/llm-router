"""CLI-HELP-1: `llm-router <subcommand> --help` must print help and change nothing.

`llm-router uninstall --help` ran a real uninstall: it rewrote ~/.claude/settings.json
and the cwd's .vscode/mcp.json and .windsurf/mcp.json (found in the #323 review).
A scan of every subcommand on adf93a02 found the same shape in `update` and
`onboard` (rewrote the hooks), `init-claude-memory`, `budget`, `team`, `gain`,
`doctor`, `summary`, `probe`, `explain-dashboard` (state writes), and `broker` /
`gateway` (started a server).

Each case runs the real CLI in a subprocess with HOME and cwd in a temp dir that
holds a host config with an llm_router entry, then compares every file under
both before and after.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from llm_router import cli

_REPO = Path(__file__).resolve().parent.parent
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _dispatched() -> list[str]:
    names = set(re.findall(r'args\[0\] == "([a-z-]+)"', inspect.getsource(cli.main)))
    names.discard("run-hook")   # runs the hook path it is given; not a subcommand with help
    return sorted(names)


def _nested() -> list[str]:
    """`<cmd> <sub>` forms the usage text documents (okf gc, team push, ...)."""
    out = set()
    for cmd, sub in re.findall(r"^\s*llm-router ([a-z-]+) ([a-z][a-z-]*)\b", cli.__doc__ or "", re.M):
        if cmd in _dispatched():
            out.add(f"{cmd} {sub}")
    return sorted(out)


_CASES = _dispatched() + _nested()


def test_the_cases_cover_every_subcommand():
    """An empty or short list would pass everything below."""
    assert len(_dispatched()) >= 60, _dispatched()
    assert {"uninstall", "update", "onboard", "gateway", "broker"} <= set(_dispatched())
    assert {"okf gc", "team push", "budget set"} <= set(_nested())


def _seed(base: Path) -> tuple[Path, Path]:
    home, cwd = base / "home", base / "cwd"
    mcp = json.dumps({"servers": {"llm_router": {"command": "llm-router"}},
                      "mcpServers": {"llm_router": {"command": "llm-router"}}})
    for d in (home / ".claude", home / ".codex", cwd / ".vscode", cwd / ".windsurf", cwd / ".cursor"):
        d.mkdir(parents=True)
    for p in (cwd / ".vscode" / "mcp.json", cwd / ".windsurf" / "mcp.json", cwd / ".cursor" / "mcp.json"):
        p.write_text(mcp)
    (home / ".claude" / "settings.json").write_text('{"hooks": {}}')
    (home / ".codex" / "config.toml").write_text('[mcp_servers.llm_router]\ncommand = "llm-router"\n')
    (cwd / "CLAUDE.md").write_text("# project\n")
    return home, cwd


def _snapshot(base: Path) -> dict[str, str]:
    return {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(base.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("argv", _CASES)
def test_help_prints_usage_exits_0_and_changes_no_file(argv, tmp_path):
    home, cwd = _seed(tmp_path)
    before = _snapshot(tmp_path)
    r = subprocess.run(
        [sys.executable, "-m", "llm_router.cli", *argv.split(), "--help"],
        cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=25,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "NO_COLOR": "1", "TERM": "dumb",
             "PYTHONPATH": str(_REPO / "src"), "OLLAMA_HOST": "127.0.0.1:9",
             "OLLAMA_BASE_URL": "http://127.0.0.1:9"},
    )
    out = _ANSI.sub("", r.stdout + r.stderr)
    after = _snapshot(tmp_path)
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    assert changed == [], f"`llm-router {argv} --help` changed {changed}:\n{out[-800:]}"
    assert "Traceback" not in out, out[-800:]
    assert r.returncode == 0, f"`llm-router {argv} --help` exited {r.returncode}:\n{out[-800:]}"
    cmd = argv.split()[0]
    assert "usage" in out.lower() or re.search(rf"llm[-_]router {re.escape(cmd)}\b", out), out[-800:]


def test_unknown_subcommand_with_help_is_still_an_error():
    r = subprocess.run(
        [sys.executable, "-m", "llm_router.cli", "nosuchcmd", "--help"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=25,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "PYTHONPATH": str(_REPO / "src")},
    )
    assert r.returncode == 2 and "unknown command 'nosuchcmd'" in r.stderr
