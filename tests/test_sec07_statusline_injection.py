"""Regression: CHZ-SEC-07 — command injection in statusline-command.sh.

The status line extracted `transcript_path` (and other fields) from the Claude
Code session JSON and interpolated them *unescaped into `python3 -c` source*:

    python3 -c "... with open('$transcript_path') as f: ..."

A crafted filename could close the string literal and inject arbitrary Python
(hence shell) that executed on every status-line render. The fix passes every
dynamic value through the environment into single-quoted Python source, so bash
never interpolates attacker data into code.

This test drives the *real* hook script with a malicious `transcript_path` that
is a genuine regular file (so the `[ -f ]` guard passes) whose name carries a
newline+indent Python injection, and asserts the injected command never runs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STATUSLINE = REPO_ROOT / "src" / "llm_router" / "hooks" / "statusline-command.sh"


@pytest.mark.skipif(not STATUSLINE.exists(), reason="statusline hook not present")
def test_transcript_path_cannot_inject_commands(tmp_path: Path) -> None:
    # Marker + anchor are RELATIVE (no slashes) so the whole payload can live in
    # a single filename component; the hook runs with cwd=tmp_path.
    marker_name = "CHZ_SEC07_PWNED"
    (tmp_path / "anchor").write_text("")

    # Filename == the exact injection that fires against the vulnerable
    # `with open('$transcript_path') as f:` construct: close the literal, run a
    # command at matching indentation, then reopen a real file to stay valid.
    payload_name = (
        "anchor') as f: pass\n"
        f"    import os; os.system('touch {marker_name}')\n"
        "    with open('anchor"
    )
    malicious = tmp_path / payload_name
    malicious.write_text("")  # real regular file so `[ -f ]` passes

    session_json = json.dumps(
        {"transcript_path": str(malicious), "cwd": str(tmp_path), "model": "claude-x"}
    )

    env = dict(os.environ)
    env["HOME"] = str(tmp_path)  # isolate ~/.llm-router
    (tmp_path / ".llm-router").mkdir(exist_ok=True)

    subprocess.run(
        ["bash", str(STATUSLINE)],
        input=session_json,
        text=True,
        capture_output=True,
        cwd=str(tmp_path),
        env=env,
        timeout=30,
    )

    assert not (tmp_path / marker_name).exists(), (
        "CHZ-SEC-07 regression: crafted transcript_path executed an injected "
        "command — the status line is interpolating hook data into python3 -c source"
    )


@pytest.mark.skipif(not STATUSLINE.exists(), reason="statusline hook not present")
def test_no_shell_var_interpolated_into_python_source() -> None:
    """Structural guard: no `python3 -c "..."` block may contain a shell var.

    A double-quoted `python3 -c "..."` heredoc lets bash interpolate `$VAR` into
    the Python source (the vulnerable shape). Blocks that read only from
    ``sys.stdin`` are safe; the tell is a ``$`` inside the double-quoted body.
    Safe blocks use single quotes: `python3 -c '...'` reading ``os.environ``.
    """
    text = STATUSLINE.read_text()
    # Extract each double-quoted `python3 -c "..."` body (non-greedy to next ").
    bodies = re.findall(r'python3 -c "(.*?)"', text, flags=re.DOTALL)
    offenders = [b for b in bodies if "$" in b]
    assert not offenders, (
        "CHZ-SEC-07 regression: a double-quoted `python3 -c \"...\"` block "
        "interpolates a shell variable ($) into Python source. Use single-quoted "
        f"source reading os.environ instead. Offending block(s): {offenders!r}"
    )
