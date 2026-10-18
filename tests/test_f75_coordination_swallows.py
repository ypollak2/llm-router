"""F75 (+F74, F69 residue): coordination shortcuts must not swallow work requests.

F75: ``_COORDINATE_STATUS_RE`` matched anywhere ("ok thanks, what's next? add tests"
was claimed on "what's next") and ``_COORDINATE_BARE_OP_PRONOUN_RE`` claimed any
"merge/deploy it" ("ok thanks. now deploy it", "deploy it now", "ok merge it"). Both are
now whole-prompt shapes. F74: a prompt that is only a URL into the user's own repo is
coordination ("look at this here"); third-party URLs and URL + words keep routing.
F69 residue: failure-status fragments and shred/shed are not owner-action acks.
"""
from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

import pytest

DEFAULT_HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"
# F75_HOOK_UNDER_TEST points this file at an older hook copy (e.g. `git show
# origin/main:...` saved to a scratch file) to see the must-route cases fail there.
HOOK = Path(os.environ.get("F75_HOOK_UNDER_TEST", DEFAULT_HOOK))

MUST_ROUTE_BRIEFED = [
    "ok thanks, what's next? add tests", "ok thanks. now deploy it", "deploy it now",
    "ok merge it", "Failed in CI", "Crashed on prod", "Blocked on CI", "Failed the tests",
    "shred it", "shed it", "Shred the logs", "Shed the old config",
]
MUST_ROUTE_INVENTED = [
    "thanks, what's left? write the changelog", "ok, what's next. fix the flaky test",
    "what's next; implement the cache", "what's left - refactor the parser",
    "done. what's the status? now update the docs", "what's next?\nadd retries to the client",
    "ok thanks, what's the status? review the diff", "so far so good, add tests",
    "did you push? then merge the PR and notes", "what's next, rename foo to bar",
    "ok. now merge it", "yes, deploy it", "redeploy it now", "deploy this now",
    "please merge them", "merge those and tell me the sha", "ok, redeploy them",
    "deploy that to staging", "merge it into main", "merge it, then write release notes",
    "great! deploy it", "thanks - merge this", "Hung on startup", "Stalled in CI",
    "Errored on the build", "Died on prod", "Expired in the cache", "Blocked the merge",
    "ran into a crash, fix it", "Failed in CI, please debug",
]
MUST_ROUTE = MUST_ROUTE_BRIEFED + MUST_ROUTE_INVENTED

STAYS_COORDINATION = [
    "ok", "thanks", "ok thanks", "yes, go ahead", "done", "ok, thanks a lot",
    "Yeah, I reconnected the mcp", "I merged it into main", "copied the hooks",
    "what's next", "what's next?", "ok thanks, what's next?", "what's left?",
    "keep going", "continue", "why is it failing again", "can you check on it?",
    "ok, what's on the list?", "so, what's the score?", "did you push?",
    "what's the status of the migration run?", "did you already push the branch?",
    "merge it once the checks are green", "deploy it and let me know when it's live",
    "merge it when green and keep going", "ok, keep going with the rest of it",
    "yes, do the first two and skip the rest", "I deployed it", "I ran it again",
]
# Documented decision (rv-595): deictic continuations are session-local by design.
DEICTIC_CONTINUATIONS = ["do the rest", "ok, do it", "do it", "keep going with the rest"]

OWN = "ypollak2/llm-router"
OWN_URLS = [
    f"https://github.com/{OWN}/actions/runs/123456/job/789012",
    f"https://github.com/{OWN}/pull/595", f"https://github.com/{OWN}/issues/12",
    f"https://github.com/{OWN}/commit/03c285c4a1b2", f"https://github.com/{OWN}/tree/main/src",
    f"https://github.com/{OWN}/blob/main/README.md", f"<https://github.com/{OWN}/pull/9>",
    f"  https://github.com/{OWN}/pull/9\n", "https://github.com/YPollak2/LLM-Router/pull/9",
]
ROUTE_URLS = [
    "https://github.com/someone/else/pull/595", "https://github.com/someone/else/actions/runs/1",
    f"why did this fail https://github.com/{OWN}/actions/runs/1",
    f"https://github.com/{OWN}/pull/595 summarize the review",
    f"review https://github.com/{OWN}/pull/595",
    f"https://github.com/{OWN}/pull/595 https://github.com/{OWN}/pull/596",
    f"https://github.com/{OWN}-fork/pull/1", f"https://github.com/{OWN}/settings",
    f"https://evil.example/github.com/{OWN}/pull/1",
]


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("_f75_hook_under_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path, monkeypatch, hook):
    """A tmp checkout whose only remotes are ypollak2/llm-router (ssh) and a fork."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:ypollak2/llm-router.git\n'
        '\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
    sub = tmp_path / "src" / "pkg"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    getattr(hook, "_OWN_REMOTES_CACHE", {}).clear()
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return tmp_path


@pytest.mark.parametrize("prompt", MUST_ROUTE)
def test_work_request_is_not_coordination(hook, prompt):
    assert not hook._is_coordination_task(prompt)


@pytest.mark.parametrize("prompt", STAYS_COORDINATION)
def test_genuine_coordination_stays_coordination(hook, prompt):
    assert hook._is_coordination_task(prompt)


@pytest.mark.parametrize("prompt", DEICTIC_CONTINUATIONS)
def test_deictic_continuations_stay_coordination_by_design(hook, prompt):
    assert hook._is_coordination_task(prompt)


@pytest.mark.parametrize("url", OWN_URLS)
def test_bare_own_repo_url_is_coordination(hook, repo, url):
    assert hook._is_coordination_task(url)


@pytest.mark.parametrize("url", ROUTE_URLS)
def test_third_party_or_wordy_url_keeps_routing(hook, repo, url):
    assert not hook._is_coordination_task(url)


def test_own_url_without_matching_remote_keeps_routing(hook, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/a/b.git\n')
    monkeypatch.chdir(tmp_path)
    getattr(hook, "_OWN_REMOTES_CACHE", {}).clear()
    assert not hook._is_coordination_task(f"https://github.com/{OWN}/pull/595")
    assert hook._is_coordination_task("https://github.com/a/b/pull/1")


def test_no_git_dir_keeps_routing(hook, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    getattr(hook, "_OWN_REMOTES_CACHE", {}).clear()
    assert not hook._is_coordination_task(f"https://github.com/{OWN}/pull/595")


def test_linked_worktree_reads_common_config(hook, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/ypollak2/llm-router\n')
    (main / ".git" / "worktrees" / "wt" / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
    monkeypatch.chdir(wt)
    getattr(hook, "_OWN_REMOTES_CACHE", {}).clear()
    assert hook._is_coordination_task(f"https://github.com/{OWN}/pull/1")


def test_populated_lists():
    # an empty parametrize set would pass everything
    assert len(MUST_ROUTE) >= 40 and len(STAYS_COORDINATION) >= 25
    assert len(OWN_URLS) >= 8 and len(ROUTE_URLS) >= 8


@pytest.mark.timing
def test_own_url_check_is_cheap(hook, repo):
    url = f"https://github.com/{OWN}/actions/runs/1/job/2"
    hook._is_coordination_task(url)  # warm the remote cache
    t0 = time.perf_counter()
    for _ in range(200):
        hook._is_coordination_task(url)
    assert (time.perf_counter() - t0) / 200 < 0.005
