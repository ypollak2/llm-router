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


def test_positive_is_kept_while_the_binary_is_unchanged(stub, clock):
    binary = str(stub(True))
    assert codex_agent.codex_supports_ignore_rules(binary) is True
    clock[0] += 10 * codex_agent._RULES_NEGATIVE_TTL_S  # a True has no TTL
    assert codex_agent.codex_supports_ignore_rules(binary) is True
    assert stub.probes() == 1


def test_positive_is_dropped_and_reprobed_when_the_binary_is_swapped(stub, clock):
    binary = str(stub(True))
    assert codex_agent.codex_supports_ignore_rules(binary) is True
    st = os.stat(binary)
    stub(False)  # a downgrade: the stand-in no longer lists the flag
    os.utime(binary, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert codex_agent.codex_supports_ignore_rules(binary) is False  # no clock advance
    assert codex_agent.unprotected_refusal(binary) is not None
    assert stub.probes() == 2


def test_size_only_change_is_reprobed(stub, clock):
    binary = str(stub(False))
    assert codex_agent.codex_supports_ignore_rules(binary) is False
    st = os.stat(binary)
    with open(binary, "a") as fh:  # same answer, longer file
        fh.write("# edited\n")
    os.utime(binary, ns=(st.st_atime_ns, st.st_mtime_ns))  # mtime restored: only the size differs
    assert os.stat(binary).st_size != st.st_size
    assert codex_agent.codex_supports_ignore_rules(binary) is False
    assert stub.probes() == 2


def test_failed_stat_never_yields_true(stub, clock):
    binary = str(stub(True))
    assert codex_agent.codex_supports_ignore_rules(binary) is True
    os.remove(binary)
    assert codex_agent._binary_signature(str(binary)) is None
    assert codex_agent.codex_supports_ignore_rules(str(binary)) is False  # refused, not cached
    assert str(binary) not in codex_agent._RULES_SUPPORT


def test_a_vanished_binary_is_refused_at_the_run_path(stub, clock, monkeypatch):
    binary = stub(True)
    monkeypatch.setenv("CODEX_PATH", str(binary))
    assert codex_agent.codex_supports_ignore_rules(str(binary)) is True
    os.remove(binary)
    assert codex_agent.codex_unsupported_binary() is None
    assert codex_agent.unprotected_refusal(str(binary)) is not None  # not started
