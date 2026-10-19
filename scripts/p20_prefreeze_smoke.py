#!/usr/bin/env python3
"""F113 pre-freeze smoke checks for the benchmark agent environment. Prints PASS/FAIL only.

  python scripts/p20_prefreeze_smoke.py [--claude-call] [--scratch DIR]

Checks: import guard and grader python (pytest) on the agent PATH, node resolves on the agent PATH, the Claude token file
is usable (mode and non-empty; the value is never shown). `--claude-call` adds one real
`claude -p` call under the scratch HOME and is opt-in: it spends a model call, so only the
owner runs it. Exit 0 when every check passes.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "p20_smoke_requal", Path(__file__).with_name("p20_requal.py"),
)
h = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = h
SPEC.loader.exec_module(h)


def check_import_guard(env: dict) -> bool:
    try:
        h.assert_no_llm_router_import(env)
    except h.Refuse:
        return False
    return bool(h.agent_pythons(env["PATH"]))  # an empty set passes everything


def check_grader_python(env: dict) -> bool:
    """A python on the agent PATH that has pytest and cannot import llm_router (F115)."""
    try:
        h.grader_python(env)
    except h.Refuse:
        return False
    return True


def check_node(env: dict) -> bool:
    return shutil.which("node", path=env["PATH"]) is not None


def check_token_file() -> bool:
    source = os.environ.get(h.CLAUDE_CREDENTIALS_ENV) or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not os.environ.get(h.CLAUDE_CREDENTIALS_ENV):
        return bool(source)
    try:
        h.read_oauth_token(os.environ[h.CLAUDE_CREDENTIALS_ENV])
    except h.Refuse:
        return False
    return True


def check_claude_call(scratch: Path) -> bool:
    try:
        home = h.scratch_home(scratch / "claude_home")
        env = h.claude_env(home, scratch)
        cwd = scratch / "cwd"
        h.private_dir(cwd)
        result = subprocess.run(
            ["claude", "-p", "--safe-mode", "--no-session-persistence", "--output-format",
             "json", "--tools", "", "--model", "haiku"],
            input="Reply with the single word ok.", text=True, capture_output=True,
            timeout=120, cwd=cwd, env=env, check=False,
        )
        value = json.loads(result.stdout)
        # F115: does Claude Code persist the auth value under the scratch HOME? That would make
        # every Claude-arm call refuse in the harness scrub; a hit here is a FAIL.
        token = env.get(h.CLAUDE_AUTH_ENV[0])
        if not token:
            return False
        h.scan_for_token(token, "smoke output", data=(result.stdout or "") + (result.stderr or ""))
        h.scan_for_token(token, "smoke scratch home", root=home)
    except (h.Refuse, OSError, subprocess.SubprocessError, ValueError):
        return False
    return result.returncode == 0 and not value.get("is_error") and bool(value.get("result"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--claude-call", action="store_true")
    ap.add_argument("--scratch", type=Path)
    args = ap.parse_args(argv)
    own_scratch = args.scratch is None
    scratch = args.scratch or Path(tempfile.mkdtemp(prefix="p20-smoke-"))
    try:
        return run_checks(args, scratch)
    finally:
        if own_scratch:
            shutil.rmtree(scratch, ignore_errors=True)  # F115: do not leave the mkdtemp dir


def run_checks(args, scratch: Path) -> int:
    env = {"PATH": h.agent_path(os.environ.get("PATH", "")), **h.AGENT_FIXED_ENV}
    results = {
        "import_guard": check_import_guard(env),
        "grader_python": check_grader_python(env),
        "node_on_agent_path": check_node(env),
        "claude_token": check_token_file(),
    }
    if args.claude_call:
        results["claude_call"] = check_claude_call(scratch)
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
