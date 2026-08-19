"""Regression: RED2-6-02 — `llm_router uninstall` must clean up claw-code + IDE
integrations, not only the primary Claude Code surfaces.

install auto-detects claw-code and IDE configs; the uninstall CLI (`_run_uninstall`)
called only `uninstall()`, so a full parallel claw-code install (hooks, sidecars,
a live MCP registration, the LLM_ROUTER_CLAW_CODE flag) and project IDE configs
survived the documented uninstall. It now calls all three removers.
"""
from __future__ import annotations

import llm_router.install_hooks as ih
from llm_router.commands import uninstall as uninstall_cmd


def test_run_uninstall_invokes_all_removers(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ih, "uninstall", lambda: (calls.append("uninstall"), ["primary removed"])[1])
    monkeypatch.setattr(ih, "uninstall_claw_code", lambda: (calls.append("claw"), ["claw removed"])[1])
    monkeypatch.setattr(ih, "uninstall_ide_configs", lambda *a, **k: (calls.append("ide"), ["ide removed"])[1])

    uninstall_cmd._run_uninstall([])

    assert calls == ["uninstall", "claw", "ide"], f"CLI did not call all removers: {calls}"
    out = capsys.readouterr().out
    assert "claw removed" in out and "ide removed" in out


def test_install_hooks_main_uninstall_delegates(monkeypatch):
    """RED2-7-01: the `llm_router-install-hooks uninstall` entry point (install_hooks.main)
    must delegate to the same _run_uninstall, so it also cleans claw-code + IDE
    configs — not just the primary Claude Code surfaces."""
    import sys
    import llm_router.install_hooks as ih2
    from llm_router.commands import uninstall as uninstall_cmd

    called = {}
    monkeypatch.setattr(uninstall_cmd, "_run_uninstall", lambda flags=None: called.setdefault("flags", flags))
    monkeypatch.setattr(sys, "argv", ["llm_router-install-hooks", "uninstall", "--purge"])
    ih2.main()
    assert "flags" in called, "main() uninstall did not delegate to _run_uninstall"
    assert called["flags"] == ["--purge"], f"flags not forwarded: {called}"


def test_remover_exception_does_not_abort_uninstall(monkeypatch, capsys):
    """A failure cleaning an optional surface must not abort the whole uninstall."""
    monkeypatch.setattr(ih, "uninstall", lambda: ["primary removed"])

    def boom():
        raise RuntimeError("claw dir locked")

    monkeypatch.setattr(ih, "uninstall_claw_code", boom)
    monkeypatch.setattr(ih, "uninstall_ide_configs", lambda *a, **k: ["ide removed"])

    uninstall_cmd._run_uninstall([])  # must not raise

    out = capsys.readouterr().out
    assert "primary removed" in out
    assert "claw-code cleanup skipped" in out
    assert "ide removed" in out
