"""INV-007 / ROU-001 regression: per-session classification side channel.

Pre-fix, the auto-route hook wrote a single shared file
``~/.llm-router/last_classification.json`` and every MCP server read from it.
Symptoms:

* Two concurrent Claude Code sessions on the same machine raced: whoever
  fired last set the verdict for both.
* Any same-user process could forge a classification within the 120 s
  freshness window — local-privilege escalation against the routing
  verdict.

Post-fix, the hook writes ``~/.llm-router/last_classification_<session_id>.json``
and the MCP reader pins to ``CLAUDE_SESSION_ID`` from the environment that
Claude Code injected when it spawned the MCP server.

This test suite proves:

1. The reader returns ``None`` when ``CLAUDE_SESSION_ID`` is missing
   (graceful fallback, no panic).
2. Two distinct session ids see independent verdicts even when both
   shards exist on disk.
3. A forged shard for a different session id cannot influence the
   current session's reader.
4. The 120 s staleness window still works.
5. The inner ``session_id`` consistency check rejects a shard that was
   relocated/renamed to look like it belongs to a different session.

See: Docs/audit/HIGH_PRIORITY_WORK_PLAN.md F-INV-007, F-ROU-001
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


# Module under test
from llm_router.tools.text import _read_hook_complexity_hint


# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_shard(home: Path, file_session_id: str, **overrides) -> Path:
    """Write a per-session classification shard with sensible defaults.

    ``file_session_id`` drives the filename; the inner payload ``session_id``
    defaults to the same value but can be overridden (used by the
    forged-payload test).
    """
    shard = home / ".llm-router" / f"last_classification_{file_session_id}.json"
    shard.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_type": "query",
        "complexity": "simple",
        "method": "heuristic",
        "issued_at": time.time(),
        "session_id": file_session_id,
    }
    payload.update(overrides)
    shard.write_text(json.dumps(payload), encoding="utf-8")
    return shard


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` at a per-test tmp dir so shards stay sandboxed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # `pathlib.Path.home()` honours $HOME on POSIX.
    return tmp_path


# ── 1. Graceful behaviour when env is missing ────────────────────────────────


def test_returns_none_when_claude_session_id_missing(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env → no read → no panic. Router falls back to length heuristic."""
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    # Even with a shard on disk: missing env means we don't trust any of them.
    _write_shard(fake_home, "sid-A", complexity="complex")
    assert _read_hook_complexity_hint() is None


def test_returns_none_when_claude_session_id_blank(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty / whitespace env behaves the same as missing."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "   ")
    _write_shard(fake_home, "sid-A", complexity="complex")
    assert _read_hook_complexity_hint() is None


# ── 2. Session isolation: two ids, two verdicts ──────────────────────────────


def test_session_a_only_reads_its_own_shard(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_shard(fake_home, "sid-A", complexity="simple")
    _write_shard(fake_home, "sid-B", complexity="complex")

    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-A")
    assert _read_hook_complexity_hint() == "simple"


def test_session_b_only_reads_its_own_shard(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_shard(fake_home, "sid-A", complexity="simple")
    _write_shard(fake_home, "sid-B", complexity="complex")

    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-B")
    assert _read_hook_complexity_hint() == "complex"


def test_unknown_session_id_has_no_shard(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_shard(fake_home, "sid-A", complexity="simple")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-other")
    assert _read_hook_complexity_hint() is None


# ── 3. Adversarial: forged shard for another session id is invisible ─────────


def test_forged_shard_for_other_session_cannot_influence_current(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROU-001: pre-fix, a forged shared file overrode the verdict.

    Post-fix, the forger must know the *current* session id to write the
    right filename. If they only write a shard for a different session
    id, the current session's reader is unaffected.
    """
    # Legit shard for the current session.
    _write_shard(fake_home, "sid-current", complexity="simple")
    # Attacker writes a shard for some other id with `complex` verdict.
    _write_shard(fake_home, "sid-attacker", complexity="complex")

    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-current")
    assert _read_hook_complexity_hint() == "simple"


def test_inner_session_id_mismatch_rejected(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-braces: even if the filename matches, the inner session_id must too.

    Defends against a half-clever forger who guesses the filename pattern
    but copies a payload from another session verbatim.
    """
    # Filename says sid-current; payload's inner session_id says sid-attacker.
    _write_shard(
        fake_home,
        "sid-current",
        session_id="sid-attacker",
        complexity="complex",
    )
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-current")
    assert _read_hook_complexity_hint() is None


# ── 4. Staleness still works ─────────────────────────────────────────────────


def test_stale_shard_rejected(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 5-minute-old shard exceeds the 120s freshness window."""
    _write_shard(
        fake_home,
        "sid-A",
        complexity="simple",
        issued_at=time.time() - 300,
    )
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-A")
    assert _read_hook_complexity_hint() is None


def test_just_in_window_shard_accepted(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 100s-old shard is still inside the 120s freshness window."""
    _write_shard(
        fake_home,
        "sid-A",
        complexity="moderate",
        issued_at=time.time() - 100,
    )
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-A")
    assert _read_hook_complexity_hint() == "moderate"


# ── 5. Malformed input is rejected, never crashes ────────────────────────────


def test_corrupt_json_returns_none(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shard = fake_home / ".llm-router" / "last_classification_sid-A.json"
    shard.parent.mkdir(parents=True, exist_ok=True)
    shard.write_text("{not valid json")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-A")
    assert _read_hook_complexity_hint() is None


def test_unknown_complexity_value_returns_none(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_shard(fake_home, "sid-A", complexity="excessive")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-A")
    assert _read_hook_complexity_hint() is None


def test_missing_issued_at_returns_none(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shard = fake_home / ".llm-router" / "last_classification_sid-A.json"
    shard.parent.mkdir(parents=True, exist_ok=True)
    shard.write_text(json.dumps({"complexity": "simple", "session_id": "sid-A"}))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-A")
    assert _read_hook_complexity_hint() is None
