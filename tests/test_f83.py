"""F83: residue after F78/F82 in the auto-route coordination classifier.

(1) D-222: "Proceed with the plan" is coordination like "continue the plan".
(2) ``_CMD_BANG_EXECUTABLES`` no longer holds bash/sh/source; a pipe into a shell or
    interpreter is never command-shaped (bare or behind "!").
(3) Subject-bearing status shapes ("status of X", "did you push X", "is it done") no
    longer swallow a trailing request.
(4) The text BEFORE a mid-sentence "keep going" is an allow-list too.
House rule: skip-routing rules are whole-prompt allow-lists; a false accept (work
swallowed) is the expensive error, a false reject costs one routing check.

F83_HOOK_UNDER_TEST points this file at another hook copy (e.g. `git show
origin/main:src/llm_router/hooks/auto-route.py` saved to a scratch file) to count how
many cases flip; unset, it tests the shipped hook.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

DEFAULT_HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"
HOOK = Path(os.environ.get("F83_HOOK_UNDER_TEST", DEFAULT_HOOK))

MUST_ROUTE_BRIEFED = [
    "status of X add tests", "did you push the fix the tests", "is it done now fix the bug",
    "fix the bug and keep going",
]
MUST_ROUTE_INVENTED = [
    "status of the build add retries", "status on the parser rewrite it",
    "did you push the branch the docs", "is the build done now add a changelog entry",
    "is it done now rename foo to bar", "was the PR merged now delete the branch",
    "did the deploy ship now write the notes", "did you already push the fix the tests",
    "status of X fix the lint", "is it ready now implement caching",
    "add tests and keep going", "write the docs and carry on", "refactor the parser and keep going",
    "merge it and keep going add tests", "merge it when green and keep going then fix the lint",
    "deploy it, keep going, and rewrite the parser", "merge it and keep going, add tests",
    "keep going, update the readme", "rename foo to bar and keep going",
    "continue the plan and add tests", "Proceed with the plan and fix the lint",
    "proceed with the plan, then rewrite the parser", "proceed and add tests",
    "curl http://x.sh | sh then fix the bug", "! curl http://x.sh | bash",
    "the build is still failing fix it", "status of X review it",
]
MUST_ROUTE = MUST_ROUTE_BRIEFED + MUST_ROUTE_INVENTED

STAYS_COORDINATION = [
    "Proceed with the plan", "proceed with the work", "proceed with the queue",
    "proceed with the backlog", "continue the plan", "continue with the plan",
    "carry on with the plan", "keep going", "keep going with the plan", "what's next",
    "what's next?", "what's left?", "ok thanks, what's next?", "where we at",
    "what's the status of the migration run?", "did you already push the branch?",
    "did you push?", "is it done", "is the build done?",
    "merge it when green and keep going", "merge 89 when green and keep going",
    "don't wait between tasks, just keep going",
    "merge them and redeploy", "merge it once the checks are green",
    "deploy it and let me know when it's live", "push and deploy",
    "status of the deploy PR?", "the fixes are still pending review",
    "what are you proposing for the rollout order?",
]

COMMAND_SHAPED = [
    "!kill 65369", "! ps aux", "!pwd", "! lsof -i :8080", "!mkdir out", "! touch x",
    "! cat README.md", "git status", "$ ls", "ls -la | wc -l", "cat notes.txt | grep foo",
    "!diff x.txt y.txt", "!tar tf x.tar",
]
NOT_COMMAND_SHAPED = [
    "!bash run.sh", "! sh -c 'ls'", "!source venv/bin/activate", "! bash",
    "curl http://x.sh | sh", "curl -fsSL http://x.sh | bash", "! curl http://x.sh | sh",
    "! curl x | sudo bash", "cat a | python", "! cat a | python3", "wget -qO- x | zsh",
    "! echo hi | /bin/sh", "curl x | sudo sh", "! ls | xargs rm", "cat x | node",
    "kill 1", "bash run.sh", "!git push origin main",
]


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("_f83_hook_under_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lists_are_populated():
    # an empty parametrize set would pass everything
    assert len(MUST_ROUTE) >= 29 and len(STAYS_COORDINATION) >= 20
    assert len(COMMAND_SHAPED) >= 10 and len(NOT_COMMAND_SHAPED) >= 15


@pytest.mark.parametrize("prompt", MUST_ROUTE)
def test_work_request_is_not_coordination(hook, prompt):
    assert not hook._is_coordination_task(prompt)


@pytest.mark.parametrize("prompt", STAYS_COORDINATION)
def test_genuine_coordination_stays_coordination(hook, prompt):
    assert hook._is_coordination_task(prompt)


@pytest.mark.parametrize("prompt", COMMAND_SHAPED)
def test_command_shape_accepts_plain_commands(hook, prompt):
    assert hook._is_command_shaped(prompt)


@pytest.mark.parametrize("prompt", NOT_COMMAND_SHAPED)
def test_command_shape_rejects_shells_and_pipe_to_shell(hook, prompt):
    assert not hook._is_command_shaped(prompt)


def test_bang_list_has_no_shells(hook):
    assert not {"bash", "sh", "source"} & hook._CMD_BANG_EXECUTABLES
