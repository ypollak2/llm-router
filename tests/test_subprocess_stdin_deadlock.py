"""Regression tests for the llm_router → CLI agent stdin pipe deadlock.

When llm_router spawns a CLI agent (codex / gemini) via
``asyncio.create_subprocess_exec`` without ``stdin=``, the child inherits
the parent's stdin file descriptor. Under the MCP runtime, that FD is a
long-lived pipe from Claude Code that never closes for the session
lifetime. ``codex exec`` and ``gemini`` both read stdin until EOF before
issuing API calls — so they block forever waiting for an EOF that never
arrives. ``proc.communicate()`` then waits on the child, producing a hang
where the agent process and the spawning llm_router session both sit idle
with near-zero CPU.

Observed in the wild: 50+ minute hangs with ``0:00.0X`` CPU on two stuck
codex children, both sharing the exact same ``unix`` socket on FD 0 — the
classic fingerprint of inherited-stdin-pipe deadlock.

The fix is to pass ``stdin=asyncio.subprocess.DEVNULL`` on every
``create_subprocess_exec`` call site.  This file pins that fix in place.

Two complementary tests:

1. ``test_safe_subprocess_exec_does_not_inherit_open_stdin`` —
   behavioural. Reproduces the exact FD layout (parent's stdin is a
   held-open pipe) and asserts the spawn returns within a tight
   wall-clock bound. Exercises the shared wrapper used by every consumer.

2. ``test_other_spawn_sites_pass_stdin_devnull`` — source-level guard
   for the three call sites that don't go through the shared wrapper
   (``codex_agent``, ``gemini_cli_agent``, ``gemini_cli_quota``). Asserts
   each ``create_subprocess_exec`` call in those files passes
   ``stdin=asyncio.subprocess.DEVNULL``. Cheap, deterministic, and
   immune to environment-specific issues like missing CLI binaries.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"

# 50+ minute hangs were the observed bug. 6 seconds is generous CI headroom
# while still catching any regression where stdin is left inherited.
_DEADLOCK_GUARD_SEC = 6.0


@pytest.fixture
def fake_cli_binary(tmp_path: Path) -> Path:
    """A minimal stand-in for ``codex exec`` / ``gemini``: reads stdin
    to EOF, then writes a fixed token and exits 0.

    The read-to-EOF behaviour is the exact pattern that triggers the
    deadlock when the parent leaves stdin inherited and open.
    """
    script = tmp_path / "fake_cli.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            sys.stdin.read()  # the trap: blocks until parent closes stdin
            sys.stdout.write("pong")
            sys.stdout.flush()
            """
        )
    )
    return script


def _run_with_held_open_stdin(
    runner_code: str, timeout: float
) -> tuple[int, bytes, bytes]:
    """Spawn a Python subprocess whose stdin is a pipe we keep held open
    for the lifetime of the call — mirroring the MCP-runtime FD layout
    where Claude Code holds the pipe to llm_router open for the entire
    session.

    Unlike ``Popen.communicate()`` (which closes ``stdin`` immediately
    when ``input`` is ``None``), this drains ``stdout``/``stderr`` on
    background threads while ``stdin``'s write-end stays open in the
    parent. That preserves the exact "never-closing pipe" condition the
    real bug needs.
    """
    import threading

    proc = subprocess.Popen(
        [sys.executable, "-c", runner_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    def _drain(stream, sink: list) -> None:
        try:
            for chunk in iter(lambda: stream.read(4096), b""):
                sink.append(chunk)
        except Exception:
            pass

    t_out = threading.Thread(
        target=_drain, args=(proc.stdout, stdout_chunks), daemon=True
    )
    t_err = threading.Thread(
        target=_drain, args=(proc.stderr, stderr_chunks), daemon=True
    )
    t_out.start()
    t_err.start()

    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
        raise
    finally:
        # Close *our* end of the stdin pipe only after the child has
        # already exited, so the runner — and the child it spawned — saw
        # a held-open stdin for the entire run.
        try:
            proc.stdin.close()
        except Exception:
            pass

    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)
    return rc, b"".join(stdout_chunks), b"".join(stderr_chunks)


def test_safe_subprocess_exec_does_not_inherit_open_stdin(
    fake_cli_binary: Path,
) -> None:
    """Behavioural reproduction of the MCP-runtime FD layout.

    A parent Python process whose stdin is a pipe held open by the test
    invokes ``safe_subprocess_exec`` against a fake CLI that reads stdin
    to EOF. Without ``stdin=DEVNULL`` on the child spawn, the fake CLI
    inherits the parent's stdin and blocks indefinitely; with the fix,
    it sees EOF immediately and exits cleanly.

    A ``subprocess.TimeoutExpired`` here is the regression signal.
    Critical: we use ``_run_with_held_open_stdin`` rather than the
    convenience ``Popen.communicate()``, because the latter closes stdin
    when input is ``None`` and would mask the bug.
    """
    runner_code = textwrap.dedent(
        f"""
        import asyncio, sys
        sys.path.insert(0, {str(_SRC)!r})
        from llm_router.safe_subprocess import safe_subprocess_exec

        async def main():
            stdout, stderr, code = await safe_subprocess_exec(
                {sys.executable!r}, {str(fake_cli_binary)!r},
                timeout=3,
            )
            sys.stdout.buffer.write(stdout)
            sys.stdout.flush()
            sys.exit(code)

        asyncio.run(main())
        """
    )

    try:
        rc, stdout, stderr = _run_with_held_open_stdin(
            runner_code, _DEADLOCK_GUARD_SEC
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "stdin pipe deadlock reproduced: safe_subprocess_exec must "
            "spawn children with stdin=asyncio.subprocess.DEVNULL so they "
            "see immediate EOF instead of inheriting the parent's "
            "long-lived stdin pipe."
        )

    assert rc == 0, f"runner exited {rc!r}, stderr={stderr.decode()!r}"
    assert stdout == b"pong", (
        f"unexpected output: {stdout!r}  stderr={stderr.decode()!r}"
    )


@pytest.mark.parametrize(
    "source_path",
    [
        _SRC / "llm_router" / "codex_agent.py",
        _SRC / "llm_router" / "gemini_cli_agent.py",
        _SRC / "llm_router" / "gemini_cli_quota.py",
    ],
)
def test_other_spawn_sites_pass_stdin_devnull(source_path: Path) -> None:
    """Source-level guard: every ``create_subprocess_exec`` call in these
    modules must pass ``stdin=asyncio.subprocess.DEVNULL``.

    These three sites don't go through ``safe_subprocess_exec``, so the
    behavioural test above doesn't cover them. A source-level assertion
    is cheap, deterministic, and catches the regression even on
    environments where the real CLI binaries aren't installed.
    """
    tree = ast.parse(source_path.read_text(), filename=str(source_path))

    def _is_subprocess_spawn(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Attribute):
            return func.attr == "create_subprocess_exec"
        if isinstance(func, ast.Name):
            return func.id == "create_subprocess_exec"
        return False

    def _passes_stdin_devnull(call: ast.Call) -> bool:
        for kw in call.keywords:
            if kw.arg != "stdin":
                continue
            return "DEVNULL" in ast.dump(kw.value)
        return False

    spawn_calls = [n for n in ast.walk(tree) if _is_subprocess_spawn(n)]
    assert spawn_calls, (
        f"expected at least one create_subprocess_exec call in "
        f"{source_path.name} — file restructured? update this test."
    )

    offenders = [
        f"{source_path.name}:{call.lineno}"
        for call in spawn_calls
        if not _passes_stdin_devnull(call)
    ]
    assert not offenders, (
        "create_subprocess_exec call(s) missing "
        "stdin=asyncio.subprocess.DEVNULL — would re-introduce the "
        "MCP-runtime stdin pipe deadlock:\n  " + "\n  ".join(offenders)
    )
