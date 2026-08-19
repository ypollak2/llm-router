"""Codex availability self-heal: ``is_codex_available()`` re-probes on
False cache so a Codex binary installed *after* the llm_router MCP daemon
started becomes visible without an MCP restart.

Production failure mode that motivated this fix:

1. User installs llm-routing and starts the MCP daemon.
2. Codex isn't on disk yet → import-time probe caches
   ``_CODEX_BINARY_PATH = None`` and never re-checks.
3. User installs Codex.app or the npm plugin minutes later.
4. ``is_codex_available()`` keeps returning False for the daemon's
   lifetime; every routing decision skips position #1 of the chain
   (Codex) and falls through to a paid API model.
5. The user pays for Gemini calls that should have been free Codex
   subscription calls. Self-healing avoids this entirely.

Pins:

1. **Positive cache is trusted.** A previously-found binary path
   short-circuits to True without touching the filesystem.
2. **Negative cache re-probes.** A None cache plus a stale last-probe
   timestamp triggers ``find_codex_binary()`` on the next call.
3. **Rate limit.** Re-probes happen at most every
   ``_PROBE_INTERVAL_SEC`` (60s by default) so the worst-case
   filesystem-hit rate is bounded even on slow / network-mounted homes.
4. **Plugin re-check.** When a re-probe finds the binary, the plugin
   availability is recomputed too — they often land together.
5. **Async safety.** The re-probe is synchronous because the original
   blocking-I/O concern is bounded by the rate limit. Tests assert
   the call returns quickly so callers in async contexts stay safe.
"""
from __future__ import annotations

import time

import pytest

from llm_router import codex_agent
from llm_router.codex_agent import (
    _PROBE_INTERVAL_SEC,
    _reset_codex_cache_for_tests,
    is_codex_available,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    _reset_codex_cache_for_tests()
    yield
    _reset_codex_cache_for_tests()


# ── 1. Positive cache is trusted ─────────────────────────────────────────────


def test_positive_cache_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the cache already holds a path, the function returns True
    without touching the filesystem."""
    monkeypatch.setattr(codex_agent, "_CODEX_BINARY_PATH", "/fake/codex")

    probe_calls = 0

    def _probe() -> str | None:
        nonlocal probe_calls
        probe_calls += 1
        return None

    monkeypatch.setattr(codex_agent, "find_codex_binary", _probe)
    assert is_codex_available() is True
    assert probe_calls == 0  # never probed


# ── 2. Negative cache re-probes ──────────────────────────────────────────────


def test_negative_cache_reprobes_and_finds_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A None cache + stale timestamp → re-probe runs. If the binary
    now exists, the cache updates and the function returns True."""
    # Force the conditions for a re-probe.
    monkeypatch.setattr(codex_agent, "_CODEX_BINARY_PATH", None)
    monkeypatch.setattr(codex_agent, "_LAST_PROBE_TS", 0.0)

    monkeypatch.setattr(
        codex_agent, "find_codex_binary", lambda: "/Applications/Codex.app/.../codex"
    )

    assert is_codex_available() is True
    # Cache was updated in-place.
    assert codex_agent._CODEX_BINARY_PATH == "/Applications/Codex.app/.../codex"


def test_negative_cache_reprobes_and_stays_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-probe runs but find_codex_binary still returns None → the
    function returns False and the cache remains None."""
    monkeypatch.setattr(codex_agent, "_CODEX_BINARY_PATH", None)
    monkeypatch.setattr(codex_agent, "_LAST_PROBE_TS", 0.0)
    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: None)

    assert is_codex_available() is False
    assert codex_agent._CODEX_BINARY_PATH is None


# ── 3. Rate limit ────────────────────────────────────────────────────────────


def test_rate_limit_skips_reprobe_within_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful re-probe at t=0 sets ``_LAST_PROBE_TS``. A second
    call within ``_PROBE_INTERVAL_SEC`` must NOT call find_codex_binary
    again — it returns the cached False directly."""
    monkeypatch.setattr(codex_agent, "_CODEX_BINARY_PATH", None)
    monkeypatch.setattr(codex_agent, "_LAST_PROBE_TS", 0.0)

    probe_calls = 0

    def _probe() -> str | None:
        nonlocal probe_calls
        probe_calls += 1
        return None

    monkeypatch.setattr(codex_agent, "find_codex_binary", _probe)
    # Start fake time past the interval so the first call genuinely
    # probes; subsequent calls are "0.1s later" — well under 60s.
    fake_time = [100.0]
    monkeypatch.setattr(codex_agent.time, "monotonic", lambda: fake_time[0])

    is_codex_available()  # probes, sets last=100.0
    fake_time[0] = 100.1
    is_codex_available()  # within interval — no probe
    fake_time[0] = 130.0
    is_codex_available()  # still within 60s — no probe

    assert probe_calls == 1


def test_rate_limit_allows_reprobe_after_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once ``_PROBE_INTERVAL_SEC`` has elapsed, a second call probes
    again. This is the self-heal: the user installed Codex during the
    interval and the next call after the floor expires picks it up."""
    monkeypatch.setattr(codex_agent, "_CODEX_BINARY_PATH", None)
    monkeypatch.setattr(codex_agent, "_LAST_PROBE_TS", 0.0)

    probe_returns = [None, "/Applications/Codex.app/.../codex"]
    probe_calls = 0

    def _probe() -> str | None:
        nonlocal probe_calls
        result = probe_returns[probe_calls]
        probe_calls += 1
        return result

    monkeypatch.setattr(codex_agent, "find_codex_binary", _probe)
    # Start past the interval so the first call probes.
    fake_time = [100.0]
    monkeypatch.setattr(codex_agent.time, "monotonic", lambda: fake_time[0])

    assert is_codex_available() is False
    assert probe_calls == 1

    # Advance time past the interval.
    fake_time[0] = 100.0 + _PROBE_INTERVAL_SEC + 0.5
    assert is_codex_available() is True
    assert probe_calls == 2


# ── 4. Plugin re-check on binary discovery ──────────────────────────────────


def test_plugin_recheck_runs_when_binary_just_appeared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a re-probe finds the binary, the plugin check runs too —
    the two often install together (e.g. via the npm package)."""
    monkeypatch.setattr(codex_agent, "_CODEX_BINARY_PATH", None)
    monkeypatch.setattr(codex_agent, "_LAST_PROBE_TS", 0.0)
    monkeypatch.setattr(codex_agent, "_CODEX_PLUGIN_AVAILABLE", False)

    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: "/fake/codex")
    plugin_called = []
    monkeypatch.setattr(
        codex_agent,
        "_check_codex_plugin",
        lambda: plugin_called.append(True) or True,
    )

    assert is_codex_available() is True
    assert plugin_called == [True]
    assert codex_agent._CODEX_PLUGIN_AVAILABLE is True


def test_plugin_recheck_does_not_run_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the binary probe returns None, the plugin check does NOT
    run — no point recomputing it when the primary signal is still
    negative."""
    monkeypatch.setattr(codex_agent, "_CODEX_BINARY_PATH", None)
    monkeypatch.setattr(codex_agent, "_LAST_PROBE_TS", 0.0)

    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: None)
    plugin_calls = 0

    def _check_plugin() -> bool:
        nonlocal plugin_calls
        plugin_calls += 1
        return True

    monkeypatch.setattr(codex_agent, "_check_codex_plugin", _check_plugin)
    assert is_codex_available() is False
    assert plugin_calls == 0


def test_plugin_recheck_failure_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken plugin check must NOT mask a successful binary find —
    fail-open on the secondary signal so routing through Codex still
    happens."""
    monkeypatch.setattr(codex_agent, "_CODEX_BINARY_PATH", None)
    monkeypatch.setattr(codex_agent, "_LAST_PROBE_TS", 0.0)

    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: "/fake/codex")

    def _boom() -> bool:
        raise RuntimeError("plugin probe blew up")

    monkeypatch.setattr(codex_agent, "_check_codex_plugin", _boom)
    assert is_codex_available() is True


# ── 5. Async safety: the re-probe is fast enough ─────────────────────────────


def test_reprobe_returns_quickly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The re-probe is bounded by find_codex_binary's speed. On local
    disk this is well under 10ms; the test asserts a generous 100ms
    budget so async callers stay responsive."""
    monkeypatch.setattr(codex_agent, "_CODEX_BINARY_PATH", None)
    monkeypatch.setattr(codex_agent, "_LAST_PROBE_TS", 0.0)
    # Don't monkeypatch find_codex_binary — exercise the real probe.

    start = time.monotonic()
    is_codex_available()
    elapsed_ms = (time.monotonic() - start) * 1000
    assert elapsed_ms < 100, f"re-probe took {elapsed_ms:.1f}ms"
