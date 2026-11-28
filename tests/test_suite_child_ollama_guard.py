"""Regression tests for the child-process Ollama guard (SUITE-GUARD-1).

The guard writes a sitecustomize.py into a temp dir, prepends it to PYTHONPATH,
and sets SUITE_BLOCK_LIVE_OLLAMA=1. Every child Python then refuses connections
to ports 11434 and 11435 and logs the refusal.
"""

import os
import subprocess
import sys

import pytest

# The guard's env var that controls the child-side block.
_GUARD_FLAG = "SUITE_BLOCK_LIVE_OLLAMA"


def _guard_active() -> bool:
    """Return True if the guard is currently installed in this process."""
    return os.environ.get(_GUARD_FLAG) == "1"


# ---------------------------------------------------------------------------
# (a) Child sys.executable -c connecting to 127.0.0.1:11434 gets the guard's
#     specific error message, not ECONNREFUSED.
# ---------------------------------------------------------------------------
def test_child_guard_refuses_11434():
    """A child Python that connects to :11434 gets the guard's OSError text."""
    assert _guard_active(), "guard must be active (conftest sets it)"
    code = (
        "import socket; "
        "s = socket.socket(); "
        "s.connect(('127.0.0.1', 11434))"
    )
    env = os.environ.copy()
    # Keep PYTHONPATH (the guard dir) so sitecustomize fires.
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert result.returncode != 0, "child should not succeed connecting to :11434"
    # The guard's message is on stderr (the OSError has no traceback because
    # the child exits with rc != 0 and the exception is unhandled).
    stderr = result.stderr or result.stdout or ""
    assert "live Ollama connection blocked in tests" in stderr, (
        f"Expected guard message in output, got: {stderr!r}"
    )


# ---------------------------------------------------------------------------
# (b) Same for 11435.
# ---------------------------------------------------------------------------
def test_child_guard_refuses_11435():
    """A child Python that connects to :11435 gets the guard's OSError text."""
    assert _guard_active(), "guard must be active (conftest sets it)"
    code = (
        "import socket; "
        "s = socket.socket(); "
        "s.connect(('127.0.0.1', 11435))"
    )
    env = os.environ.copy()
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert result.returncode != 0
    stderr = result.stderr or result.stdout or ""
    assert "live Ollama connection blocked in tests" in stderr, (
        f"Expected guard message in output, got: {stderr!r}"
    )


# ---------------------------------------------------------------------------
# (c) The in-process guard refuses 11435 (and still 11434), with its own text.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("port", [11434, 11435])
def test_in_process_guard_refuses_ollama_ports(port):
    import socket

    with pytest.raises(OSError, match=r"live Ollama connection blocked in tests \(localhost:%d\)" % port):
        socket.socket().connect(("127.0.0.1", port))


# ---------------------------------------------------------------------------
# (d) Red-check: remove the PYTHONPATH injection and see (a) fail.
# ---------------------------------------------------------------------------
def test_red_check_no_pythonpath_fails():
    """The child guard depends on the PYTHONPATH injection: without it the child's
    socket.connect is the stdlib one; with it, the guard's wrapper. Checked by name,
    so this test never opens a connection to a live port."""
    probe = "import socket; print(socket.socket.connect.__name__)"
    bare = os.environ.copy()
    bare.pop("PYTHONPATH", None)
    bare.pop(_GUARD_FLAG, None)
    without = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                             text=True, env=bare, timeout=10).stdout.strip()
    with_guard = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                                text=True, env=os.environ.copy(), timeout=10).stdout.strip()
    assert without == "connect", without
    assert with_guard == "_connect", with_guard


# ---------------------------------------------------------------------------
# (e) A refused child connection leaves one evidence line in the refusal log.
# ---------------------------------------------------------------------------
def test_child_refusal_is_logged(tmp_path):
    log = tmp_path / "refusals.log"
    env = os.environ.copy()
    env["SUITE_OLLAMA_REFUSAL_LOG"] = str(log)
    env["PYTEST_CURRENT_TEST"] = "tests/x.py::marker_test (call)"
    code = (
        "import socket\n"
        "try:\n"
        "    socket.socket().connect(('127.0.0.1', 11435))\n"
        "except OSError:\n"
        "    pass  # swallowed, like a real probe\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=10)
    assert r.returncode == 0
    lines = log.read_text().splitlines()
    assert len(lines) == 1, lines
    port, test_id, argv = lines[0].split("\t")
    assert port == "11435"
    assert test_id == "tests/x.py::marker_test (call)"
    assert argv.startswith("['-c'")
