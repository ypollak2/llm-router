"""#818 review N-D: a definite False from the ``--ignore-rules`` probe is not kept for the life of the process.

A stub ``codex`` on disk answers ``exec --help``; the real probe runs against it (never the real codex). The clock is
``time.monotonic`` patched on the module."""
from __future__ import annotations

import os
import sys

import pytest

from llm_router import codex_agent
from tests._bind_support import write_codex_stub

pytestmark = [pytest.mark.real_codex_probe, pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell stub")]


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(codex_agent.time, "monotonic", lambda: now[0])
    return now


@pytest.fixture
def stub(tmp_path, monkeypatch, clock):
    log = tmp_path / "argv.jsonl"
    codex_agent._reset_codex_cache_for_tests()

    def make(listed: bool):
        return write_codex_stub(tmp_path / "bin", argv_log=log, help_lists_ignore_rules=listed)

    def probes() -> int:
        p = tmp_path / "argv.jsonl.probes"
        return len(p.read_text().splitlines()) if p.exists() else 0

    make.probes = probes
    yield make
    codex_agent._reset_codex_cache_for_tests()


def test_negative_is_cached_inside_the_ttl(stub, clock):
    binary = str(stub(False))
    assert codex_agent.codex_supports_ignore_rules(binary) is False
    clock[0] += codex_agent._RULES_NEGATIVE_TTL_S - 1
    assert codex_agent.codex_supports_ignore_rules(binary) is False
    assert stub.probes() == 1


def test_negative_is_reprobed_after_the_ttl(stub, clock, monkeypatch):
    monkeypatch.setattr(codex_agent, "_binary_signature", lambda b: (1, 1))  # the binary looks unchanged: only the TTL differs
    binary = str(stub(False))
    assert codex_agent.codex_supports_ignore_rules(binary) is False
    stub(True)
    clock[0] += codex_agent._RULES_NEGATIVE_TTL_S - 1
    assert codex_agent.codex_supports_ignore_rules(binary) is False
    clock[0] += 2
    assert codex_agent.codex_supports_ignore_rules(binary) is True
    assert stub.probes() == 2


def test_negative_is_reprobed_at_once_when_the_binary_changes(stub, clock):
    binary = str(stub(False))
    assert codex_agent.codex_supports_ignore_rules(binary) is False
    st = os.stat(binary)
    stub(True)  # the upgrade: new content, and the mtime moves
    os.utime(binary, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert codex_agent.codex_supports_ignore_rules(binary) is True  # no clock advance
    assert stub.probes() == 2


def test_unchanged_binary_inside_ttl_is_not_reprobed(stub, clock):
    binary = str(stub(False))
    codex_agent.codex_supports_ignore_rules(binary)
    clock[0] += 5
    codex_agent.codex_supports_ignore_rules(binary)
    assert stub.probes() == 1


def test_positive_is_kept_for_good(stub, clock):
    binary = str(stub(True))
    assert codex_agent.codex_supports_ignore_rules(binary) is True
    clock[0] += 10 * codex_agent._RULES_NEGATIVE_TTL_S
    os.utime(binary, ns=(1, 1))
    assert codex_agent.codex_supports_ignore_rules(binary) is True
    assert stub.probes() == 1


def test_availability_recovers_after_an_upgrade(stub, clock, monkeypatch):
    binary = stub(False)
    monkeypatch.setenv("CODEX_PATH", str(binary))
    assert codex_agent.is_codex_available() is True  # path found, capability not asked yet
    assert codex_agent.codex_supports_ignore_rules(str(binary)) is False
    assert codex_agent.is_codex_available() is False
    assert codex_agent.codex_unsupported_binary() == str(binary)
    st = os.stat(binary)
    stub(True)
    os.utime(binary, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert codex_agent.is_codex_available() is True  # stale negative dropped, stat only
    assert codex_agent.codex_unsupported_binary() is None
    assert codex_agent.unprotected_refusal(str(binary)) is None
