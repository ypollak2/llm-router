"""S2-1 — a session's events must stay in ONE place, whatever the cwd is.

Field evidence (2026-09-10, `~/.llm-router/projects`):

    session 1eaecfb8   7 buckets   [176, 81, 15, 11, 8, 7, 1]  = 299 events
    session b66303b1   6 buckets   [23, 9, 6, 5, 2, 1]         =  46 events

    across all fragmented sessions: 451 recorded, 284 readable, 167 lost (37%)

`_project_id()` hashes `os.getcwd()`, and the PostToolUse hook runs with whatever
cwd the tool call left behind. A session that touches several directories — every
real working session — therefore scatters its events across several buckets, while
`build_session_context` reads exactly one. The context was never thin because
events were not recorded. It was thin because they were unreadable.

Same defect class as OKF-SCOPE-01: identity resolved from a per-call cwd rather
than from something stable about the work.

The fix is `_project_id()` resolving the git ROOT of the cwd, so `repo/`, `repo/src/`
and `repo/tests/` are one project rather than three. That is the dominant case: a
session works within a repo and moves between its directories constantly.

A second half was written and then removed. `_session_path()` following a session
id across buckets would have consolidated the rest of the fragmentation, on the
argument that a session id is a stronger identity than a directory. It also breaks
the isolation guarantee `CHZ-AUD-024` pins — a project must not be able to read a
session it does not own, even knowing its id — so it is not worth having.
`test_a_session_id_is_not_a_key_into_another_project` below pins that refusal, so
the trade is recorded rather than rediscovered.

What remains unfixed is therefore bounded and deliberate: events recorded while the
cwd was in a genuinely different project stay with that project.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_router import session_store


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ID", raising=False)
    yield


def _repo(tmp_path: Path, name: str) -> Path:
    r = tmp_path / name
    (r / ".git").mkdir(parents=True)
    (r / "src").mkdir()
    (r / "tests").mkdir()
    return r


def _buckets(tmp_path: Path, session_id: str) -> list[Path]:
    root = tmp_path / ".llm-router" / "projects"
    if not root.exists():
        return []
    return sorted(root.glob(f"*/session_context_{session_id}*.jsonl"))


# ── half 1: subdirectories are one project ──────────────────────────────────

def test_subdirectories_of_a_repo_share_one_project_id(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo-a")
    monkeypatch.chdir(repo)
    at_root = session_store._project_id()
    monkeypatch.chdir(repo / "src")
    at_src = session_store._project_id()
    monkeypatch.chdir(repo / "tests")
    at_tests = session_store._project_id()
    assert at_root == at_src == at_tests, (
        "cd-ing within one repo produced different project buckets"
    )


def test_separate_repos_still_get_separate_project_ids(tmp_path, monkeypatch):
    """The counterpart: stabilising scope must not merge unrelated projects."""
    a, b = _repo(tmp_path, "repo-a"), _repo(tmp_path, "repo-b")
    monkeypatch.chdir(a)
    id_a = session_store._project_id()
    monkeypatch.chdir(b)
    id_b = session_store._project_id()
    assert id_a != id_b


def test_explicit_project_id_still_wins(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "repo-a")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "pinned-scope")
    assert session_store._project_id() == "pinned-scope"


# ── the trade: consolidation vs. isolation ─────────────────────────────────

def test_events_stay_in_one_file_across_one_repos_subdirectories(tmp_path, monkeypatch):
    """The field failure, reproduced at the scale the fix actually covers."""
    sid = "sess-fragment-01"
    a = _repo(tmp_path, "repo-a")

    monkeypatch.chdir(a)
    session_store.record_event(sid, "user_prompt", "first turn at the root", role="user")
    monkeypatch.chdir(a / "src")
    session_store.record_event(sid, "user_prompt", "second turn in src", role="user")
    monkeypatch.chdir(a / "tests")
    session_store.record_event(sid, "user_prompt", "third turn in tests", role="user")

    files = _buckets(tmp_path, sid)
    assert len(files) == 1, (
        f"one session wrote {len(files)} log files: {[f.parent.name for f in files]}"
    )


def test_a_session_id_is_not_a_key_into_another_project(tmp_path, monkeypatch):
    """The trade this fix refuses to make (CHZ-AUD-024).

    Following a session id across buckets would consolidate the remaining
    fragmentation, and it would also let any project read any session whose id it
    knows. Context is not worth that.
    """
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "proj-alpha")
    session_store.record_event("sess-shared-id", "user_prompt", "ALPHA ONLY", role="user")
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "proj-beta")
    assert session_store.load_events("sess-shared-id") == [], (
        "another project read a session it does not own"
    )


def test_every_recorded_event_is_readable_afterwards(tmp_path, monkeypatch):
    """Fragmentation's real cost — this is the 37% that was being lost."""
    sid = "sess-fragment-02"
    a = _repo(tmp_path, "repo-a")

    monkeypatch.chdir(a)
    session_store.record_event(sid, "user_prompt", "alpha", role="user")
    session_store.record_event(sid, "user_prompt", "bravo", role="user")
    monkeypatch.chdir(a / "src")
    session_store.record_event(sid, "user_prompt", "charlie", role="user")
    monkeypatch.chdir(a / "tests")
    session_store.record_event(sid, "user_prompt", "delta", role="user")

    bodies = " ".join(e.get("content", "") for e in session_store.load_events(sid, limit=50))
    for word in ("alpha", "bravo", "charlie", "delta"):
        assert word in bodies, f"event {word!r} was recorded but is not readable"


def test_context_block_survives_a_cwd_change(tmp_path, monkeypatch):
    """What the routed model actually receives, which is the point of all this."""
    sid = "sess-fragment-03"
    a = _repo(tmp_path, "repo-a")

    monkeypatch.chdir(a)
    session_store.record_event(
        sid, "user_prompt", "we are refactoring the invoice reconciler", role="user"
    )
    monkeypatch.chdir(a / "src")
    ctx = session_store.build_session_context(
        sid, max_tokens=800, task_type="code", query="reconciler", target_provider="ollama"
    )
    assert "invoice reconciler" in ctx, (
        "the earlier turn vanished from context after a cd — this is the bug"
    )


def test_a_brand_new_session_lands_in_the_current_project(tmp_path, monkeypatch):
    """Following an existing file must not stop new sessions being scoped."""
    repo = _repo(tmp_path, "repo-a")
    monkeypatch.chdir(repo)
    session_store.record_event("sess-new-01", "user_prompt", "hello", role="user")
    written = _buckets(tmp_path, "sess-new-01")
    assert len(written) == 1
    assert written[0].parent.name == session_store._project_id()


def test_lock_files_are_not_mistaken_for_session_logs(tmp_path, monkeypatch):
    """`.jsonl.lock` siblings sit next to every log; resolution must ignore them."""
    sid = "sess-lock-01"
    repo = _repo(tmp_path, "repo-a")
    monkeypatch.chdir(repo)
    session_store.record_event(sid, "user_prompt", "one", role="user")
    resolved = session_store._session_path(sid)
    assert resolved.suffix == ".jsonl"
    assert not resolved.name.endswith(".lock")
