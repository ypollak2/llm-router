"""The CLI must not die because its own output contains emoji (CHZ-WIN-01).

WHY THIS EXISTS

`llm_router doctor` prints ✓ / ✗ / ⚡ / 💰. On Windows the console default is cp1252,
which encodes none of them, so the first status glyph raised UnicodeEncodeError
and the command exited non-zero with a traceback — on a machine where nothing was
wrong.

WHY CI DID NOT CATCH IT FOR SO LONG

The windows smoke job sets PYTHONUTF8=1 and PYTHONIOENCODING=utf-8 at the job
level. Those make the suite pass and do nothing for a user, who has neither. A
new docs-command job without them found the bug within one run.

The tempting fix was to copy those env vars into the new job. That would have
turned CI green while every Windows user kept a crashing `doctor` — repairing the
signal instead of the defect. This test exists so the fix stays in the product.

WHY IT RUNS EVERYWHERE

Reproduced by forcing a cp1252 stream rather than by requiring Windows. A test
that only runs on one platform is a test that runs rarely and gets believed
anyway — and this whole audit began with a Windows failure invisible on every
machine the maintainer could reach.
"""

from __future__ import annotations

import io
import sys

import pytest

from llm_router.cli import _make_output_encoding_safe

#: Characters the CLI actually prints. Not a general Unicode test — these are the
#: ones that crashed.
_GLYPHS = "✓ ✗ ⚡ 💰 ⚖ 🛡"


def _cp1252_stream() -> io.TextIOWrapper:
    """A stream that behaves like a default Windows console."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


def test_cp1252_really_cannot_encode_the_glyphs():
    """Guards the guard: if this stops raising, the test below proves nothing."""
    stream = _cp1252_stream()
    with pytest.raises(UnicodeEncodeError):
        stream.write(_GLYPHS)
        stream.flush()


def test_writing_glyphs_survives_a_cp1252_stdout(monkeypatch):
    stream = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", _cp1252_stream())

    _make_output_encoding_safe()

    # Must not raise. Whether the glyph survives or becomes `?` is secondary — a
    # diagnostic command that refuses to run because it cannot draw a tick is
    # worse than one that draws the wrong character.
    sys.stdout.write(_GLYPHS)
    sys.stdout.flush()


def test_utf8_stdout_is_left_alone(monkeypatch):
    """No-op where the stream is already correct, so this cannot break Linux/macOS."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", stream)
    _make_output_encoding_safe()
    assert stream.encoding.lower().replace("-", "") == "utf8"
    stream.write(_GLYPHS)


def test_a_stream_that_cannot_reconfigure_is_survived(monkeypatch):
    """A detached or wrapped stream must never be the reason a command fails."""

    class _NoReconfigure:
        encoding = "cp1252"

        def write(self, _s): return 0
        def flush(self): pass

    monkeypatch.setattr(sys, "stdout", _NoReconfigure())
    monkeypatch.setattr(sys, "stderr", _NoReconfigure())
    _make_output_encoding_safe()  # must not raise
