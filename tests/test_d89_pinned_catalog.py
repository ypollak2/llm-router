from __future__ import annotations

import importlib.util
import logging
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from llm_router import benchmark_fetcher as bf


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def read(self) -> bytes:
        return self.data


def test_fetch_pinned_match(monkeypatch: pytest.MonkeyPatch) -> None:
    data = b'{"version": 1}'
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse(data))
    assert bf.fetch_pinned("https://example.test/catalog", bf.sha256_hex(data)) == data


def test_fetch_pinned_mismatch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    data = b'{"version": 1}'
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse(data))
    with caplog.at_level(logging.WARNING, logger="llm_router.benchmark_fetcher"):
        assert bf.fetch_pinned("https://example.test/catalog", "0" * 64) is None
    records = [r for r in caplog.records if r.name == "llm_router.benchmark_fetcher"]
    assert len(records) == 1
    assert records[0].getMessage() == (
        "catalog rejected: sha256 mismatch for https://example.test/catalog "
        f"(expected {'0' * 12}..., got {bf.sha256_hex(data)[:12]}...)"
    )


@pytest.mark.parametrize("matches", [True, False])
def test_catalog_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matches: bool,
) -> None:
    data = b'{"version": 1}'
    digest = bf.sha256_hex(data) if matches else "0" * 64
    monkeypatch.setattr(
        bf, "load_pins", lambda: {"benchmarks": {"tag": "vtest", "sha256": digest}},
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse(data))
    output = tmp_path / "benchmarks.json"
    assert bf.fetch_pinned_catalog(output) is matches
    if matches:
        assert output.read_bytes() == data
    else:
        assert not output.exists()
        assert list(tmp_path.iterdir()) == []


def test_session_start_unset_makes_zero_outbound_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    hook = Path(__file__).resolve().parent.parent / "src/llm_router/hooks/session-start.py"
    spec = importlib.util.spec_from_file_location("_d89_session_start", hook)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        calls.append("outbound")
        raise AssertionError("unexpected outbound call")

    monkeypatch.delenv("LLM_ROUTER_AUTO_BENCHMARK_FETCH", raising=False)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    module._maybe_refresh_benchmarks_bg()
    assert calls == []


@pytest.mark.parametrize("matches", [True, False])
def test_session_start_flag_on_uses_catalog_only_on_matching_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matches: bool,
) -> None:
    """Flag on: the detached script the hook spawns installs the catalog iff the pin matches."""
    hook = Path(__file__).resolve().parent.parent / "src/llm_router/hooks/session-start.py"
    spec = importlib.util.spec_from_file_location("_d89_session_start_on", hook)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = b'{"version": 7}'
    digest = bf.sha256_hex(data) if matches else "f" * 64
    monkeypatch.setattr(bf, "load_pins", lambda: {"benchmarks": {"tag": "vtest", "sha256": digest}})
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse(data))
    monkeypatch.setenv("LLM_ROUTER_AUTO_BENCHMARK_FETCH", "1")
    monkeypatch.setattr(module, "_state_dir", lambda: str(tmp_path))
    spawned: list[list[str]] = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda argv, **k: spawned.append(argv))
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="/usr/bin/uv"),
    )
    module._maybe_refresh_benchmarks_bg()
    assert len(spawned) == 1
    exec(spawned[0][-1], {})  # the script text the child process would run
    assert (tmp_path / "benchmarks.json").exists() is matches
