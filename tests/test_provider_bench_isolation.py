"""PROVIDER-BENCH-LEAK-1: subprocess writers retain the pytest sandbox."""

import json
import subprocess

import pytest

from llm_router import provider_bench_log, provider_reset
from tests.test_provider_reset import TestPersistence as _Persistence


@pytest.mark.parametrize("concurrent", [False, True], ids=["codex", "ten-providers"])
def test_provider_reset_children_keep_bench_log_isolated(tmp_path, monkeypatch, concurrent):
    # All paths are temporary; the sentinel represents an operator's home.
    fake_home = tmp_path / "fake-real-home"
    sentinel = fake_home / ".llm-router" / "provider_bench.jsonl"
    sentinel.parent.mkdir(parents=True)
    original = b'{"sentinel":true}\n'
    sentinel.write_bytes(original)
    state = tmp_path / "isolated-state"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
    monkeypatch.delenv("LLM_ROUTER_PROVIDER_RESET_PATH", raising=False)

    # Check before launching: a regression must fail without reaching real HOME.
    popen = subprocess.Popen

    def guarded_popen(*args, **kwargs):
        env = kwargs["env"]
        assert env.get("HOME") == str(fake_home)
        assert env.get("LLM_ROUTER_HOME") == str(state)
        return popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    writer = _Persistence()
    if concurrent:
        writer.test_concurrent_processes_recording_different_providers_all_persist()
        expected = {f"provider-{i}" for i in range(10)}
    else:
        writer.test_visible_to_a_separate_short_lived_process(tmp_path)
        expected = {"codex"}

    assert provider_reset._state_file() == state / "provider_reset.json"
    assert provider_bench_log.store_path() == state / "provider_bench.jsonl"
    rows = [json.loads(line) for line in (state / "provider_bench.jsonl").read_text().splitlines()]
    assert len(rows) == len(expected)
    assert {(row["kind"], row["provider"], row["trigger"]) for row in rows} == {
        ("bench", provider, "unknown") for provider in expected
    }
    # The operator's real bench log is untouched.
    assert sentinel.read_bytes() == original
