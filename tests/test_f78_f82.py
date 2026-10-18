"""F78 + F82: the last coordination swallows after F75, and pasted shell escapes.

F78(1): unpunctuated "what's next <request>" was claimed by the status path.
F78(2): ``_COORDINATE_MULTISTEP_RE`` was an unanchored search ("push the fix and deploy").
F78(3): the own-repo URL check on "github.com" was case-sensitive.
F82: " ! kill 65369" / "!git status" (a shell escape pasted as text) is command-shaped.
Also: "continue the plan" was query/simple and held read-only Bash.
Every skip-routing rule is a whole-prompt allow-list; a false accept (work swallowed)
is the expensive error.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

DEFAULT_HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"
# F78_HOOK_UNDER_TEST points this file at another hook copy (e.g. `git show
# origin/main:...` saved to a scratch file) to see the must-route cases fail there.
HOOK = Path(os.environ.get("F78_HOOK_UNDER_TEST", DEFAULT_HOOK))

MUST_ROUTE_BRIEFED = [
    "what's next add tests", "what's next fix it", "what's next and fix bug in parser",
    "push the fix and deploy",
]
MUST_ROUTE_INVENTED = [
    "what's left write the changelog", "what's left refactor the parser",
    "ok thanks what's next implement the cache", "what's the status review the diff",
    "next steps rename foo to bar", "what's next and then add retries to the client",
    "what's left and fix the flaky test", "what's missing add a docstring to foo",
    "so far so good add tests", "what remains update the readme",
    "push the fix and deploy it to staging", "push the branch and deploy",
    "merge the PR and release", "merge the pull request and redeploy",
    "release v2 and report back", "publish the package and deploy the docs",
    "rebase on main and resolve the conflicts", "deploy the api and merge the docs PR",
    "roll out the new config and report back", "push and deploy the hotfix",
    "merge it and then write release notes", "deploy it and rewrite the readme",
    "!git push origin main", "! rm -rf build", " ! git commit -m 'x'", "!fix the failing test",
    "continue the plan and add tests", "continue refactoring the parser",
    "keep going and fix the lint errors", "go on and write the tests",
    "carry on with the plan but rename the module",
]
MUST_ROUTE = MUST_ROUTE_BRIEFED + MUST_ROUTE_INVENTED

STAYS_COORDINATION = [
    "ok", "thanks", "ok thanks", "yes, go ahead", "done", "what's next", "what's next?",
    "ok thanks, what's next?", "ok thanks what's next", "what's left?", "what's left to do?",
    "so, what's the score?", "did you push?", "next steps?", "where we at",
    "what's the status of the migration run?", "did you already push the branch?",
    "ok thanks. what's next? and the rest?", "what's next, keep going",
    "merge them and redeploy", "merge it and deploy it", "deploy it and report back",
    "roll it out and report back", "push and deploy", "push and create a release",
    "what's left before we can ship?", "merge it once the checks are green",
    "deploy it and let me know when it's live", "continue the plan", "continue",
    "keep going", "go on", "carry on", "carry on with the plan", "continue with the plan",
    "ok, continue the plan", "keep going with the plan", "continue with the rest",
    # D-222 (F83): moved from MUST_ROUTE; "Proceed with the plan" is coordination.
    "Proceed with the plan",
]

COMMAND_SHAPED = [
    " ! kill 65369", "!git status", "  ! ls -la", "! ps aux", "!pwd", "  !   lsof -i :8080",
    "! cat README.md", "!uv run pytest tests/ -q", "$ ls", "git status",
]
NOT_COMMAND_SHAPED = [
    "!", "!!", "! why does git rebase fail", "!git push origin main", "! rm -rf build",
    "!fix the bug", "!important: fix the parser", "! hello there", "!git status?",
    "! git commit -m x", "kill 65369", "touch new.txt",
]


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("_f78_hook_under_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("prompt", MUST_ROUTE)
def test_work_request_is_not_coordination(hook, prompt):
    assert not hook._is_coordination_task(prompt)


@pytest.mark.parametrize("prompt", STAYS_COORDINATION)
def test_genuine_coordination_stays_coordination(hook, prompt):
    assert hook._is_coordination_task(prompt)


@pytest.mark.parametrize("prompt", COMMAND_SHAPED)
def test_command_shape_accepts_shell_escape(hook, prompt):
    assert hook._is_command_shaped(prompt)
    assert hook._is_coordination_task(prompt)


@pytest.mark.parametrize("prompt", NOT_COMMAND_SHAPED)
def test_command_shape_rejects_prose_and_outward_commands(hook, prompt):
    assert not hook._is_command_shaped(prompt)


def test_own_url_host_is_case_insensitive(hook, tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:ypollak2/llm-router.git\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    getattr(hook, "_OWN_REMOTES_CACHE", {}).clear()
    assert hook._is_own_repo_url("HTTPS://GITHUB.COM/YPOLLAK2/LLM-ROUTER/pull/601")
    assert hook._is_own_repo_url("https://GitHub.com/ypollak2/llm-router/pull/601")
    assert not hook._is_own_repo_url("https://GITHUB.COM/someone/else/pull/601")
