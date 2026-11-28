"""Keep the test suite, and every Python child it starts, away from a live Ollama.

2026-11-28: the full suite loaded real models (devstral-small-2:24b, 15 GB) on the
operator's Ollama at :11434 while qwen3.6 sat pinned on :11435, and the :11435 runner hit
Metal OutOfMemory. The in-process guard in conftest (`_hermetic_reachability`) only covers
the pytest process; hook subprocesses inherit the environment, not the monkeypatch.

Mechanism (test-only, no product code): ``install()`` writes a ``sitecustomize.py`` into a
temp dir, prepends that dir to PYTHONPATH and sets ``SUITE_BLOCK_LIVE_OLLAMA=1``. Every child
Python then patches ``socket.connect``/``connect_ex`` to refuse ports 11434 and 11435 and
appends one line per refusal (port, PYTEST_CURRENT_TEST, argv[:3]) to
``SUITE_OLLAMA_REFUSAL_LOG``, because many probes swallow the exception.
A test that really needs the daemon is marked ``requires_ollama``; its fixture clears the flag.
Children started with a hand-built env that drops PYTHONPATH (or run with ``-I``/``-S``)
are not covered.
"""

from __future__ import annotations

import os
import tempfile

FLAG = "SUITE_BLOCK_LIVE_OLLAMA"
LOG_ENV = "SUITE_OLLAMA_REFUSAL_LOG"
PORTS = (11434, 11435)
MESSAGE = "live Ollama connection blocked in tests"

SITECUSTOMIZE_SOURCE = '''\
import os as _os
import socket as _socket
import sys as _sys

if _os.environ.get("SUITE_BLOCK_LIVE_OLLAMA") == "1":
    _PORTS = (11434, 11435)

    def _refuse_if_ollama(address):
        try:
            port = int(address[1]) if isinstance(address, tuple) and len(address) >= 2 else None
        except (TypeError, ValueError):
            port = None
        if port not in _PORTS:
            return
        try:
            log = _os.environ.get("SUITE_OLLAMA_REFUSAL_LOG")
            if log:
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write("%s\\t%s\\t%r\\n" % (port, _os.environ.get("PYTEST_CURRENT_TEST", "-"), _sys.argv[:3]))
        except Exception:
            pass
        raise OSError(
            "live Ollama connection blocked in tests (localhost:%d) [child guard]. "
            "Mark the test @pytest.mark.requires_ollama if it genuinely needs the daemon; "
            "otherwise stub the call. See tests/_child_ollama_guard.py." % port
        )

    _real_connect = _socket.socket.connect
    _real_connect_ex = _socket.socket.connect_ex

    def _connect(self, address):
        _refuse_if_ollama(address)
        return _real_connect(self, address)

    def _connect_ex(self, address):
        _refuse_if_ollama(address)
        return _real_connect_ex(self, address)

    _socket.socket.connect = _connect
    _socket.socket.connect_ex = _connect_ex
'''


def install() -> str:
    """Create the guard dir, export the env, return the dir. Call once, at conftest import."""
    guard_dir = tempfile.mkdtemp(prefix="llm_router-suite-childguard-")
    with open(os.path.join(guard_dir, "sitecustomize.py"), "w", encoding="utf-8") as fh:
        fh.write(SITECUSTOMIZE_SOURCE)
    log = os.path.join(guard_dir, "refusals.log")
    open(log, "a", encoding="utf-8").close()
    prior = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = guard_dir + (os.pathsep + prior if prior else "")
    os.environ[FLAG] = "1"
    os.environ[LOG_ENV] = log
    return guard_dir


def record_refusal(port: int) -> None:
    """In-process twin of the child guard's log line."""
    import sys

    log = os.environ.get(LOG_ENV)
    if not log:
        return
    try:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("%s\t%s\t%r\n" % (port, os.environ.get("PYTEST_CURRENT_TEST", "-"), sys.argv[:3]))
    except Exception:
        pass
