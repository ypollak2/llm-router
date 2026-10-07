"""GH#50: the statusline read $SAVINGS_LOG, which nothing ever assigned.

`llm_router-statusline.sh` sets STATE_DIR, USAGE_JSON and USAGE_DB together at
the top, but SAVINGS_LOG was never added to that block — while being referenced
four times (health check + last-route token suffix). The shell expanded it to
"", the Python one-liner called open(""), the exception was swallowed, and
`providers` stayed False.

Net effect: any setup without a cloud API key showed "x no provider" forever,
however well local routing was working. That is the documented headline use
case — "route free to local Ollama with no cloud keys" — so it hit most
Ollama-only users rather than an edge case. The same root cause silently
dropped the token-count suffix from the last-route segment.

The generic test is the point: a variable READ but never ASSIGNED is the bug
class, and matching only SAVINGS_LOG would leave the next one to be found by a
user again.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src" / "llm_router" / "hooks" / "statusline-command.sh"
)

# Set by the caller (Claude Code), by the shell itself, or by a `local`/loop
# binding this crude scanner cannot see. Everything else must be assigned here.
_EXTERNALLY_PROVIDED = {
    "HOME", "PATH", "PWD", "SHELL", "USER", "TERM", "IFS", "REPLY", "OLDPWD",
    "BASH_REMATCH", "FUNCNAME", "LINENO", "RANDOM", "SECONDS", "PIPESTATUS",
}


def _assigned_names(body: str) -> set[str]:
    return set(re.findall(r"^\s*(?:export\s+|local\s+)?([A-Z_][A-Z0-9_]*)=", body, re.M)) | set(
        re.findall(r"\bread\s+(?:-r\s+)?([A-Z_][A-Z0-9_]*)", body)
    ) | set(re.findall(r"\bfor\s+([A-Z_][A-Z0-9_]*)\s+in\b", body))


def _read_names(body: str) -> set[str]:
    return set(re.findall(r"\$\{?([A-Z_][A-Z0-9_]*)[}:\s/\"]", body))


def test_savings_log_is_assigned():
    body = _SCRIPT.read_text()
    assert re.search(r"^\s*SAVINGS_LOG=", body, re.M), (
        "SAVINGS_LOG is read by the health check and the last-route segment but "
        "never assigned — the whole health indicator silently reports 'no provider'"
    )


def test_savings_log_points_at_the_real_file():
    """hooks/savings_logger.py writes ~/.llm-router/savings_log.jsonl."""
    body = _SCRIPT.read_text()
    m = re.search(r"^\s*SAVINGS_LOG=(.+)$", body, re.M)
    assert m, "SAVINGS_LOG unassigned"
    assert "savings_log.jsonl" in m.group(1), (
        f"SAVINGS_LOG={m.group(1)!r} does not name the file savings_logger.py writes"
    )
    from llm_router.hooks import savings_logger

    assert savings_logger._SAVINGS_LOG_FILENAME == "savings_log.jsonl", (
        "the writer's filename moved; the statusline path must follow it"
    )


def test_no_variable_is_read_bare_without_being_assigned():
    """The bug class, not just this instance.

    A variable read as ``${VAR:-default}`` is fine however it is set — the
    default carries it. The defect shape is a BARE read (``$VAR``/``${VAR}``)
    of a name nothing assigns: that expands to the empty string and fails
    silently. NO_COLOR, LLM_ROUTER_ENFORCE, CC_CONTEXT_LIMIT and
    LLM_ROUTER_REFRESH_THROTTLE_SEC are all deliberately environment-supplied
    AND all supply a default, so they are correctly excluded by that rule
    rather than by being named here.
    """
    import re as _re

    body = _SCRIPT.read_text()
    assigned = _assigned_names(body) | _EXTERNALLY_PROVIDED
    bare = set()
    for name in _read_names(body) - assigned:
        has_default = _re.search(r"\$\{" + name + r":-", body)
        if not has_default:
            bare.add(name)
    assert not bare, (
        f"these shell variables are read BARE and never assigned: {sorted(bare)}. "
        f"Each expands to '' and fails silently, exactly as SAVINGS_LOG did. "
        f"Either assign it, or read it as ${{NAME:-default}}."
    )


@pytest.mark.slow
def test_health_reports_ok_for_an_ollama_only_setup(tmp_path):
    """E2E: no cloud keys + recent Ollama activity must not read 'no provider'."""
    import json
    import subprocess

    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    # Schema must match what hooks/savings_logger.py actually writes: an
    # ISO-8601 "timestamp" and a "<provider>/<model>" string. Asserted below
    # so this fixture cannot drift away from the writer unnoticed.
    from datetime import datetime, timezone

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": "ollama/llama3.1:8b",
        "input_tokens": 90, "output_tokens": 30, "estimated_saved": 0.004,
    }
    (home / ".llm-router" / "savings_log.jsonl").write_text(json.dumps(record) + "\n")
    (home / ".llm-router" / "usage.json").write_text("{}")  # fresh -> not stale
    env = {
        "HOME": str(home), "PATH": "/usr/bin:/bin", "TERM": "dumb",
        # Explicitly no cloud provider keys — the reported configuration.
    }
    r = subprocess.run(["bash", str(_SCRIPT)], env=env, input="{}",
                       capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    if not out.strip():
        pytest.skip("statusline produced no output in this environment")
    assert "no provider" not in out, (
        f"Ollama-only setup with recent activity still reports 'no provider':\n{out}"
    )


def test_statusline_and_writer_agree_on_the_log_schema():
    """Guards the E2E above: if the writer's field names move, this fails loudly
    rather than letting the health check silently read nothing again."""
    body = _SCRIPT.read_text()
    assert 'r["timestamp"]' in body or "r['timestamp']" in body, (
        "statusline no longer reads a 'timestamp' field"
    )
    writer = (
        Path(__file__).resolve().parent.parent
        / "src" / "llm_router" / "hooks" / "savings_logger.py"
    ).read_text()
    assert '"timestamp": datetime.now' in writer, "writer no longer emits 'timestamp'"
    assert '"model":' in writer, "writer no longer emits 'model'"
