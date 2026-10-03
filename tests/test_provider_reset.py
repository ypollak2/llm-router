"""A provider that reports a reset time is skipped until that time.

Observed 2026-10-03: Codex (ChatGPT Plus) returned a usage-limit error with a
reset the next morning, and the 15s rate-limit cooldown let the router retry it
on every request. These tests pin the behaviour end to end: parse the reset,
persist it where other processes can see it, skip the provider until it passes,
and leave every other failure on the old 15s path.

NOTE: most messages below are representative shapes ("try again at <time>", an
ISO stamp, "try again in 2h"). The one exception is
TestRealCapturedCodexQuotaMessage, which quotes the real Codex error line
captured 2026-10-03.
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
        far = datetime.fromtimestamp(now + 90 * 24 * HOUR).strftime("%Y-%m-%d %H:%M")
        got = provider_reset.parse_reset_epoch(
            f"Usage limit reached, try again at {far}", now=now
        )
        assert got == now + provider_reset.MAX_SKIP_SECONDS
        # A far header is capped the same way.
        assert provider_reset.parse_reset_epoch(
            "429", headers={"Retry-After": str(90 * 24 * HOUR)}, now=now
        ) == now + provider_reset.MAX_SKIP_SECONDS

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
async def test_claude_cli_usage_limit_text_never_benches_anthropic(
    temp_db, mock_env, monkeypatch
):
    """Anthropic is benched from headers only. A usage-limit sentence from the
    Claude CLI (even one with a precise reset) must keep the 15s cooldown: a
    misparse there would take out the one provider every chain ends on."""
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

    assert provider_reset.get_provider_reset_until("anthropic") is None
    assert provider_reset.all_provider_resets() == {}


# ===================================================== review round 1 (PR #247)
#
# Blocker 1: relative durations in generic advice are not reset reports.
# Blocker 2: hooks/direct_executor.execute_chain is a second dispatch path.
# Plus: "tomorrow at", the 24h text cap, anthropic header-only, last provider in
# the chain, `provider unban`, and one record per failure.


class TestTextIsOnlyReadWhereItIsAReport:
    # Each of these mentions a limit AND a "retry in N" shape, but as advice,
    # documentation, or an unrelated number. None may bench a provider.
    INCIDENTAL = [
        # The exact message from the review.
        "429 Too Many Requests. For details on rate limits see our docs; in "
        "general you should retry in 3 days if you keep seeing this.",
        "Rate limit exceeded. You may retry in 2 days after upgrading your plan.",
        "Quota exceeded for this project. Our docs recommend you retry in 5 days "
        "for batch jobs, but the limit usually clears sooner.",
        "HTTP 429. If this keeps happening, contact support, or generally wait and "
        "retry in 3 days before filing a ticket.",
        "Too many requests. Clients that sleep and retry in 7 days are not supported; "
        "use exponential backoff.",
        "Rate limited (429). See https://docs.example.com/limits -- a limit resets in "
        "3 days for free tier accounts, paid tiers differ.",
        # "you can" is advice, not a report.
        "429 Too Many Requests. This is a rate limit. You can retry in 2 hours on "
        "the free plan, upgrade for faster access.",
        "Rate limit hit. You can retry in 3 days for safety, but it usually clears "
        "within an hour.",
    ]

    @pytest.mark.parametrize("message", INCIDENTAL)
    def test_incidental_advice_is_not_a_reset(self, message):
        assert provider_reset.parse_reset_epoch(message, now=time.time()) is None

    @pytest.mark.parametrize("message", INCIDENTAL)
    def test_incidental_advice_leaves_the_provider_usable(self, message):
        assert provider_reset.note_provider_error("codex", RuntimeError(message)) is None
        assert HealthTracker().is_healthy("codex")
        assert provider_reset.all_provider_resets() == {}

    @pytest.mark.parametrize(
        "message, seconds",
        [
            # Codex / CLI shape: the verb opens the sentence right after the limit.
            ("You've hit your usage limit. Try again in 3 hours.", 3 * HOUR),
            ("You've hit your usage limit. Upgrade to Pro (https://openai.com/chatgpt/"
             "pricing), or try again in 5h 30m.", 5 * HOUR + 30 * 60),
            ("Rate limit reached for gpt-4o in organization org-x. Please try again in 1m30s.", 90),
            ("Your usage limit resets in 2 hours.", 2 * HOUR),
            ("Quota exhausted; it will reset in 90 minutes.", 90 * 60),
        ],
    )
    def test_genuine_reports_still_parse(self, message, seconds):
        now = 1_000_000.0
        assert provider_reset.parse_reset_epoch(message, now=now) == now + seconds

    def test_text_derived_bench_is_capped_at_24h(self):
        now = 1_000_000.0
        got = provider_reset.parse_reset_epoch(
            "You've hit your usage limit. Try again in 4 days 7 hours.", now=now
        )
        assert got == now + provider_reset.MAX_TEXT_SKIP_SECONDS == now + 24 * HOUR

    def test_absolute_timestamp_is_not_subject_to_the_text_cap(self):
        now = time.time()
        reset = now + 3 * 24 * HOUR
        got = provider_reset.parse_reset_epoch(
            f"Usage limit reached. Try again at {_local_iso(reset)}.", now=now
        )
        assert got == pytest.approx(reset, abs=60)

    def test_header_is_not_subject_to_the_text_cap(self):
        now = 1_000_000.0
        assert provider_reset.parse_reset_epoch(
            "429", headers={"Retry-After": str(3 * 24 * HOUR)}, now=now
        ) == now + 3 * 24 * HOUR

    def test_header_wins_over_incidental_text(self):
        now = 1_000_000.0
        got = provider_reset.parse_reset_epoch(
            "Rate limit hit, you should retry in 3 days.",
            headers={"Retry-After": "7200"}, now=now,
        )
        assert got == now + 7200


class TestCodexDatedWording:
    """Codex prints the reset as a dated clock time ("try again at Oct 6th, 2026
    10:34 PM."). Wording as publicly reported, not captured from a live run."""

    NOW = datetime(2026, 10, 3, 12, 0).timestamp()

    @pytest.mark.parametrize(
        "message, expected",
        [
            ("You've hit your usage limit. Upgrade to Pro (https://openai.com/chatgpt/pricing), "
             "or try again at Oct 6th, 2026 10:34 PM.", datetime(2026, 10, 6, 22, 34)),
            ("You've hit your usage limit. Try again at Oct 9th, 2026 3:31 PM.",
             datetime(2026, 10, 9, 15, 31)),
            ("You've hit your usage limit. Try again at October 5, 2026 9:05 AM.",
             datetime(2026, 10, 5, 9, 5)),
            # No year: the next such date.
            ("You've hit your usage limit. Try again at Oct 7 6:39 AM.", datetime(2026, 10, 7, 6, 39)),
        ],
    )
    def test_dated_reset_is_parsed(self, message, expected):
        got = provider_reset.parse_reset_epoch(message, now=self.NOW)
        assert datetime.fromtimestamp(got) == expected

    def test_dated_reset_is_absolute_so_not_capped_at_24h(self):
        got = provider_reset.parse_reset_epoch(
            "You've hit your usage limit. Try again at Oct 6th, 2026 10:34 PM.", now=self.NOW
        )
        assert got - self.NOW > provider_reset.MAX_TEXT_SKIP_SECONDS

    def test_a_dated_reset_far_ahead_is_capped_at_the_header_ceiling(self):
        got = provider_reset.parse_reset_epoch(
            "You've hit your usage limit. Try again at Apr 12th, 2027 3:31 PM.", now=self.NOW
        )
        assert got == self.NOW + provider_reset.MAX_SKIP_SECONDS

    def test_a_non_month_word_is_not_a_date(self):
        assert provider_reset.parse_reset_epoch(
            "You've hit your usage limit. Try again at Foo 6th, 2026 10:34 PM.", now=self.NOW
        ) is None

    def test_dated_reset_in_the_past_is_expired(self):
        assert provider_reset.parse_reset_epoch(
            "You've hit your usage limit. Try again at Oct 1st, 2026 10:34 PM.", now=self.NOW
        ) is None


class TestTomorrowQualifier:
    def test_tomorrow_before_the_clock_time_means_the_next_calendar_day(self):
        # 09:00 now; a bare "at 18:39" would be today, "tomorrow at 18:39" is not.
        now = datetime(2026, 10, 3, 9, 0).timestamp()
        got = provider_reset.parse_reset_epoch(
            "Usage limit reached. Try again tomorrow at 6:39 PM.", now=now
        )
        # Tomorrow 18:39 is 33h39m away; text-derived benches are capped at 24h.
        assert got == now + provider_reset.MAX_TEXT_SKIP_SECONDS
        assert got > datetime(2026, 10, 3, 18, 39).timestamp(), "must not resolve to today"

    def test_tomorrow_morning_after_a_late_night_limit(self):
        now = datetime(2026, 10, 3, 22, 0).timestamp()
        got = provider_reset.parse_reset_epoch(
            "You've hit your usage limit. Try again tomorrow at 06:39.", now=now
        )
        assert datetime.fromtimestamp(got) == datetime(2026, 10, 4, 6, 39)

    def test_trailing_tomorrow_is_honoured_too(self):
        now = datetime(2026, 10, 3, 7, 0).timestamp()
        got = provider_reset.parse_reset_epoch(
            "Usage limit reached. Resets at 8:00 AM tomorrow.", now=now
        )
        assert got > datetime(2026, 10, 3, 8, 0).timestamp(), "must not resolve to today"


class TestRealCapturedCodexQuotaMessage:
    """The actual error line a ChatGPT Plus `codex exec` run emitted on hitting
    its usage limit (captured 2026-10-03 in codex_agent/raw transcripts during
    the routing experiment that motivated #247/#251 — only the error line is
    quoted here, never any task content).

    This wording joins the credits-purchase clause to "try again" with a bare
    "or" and NO comma before it ("...more credits or try again at 11:33 PM."),
    unlike every synthetic fixture elsewhere in this file, which has a comma
    before "or" (see test_genuine_reports_still_parse's gpt pricing case). That
    comma-less join put the whole clause in `_from_message`'s `lead`, which
    `_CLAUSE_LEADS` could not match -- the real message did not parse at all
    before the ``lead.endswith(" or")`` fix.
    """

    REAL_MESSAGE = (
        "You've hit your usage limit. Upgrade to Pro "
        "(https://chatgpt.com/explore/pro), visit "
        "https://chatgpt.com/codex/settings/usage to purchase more credits "
        "or try again at 11:33 PM."
    )

    def test_real_message_parses_same_day(self):
        # 20:00 now; 11:33 PM has not happened yet today.
        now = datetime(2026, 10, 3, 20, 0).timestamp()
        got = provider_reset.parse_reset_epoch(self.REAL_MESSAGE, now=now)
        assert got is not None, "the real captured quota message failed to parse"
        assert datetime.fromtimestamp(got) == datetime(2026, 10, 3, 23, 33)

    def test_real_message_parses_next_day_after_the_clock_time_has_passed(self):
        # 00:30 now; 11:33 PM already happened today, so it means tomorrow's.
        now = datetime(2026, 10, 4, 0, 30).timestamp()
        got = provider_reset.parse_reset_epoch(self.REAL_MESSAGE, now=now)
        assert got is not None
        assert datetime.fromtimestamp(got) == datetime(2026, 10, 4, 23, 33)

    def test_real_message_is_capped_at_24h(self):
        # A bare clock time is a `text`-sourced reset, capped at 24h, not the
        # 7-day header ceiling -- it is never more than a few hours out here.
        now = datetime(2026, 10, 3, 20, 0).timestamp()
        got = provider_reset.parse_reset_epoch(self.REAL_MESSAGE, now=now)
        assert got - now < provider_reset.MAX_TEXT_SKIP_SECONDS == 24 * HOUR

    def test_real_message_benches_codex_not_anthropic(self):
        now = time.time()
        msg = self.REAL_MESSAGE.replace(
            "11:33 PM", (datetime.fromtimestamp(now) + timedelta(hours=1)).strftime("%I:%M %p")
        )
        assert provider_reset.note_provider_error("codex", RuntimeError(msg)) is not None
        assert not HealthTracker().is_healthy("codex")
        assert HealthTracker().is_healthy("anthropic")


class TestWhoMayBeBenched:
    def test_anthropic_is_never_benched_from_text(self):
        msg = "You've hit your usage limit. Try again at " + _local_iso(time.time() + 5 * HOUR)
        assert provider_reset.note_provider_error("anthropic", RuntimeError(msg)) is None
        assert HealthTracker().is_healthy("anthropic")

    def test_anthropic_is_benched_from_its_reset_header(self):
        stamp = (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        until = provider_reset.note_provider_error(
            "anthropic", RuntimeError("rate_limit_error"),
            {"anthropic-ratelimit-tokens-reset": stamp},
        )
        assert until is not None
        assert not HealthTracker().is_healthy("anthropic")

    def test_last_provider_in_the_chain_is_not_benched(self):
        msg = "You've hit your usage limit. Try again in 5 hours."
        assert provider_reset.note_provider_error(
            "codex", RuntimeError(msg), alternatives=["codex"]
        ) is None
        assert HealthTracker().is_healthy("codex")

    def test_provider_with_a_usable_alternative_is_benched(self):
        msg = "You've hit your usage limit. Try again in 5 hours."
        assert provider_reset.note_provider_error(
            "codex", RuntimeError(msg), alternatives=["codex", "gemini"]
        ) is not None
        assert not HealthTracker().is_healthy("codex")

    def test_alternative_that_is_itself_benched_does_not_count(self):
        now = time.time()
        provider_reset.record_provider_reset("gemini", now + 5 * HOUR, now=now)
        msg = "You've hit your usage limit. Try again in 5 hours."
        assert provider_reset.note_provider_error(
            "codex", RuntimeError(msg), alternatives=["codex", "gemini"]
        ) is None
        assert HealthTracker().is_healthy("codex")

    def test_header_reset_gets_the_same_last_provider_protection(self):
        assert provider_reset.note_provider_error(
            "openai", RuntimeError("429"), {"Retry-After": "7200"}, alternatives=["openai"]
        ) is None

    def test_a_failure_is_recorded_once(self):
        exc = RuntimeError("You've hit your usage limit. Try again in 5 hours.")
        with patch.object(provider_reset, "record_provider_reset", return_value=True) as rec:
            provider_reset.note_provider_error("codex", exc, alternatives=["codex", "gemini"])
            provider_reset.note_provider_error("codex", exc, alternatives=["codex", "gemini"])
        assert rec.call_count == 1


@pytest.mark.asyncio
async def test_cli_failure_is_recorded_once_across_the_inner_and_generic_handler(
    codex_first, monkeypatch
):
    reset = time.time() + 5 * HOUR
    limit_msg = f"You've hit your usage limit. Try again at {_local_iso(reset)}."
    calls = []
    real = provider_reset.record_provider_reset

    def _spy(*a, **k):
        calls.append(a[0])
        return real(*a, **k)

    monkeypatch.setattr(provider_reset, "record_provider_reset", _spy)

    async def _other_providers_fail(**kwargs):
        raise RuntimeError("Simulated litellm failure")

    with patch("litellm.acompletion", side_effect=_other_providers_fail), \
         patch("llm_router.router.run_codex", return_value=_codex_failure(limit_msg)):
        with pytest.raises(Exception):
            await _route()
    assert calls.count("codex") == 1


# --------------------------------------------------- execute_chain (Blocker 2)


class TestExecuteChainHonoursResets:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "g-test")
        monkeypatch.setenv("OPENAI_API_KEY", "o-test")
        monkeypatch.setattr(
            "llm_router.hooks.direct_executor._paid_budget_exhausted", lambda p: False
        )
        monkeypatch.setattr(
            "llm_router.hooks.direct_executor.available_ollama_models",
            lambda timeout=0.5: {"qwen3.5", "qwen3.5:latest"},
        )
        monkeypatch.setattr("llm_router.hooks.direct_executor.ollama_is_alive", lambda **k: True)

    @pytest.mark.parametrize("provider, fn", [("gemini", "call_gemini"), ("openai", "call_openai")])
    def test_benched_provider_is_skipped(self, provider, fn):
        from llm_router.hooks.direct_executor import ModelSpec, execute_chain

        now = time.time()
        provider_reset.record_provider_reset(provider, now + 5 * HOUR, now=now)
        chain = [ModelSpec(provider, "m-1"), ModelSpec("ollama", "qwen3.5")]
        with patch(f"llm_router.hooks.direct_executor.{fn}",
                   return_value=("the benched provider answered", {})) as benched, \
             patch("llm_router.hooks.direct_executor.call_ollama",
                   return_value=("the fallback answered", {})):
            result = execute_chain("hello", chain, "query")
        assert benched.call_count == 0
        assert result is not None and result.model.provider == "ollama"

    def test_unbenched_provider_is_still_called(self):
        from llm_router.hooks.direct_executor import ModelSpec, execute_chain

        chain = [ModelSpec("gemini", "gemini-2.5-flash")]
        with patch("llm_router.hooks.direct_executor.call_gemini",
                   return_value=("gemini answered fine", {})) as gem:
            result = execute_chain("hello", chain, "query")
        assert gem.call_count == 1 and result is not None

    def test_http_429_with_retry_after_benches_the_provider_for_next_time(self):
        """call_gemini swallows its exception; the 429's headers must still reach
        provider_reset, and the second chain must skip the provider entirely."""
        import io
        import urllib.error
        from email.message import Message

        from llm_router.hooks.direct_executor import ModelSpec, execute_chain

        hdrs = Message()
        hdrs["Retry-After"] = "7200"
        err = urllib.error.HTTPError(
            "https://generativelanguage.googleapis.com/x", 429, "Too Many Requests",
            hdrs, io.BytesIO(b'{"error": "quota"}'),
        )
        chain = [ModelSpec("gemini", "gemini-2.5-flash"), ModelSpec("ollama", "qwen3.5")]
        with patch("urllib.request.urlopen", side_effect=err) as urlopen, \
             patch("llm_router.hooks.direct_executor.call_ollama",
                   return_value=("the fallback answered", {})):
            first = execute_chain("hello", chain, "query")
            assert first is not None and first.model.provider == "ollama"
            assert urlopen.call_count == 1
            assert provider_reset.get_provider_reset_until("gemini") == pytest.approx(
                time.time() + 7200, abs=10
            )
            second = execute_chain("hello", chain, "query")
        assert second is not None
        assert urlopen.call_count == 1, "the benched provider must not be called again"

    def test_a_provider_call_that_raises_with_a_usage_limit_report_is_benched(self):
        from llm_router.hooks.direct_executor import ModelSpec, execute_chain

        boom = RuntimeError("You've hit your usage limit. Try again in 5 hours.")
        chain = [ModelSpec("openai", "gpt-4o-mini"), ModelSpec("ollama", "qwen3.5")]
        with patch("llm_router.hooks.direct_executor.call_openai", side_effect=boom), \
             patch("llm_router.hooks.direct_executor.call_ollama",
                   return_value=("the fallback answered", {})):
            assert execute_chain("hello", chain, "query") is not None
        assert provider_reset.get_provider_reset_until("openai") == pytest.approx(
            time.time() + 5 * HOUR, abs=10
        )

    def test_a_raising_provider_after_a_failed_one_is_not_benched(self):
        """Same guard on the raising path: only providers LATER in the chain count."""
        from llm_router.hooks.direct_executor import ModelSpec, execute_chain

        boom = RuntimeError("You've hit your usage limit. Try again in 5 hours.")
        chain = [ModelSpec("gemini", "gemini-2.5-flash"), ModelSpec("openai", "gpt-4o-mini")]
        with patch("llm_router.hooks.direct_executor.call_gemini", return_value=(None, {})), \
             patch("llm_router.hooks.direct_executor.call_openai", side_effect=boom):
            assert execute_chain("hello", chain, "query") is None
        assert provider_reset.get_provider_reset_until("openai") is None

    def test_providers_that_already_failed_this_request_are_not_alternatives(self):
        """[gemini, openai]: gemini fails (500), then openai reports a usage limit.
        Benching openai would leave only the provider that just failed."""
        import io
        import urllib.error
        from email.message import Message

        from llm_router.hooks.direct_executor import ModelSpec, execute_chain

        def _urlopen(req, timeout=None):
            if "googleapis" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 500, "boom", Message(), io.BytesIO(b""))
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", Message(),
                io.BytesIO(b"You've hit your usage limit. Try again in 5 hours."),
            )

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            chain = [ModelSpec("gemini", "gemini-2.5-flash"), ModelSpec("openai", "gpt-4o-mini")]
            assert execute_chain("hello", chain, "query") is None
        assert provider_reset.all_provider_resets() == {}

    def test_a_provider_with_a_later_alternative_in_the_chain_is_benched(self):
        import io
        import urllib.error
        from email.message import Message

        from llm_router.hooks.direct_executor import ModelSpec, execute_chain

        def _urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", Message(),
                io.BytesIO(b"You've hit your usage limit. Try again in 5 hours."),
            )

        chain = [ModelSpec("openai", "gpt-4o-mini"), ModelSpec("ollama", "qwen3.5")]
        with patch("urllib.request.urlopen", side_effect=_urlopen), \
             patch("llm_router.hooks.direct_executor.call_ollama", return_value=("fallback ok", {})):
            assert execute_chain("hello", chain, "query") is not None
        assert provider_reset.get_provider_reset_until("openai") is not None

    def test_failure_reason_logging_for_gemini_is_unchanged(self):
        """The error is stashed for provider_reset; the existing reason an
        attempt_log reader sees ("empty response") must not change."""
        from llm_router.hooks import direct_executor as de

        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            assert de.call_gemini("hi") == (None, {})
        assert "gemini/gemini-2.5-flash" not in de._LAST_CALL_FAILURE
        de._LAST_CALL_ERROR.pop("gemini/gemini-2.5-flash", None)

    def test_a_chain_of_one_is_not_benched_by_its_own_failure(self):
        import io
        import urllib.error
        from email.message import Message

        from llm_router.hooks.direct_executor import ModelSpec, execute_chain

        hdrs = Message()
        hdrs["Retry-After"] = "7200"
        err = urllib.error.HTTPError("https://x", 429, "Too Many Requests", hdrs, io.BytesIO(b""))
        with patch("urllib.request.urlopen", side_effect=err):
            assert execute_chain("hello", [ModelSpec("openai", "gpt-4o-mini")], "query") is None
        assert provider_reset.all_provider_resets() == {}


# ------------------------------------------------------ provider list / unban


class TestProviderCommand:
    def _bench(self, *names):
        now = time.time()
        for n in names:
            provider_reset.record_provider_reset(n, now + 5 * HOUR, now=now)

    def test_unban_clears_one_provider_only(self, capsys):
        from llm_router.commands.provider import cmd_provider

        self._bench("codex", "gemini")
        cmd_provider(["unban", "codex"])
        assert "Cleared: codex" in capsys.readouterr().out
        assert HealthTracker().is_healthy("codex")
        assert not HealthTracker().is_healthy("gemini")

    def test_unban_all(self, capsys):
        from llm_router.commands.provider import cmd_provider

        self._bench("codex", "gemini")
        cmd_provider(["unban", "--all"])
        assert provider_reset.all_provider_resets() == {}
        assert "codex" in capsys.readouterr().out

    def test_unban_unknown_provider_says_so(self, capsys):
        from llm_router.commands.provider import cmd_provider

        self._bench("codex")
        cmd_provider(["unban", "nope"])
        assert "Nothing to clear" in capsys.readouterr().out
        assert not HealthTracker().is_healthy("codex")

    def test_unban_with_no_state_file_is_quiet(self, capsys):
        from llm_router.commands.provider import cmd_provider

        cmd_provider(["unban", "codex"])
        assert "Nothing to clear" in capsys.readouterr().out

    def test_list_shows_benched_providers_and_when(self, capsys):
        from llm_router.commands.provider import cmd_provider

        self._bench("codex")
        cmd_provider(["list"])
        out = capsys.readouterr().out
        assert "codex: unavailable until" in out and "in 4h" in out

    def test_list_when_nothing_is_benched(self, capsys):
        from llm_router.commands.provider import cmd_provider

        cmd_provider(["list"])
        assert "No provider is benched" in capsys.readouterr().out

    def test_cli_dispatches_provider(self, monkeypatch, capsys):
        from llm_router import cli

        self._bench("codex")
        monkeypatch.setattr(sys, "argv", ["llm-router", "provider", "list"])
        cli.main()
        assert "codex: unavailable until" in capsys.readouterr().out

    def test_unban_missing_name_exits_2(self):
        from llm_router.commands.provider import cmd_provider

        with pytest.raises(SystemExit) as e:
            cmd_provider(["unban"])
        assert e.value.code == 2
