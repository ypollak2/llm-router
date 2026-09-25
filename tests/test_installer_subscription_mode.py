"""North Star #2 — flat-rate subscribers first, zero workflow change.

`LLM_ROUTER_CLAUDE_SUBSCRIPTION=true` is what `discover.get_available_providers()`
reads to route Claude work back through the subscription instead of a metered
API key, but `llm-router install` already runs a seat probe (`seats.py`) that
knows whether a Claude seat is logged in, and it never acted on that knowledge
— a Pro/Max subscriber had to hand-set the variable even after the installer
had already detected their seat.

`install._maybe_enable_claude_subscription()` closes that gap: when the seat
probe finds a Claude seat AND the variable has never been set anywhere (not in
the process environment, not in the persisted `~/.llm-router/.env`), it writes
`LLM_ROUTER_CLAUDE_SUBSCRIPTION=true` there and prints one line saying so and
how to turn it off. Every other outcome — an explicit value already set
(true or false), no seat, or the probe itself failing/being inconclusive —
must leave the variable alone. "Unknown" is not a subscription.

All tests point HOME at a throwaway directory and never touch the operator's
real ``~/.llm-router`` or ``~/.claude``.
"""

from __future__ import annotations

from pathlib import Path

from llm_router.commands import install
from llm_router.seats import Seat, Seats


def _use_temp_home(monkeypatch, tmp_path: Path) -> Path:
    """Force `paths.state_path()` to resolve via `Path.home() / ".llm-router"`
    under a throwaway HOME, the same code path production hits — not the
    `LLM_ROUTER_HOME` test-isolation override the rest of the suite uses.
    Returns the `.llm-router/.env` path the installer would write to."""
    monkeypatch.delenv("LLM_ROUTER_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
    return tmp_path / ".llm-router" / ".env"


def _seat_present() -> Seats:
    return Seats(claude=Seat(kind="claude.ai", plan="max"))


def _seat_absent() -> Seats:
    return Seats(claude=Seat())  # kind=None: never logged in


# ── seat detected + var unset → enabled ─────────────────────────────────────

def test_seat_detected_and_var_unset_enables_subscription_mode(monkeypatch, tmp_path):
    env_file = _use_temp_home(monkeypatch, tmp_path)

    install._maybe_enable_claude_subscription(_seat_present())

    assert env_file.exists(), "expected the installer to create ~/.llm-router/.env"
    contents = env_file.read_text()
    assert "LLM_ROUTER_CLAUDE_SUBSCRIPTION=true" in contents


# ── seat detected + var explicitly false → untouched ────────────────────────

def test_seat_detected_but_process_env_explicitly_false_is_untouched(monkeypatch, tmp_path):
    env_file = _use_temp_home(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "false")

    install._maybe_enable_claude_subscription(_seat_present())

    assert not env_file.exists(), (
        "an explicit LLM_ROUTER_CLAUDE_SUBSCRIPTION=false must never be overridden"
    )


def test_seat_detected_but_persisted_env_explicitly_false_is_untouched(monkeypatch, tmp_path):
    env_file = _use_temp_home(monkeypatch, tmp_path)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("LLM_ROUTER_CLAUDE_SUBSCRIPTION=false\n")

    install._maybe_enable_claude_subscription(_seat_present())

    assert env_file.read_text() == "LLM_ROUTER_CLAUDE_SUBSCRIPTION=false\n", (
        "an explicit value already persisted to ~/.llm-router/.env must never be "
        "overridden, even when the seat probe finds a seat"
    )


def test_rerunning_install_after_it_already_enabled_is_a_noop(monkeypatch, tmp_path):
    """Upgrade path: an existing install re-run must not duplicate the line or
    otherwise change behaviour — the variable it set last time now reads as an
    explicit value too."""
    env_file = _use_temp_home(monkeypatch, tmp_path)

    install._maybe_enable_claude_subscription(_seat_present())
    first = env_file.read_text()

    install._maybe_enable_claude_subscription(_seat_present())
    second = env_file.read_text()

    assert first == second
    assert first.count("LLM_ROUTER_CLAUDE_SUBSCRIPTION") == 1


# ── detection unknown/error → not enabled ───────────────────────────────────

def test_detection_failure_does_not_enable_subscription_mode(monkeypatch, tmp_path, capsys):
    env_file = _use_temp_home(monkeypatch, tmp_path)

    install._maybe_enable_claude_subscription(None)

    assert not env_file.exists(), "unknown detection must not be treated as a subscription"
    out = capsys.readouterr().out
    assert "LLM_ROUTER_CLAUDE_SUBSCRIPTION" in out
    assert "true" not in out.lower()


# ── no seat → not enabled ────────────────────────────────────────────────────

def test_no_seat_does_not_enable_subscription_mode(monkeypatch, tmp_path):
    env_file = _use_temp_home(monkeypatch, tmp_path)

    install._maybe_enable_claude_subscription(_seat_absent())

    assert not env_file.exists()


# ── the message is printed ──────────────────────────────────────────────────

def test_enabling_prints_how_to_turn_it_off(monkeypatch, tmp_path, capsys):
    _use_temp_home(monkeypatch, tmp_path)

    install._maybe_enable_claude_subscription(_seat_present())

    out = capsys.readouterr().out
    assert "LLM_ROUTER_CLAUDE_SUBSCRIPTION=true" in out
    assert "LLM_ROUTER_CLAUDE_SUBSCRIPTION=false" in out, (
        "message must say how to turn it off"
    )


def test_var_already_set_prints_nothing_new(monkeypatch, tmp_path, capsys):
    _use_temp_home(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "false")

    install._maybe_enable_claude_subscription(_seat_present())

    out = capsys.readouterr().out
    assert out == ""
