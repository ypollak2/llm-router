"""`_install_codex_files` must not write into the operator's real ~/.codex — 2026-09-27.

Evidence: the operator's real ``~/.codex/hooks.json`` had 84 UserPromptSubmit
entries, ~82 of them pointing at deleted pytest tmp directories.
``_codex_hook_command()`` (commands/install.py) builds its ``command`` from
``paths.state_path("hooks")``, which DOES honour ``LLM_ROUTER_HOME`` under
test (see ``_isolate_llm_router_writes`` in conftest.py) -- but the file it
writes that command INTO, ``hooks_json = Path.home() / ".codex" / "hooks.json"``,
is resolved from a bare ``Path.home()`` that honours nothing. Any test that
called ``_install_codex_files()`` without separately patching ``Path.home()``
away from the real one wrote a permanent, real entry pointing at a directory
pytest deletes at teardown.

The fix lives in ``tests/conftest.py``'s ``_hermetic_host_state`` fixture,
which now wraps ``_install_codex_files`` the same way it already wrapped its
uninstall sibling ``uninstall_host_integrations``: a no-op unless the calling
test has patched ``Path.home()`` away from the real one.

This file never touches the true operator home. It only asserts the
invariant the wrapper provides -- calling the function without isolating
``Path.home()`` is a no-op, and calling it WITH isolation still does real
work -- which is exactly what removing or keeping the fixture flips.
"""

from __future__ import annotations

import pathlib

from llm_router.commands import install as install_module


def test_install_codex_files_is_a_noop_at_the_real_home(monkeypatch):
    """The exact shape of the historical bug: a test that forgot to patch home.

    Deliberately does NOT touch ``Path.home()``/``HOME`` at all. If
    ``_hermetic_host_state`` is wired up, the ``_install_codex_files`` name
    seen here already resolves to its wrapped version, and calling it must be
    a no-op: no action reported, and nothing appears under the real home.

    Remove the wrapping in conftest.py's ``_hermetic_host_state`` (delete the
    ``monkeypatch.setattr(_install_module, "_install_codex_files", ...)``
    line added 2026-09-27) and this test fails, because the raw installer runs
    and reports at least one action.
    """
    # 2026-10-06: the suite now sandboxes $HOME for every test, so a bare `Path.home()`
    # is no longer the real home. This test is ABOUT the real home, so it points
    # `Path.home()` there explicitly -- safe, because the wrapper under test is a no-op
    # and `tests/_real_home_guard.py` refuses any write that nevertheless got through.
    from tests._real_home_guard import TRUE_HOME

    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: TRUE_HOME))
    real_home = pathlib.Path.home()
    before_exists = (real_home / ".codex" / "hooks.json").exists()

    actions = install_module._install_codex_files()

    assert actions == [], (
        "_install_codex_files ran against the real home instead of being "
        "guarded to a no-op -- the _hermetic_host_state wrapper in "
        "conftest.py appears to be missing or bypassed"
    )
    after_exists = (real_home / ".codex" / "hooks.json").exists()
    assert before_exists == after_exists, (
        "a hooks.json file appeared under the real home during this test"
    )


def test_install_codex_files_still_runs_when_home_is_isolated(monkeypatch, tmp_path):
    """Anti-vacuity: the guard must gate on isolation, not disable the feature.

    Same isolation shape as ``tests/test_codex_install.py``'s ``home`` fixture:
    patch Path.home() and HOME to a tmp_path before calling. With that done,
    the wrapper's ``Path.home() == real_home`` check is false, so it must
    delegate to the real installer and actually write the sandboxed files.
    """
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "llm_router.install_hooks._build_mcp_entry",
        lambda: ({"command": "/opt/llm/bin/llm-router", "args": []}, []),
    )
    monkeypatch.setattr("llm_router.install_hooks._python_exe", lambda: "/opt/py/bin/python3")

    actions = install_module._install_codex_files()

    assert actions, (
        "no actions were reported even with Path.home() isolated -- the "
        "guard is swallowing every call instead of gating on the real home"
    )
    assert (tmp_path / ".codex" / "hooks.json").exists()
