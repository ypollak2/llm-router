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

import importlib.util
import os
import sys
from pathlib import Path

from llm_router.commands import install
from llm_router.seats import Seat, Seats

_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = _ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"


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


def test_seat_detected_but_persisted_export_false_is_untouched(monkeypatch, tmp_path):
    """PR #151 review: a shell-style `export VAR=value` line was invisible to
    the "already set" check, because partitioning on "=" read its key as
    "export LLM_ROUTER_CLAUDE_SUBSCRIPTION" rather than the real variable
    name. The installer then appended a contradictory `=true` line below the
    user's `export …=false`, and the hook's own dotenv loader — which has the
    same "export" blind spot, so it never resolves the first line at all —
    picked up the unprefixed second line as the live value, silently flipping
    an explicit false to true. Reproduced live in review."""
    env_file = _use_temp_home(monkeypatch, tmp_path)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("export LLM_ROUTER_CLAUDE_SUBSCRIPTION=false\n")

    install._maybe_enable_claude_subscription(_seat_present())

    assert env_file.read_text() == "export LLM_ROUTER_CLAUDE_SUBSCRIPTION=false\n", (
        "an explicit `export VAR=false` line must count as already set and "
        "must never get a contradictory line appended below it"
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


# ── runtime read path: what the installer writes, the hook and the router
#    config both actually pick up ─────────────────────────────────────────────

def test_installed_subscription_mode_is_seen_by_the_hook_and_excludes_anthropic(
    monkeypatch, tmp_path
):
    """End to end: `_maybe_enable_claude_subscription` writes the .env, the
    UserPromptSubmit hook's own dotenv loader (`hooks/auto-route.py::_load_dotenv`,
    which every hook process runs at import time) is what actually populates
    `os.environ` for that process — not the installer's own in-memory state —
    and `discover.get_available_providers()` must then exclude "anthropic"
    even though an ANTHROPIC_API_KEY is configured. Never touches the
    operator's real ~/.llm-router; HOME and LLM_ROUTER_HOME both point at
    tmp_path for the whole test.
    """
    _use_temp_home(monkeypatch, tmp_path)
    # get_config()'s dotenv source also resolves through LLM_ROUTER_HOME (see
    # paths.state_path); point it at the same throwaway directory as HOME so
    # both the hook's loader and RouterConfig read the one .env this test
    # writes, not the suite-wide autouse sandbox.
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    install._maybe_enable_claude_subscription(_seat_present())

    # The var must not already be in this process's environment — otherwise
    # the hook's loader (which never overrides an existing key) would prove
    # nothing about reading the file it just wrote.
    assert "LLM_ROUTER_CLAUDE_SUBSCRIPTION" not in os.environ

    spec = importlib.util.spec_from_file_location(
        "_subscription_regression_hook", _HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        try:
            spec.loader.exec_module(mod)  # runs _load_dotenv() at import time
        except SystemExit:
            pass

        assert os.environ.get("LLM_ROUTER_CLAUDE_SUBSCRIPTION") == "true", (
            "the hook's own dotenv loader did not pick up what the installer wrote"
        )

        import llm_router.config as _cfg
        _cfg._config = None  # force a fresh RouterConfig read of the current env
        from llm_router.discover import get_available_providers

        providers = get_available_providers()
        assert "anthropic" not in providers, (
            f"ANTHROPIC_API_KEY is configured but subscription mode should "
            f"exclude it from routing: {providers}"
        )
    finally:
        sys.modules.pop(spec.name, None)
        # `_load_dotenv()` writes straight to `os.environ`, bypassing
        # monkeypatch's own tracking — undo it by hand so this doesn't leak
        # into later tests.
        os.environ.pop("LLM_ROUTER_CLAUDE_SUBSCRIPTION", None)
