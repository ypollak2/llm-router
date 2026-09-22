"""K1 — the frozen state is generated, and it never names the operator.

Phase 0 of the 2026-09-22 audit was assembled by hand. It is mechanical, and a
hand-assembled record drifts from what was actually examined.

The assertion that matters most here is the privacy one. `FROZEN_STATE.md` is
committed to a PUBLIC repository, and the first draft of the generator printed
`sys.executable` — which on a developer machine is `/Users/<name>/...`. An
audit artifact should record the SHAPE of a machine, never whose machine it
was, and that is a standing constraint in this repo rather than a nicety.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "audit" / "freeze_state.py"


def _run(env_extra: dict | None = None) -> str:
    env = {**os.environ, **(env_extra or {})}
    r = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=REPO, capture_output=True,
        text=True, timeout=120, env=env,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_it_never_prints_a_path_under_a_home_directory():
    """The standing privacy constraint, enforced rather than remembered."""
    out = _run()
    home = str(Path.home())
    assert home not in out, (
        f"the frozen state names the operator's home directory ({home}). This "
        "file is committed to a public repo."
    )
    # And no other user's home either.
    leaks = re.findall(r"/(?:Users|home)/[A-Za-z0-9._-]+", out)
    leaks = [x for x in leaks if not x.endswith(("/Users", "/home"))]
    assert not leaks, f"absolute home paths in the frozen state: {set(leaks)}"


def test_a_home_path_in_an_env_var_is_redacted():
    """LLM_ROUTER_HOME is a path under $HOME and is printed by name."""
    out = _run({"LLM_ROUTER_HOME": str(Path.home() / ".llm-router-probe")})
    assert "LLM_ROUTER_HOME" in out
    assert str(Path.home()) not in out
    assert "~/.llm-router-probe" in out, (
        "the home prefix was not replaced with ~, so redaction is not "
        "reaching env var values"
    )


def test_secrets_are_reported_as_presence_only():
    out = _run({"LLM_ROUTER_GATEWAY_TOKEN": "s3cr3t-do-not-print",
                "OPENAI_API_KEY": "sk-do-not-print"})
    assert "s3cr3t-do-not-print" not in out
    assert "sk-do-not-print" not in out
    assert "(set)" in out, "a set token is not reported at all"
    assert "| `OPENAI_API_KEY` | yes |" in out


def test_absent_credentials_are_stated_as_absent():
    """'Never exercised' and 'works' are different claims."""
    env = {k: "" for k in ("OPENAI_API_KEY", "GEMINI_API_KEY")}
    out = _run(env)
    assert "| `OPENAI_API_KEY` | NO |" in out
    assert "NEVER" in out and "EXERCISED" in out, (
        "the report lists an absent key without saying what absence means"
    )


def test_it_records_the_ambient_environment_as_not_default():
    """The audit ran with non-default flags set in the operator's shell.

    Behaviour observed under those is not what a fresh install does, and an
    audit that omits this reports the operator's configuration as the product.
    """
    out = _run({"LLM_ROUTER_CLAUDE_SUBSCRIPTION": "true"})
    assert "NOT the product default" in out
    assert "LLM_ROUTER_CLAUDE_SUBSCRIPTION" in out


def test_it_says_when_the_tree_is_dirty():
    """An audit of an uncommitted tree cannot be reproduced from its HEAD."""
    out = _run()
    assert ("Working tree: clean" in out
            or "DIRTY — the audit did not examine a committed state" in out)


def test_it_declares_the_conflict_of_interest():
    out = _run()
    assert "Conflict of interest" in out
    assert "Correlated failure" in out or "correlated failure" in out.lower(), (
        "the declaration does not name the actual risk"
    )


def test_the_counts_are_not_zero():
    """Anti-vacuity: a generator reporting an empty repo passes everything."""
    out = _run()
    m = re.search(r"source files: (\d+)", out)
    assert m and int(m.group(1)) > 100, f"suspicious source count: {out[:400]}"
    m = re.search(r"test files: (\d+)", out)
    assert m and int(m.group(1)) > 100, "suspicious test count"
