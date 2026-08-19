"""Filesystem tools must not register unless the operator opts in (SEC-002).

WHY THIS EXISTS
===============

`llm_fs_find`, `llm_fs_rename`, `llm_fs_edit_many` and `llm_fs_analyze_context`
registered unconditionally. Upstream gates the same tools behind an opt-in env var, for the reason its own
comment gives:

    "The llm_fs_* tools read user files into model prompts. Without a sandbox an
     agent could exfiltrate ~/.ssh/** or any other readable path in one call."

That mitigation never reached this repository. It was found by sweeping the
SEC-* tags after porting SEC-003 — i.e. by asking "what ELSE is unported", not
by anyone reporting it.

WHAT IS STILL MISSING, STATED PLAINLY
=====================================

Upstream SEC-002 is defence in depth, two layers:

    1. tools not registered without the opt-in          <-- ported, tested here
    2. `project_root` required, paths that resolve      <-- ABSENT from this repo
       outside it rejected (`_assert_under_root`)

Layer 2 does not exist here at all — `_assert_under_root`, `project_root` and
`FsSandboxError` have zero occurrences — and two of these four tools WRITE
(`llm_fs_rename`, `llm_fs_edit_many`). So an operator who opts in gets tools with
no path confinement whatsoever.

Layer 1 bounds WHO is exposed. It does not bound WHAT an opted-in agent can
reach. Adding layer 2 changes tool signatures, so it needs its own change, its
own design and its own control — and until it lands, `LLM_ROUTER_FS_TOOLS=on`
should be read as "I accept unconfined filesystem access", which is a much
larger statement than the flag's name suggests.

This docstring is the record of that gap. Delete it when layer 2 lands, not
before.
"""

from __future__ import annotations

import pytest

from llm_router.tools import fs


class _RecordingMCP:
    def __init__(self) -> None:
        self.registered: list[str] = []

    def tool(self, *_a, **_k):
        def _decorator(fn):
            self.registered.append(getattr(fn, "__name__", repr(fn)))
            return fn

        return _decorator


def _register_with(monkeypatch, value: str | None) -> list[str]:
    if value is None:
        monkeypatch.delenv(fs._FS_TOOLS_ENV, raising=False)
    else:
        monkeypatch.setenv(fs._FS_TOOLS_ENV, value)
    mcp = _RecordingMCP()
    fs.register(mcp)
    return mcp.registered


def test_unset_environment_registers_nothing(monkeypatch):
    assert _register_with(monkeypatch, None) == [], (
        "llm_fs_* tools registered with no opt-in — a default install exposes "
        "filesystem read (and, via rename/edit_many, write) to the model"
    )


@pytest.mark.parametrize("value", ["", "0", "off", "false", "no", "nope"])
def test_non_affirmative_values_register_nothing(monkeypatch, value: str):
    """A gate that reads "off" as truthy is the classic fail-open."""
    assert _register_with(monkeypatch, value) == [], (
        f"{value!r} was treated as an opt-in — the gate must fail closed"
    )


@pytest.mark.parametrize("value", ["1", "on", "true", "yes", "ON", "True"])
def test_affirmative_values_do_register(monkeypatch, value: str):
    """Without this, `register()` could just `return` and every test above would
    still pass — the tools would be silently dead rather than gated."""
    registered = _register_with(monkeypatch, value)
    assert registered, f"{value!r} should enable the tools but registered none"


def test_all_four_tools_are_behind_the_same_gate(monkeypatch):
    """A partial gate is the shape this defect class leaves behind."""
    registered = _register_with(monkeypatch, "on")
    expected = {
        "llm_fs_find",
        "llm_fs_rename",
        "llm_fs_edit_many",
        "llm_fs_analyze_context",
    }
    missing = expected - set(registered)
    assert not missing, f"not registered under the opt-in: {sorted(missing)}"


def test_layer_two_has_landed():
    """SEC-002 layer 2 — path confinement — is present. It was not, until 13.0.0.

    This test used to be the inverse: ``test_layer_two_is_still_missing_and_
    this_is_deliberate``, an xfail-style tripwire asserting the gap, written so
    that closing it could not go unrecorded. It fired on the upstream sync,
    which is exactly what it was for. Flipped here, in the change that closed
    the gap, rather than deleted — a gap that closes silently is how the
    docstrings claiming it was open would have quietly become wrong.

    Checks for a real DEFINITION, not a mention. An earlier version matched the
    string anywhere in the file and tripped on the SEC-002 comment that names
    ``_assert_under_root`` while explaining its absence — textual co-presence
    failing the way it always does.
    """
    assert hasattr(fs, "_assert_under_root"), (
        "path confinement has disappeared again. llm_fs_* tools would accept "
        "paths outside project_root, which is SEC-002 layer 2 regressing."
    )
    assert hasattr(fs, "FsSandboxError"), (
        "the sandbox error type is gone, so escapes have nothing to raise"
    )


def test_confinement_actually_rejects_an_escape(tmp_path):
    """The behaviour, not just the symbol. `hasattr` is not enforcement."""
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "ok.txt").write_text("inside")
    outside = tmp_path / "outside.txt"
    outside.write_text("sk-live-must-not-be-readable")

    resolved = fs._resolve_root(str(root))
    fs._assert_under_root(root / "src" / "ok.txt", resolved)  # must not raise

    with pytest.raises(fs.FsSandboxError):
        fs._assert_under_root(outside, resolved)

    with pytest.raises(fs.FsSandboxError):
        fs._assert_under_root(root / "src" / ".." / ".." / "outside.txt", resolved)
