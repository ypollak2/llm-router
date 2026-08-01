# Ported from Chuzom's session_store.py tests (including
# tests/test_session_store_concurrency.py, the CHZ-AUD-C-01 regression test);
# env vars renamed to LLM_ROUTER_*; `from chuzom import session_store as ss`
# rewired to `from llm_router import session_store as ss`; worker/iteration
# counts scaled down for CI speed while still using real multiprocessing and
# still exceeding the production `_MAX_RECORDS` threshold within a round so
# organic compaction cycles are still exercised.
"""Tests for src/llm_router/session_store.py (WS1).

Covers: the CHZ-AUD-C-01 real multi-process concurrency regression (zero
lost writes across concurrent record_event() + compaction), consecutive-
duplicate dedup, TTL-based physical purge, sentinel-wrapped context
assembly, privacy modes (all/local/off), and brand-leak absence.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from llm_router import session_store as ss

# ---------------------------------------------------------------------------
# CHZ-AUD-C-01: real multi-process concurrency regression
# ---------------------------------------------------------------------------
#
# Root cause (chuzom audit): session_store.record_event()'s append-then-
# maybe-compact critical section had no cross-process coordination. A
# concurrent process's compaction could read a stale snapshot of the JSONL
# file (taken before this process's append landed) and then os.replace() the
# file with that stale snapshot, silently orphaning this process's
# just-written record even though the write itself succeeded on disk moments
# earlier. llm-router's session_store.py ports the fix: an exclusive advisory
# lock (`exclusive_lock`) on a sibling `.lock` file guards the whole
# append + _maybe_compact() critical section.
#
# Methodology (unchanged from chuzom): N real OS processes each call
# record_event() repeatedly against the same session_id, with a unique
# marker per write; each process immediately re-scans the file's raw bytes
# for its own marker. This deliberately is NOT "does the final file contain
# all N*iterations records" -- production compaction is *designed* to prune
# older records once the log grows past its cap, and that pruning is not
# data loss. What must never happen is a record's immediate self-readback
# (right after its own successful write) coming up empty.
#
# Scaled down from chuzom's 6 workers x 200 iterations x 3 rounds to 4 x 100
# x 2 for CI speed, while 4*100=400 still exceeds _MAX_RECORDS=300 within a
# single round -- so at least one organic compaction cycle is still
# guaranteed per round.

N_WORKERS = 4
ITERATIONS_PER_WORKER = 100
N_ROUNDS = 2
JOIN_TIMEOUT_SECONDS = 120


def _mp_context():
    """Prefer fork (fast process startup) where available; spawn elsewhere."""
    try:
        return multiprocessing.get_context("fork")
    except ValueError:
        return multiprocessing.get_context("spawn")


def _worker(
    home: str,
    project_id: str,
    session_id: str,
    worker_id: int,
    iterations: int,
    result_path: str,
) -> None:
    """Write *iterations* uniquely-marked events, self-verifying each one.

    Runs in a separate OS process. Sets HOME/LLM_ROUTER_PROJECT_ID explicitly
    (rather than relying on inheritance) so the test is correct under both
    fork and spawn start methods.
    """
    os.environ["HOME"] = home
    os.environ["LLM_ROUTER_PROJECT_ID"] = project_id
    os.environ.pop("LLM_ROUTER_SESSION_CONTEXT", None)
    os.environ.pop("CLAUDE_SESSION_ID", None)
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    from llm_router import session_store as ss

    lost: list[str] = []
    path = ss._session_path(session_id)
    for i in range(iterations):
        marker = f"MARK-w{worker_id}-i{i}-pid{os.getpid()}"
        ss.record_event(session_id, "user_prompt", f"payload {marker} payload")
        try:
            raw = path.read_bytes()
        except OSError:
            raw = b""
        if marker.encode("utf-8") not in raw:
            lost.append(marker)

    Path(result_path).write_text(
        json.dumps({"worker_id": worker_id, "lost": lost, "wrote": iterations}),
        encoding="utf-8",
    )


def _run_round(tmp_path: Path, round_idx: int) -> list[str]:
    home = str(tmp_path)
    project_id = "concurrency-test-project"
    session_id = f"concurrency-round-{round_idx}"
    ctx = _mp_context()

    result_paths = [
        str(tmp_path / f"result_r{round_idx}_w{i}.json") for i in range(N_WORKERS)
    ]
    procs = []
    for i in range(N_WORKERS):
        p = ctx.Process(
            target=_worker,
            args=(home, project_id, session_id, i, ITERATIONS_PER_WORKER, result_paths[i]),
        )
        p.start()
        procs.append(p)

    for p in procs:
        p.join(timeout=JOIN_TIMEOUT_SECONDS)
        assert not p.is_alive(), "worker process hung past join timeout"
        assert p.exitcode == 0, f"worker process exited with code {p.exitcode}"

    all_lost: list[str] = []
    total_written = 0
    for rp in result_paths:
        data = json.loads(Path(rp).read_text(encoding="utf-8"))
        all_lost.extend(data["lost"])
        total_written += data["wrote"]
    assert total_written == N_WORKERS * ITERATIONS_PER_WORKER
    return all_lost


def test_session_store_concurrency_zero_lost_writes():
    """N processes x iterations x rounds against the same session_id.

    Each round exercises production compaction thresholds (_MAX_RECORDS/
    _COMPACT_TO are left at their real defaults, guaranteeing at least one
    organic compaction cycle mid-round given N_WORKERS*ITERATIONS_PER_WORKER
    > _MAX_RECORDS). Asserts zero lost writes in every round.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="llm-router-c01-") as tmp:
        tmp_path = Path(tmp)
        for round_idx in range(N_ROUNDS):
            lost = _run_round(tmp_path, round_idx)
            assert lost == [], (
                f"round {round_idx}: {len(lost)} writes were lost "
                f"(first few: {lost[:10]})"
            )


# ---------------------------------------------------------------------------
# Single-process unit tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    """Point session_store at a scratch state dir, isolated from the real
    ``~/.llm-router/state`` and from any other test's project scope."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LLM_ROUTER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "test-project")
    monkeypatch.delenv("LLM_ROUTER_SESSION_CONTEXT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PERSIST_TTL_DAYS", raising=False)
    return state_dir


class TestRecordEventDedup:
    def test_consecutive_duplicate_is_deduped(self, isolated_store):
        session_id = "dedup-session"
        ss.record_event(session_id, "user_prompt", "hello world")
        ss.record_event(session_id, "user_prompt", "hello world")
        events = ss.load_events(session_id)
        assert len(events) == 1

    def test_non_consecutive_duplicate_is_kept(self, isolated_store):
        session_id = "dedup-session-2"
        ss.record_event(session_id, "user_prompt", "hello world")
        ss.record_event(session_id, "user_prompt", "something else")
        ss.record_event(session_id, "user_prompt", "hello world")
        events = ss.load_events(session_id)
        assert len(events) == 3

    def test_empty_content_is_noop(self, isolated_store):
        session_id = "empty-session"
        ss.record_event(session_id, "user_prompt", "")
        ss.record_event(session_id, "user_prompt", "   ")
        assert ss.load_events(session_id) == []

    def test_none_session_id_is_noop(self, isolated_store):
        # Fail-open: must never raise even with a garbage session_id.
        ss.record_event(None, "user_prompt", "hello")


class TestSecretScrubbing:
    def test_api_key_is_redacted(self, isolated_store):
        session_id = "secret-session"
        ss.record_event(
            session_id, "user_prompt", "my OPENAI_API_KEY=sk-abcdef1234567890abcdef"
        )
        events = ss.load_events(session_id)
        assert len(events) == 1
        assert "sk-abcdef1234567890abcdef" not in events[0]["content"]
        assert "[REDACTED]" in events[0]["content"]


class TestTTLPurge:
    def test_purge_expired_removes_old_records(self, isolated_store, monkeypatch):
        session_id = "ttl-session"
        ss.record_event(session_id, "user_prompt", "old record")
        path = ss._session_path(session_id)

        # Rewrite the just-written record's timestamp to be far in the past.
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        rec["ts"] = time.time() - 999 * 86400
        path.write_text(json.dumps(rec) + "\n", encoding="utf-8")

        monkeypatch.setenv("LLM_ROUTER_PERSIST_TTL_DAYS", "1")
        ss.purge_expired(session_id)

        assert ss.load_events(session_id) == []

    def test_purge_expired_noop_for_missing_session(self, isolated_store):
        # Fail-open: no file on disk yet -> must not raise.
        ss.purge_expired("does-not-exist")


class TestBuildSessionContext:
    def test_wraps_content_in_sentinels(self, isolated_store):
        session_id = "context-session"
        ss.record_event(session_id, "user_prompt", "what is the capital of France")
        context = ss.build_session_context(session_id)
        assert context.startswith(ss.SENTINEL_OPEN)
        assert context.endswith(ss.SENTINEL_CLOSE)
        assert "USER: what is the capital of France" in context

    def test_empty_session_returns_empty_string(self, isolated_store):
        assert ss.build_session_context("no-such-session") == ""

    def test_mode_off_returns_empty_string(self, isolated_store, monkeypatch):
        session_id = "off-session"
        ss.record_event(session_id, "user_prompt", "hello")
        monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "off")
        assert ss.build_session_context(session_id) == ""

    def test_mode_local_blocks_external_provider(self, isolated_store, monkeypatch):
        session_id = "local-session"
        ss.record_event(session_id, "user_prompt", "hello")
        monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
        assert ss.build_session_context(session_id, target_provider="openai") == ""

    def test_mode_local_allows_local_provider(self, isolated_store, monkeypatch):
        session_id = "local-session-2"
        ss.record_event(session_id, "user_prompt", "hello")
        monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
        context = ss.build_session_context(session_id, target_provider="ollama")
        assert context != ""


class TestGetMode:
    def test_defaults_to_all(self, isolated_store):
        assert ss.get_mode() == "all"

    @pytest.mark.parametrize(
        "env_value,expected",
        [("on", "all"), ("all", "all"), ("local", "local"), ("off", "off")],
    )
    def test_env_var_selects_mode(self, isolated_store, monkeypatch, env_value, expected):
        monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", env_value)
        assert ss.get_mode() == expected


class TestArchiveAndCleanup:
    def test_archive_session_removes_file(self, isolated_store):
        session_id = "archive-session"
        ss.record_event(session_id, "user_prompt", "hello")
        path = ss._session_path(session_id)
        assert path.exists()
        ss.archive_session(session_id)
        assert not path.exists()

    def test_archive_missing_session_is_noop(self, isolated_store):
        ss.archive_session("never-existed")


class TestBrandLeak:
    def test_no_chuzom_in_public_names_or_values(self):
        public_names = [name for name in dir(ss) if not name.startswith("_")]
        for name in public_names:
            assert "chuzom" not in name.lower()
            value = getattr(ss, name)
            if isinstance(value, str):
                assert "chuzom" not in value.lower()
