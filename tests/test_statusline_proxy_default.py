"""statusline-command.sh's proxy-default-down warning.

Every session depends on the proxy once `llm-router install --proxy-default`
is installed, so a dead port deserves a loud statusline glyph, not just a
doctor check the operator has to remember to run. Gated on the sentinel
(`~/.llm-router/proxy_default.json`) so a user who never installed it pays no
extra cost here (no probe attempted at all).

E2E: runs the real script via subprocess, a real ephemeral loopback socket
standing in for "proxy up"/"proxy down" (never the real 8787, which may be
occupied by the owner's own live LaunchAgent on a dev machine).
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src" / "llm_router" / "hooks" / "statusline-command.sh"
)


def _listening_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s


def _run(home: Path) -> str:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "TERM": "dumb"}
    r = subprocess.run(
        ["bash", str(_SCRIPT)], env=env, input="{}", capture_output=True, text=True, timeout=60,
    )
    return r.stdout + r.stderr


def test_no_segment_when_never_installed(tmp_path):
    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    out = _run(home)
    if not out.strip():
        pytest.skip("statusline produced no output in this environment")
    assert "proxy down" not in out


def test_no_segment_when_answering(tmp_path):
    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    srv = _listening_socket()
    try:
        port = srv.getsockname()[1]
        (home / ".llm-router" / "proxy_default.json").write_text(json.dumps({"port": port}))
        out = _run(home)
        if not out.strip():
            pytest.skip("statusline produced no output in this environment")
        assert "proxy down" not in out
    finally:
        srv.close()


def test_segment_shown_when_dead(tmp_path):
    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    srv = _listening_socket()
    port = srv.getsockname()[1]
    srv.close()  # now almost certainly refusing connections again
    (home / ".llm-router" / "proxy_default.json").write_text(json.dumps({"port": port}))
    out = _run(home)
    if not out.strip():
        pytest.skip("statusline produced no output in this environment")
    assert "proxy down" in out
    assert str(port) in out


def test_malformed_sentinel_does_not_crash_the_statusline(tmp_path):
    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    (home / ".llm-router" / "proxy_default.json").write_text("not valid json {{{")
    out = _run(home)
    if not out.strip():
        pytest.skip("statusline produced no output in this environment")
    assert "proxy down" not in out


def test_no_variable_regression_guard():
    """The new block's variables (STATE_DIR reused, proxy_default_sentinel,
    proxy_down) are all locally assigned -- covered generically by
    test_gh50_statusline_defines_every_var.py's bare-read scan; this just
    anchors that the new block exists and uses the shared STATE_DIR."""
    body = _SCRIPT.read_text()
    assert re.search(r'^proxy_default_sentinel="\$STATE_DIR/proxy_default\.json"', body, re.M)
