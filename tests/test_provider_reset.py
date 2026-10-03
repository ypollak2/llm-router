"""A provider that reports a reset time is skipped until that time.

Observed 2026-10-03: Codex (ChatGPT Plus) returned a usage-limit error with a
reset the next morning, and the 15s rate-limit cooldown let the router retry it
on every request. These tests pin the behaviour end to end: parse the reset,
persist it where other processes can see it, skip the provider until it passes,
and leave every other failure on the old 15s path.

NOTE: the live Codex wording could not be captured; the messages below are
representative shapes ("try again at <time>", an ISO stamp, "try again in 2h"),
not a copy of one provider string.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from llm_router import provider_reset
from llm_router.codex_agent import CodexResult
from llm_router.health import HealthTracker
from llm_router.router import route_and_call
from llm_router.types import RoutingProfile, TaskType

HOUR = 3600


def _local_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------- parsing


class TestParse:
    def test_absolute_timestamp_in_message(self):
        now = time.time()
        reset = now + 5 * HOUR
        got = provider_reset.parse_reset_epoch(
            f"You've hit your usage limit. Try again at {_local_iso(reset)}.", now=now
        )
        assert got == pytest.approx(reset, abs=60)  # the stamp has minute resolution

    def test_clock_time_resolves_to_next_occurrence(self):
        # 22:00 now; "6:39 AM" has passed today, so it means tomorrow morning.
        now = datetime(2026, 10, 3, 22, 0).timestamp()
        got = provider_reset.parse_reset_epoch(
            "You've reached your usage limit. Please try again at 6:39 AM.", now=now
        )
        assert datetime.fromtimestamp(got) == datetime(2026, 10, 4, 6, 39)

    def test_clock_time_later_today_stays_today(self):
        now = datetime(2026, 10, 3, 9, 0).timestamp()
        got = provider_reset.parse_reset_epoch(
            "Usage limit reached. Resets at 6:39 PM.", now=now
        )
        assert datetime.fromtimestamp(got) == datetime(2026, 10, 3, 18, 39)

    def test_relative_duration(self):
        now = 1_000_000.0
        got = provider_reset.parse_reset_epoch("Rate limit reached, try again in 2h 15m", now=now)
        assert got == now + 2 * HOUR + 15 * 60

    def test_retry_after_seconds_header(self):
        now = 1_000_000.0
        assert provider_reset.parse_reset_epoch(
            "429", headers={"Retry-After": "7200"}, now=now
        ) == now + 7200

    def test_retry_after_http_date_header(self):
        now = time.time()
        stamp = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        got = provider_reset.parse_reset_epoch("429", headers={"retry-after": stamp}, now=now)
        assert got == pytest.approx(now + 3 * HOUR, abs=5)

    def test_anthropic_reset_header(self):
        now = time.time()
        stamp = (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        got = provider_reset.parse_reset_epoch(
            "rate_limit_error", headers={"anthropic-ratelimit-tokens-reset": stamp}, now=now
        )
        assert got == pytest.approx(now + 4 * HOUR, abs=5)

    def test_reset_in_the_past_is_expired(self):
        now = time.time()
        assert provider_reset.parse_reset_epoch(
            f"Usage limit reached. Try again at {_local_iso(now - 2 * HOUR)}", now=now
        ) is None

    def test_far_future_reset_is_capped_at_seven_days(self):
        now = 1_000_000.0
        got = provider_reset.parse_reset_epoch(
            "Usage limit reached, try again in 90 days", now=now
        )
        assert got == now + provider_reset.MAX_SKIP_SECONDS

    def test_unrelated_timestamp_elsewhere_in_message_is_not_a_reset(self):
        """CHZ-RESET-R-01: an ISO-looking stamp not adjacent to a reset verb,
        in an otherwise ordinary (and even retry-flavoured) error, must not
        be read as a usage-limit reset."""
        now = time.time()
        msg = "Error at 2026-10-04T23:59:00Z: connection refused, please retry"
        assert provider_reset.parse_reset_epoch(msg, now=now) is None

    def test_message_without_a_limit_signal_is_never_parsed(self):
        """A 'try again at HH:MM' shape with no limit/quota/rate wording at
        all (e.g. UI chatter, not a provider error) must not bench anything."""
        now = time.time()
        msg = "DEBUG: user clicked try again at 06:39 button"
        assert provider_reset.parse_reset_epoch(msg, now=now) is None

    @pytest.mark.parametrize(
        "message",
        ["connection reset by peer", "internal server error", "", "try again later", None],
    )
    def test_unparseable_yields_none(self, message):
        assert provider_reset.parse_reset_epoch(message, now=1_000_000.0) is None


# ------------------------------------------------------- persistence and health


class TestPersistence:
    def test_blocks_until_reset_then_recovers(self):
        now = time.time()
        assert provider_reset.record_provider_reset("codex", now + 5 * HOUR, now=now)
        tracker = HealthTracker()
        assert not tracker.is_healthy("codex")
        assert tracker.is_healthy("gemini"), "only the reporting provider is benched"
        # Same record, read from after the reset time: expired, usable again.
        assert provider_reset.get_provider_reset_until("codex", now=now + 5 * HOUR + 1) is None

    def test_short_reset_is_left_to_the_in_process_cooldown(self):
        now = time.time()
        assert not provider_reset.record_provider_reset("codex", now + 20, now=now)
        assert HealthTracker().is_healthy("codex")

    def test_record_is_capped_at_seven_days(self):
        now = time.time()
        provider_reset.record_provider_reset("codex", now + 400 * 24 * HOUR, now=now)
        until = provider_reset.get_provider_reset_until("codex", now=now)
        assert until == pytest.approx(now + provider_reset.MAX_SKIP_SECONDS, abs=1)

    def test_corrupt_state_fails_open(self):
        provider_reset._state_file().parent.mkdir(parents=True, exist_ok=True)
        provider_reset._state_file().write_text("{not json")
        assert HealthTracker().is_healthy("codex")
        assert provider_reset.all_provider_resets() == {}

    def test_wrong_shape_state_fails_open(self):
        provider_reset._state_file().parent.mkdir(parents=True, exist_ok=True)
        provider_reset._state_file().write_text(json.dumps({"providers": {"codex": "soon"}}))
        assert HealthTracker().is_healthy("codex")

    def test_corrupt_state_is_replaced_on_next_write(self):
        provider_reset._state_file().parent.mkdir(parents=True, exist_ok=True)
        provider_reset._state_file().write_text("{not json")
        now = time.time()
        assert provider_reset.record_provider_reset("codex", now + 2 * HOUR, now=now)
        assert not HealthTracker().is_healthy("codex")

    def test_unwritable_state_fails_open_and_is_accounted(self, monkeypatch, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        monkeypatch.setenv("LLM_ROUTER_PROVIDER_RESET_PATH", str(blocker / "sub" / "r.json"))
        now = time.time()
        assert provider_reset.record_provider_reset("codex", now + 2 * HOUR, now=now) is False
        assert HealthTracker().is_healthy("codex")

    def test_visible_to_a_separate_short_lived_process(self, tmp_path):
        """A hook process records the reset; this process (the MCP server) sees it."""
        env = {
            "PATH": "/usr/bin:/bin",
            "LLM_ROUTER_PROVIDER_RESET_PATH": str(provider_reset._state_file()),
        }
        script = (
            "import time; from llm_router import provider_reset as p; "
            "assert p.record_provider_reset('codex', time.time() + 7200)"
        )
        subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=60)
        assert not HealthTracker().is_healthy("codex")

        other = (
            "import sys; from llm_router.health import HealthTracker; "
            "sys.exit(0 if not HealthTracker().is_healthy('codex') else 1)"
        )
        assert subprocess.run([sys.executable, "-c", other], env=env, timeout=60).returncode == 0

    def test_concurrent_processes_recording_different_providers_all_persist(self):
        """CHZ-RESET-R-02: a read-modify-write with no cross-process lock lets
        a later writer's stale read clobber an earlier writer's just-committed
        entry. Fire several real processes at the same file near-simultaneously
        -- the fix (file_lock.exclusive_lock around the critical section) must
        serialize them so every one of them survives."""
        n = 10
        env = {
            "PATH": "/usr/bin:/bin",
            "LLM_ROUTER_PROVIDER_RESET_PATH": str(provider_reset._state_file()),
        }
        script = (
            "import sys, time; from llm_router import provider_reset as p; "
            "sys.exit(0 if p.record_provider_reset(sys.argv[1], time.time() + 7200) else 1)"
        )
        procs = [
            subprocess.Popen([sys.executable, "-c", script, f"provider-{i}"], env=env)
            for i in range(n)
        ]
        for proc in procs:
            assert proc.wait(timeout=30) == 0
        resets = provider_reset.all_provider_resets()
        assert len(resets) == n, (
            f"expected all {n} concurrent writers to persist without clobbering "
            f"each other, got {sorted(resets)}"
        )


# --------------------------------------------------------------- status display


def test_status_shows_benched_provider_and_is_quiet_otherwise():
    from llm_router.ui.status_premium import PremiumStatusCommand

    cmd = PremiumStatusCommand()
    assert str(cmd.render_provider_resets()) == ""
    reset = time.time() + 5 * HOUR
    provider_reset.record_provider_reset("codex", reset)
    shown = str(cmd.render_provider_resets())
    assert "codex unavailable until" in shown
    assert time.strftime("%H:%M", time.localtime(reset)) in shown


# ----------------------------------------------------------- router dispatch


def _codex_failure(message: str) -> CodexResult:
    return CodexResult(content=message, model="gpt-5.5", exit_code=1, duration_sec=0.1)


@pytest.fixture
def codex_first(temp_db, mock_env, monkeypatch):
    """Subscription mode with Codex available, as in test_codex_routing.py."""
    # llm_router.gateway does os.environ.setdefault(..., "codex,gemini_cli") as a
    # process-wide side effect on import; a test file that happens to run first
    # in this xdist worker leaves that set for the rest of the process, which
    # silently drops codex/gemini_cli from the chain here. Same fix as
    # test_okf_cli_dispatch_scope.py's _clear_subprocess_backend_gates.
    monkeypatch.delenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_ROUTER_BLOCK_PROVIDERS", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.1)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "")
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models", lambda: [])
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)

    async def _no_real_claude(*args, **kwargs):
        # Without this the chain falls through to a REAL `claude -p` subprocess.
        raise RuntimeError("Simulated claude CLI failure")

    monkeypatch.setattr("llm_router.router.run_claude", _no_real_claude)
    import llm_router.config as config_module

    config_module._config = None


async def _route():
    return await route_and_call(
        TaskType.CODE, "implement a binary search function", profile=RoutingProfile.BALANCED
    )


@pytest.mark.asyncio
async def test_codex_usage_limit_skips_codex_until_reset_then_uses_it_again(
    codex_first, monkeypatch
):
    reset = time.time() + 5 * HOUR
    limit_msg = f"You've hit your usage limit. Try again at {_local_iso(reset)}."

    async def _other_providers_ok(**kwargs):
        raise RuntimeError("Simulated litellm failure")

    # Request 1: Codex is tried, reports the limit, and the failure is persisted.
    with patch("litellm.acompletion", side_effect=_other_providers_ok), \
         patch("llm_router.router.run_codex", return_value=_codex_failure(limit_msg)) as codex:
        with pytest.raises(Exception):
            await _route()
    assert codex.call_count == 1
    assert provider_reset.get_provider_reset_until("codex") is not None

    # Request 2: Codex is not called at all while the reset is in the future.
    with patch("litellm.acompletion", side_effect=_other_providers_ok), \
         patch("llm_router.router.run_codex", return_value=_codex_failure(limit_msg)) as codex:
        with pytest.raises(Exception):
            await _route()
    assert codex.call_count == 0, "Codex must be skipped until its reported reset"

    # Reset time passes: Codex is used again and succeeds.
    later = types.SimpleNamespace(
        time=lambda: reset + 60, strftime=time.strftime, localtime=time.localtime
    )
    monkeypatch.setattr(provider_reset, "time", later)
    ok = CodexResult(content="codex output", model="gpt-5.5", exit_code=0, duration_sec=0.1)
    with patch("litellm.acompletion", side_effect=_other_providers_ok), \
         patch("llm_router.router.run_codex", return_value=ok) as codex:
        resp = await _route()
    assert codex.called
    assert resp.provider == "codex"


@pytest.mark.asyncio
async def test_unparseable_codex_error_keeps_the_short_cooldown(codex_first):
    async def _fail(**kwargs):
        raise RuntimeError("Simulated litellm failure")

    with patch("litellm.acompletion", side_effect=_fail), patch(
        "llm_router.router.run_codex",
        return_value=_codex_failure("codex: something broke, no time given"),
    ):
        with pytest.raises(Exception):
            await _route()
    assert provider_reset.get_provider_reset_until("codex") is None
    assert not provider_reset._state_file().exists()


def test_429_with_short_retry_after_behaves_as_before():
    """A 20s Retry-After stays on the in-process cooldown: nothing is persisted."""
    from llm_router.router import _extract_retry_after, _response_headers

    class _Resp:
        headers = {"retry-after": "20"}

    class _RateLimited(Exception):
        http_response = _Resp()

    exc = _RateLimited("429 Too Many Requests")
    assert _extract_retry_after(exc) == 20  # unchanged behaviour
    assert provider_reset.note_provider_error("openai", exc, _response_headers(exc)) is None
    assert not provider_reset._state_file().exists()
    tracker = HealthTracker()
    tracker.record_rate_limit("openai", cooldown_seconds=20)
    assert not tracker.is_healthy("openai")  # the existing in-process cooldown still applies


def test_429_with_long_retry_after_is_persisted():
    from llm_router.router import _response_headers

    class _Resp:
        headers = {"Retry-After": "18000"}

    class _RateLimited(Exception):
        http_response = _Resp()

    exc = _RateLimited("429 Too Many Requests")
    until = provider_reset.note_provider_error("openai", exc, _response_headers(exc))
    assert until == pytest.approx(time.time() + 18000, abs=5)
    assert not HealthTracker().is_healthy("openai")


# --------------------------------------------- gemini_cli / Claude CLI branches


@pytest.mark.asyncio
async def test_gemini_cli_usage_limit_is_parsed_from_full_content_not_truncated(
    temp_db, mock_env, monkeypatch
):
    """CHZ-RESET-R-03: the chain-error message the router raises keeps only
    the first 200 chars of CLI output. A reset phrase past that point is only
    visible if the gemini_cli branch parses the FULL ``gemini_result.content``
    itself, the same way the Codex branch already does."""
    from llm_router.gemini_cli_agent import GeminiCLIResult

    # llm_router.gateway does os.environ.setdefault(..., "codex,gemini_cli") as a
    # process-wide side effect on import; whichever test runs first in this
    # xdist worker leaves that set for the rest of the process, which silently
    # drops gemini_cli from the gemini-injection reachability check below (same
    # issue test_okf_cli_dispatch_scope.py's _clear_subprocess_backend_gates
    # exists to fix).
    monkeypatch.delenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_ROUTER_BLOCK_PROVIDERS", raising=False)
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: True)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.97)

    reset = time.time() + 5 * HOUR
    padding = "x" * 250  # pushes the reset phrase past the 200-char truncation
    limit_msg = f"{padding} Usage limit reached. Try again at {_local_iso(reset)}."

    async def _fail(*a, **k):
        return GeminiCLIResult(
            content=limit_msg, model="gemini-2.5-flash", exit_code=1, duration_sec=0.1
        )

    async def _litellm_fail(**kwargs):
        raise RuntimeError("Simulated litellm failure")

    monkeypatch.setattr("llm_router.router.run_gemini_cli", _fail)
    with patch("litellm.acompletion", side_effect=_litellm_fail):
        with pytest.raises(Exception):
            await route_and_call(
                TaskType.CODE, "refactor this function", profile=RoutingProfile.BALANCED
            )

    assert provider_reset.get_provider_reset_until("gemini_cli") is not None


@pytest.mark.asyncio
async def test_claude_cli_usage_limit_is_parsed_from_full_content_not_truncated(
    temp_db, mock_env, monkeypatch
):
    """Same as above, for the Claude Code CLI subscription-offload branch."""
    from llm_router.claude_agent import ClaudeResult

    # Defensive, same reason as the gemini_cli test above.
    monkeypatch.delenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_ROUTER_BLOCK_PROVIDERS", raising=False)
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)
    monkeypatch.setattr("llm_router.router.claude_offload_available", lambda config: True)
    # Puts anthropic/claude-sonnet-4-6 in the chain so the CLI-offload branch
    # (gated on claude_offload_available, patched True above) is reached.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "")
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models", lambda: [])
    import llm_router.config as config_module

    config_module._config = None

    reset = time.time() + 5 * HOUR
    padding = "x" * 250
    limit_msg = f"{padding} Usage limit reached. Try again at {_local_iso(reset)}."

    async def _fail(*a, **k):
        return ClaudeResult(
            content=limit_msg, model="claude-sonnet-4-6", exit_code=1, duration_sec=0.1
        )

    async def _litellm_fail(**kwargs):
        raise RuntimeError("Simulated litellm failure")

    monkeypatch.setattr("llm_router.router.run_claude", _fail)
    with patch("litellm.acompletion", side_effect=_litellm_fail):
        with pytest.raises(Exception):
            await route_and_call(
                TaskType.CODE, "refactor this function", profile=RoutingProfile.BALANCED
            )

    assert provider_reset.get_provider_reset_until("anthropic") is not None
