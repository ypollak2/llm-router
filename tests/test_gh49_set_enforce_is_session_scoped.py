"""GH#49: set-enforce silently changed every other running session.

`set-enforce` wrote `enforce:` into ~/.llm-router/routing.yaml, and
`resolve_enforce_mode()` re-read that file on every hook invocation with no
caching. Nothing scoped it to a session, so `set-enforce hard` in one Claude
Code window immediately started blocking tool calls in every other window on
the machine whose process env did not happen to carry LLM_ROUTER_ENFORCE. The
reporter confirmed it live in both directions.

Meanwhile the command printed "Restart Claude Code for the change to take
effect" — so the message and the behaviour disagreed, and a user reasoning from
the message would not expect the blast radius.

Resolved per the maintainer's decision: SESSION-SCOPED. A setting made in one
session governs that session; other sessions are untouched. `--global` remains
available for the old machine-wide behaviour, explicitly asked for.

Priority becomes: env > session > repo > global > default. Session sits under
env (an explicit export still wins) and above repo (a deliberate in-session
change beats a checked-in default).
"""
from __future__ import annotations

import pytest

from llm_router import enforce_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    (tmp_path / ".llm-router").mkdir()
    monkeypatch.delenv("LLM_ROUTER_ENFORCE", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    return tmp_path


def _write_global(home, mode):
    (home / ".llm-router" / "routing.yaml").write_text(f"enforce: {mode}\n")


def _write_session(home, sid, mode):
    d = home / ".llm-router" / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "enforce").write_text(mode + "\n")


def test_session_setting_wins_over_global(home, monkeypatch):
    _write_global(home, "hard")
    _write_session(home, "sess-A", "off")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-A")
    assert enforce_config.resolve_enforce_mode(cwd=home, home=home) == "off"


def test_other_sessions_are_untouched(home, monkeypatch):
    """The reported defect: one session's change reached all the others."""
    _write_global(home, "smart")
    _write_session(home, "sess-A", "hard")

    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-B")
    assert enforce_config.resolve_enforce_mode(cwd=home, home=home) == "smart", (
        "session B picked up session A's setting"
    )
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-A")
    assert enforce_config.resolve_enforce_mode(cwd=home, home=home) == "hard"


def test_env_still_outranks_the_session_file(home, monkeypatch):
    """An explicit export is the strongest signal a user can give."""
    _write_session(home, "sess-A", "off")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-A")
    monkeypatch.setenv("LLM_ROUTER_ENFORCE", "hard")
    assert enforce_config.resolve_enforce_mode(cwd=home, home=home) == "hard"


def test_no_session_id_falls_back_to_global(home):
    """Launch paths without CLAUDE_SESSION_ID must keep working."""
    _write_global(home, "hard")
    assert enforce_config.resolve_enforce_mode(cwd=home, home=home) == "hard"


def test_unknown_session_falls_back_to_global(home, monkeypatch):
    _write_global(home, "soft")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "never-seen")
    assert enforce_config.resolve_enforce_mode(cwd=home, home=home) == "soft"


def test_corrupt_session_file_never_raises(home, monkeypatch):
    d = home / ".llm-router" / "sessions" / "sess-A"
    d.mkdir(parents=True)
    (d / "enforce").write_bytes(b"\xff\xfe not utf 8")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-A")
    assert enforce_config.resolve_enforce_mode(cwd=home, home=home) == enforce_config.DEFAULT_ENFORCE


def test_session_id_cannot_escape_the_sessions_directory(home, monkeypatch):
    """A session id arrives from the environment; treat it as untrusted input."""
    _write_global(home, "smart")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "../../../etc/passwd")
    mode = enforce_config.resolve_enforce_mode(cwd=home, home=home)
    assert mode == "smart", f"path traversal in session id changed resolution: {mode}"


def test_set_enforce_writes_the_session_file_not_the_global_one(home, monkeypatch):
    """E2E over the real command: default write must be session-scoped."""
    from llm_router.commands import set_enforce as se

    monkeypatch.setattr(se.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-E2E")
    _write_global(home, "smart")

    se.cmd_set_enforce(["hard"])

    sess = home / ".llm-router" / "sessions" / "sess-E2E" / "enforce"
    assert sess.is_file() and sess.read_text().strip() == "hard"
    assert "enforce: smart" in (home / ".llm-router" / "routing.yaml").read_text(), (
        "the global file was modified by a session-scoped set-enforce"
    )
    assert enforce_config.resolve_enforce_mode(cwd=home, home=home) == "hard"


def test_global_flag_still_writes_machine_wide(home, monkeypatch):
    from llm_router.commands import set_enforce as se

    monkeypatch.setattr(se.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-E2E")
    se.cmd_set_enforce(["off", "--global"])

    assert "enforce: off" in (home / ".llm-router" / "routing.yaml").read_text()
    assert not (home / ".llm-router" / "sessions" / "sess-E2E" / "enforce").exists()


def test_no_session_id_still_writes_globally(home, monkeypatch):
    """A shell with no CLAUDE_SESSION_ID keeps the old, documented behaviour."""
    from llm_router.commands import set_enforce as se

    monkeypatch.setattr(se.Path, "home", classmethod(lambda cls: home))
    se.cmd_set_enforce(["soft"])
    assert "enforce: soft" in (home / ".llm-router" / "routing.yaml").read_text()
