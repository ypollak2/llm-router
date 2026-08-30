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

GH#59 follow-up: the fix above resolved the session id via a bare
``os.environ.get("CLAUDE_SESSION_ID")``. Real Claude Code never sets that
var — it sets ``CLAUDE_CODE_SESSION_ID`` — so the session branch above
silently never engaged there and every call fell through to the exact global
write #49 was about, while the printed message still claimed session scope.
This file's original tests all set ``CLAUDE_SESSION_ID`` and therefore
validated the wrong tier end-to-end; see the parametrized cases below, which
close that regression class for both variables the same way.
"""
from __future__ import annotations

import pytest

from llm_router import enforce_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    (tmp_path / ".llm-router").mkdir()
    monkeypatch.delenv("LLM_ROUTER_ENFORCE", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    # GH#59: the fix now resolves the session id through
    # session_store.resolve_session_id(), whose 4th tier is a
    # current_session.json pointer file under llm_router_home() — and whose
    # 3rd tier is CLAUDE_CODE_SESSION_ID, which a real Claude Code process
    # (this test runner included) actually has set. Both must be neutralised
    # so these tests observe only what they explicitly set up, never the
    # developer's/agent's real session or real ~/.llm-router.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
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


# ── GH#59: real Claude Code sets CLAUDE_CODE_SESSION_ID, not CLAUDE_SESSION_ID ──
#
# The tests above all set CLAUDE_SESSION_ID and therefore validated the
# session-scoping fix against a variable real Claude Code never exports —
# passing green while the actual reported defect (session branch never
# engages on real Claude Code, every call falls through to the global write)
# went uncaught. Parametrizing over both variables closes that gap for good:
# whichever one gets renamed or dropped next, the wrong-tier regression can't
# silently pass again.

@pytest.mark.parametrize("env_var", ["CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"])
def test_set_enforce_is_session_scoped_for_either_session_env_var(home, monkeypatch, env_var):
    """Writer (set_enforce) and reader (resolve_enforce_mode) must agree on
    "this session" no matter which of the two session-id env vars is the one
    actually present. Real Claude Code only ever sets CLAUDE_CODE_SESSION_ID —
    before the GH#59 fix, that case silently fell through to a global write."""
    from llm_router.commands import set_enforce as se

    monkeypatch.setattr(se.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv(env_var, "sess-GH59")

    se.cmd_set_enforce(["soft"])

    sess_file = home / ".llm-router" / "sessions" / "sess-GH59" / "enforce"
    assert sess_file.is_file() and sess_file.read_text().strip() == "soft", (
        f"set-enforce with only {env_var} set did not write the per-session file"
    )
    routing_yaml = home / ".llm-router" / "routing.yaml"
    assert not routing_yaml.exists(), (
        f"set-enforce with only {env_var} set wrote the global routing.yaml instead "
        "of scoping to the session"
    )

    monkeypatch.delenv("LLM_ROUTER_ENFORCE", raising=False)
    assert enforce_config.resolve_enforce_mode(cwd=home, home=home) == "soft", (
        f"resolve_enforce_mode did not pick up the session set via {env_var}"
    )


def test_neither_session_env_var_set_writes_globally_and_says_so(home, monkeypatch, capsys):
    """Negative case from the plan: with no session id resolvable at all, the
    write must be global — and the printed message must honestly say so,
    never claim session-only scope for a write that wasn't."""
    from llm_router.commands import set_enforce as se

    monkeypatch.setattr(se.Path, "home", classmethod(lambda cls: home))
    # `home` fixture already deleted CLAUDE_SESSION_ID and CLAUDE_CODE_SESSION_ID
    # and pointed LLM_ROUTER_HOME at an empty tmp dir, so no pointer-file
    # fallback can resolve a session id here either.

    se.cmd_set_enforce(["hard"])

    routing_yaml = home / ".llm-router" / "routing.yaml"
    assert routing_yaml.is_file() and "enforce: hard" in routing_yaml.read_text()
    assert not (home / ".llm-router" / "sessions").exists(), (
        "no session id resolved, but a per-session file was written anyway"
    )

    out = capsys.readouterr().out
    assert "this session only" not in out, (
        "message claimed session-only scope while the write was global"
    )
    assert "every session on this machine" in out


def test_resolve_session_id_raising_never_breaks_enforce_resolution(home, monkeypatch):
    """GH#59: enforce_config runs inside hooks, so the session_store import/call
    must be guarded — if resolve_session_id() itself raises, resolution must
    fall through to the next tier instead of propagating into the hook."""
    import llm_router.session_store as session_store

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(session_store, "resolve_session_id", _boom)
    _write_global(home, "hard")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-boom")

    assert enforce_config.resolve_enforce_mode(cwd=home, home=home) == "hard"


def test_set_enforce_never_crashes_if_resolver_raises(home, monkeypatch):
    """Same guard on the writer side: a raising resolver must not crash the
    CLI command, and must fall through to the global write."""
    from llm_router.commands import set_enforce as se
    import llm_router.session_store as session_store

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(session_store, "resolve_session_id", _boom)
    monkeypatch.setattr(se.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-boom")

    se.cmd_set_enforce(["hard"])  # must not raise

    assert "enforce: hard" in (home / ".llm-router" / "routing.yaml").read_text()
    assert not (home / ".llm-router" / "sessions").exists()
