"""PR2 — the benchmark background fetch is opt-in, not on by default.

North Star #5 (local-first): ``_maybe_refresh_benchmarks_bg`` used to spawn a
detached fetch from huggingface.co / github / litellm whenever
``~/.llm-router/benchmarks.json`` was missing or more than
``LLM_ROUTER_BENCHMARK_TTL_DAYS`` old — with no opt-in. That is a network call
a user never asked for, made from every session that happens to have a stale
or absent benchmark file.

This pins two things:

1. The fetch itself is gated on ``LLM_ROUTER_AUTO_BENCHMARK_FETCH=1``
   (default off) — mirrors ``test_okf_autoindex_on_session_start.py``'s
   pattern for the sibling background job.
2. Now that the fetch is off by default, ``get_benchmark_data()`` must not
   let a stale INSTALLED copy (fetched once, long ago, before the opt-in
   default existed) permanently shadow a newer BUNDLED copy shipped in a
   later release. It picks whichever file is actually newer by
   ``generated_at``, falling back to the documented "prefer installed when
   both are unknown" rule only when neither side can be dated.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import llm_router.benchmarks as bm

HOOK = Path(__file__).resolve().parent.parent / "src/llm_router/hooks/session-start.py"

spec = importlib.util.spec_from_file_location("_bench_fetch_sshook", HOOK)
sshook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sshook)


class _FakeProc:
    pid = 1234


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def _spawned(monkeypatch):
    """Capture subprocess.Popen calls and make `which uv` succeed."""
    calls = []
    monkeypatch.setattr(
        sshook.subprocess, "Popen",
        lambda *a, **k: calls.append((a, k)) or _FakeProc(),
    )
    monkeypatch.setattr(
        sshook.subprocess, "run",
        lambda *a, **k: _FakeCompleted("/usr/bin/uv"),
    )
    return calls


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setattr(sshook, "_state_dir", lambda: str(d))
    return d


# ---------------------------------------------------------------------------
# 1. The fetch itself is opt-in.
# ---------------------------------------------------------------------------

def test_env_unset_does_not_spawn_a_fetch(state_dir, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_AUTO_BENCHMARK_FETCH", raising=False)
    calls = _spawned(monkeypatch)
    sshook._maybe_refresh_benchmarks_bg()
    assert not calls, (
        "no ~/.llm-router/benchmarks.json and no opt-in — this must not reach "
        "out to huggingface.co / github / litellm without consent"
    )


def test_env_explicitly_off_does_not_spawn_a_fetch(state_dir, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_AUTO_BENCHMARK_FETCH", "0")
    calls = _spawned(monkeypatch)
    sshook._maybe_refresh_benchmarks_bg()
    assert not calls


def test_env_on_spawns_a_fetch_when_stale(state_dir, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_AUTO_BENCHMARK_FETCH", "1")
    calls = _spawned(monkeypatch)
    sshook._maybe_refresh_benchmarks_bg()
    assert calls, "opted in, and no local file exists (stale) — a fetch must start"
    argv = calls[0][0][0]
    assert "generate_benchmarks_json" in argv[-1]


def test_env_on_but_fresh_file_does_not_spawn_a_fetch(state_dir, monkeypatch):
    from datetime import datetime, timezone
    import json

    state_dir.mkdir(parents=True, exist_ok=True)
    fresh = (state_dir / "benchmarks.json")
    fresh.write_text(json.dumps({
        "version": 9,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }))
    monkeypatch.setenv("LLM_ROUTER_AUTO_BENCHMARK_FETCH", "1")
    calls = _spawned(monkeypatch)
    sshook._maybe_refresh_benchmarks_bg()
    assert not calls, "opted in, but the file is fresh — nothing to refresh"


# ---------------------------------------------------------------------------
# 2. get_benchmark_data() prefers whichever copy is actually newer.
# ---------------------------------------------------------------------------

def _iso(days_ago: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _write(path: Path, version: int, generated_at) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {"version": version, "marker": str(path)}
    if generated_at is not None:
        payload["generated_at"] = generated_at
    path.write_text(json.dumps(payload))


@pytest.fixture
def benchmark_files(tmp_path, monkeypatch):
    installed = tmp_path / "installed" / "benchmarks.json"
    bundled = tmp_path / "bundled" / "benchmarks.json"
    monkeypatch.setattr(bm, "_installed", lambda: installed)
    monkeypatch.setattr(bm, "_BUNDLED", bundled)
    bm._cache = None
    bm._cache_loaded = False
    yield installed, bundled
    bm._cache = None
    bm._cache_loaded = False


def test_stale_installed_and_newer_bundled_returns_bundled(benchmark_files):
    installed, bundled = benchmark_files
    _write(installed, version=1, generated_at=_iso(days_ago=400))
    _write(bundled, version=2, generated_at=_iso(days_ago=1))
    data = bm.get_benchmark_data()
    assert data["marker"] == str(bundled), (
        "the installed copy is 400 days stale; a bundled copy from yesterday "
        "must win now that the background refetch is opt-in"
    )


def test_newer_installed_returns_installed(benchmark_files):
    installed, bundled = benchmark_files
    _write(installed, version=3, generated_at=_iso(days_ago=1))
    _write(bundled, version=2, generated_at=_iso(days_ago=400))
    data = bm.get_benchmark_data()
    assert data["marker"] == str(installed), (
        "the installed copy is genuinely newer and must still win"
    )


def test_unknown_installed_date_falls_back_to_the_known_bundled_date(benchmark_files):
    installed, bundled = benchmark_files
    _write(installed, version=99, generated_at=None)   # unknown date
    _write(bundled, version=1, generated_at=_iso(days_ago=1))
    data = bm.get_benchmark_data()
    assert data["marker"] == str(bundled), (
        "an unknown installed date must not win by default over a known "
        "bundled date — unknown is not the favourable answer"
    )


def test_unknown_bundled_date_falls_back_to_the_known_installed_date(benchmark_files):
    installed, bundled = benchmark_files
    _write(installed, version=1, generated_at=_iso(days_ago=1))
    _write(bundled, version=99, generated_at=None)   # unknown date
    data = bm.get_benchmark_data()
    assert data["marker"] == str(installed), (
        "an unknown bundled date must not win by default over a known "
        "installed date"
    )


def test_both_dates_unknown_keeps_the_documented_installed_first_fallback(benchmark_files):
    installed, bundled = benchmark_files
    _write(installed, version=1, generated_at=None)
    _write(bundled, version=99, generated_at=None)
    data = bm.get_benchmark_data()
    assert data["marker"] == str(installed), (
        "when neither side can be dated, the documented fallback keeps the "
        "historical installed-first preference"
    )
