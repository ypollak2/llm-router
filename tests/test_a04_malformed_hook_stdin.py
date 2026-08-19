"""Regression: CHZ-AUD-A-04 — malformed hook stdin must not silently bypass routing.
In zero-Claude it must fail CLOSED (block); otherwise pass through but log visibly."""
import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


def _run(stdin_bytes: bytes, env_extra: dict) -> subprocess.CompletedProcess:
    import os
    env = {**os.environ, "LLM_ROUTER_DB_PATH": "/tmp/chz_a04_test.db", **env_extra}
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_bytes,
                          capture_output=True, env=env, timeout=30)


def test_malformed_stdin_zero_claude_blocks():
    r = _run(b"{not valid json", {"LLM_ROUTER_ZERO_CLAUDE": "1"})
    # Must emit a block decision on stdout (fail-closed), not a silent exit.
    out = r.stdout.decode().strip()
    assert out, "zero-Claude produced NO output on malformed stdin (silent bypass)"
    dec = json.loads(out)
    assert dec.get("decision") == "block", f"expected block, got {dec}"
    assert "could not be parsed" in dec.get("reason", "").lower()


def test_malformed_stdin_normal_mode_logs_visibly():
    r = _run(b"{not valid json", {"LLM_ROUTER_ZERO_CLAUDE": "0"})
    # Non-enforcing: pass through (exit 0, no block) BUT a visible stderr warning.
    assert r.returncode == 0
    assert b"could not parse hook stdin" in r.stderr, "malformed stdin bypass was SILENT (no stderr)"
    # stdout must not carry a block decision in normal mode
    assert b'"decision": "block"' not in r.stdout
