"""Permanent regression test for CHZ-AUD-B-04 (session/project scope identity).

Root cause: the in-process SessionBuffer used to be a single module-level
singleton (``context._session_buffer``), and ``get_session_buffer()`` took no
arguments. In a long-lived process handling multiple projects/sessions (an
MCP server serving many concurrent Claude Code sessions, or a test process
running many test cases back to back), every caller shared that ONE buffer:
recent conversation content recorded for project A / session 1 would be
injected into prompts assembled for a completely unrelated project B /
session 2, because ``build_context_messages()``'s Layer 2 called
``get_session_buffer()`` with no identity at all and ignored its own
``session_id``/``project_id`` parameters.

The fix (``src/llm_router/context.py``) replaces the singleton with a bounded,
evictable registry keyed by ``(project_id, session_id)`` — see
``get_session_buffer(project_id, session_id=None)``,
``_resolve_context_identity()``, and ``_reset_session_buffers_for_test()``.

This file is the MANDATORY permanent "session isolation test" required by
the release-hardening task's Section 6 test spec, covering the B-04-relevant
subset: A/B project switch, same-project-different-session,
same-session-different-project, concurrent projects, buffer eviction
(capacity + idle), and restart-equivalence. It is hermetic: every test
resets the registry before/after and uses only explicit identities (no
reliance on ambient env/cwd), so it is immune to the ambient-identity
collisions that the rest of ``tests/test_context.py`` has to work around via
its ``_ambient_identity()`` helper.
"""

from __future__ import annotations

import time

import pytest

from llm_router.context import (
    _buffers,
    _reset_session_buffers_for_test,
    _resolve_context_identity,
    get_session_buffer,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    _reset_session_buffers_for_test()
    yield
    _reset_session_buffers_for_test()


class TestProjectAndSessionIsolation:
    """The core B-04 leak: distinct identities must never share a buffer,
    and distinct identities' recorded content must never cross-contaminate."""

    def test_ab_project_switch_does_not_leak(self):
        """Recording in project A, then switching to project B in the same
        process, must not surface project A's content to project B."""
        buf_a = get_session_buffer("project-a", "sess-1")
        buf_a.record("user", "secret project A detail", task_type="query")

        buf_b = get_session_buffer("project-b", "sess-1")
        assert buf_b.message_count == 0
        assert "secret project A" not in buf_b.format_for_injection()

        # Switching back to A must still see A's own content (isolation is
        # not a one-way erase — each identity's buffer is independently
        # preserved for the life of the registry entry).
        buf_a_again = get_session_buffer("project-a", "sess-1")
        assert buf_a_again is buf_a
        assert "secret project A" in buf_a_again.format_for_injection()

    def test_same_project_different_session_isolated(self):
        """Two sessions within the SAME project must not share a buffer —
        e.g. two concurrent Claude Code windows on the same repo."""
        buf_s1 = get_session_buffer("project-a", "sess-1")
        buf_s1.record("user", "session 1 only content", task_type="query")

        buf_s2 = get_session_buffer("project-a", "sess-2")
        assert buf_s1 is not buf_s2
        assert buf_s2.message_count == 0
        assert "session 1 only" not in buf_s2.format_for_injection()

    def test_same_session_id_different_project_isolated(self):
        """A colliding session_id across two different projects (e.g. both
        resolved from the same CLAUDE_SESSION_ID env value while cwd
        differs) must still be scoped by project_id and not merge."""
        buf_p1 = get_session_buffer("project-a", "shared-sess-id")
        buf_p1.record("user", "project A content under shared session id", task_type="query")

        buf_p2 = get_session_buffer("project-b", "shared-sess-id")
        assert buf_p1 is not buf_p2
        assert buf_p2.message_count == 0

    def test_concurrent_projects_each_keep_own_history(self):
        """Simulates a long-lived MCP server juggling several projects at
        once: each (project, session) pair accumulates its own, unmixed
        history no matter the interleaving of calls."""
        pairs = [("proj-1", "s1"), ("proj-2", "s1"), ("proj-1", "s2"), ("proj-3", None)]
        for pid, sid in pairs:
            buf = get_session_buffer(pid, sid)
            buf.record("user", f"marker-for-{pid}-{sid}", task_type="query")

        for pid, sid in pairs:
            buf = get_session_buffer(pid, sid)
            content = buf.format_for_injection()
            assert f"marker-for-{pid}-{sid}" in content
            for other_pid, other_sid in pairs:
                if (other_pid, other_sid) == (pid, sid):
                    continue
                assert f"marker-for-{other_pid}-{other_sid}" not in content

    def test_none_session_id_normalized_and_isolated_from_named_sessions(self):
        """project_id with session_id=None (e.g. no resolvable session) must
        not collide with an explicitly-named session on the same project."""
        buf_none = get_session_buffer("project-a", None)
        buf_none.record("user", "no-session content", task_type="query")

        buf_named = get_session_buffer("project-a", "sess-1")
        assert buf_none is not buf_named
        assert buf_named.message_count == 0


class TestRegistryIdentityCaching:
    def test_same_key_returns_same_instance(self):
        buf1 = get_session_buffer("proj-a", "sess-1")
        buf2 = get_session_buffer("proj-a", "sess-1")
        assert buf1 is buf2

    def test_falsy_project_id_normalized_to_placeholder_key(self):
        """An empty-string project_id must not collide with the
        "no project resolved at all" case going through a different code
        path (e.g. accidentally reusing the same dict key by coincidence is
        fine; behaving inconsistently is not)."""
        buf_empty = get_session_buffer("", "sess-1")
        buf_empty_again = get_session_buffer("", "sess-1")
        assert buf_empty is buf_empty_again


class TestBufferEviction:
    def test_capacity_eviction_is_lru(self):
        """When the registry is at capacity, the LEAST recently accessed
        buffer must be evicted first — a busy long-lived process must not
        randomly drop an active project's history to make room."""
        import llm_router.context as context_module

        original_max = context_module._MAX_BUFFERS
        context_module._MAX_BUFFERS = 3
        try:
            get_session_buffer("proj-old", "s1")  # oldest access
            get_session_buffer("proj-mid", "s1")
            get_session_buffer("proj-new", "s1")
            assert len(_buffers) == 3

            # touch proj-old again so it is no longer the LRU entry
            get_session_buffer("proj-old", "s1")

            # proj-mid is now the least-recently-accessed of the three —
            # adding a 4th distinct key must evict it, not proj-old.
            get_session_buffer("proj-fourth", "s1")
            assert len(_buffers) == 3
            assert ("proj-old", "s1") in _buffers
            assert ("proj-fourth", "s1") in _buffers
            assert ("proj-mid", "s1") not in _buffers
        finally:
            context_module._MAX_BUFFERS = original_max
            _reset_session_buffers_for_test()

    def test_idle_buffer_is_evicted_and_replaced_with_fresh_history(self):
        """A buffer that has been idle past the eviction window must be
        dropped (and its content gone) on next access, rather than served
        stale forever — bounding memory in a long-lived process."""
        import llm_router.context as context_module

        original_idle = context_module._BUFFER_IDLE_EVICT_SECONDS
        context_module._BUFFER_IDLE_EVICT_SECONDS = 0.01
        try:
            buf = get_session_buffer("proj-idle", "s1")
            buf.record("user", "will be evicted", task_type="query")
            time.sleep(0.05)

            # Accessing ANY key runs the opportunistic stale sweep.
            get_session_buffer("proj-other", "s1")
            assert ("proj-idle", "s1") not in _buffers

            fresh = get_session_buffer("proj-idle", "s1")
            assert fresh.message_count == 0
        finally:
            context_module._BUFFER_IDLE_EVICT_SECONDS = original_idle
            _reset_session_buffers_for_test()


class TestRestartEquivalence:
    def test_reset_for_test_clears_everything(self):
        """_reset_session_buffers_for_test() is the hermetic-test stand-in
        for a process restart — the registry must come back completely
        empty, with no leftover identity or content from before the reset."""
        get_session_buffer("proj-a", "sess-1").record("user", "pre-restart", task_type="query")
        get_session_buffer("proj-b", "sess-2").record("user", "pre-restart", task_type="query")
        assert len(_buffers) == 2

        _reset_session_buffers_for_test()

        assert len(_buffers) == 0
        post = get_session_buffer("proj-a", "sess-1")
        assert post.message_count == 0


class TestResolveContextIdentity:
    """_resolve_context_identity() is the shared identity-resolution helper
    that build_context_messages(), auto_summarize_session(), and
    router.route_and_call()'s success path all use to key the registry —
    it must behave consistently for all three callers."""

    def test_explicit_project_id_wins_outright(self, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "env-project")
        pid, _sid = _resolve_context_identity("explicit-project", None)
        assert pid == "explicit-project"

    def test_falls_back_to_session_store_project_id(self, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "env-project")
        pid, _sid = _resolve_context_identity(None, None)
        assert pid == "env-project"

    def test_fails_open_on_session_store_import_error(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _blocking_import(name, *args, **kwargs):
            if name == "llm_router.session_store" or name == "llm_router":
                raise ImportError("simulated import failure")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocking_import)
        pid, sid = _resolve_context_identity(None, None)
        assert pid == "_unknown"
        assert sid is None
