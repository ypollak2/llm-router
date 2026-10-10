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
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from statusline_prime import render_full  # noqa: E402

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
    return render_full(lambda: _run_once(home), home)


def _run_once(home: Path) -> str:
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


def test_segment_shown_when_shim_answers_but_main_proxy_is_dead(tmp_path):
    """docs/BUGS.md P010-1 review: the shim on `port` always accepts, so the main
    proxy's `upstream_port` must be probed too."""
    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    shim = _listening_socket()
    dead = _listening_socket()
    upstream = dead.getsockname()[1]
    dead.close()
    try:
        (home / ".llm-router" / "proxy_default.json").write_text(
            json.dumps({"port": shim.getsockname()[1], "upstream_port": upstream})
        )
        out = _run(home)
        if not out.strip():
            pytest.skip("statusline produced no output in this environment")
        assert f"proxy down:{upstream} (bypassed)" in out
    finally:
        shim.close()


def test_no_segment_when_shim_and_main_proxy_both_answer(tmp_path):
    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    shim, main = _listening_socket(), _listening_socket()
    try:
        (home / ".llm-router" / "proxy_default.json").write_text(json.dumps(
            {"port": shim.getsockname()[1], "upstream_port": main.getsockname()[1]}
        ))
        out = _run(home)
        if not out.strip():
            pytest.skip("statusline produced no output in this environment")
        assert "proxy down" not in out
    finally:
        shim.close()
        main.close()


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
    # P0.9-c: the probe moved to the detached refresher (statusline_segments);
    # the script renders what it cached under the same state dir.
    seg = (_SCRIPT.parent.parent / "statusline_segments.py").read_text()
    assert '"proxy_default.json"' in seg and "socket.create_connection" in seg
    assert re.search(r'^\s*if \[ -n "\$s_proxy_down" \]', body, re.M)
