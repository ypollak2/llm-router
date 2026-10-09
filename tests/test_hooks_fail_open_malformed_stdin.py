"""HOOKS-FAILOPEN-1: no hook may crash on malformed stdin (docs/bugs/HOOKS-FAILOPEN-1.md).

Every script in hooks/ is run as a subprocess with a scratch HOME / LLM_ROUTER_HOME and a
payload that is valid JSON but not an object (`[]`, `"x"`, `123`), an empty object, empty
stdin, or invalid JSON. The host must never see a traceback or a non-zero exit from a hook
that was handed junk: fail open.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1] / "hooks"
HOOKS = sorted(p for p in HOOKS_DIR.glob("*.py") if not p.name.startswith("llm_router_"))
PAYLOADS = ["[]", '"x"', "123", "{}", "", "not json"]

# Hooks that deliberately fail closed on unparseable input (test_a04_malformed_hook_stdin.py).
_DOCUMENTED_CODES: dict[str, set[int]] = {}


def test_hook_set_is_not_empty() -> None:
    # an empty parametrisation passes everything
    assert len(HOOKS) >= 12, HOOKS


@pytest.mark.parametrize("stdin_text", PAYLOADS, ids=lambda s: repr(s))
@pytest.mark.parametrize("hook", HOOKS, ids=lambda p: p.name)
def test_hook_fails_open_on_malformed_stdin(hook: Path, stdin_text: str, tmp_path: Path) -> None:
    home = tmp_path / "home"
    router_home = tmp_path / "router"
    home.mkdir()
    router_home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("LLM_ROUTER_")}
    env.update(
        HOME=str(home),
        LLM_ROUTER_HOME=str(router_home),
        LLM_ROUTER_DB_PATH=str(router_home / "usage.db"),
        CLAUDE_SESSION_ID="",
    )
    r = subprocess.run(
        [sys.executable, str(hook)],
        input=stdin_text.encode(),
        capture_output=True,
        env=env,
        cwd=str(tmp_path),
        timeout=60,
    )
    ok = {0} | _DOCUMENTED_CODES.get(hook.name, set())
    err = r.stderr.decode(errors="replace")
    assert r.returncode in ok, f"{hook.name} rc={r.returncode}\n{err[-800:]}"
    assert "Traceback" not in err, f"{hook.name} traceback:\n{err[-800:]}"
