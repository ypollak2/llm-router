"""GH#51/#52: three console scripts ignored --help; two crashed on it.

From `pipx list`, only `llm-router` and `llm-router-install-hooks` handled the
flag. The rest ran their default behaviour instead:

  * llm-router-onboard --help   -> launched the interactive API-key wizard and
                                   died on EOFError the moment stdin was not a
                                   TTY (onboard.py's input() call)
  * llm-router-quickstart --help-> same shape
  * llm-router-isolation-test   -> ignored the flag and ran the full health check

`--help` is the one flag a user tries on an unfamiliar command, and on a
non-interactive stdin (CI, scripted onboarding, a piped shell) two of them
produced an unhandled traceback.

The strongest requirement here is not "print something" — it is that --help
must be INERT. GH#52 found `llm-router-sse --help` reaching a live model call
before crashing, which on a paid provider key would have been a billed request
from the safest flag on the CLI. So the E2E asserts no network egress, not
just a clean exit.
"""
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent


def _console_scripts() -> dict[str, str]:
    with (_REPO / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"].get("scripts", {})


def _module_and_attr(target: str) -> tuple[str, str]:
    mod, _, attr = target.partition(":")
    return mod, attr


@pytest.mark.parametrize("name,target", sorted(_console_scripts().items()))
def test_help_exits_cleanly_and_non_interactively(name, target):
    """Runs the real entry point with --help, stdin closed, in a subprocess."""
    mod, attr = _module_and_attr(target)
    code = (
        f"import sys; sys.argv = ['{name}', '--help']\n"
        f"from {mod} import {attr} as _f\n"
        "try:\n"
        "    _f()\n"
        "except SystemExit as e:\n"
        "    raise SystemExit(e.code if isinstance(e.code, int) else 0)\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "NO_COLOR": "1",
             "PYTHONPATH": str(_REPO / "src")},
    )
    combined = r.stdout + r.stderr
    assert "EOFError" not in combined, (
        f"{name} --help crashed on non-interactive stdin:\n{combined[-800:]}"
    )
    assert "Traceback" not in combined, (
        f"{name} --help raised an unhandled exception:\n{combined[-800:]}"
    )
    assert r.returncode == 0, f"{name} --help exited {r.returncode}:\n{combined[-800:]}"


@pytest.mark.parametrize("name,target", sorted(_console_scripts().items()))
def test_help_prints_usage(name, target):
    mod, attr = _module_and_attr(target)
    code = (
        f"import sys; sys.argv = ['{name}', '--help']\n"
        f"from {mod} import {attr} as _f\n"
        "try:\n    _f()\nexcept SystemExit:\n    pass\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], cwd=_REPO, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "NO_COLOR": "1",
             "PYTHONPATH": str(_REPO / "src")},
    )
    out = (r.stdout + r.stderr).lower()
    assert any(w in out for w in ("usage", "options", "commands")), (
        f"{name} --help printed no usage text:\n{(r.stdout + r.stderr)[:600]}"
    )


@pytest.mark.slow
@pytest.mark.parametrize("name,target", sorted(_console_scripts().items()))
def test_help_makes_no_network_call(name, target):
    """GH#52's real finding: --help must not be able to reach a provider.

    socket.socket is replaced before the entry point is imported, so any
    outbound connection — a model call, a probe, a health check — fails the
    test loudly instead of silently costing money.
    """
    mod, attr = _module_and_attr(target)
    code = (
        "import socket\n"
        "class _Blocked(Exception): pass\n"
        "def _no_net(*a, **k):\n"
        "    raise AssertionError('NETWORK_CALL_DURING_HELP')\n"
        "socket.socket = _no_net\n"
        "socket.create_connection = _no_net\n"
        f"import sys; sys.argv = ['{name}', '--help']\n"
        f"from {mod} import {attr} as _f\n"
        "try:\n    _f()\nexcept SystemExit:\n    pass\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], cwd=_REPO, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "NO_COLOR": "1",
             "PYTHONPATH": str(_REPO / "src")},
    )
    assert "NETWORK_CALL_DURING_HELP" not in (r.stdout + r.stderr), (
        f"{name} --help opened a network connection — on a paid provider key "
        f"that is a billed API call from --help"
    )
