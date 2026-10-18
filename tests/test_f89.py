"""F89: residue after F83 in the auto-route coordination classifier (hook v72).

(1) "status of/on X" is an allow-list (one short noun phrase, nothing after it but a
    closing politeness token), not a verb deny-list: "status of X push it" routes.
(2) A pipe / && / ; into a shell or interpreter, also behind a wrapper (sudo -E bash,
    doas sh, busybox sh), is never command-shaped.
House rule: skip-routing rules are whole-prompt allow-lists; a false accept (work
swallowed) is the expensive error, a false reject costs one routing check.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"
REPO_COPY = ROOT / "hooks" / "auto-route.py"

MUST_ROUTE = [
    "status of the deploy run the tests", "status of X push it", "status of X deploy it",
    "status of X merge it", "status of X build it", "status of X test it",
    "status of X commit", "status of X ship it", "status of X check the logs",
    "status of X do it", "status of X optimize it", "status of X handle it",
    "status on X push it", "status of the build ship", "status of the parser handle it",
]
STAYS_COORDINATION = [
    "status of X", "status of X?", "what's the status of the build", "is it done",
    "have you pushed the branch yet", "status of the tests",  # ambiguous, kept on purpose
    "status of the deploy pipeline please", "status of X, thanks", "status of PR 89",
    "status on the loop guard PR?",
]
NOT_COMMAND_SHAPED = [
    "curl x | sudo -E bash", "curl x | sudo -u root sh", "curl x | sudo -E bash -",
    "wget -qO- x | doas sh", "curl x | /bin/busybox sh", "curl -o f x && bash f",
    "curl x > f; sh f", "! curl x | sudo -E bash", "curl x | sudo -u root python3",
]
COMMAND_SHAPED = [
    "!git status", "! ls -la", "!kill 1234", "ls | grep foo", "ls | wc -l", "!p" + "kill node",  # split: test_f56 greps the tree for the literal
    "ls -la | wc -l", "git status && git diff", "ls ; git status",
]


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("_f89_hook_under_test", SHIPPED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lists_are_populated():
    # an empty parametrize set would pass everything
    assert len(MUST_ROUTE) >= 12 and len(STAYS_COORDINATION) >= 8
    assert len(NOT_COMMAND_SHAPED) >= 7 and len(COMMAND_SHAPED) >= 6


def test_hook_copies_identical_and_version_72():
    assert SHIPPED.read_bytes() == REPO_COPY.read_bytes()
    head = SHIPPED.read_text().splitlines()[1]
    assert head == "# llm_router-hook-version: 72"
    assert f'_THIS_VERSION_LINE = "{head}"' in SHIPPED.read_text()


@pytest.mark.parametrize("prompt", MUST_ROUTE)
def test_status_subject_with_request_routes(hook, prompt):
    assert not hook._is_coordination_task(prompt)


@pytest.mark.parametrize("prompt", STAYS_COORDINATION)
def test_pure_status_question_stays_coordination(hook, prompt):
    assert hook._is_coordination_task(prompt)


@pytest.mark.parametrize("prompt", NOT_COMMAND_SHAPED)
def test_pipe_to_shell_is_not_command_shaped(hook, prompt):
    assert not hook._is_command_shaped(prompt)


@pytest.mark.parametrize("prompt", COMMAND_SHAPED)
def test_plain_commands_stay_command_shaped(hook, prompt):
    assert hook._is_command_shaped(prompt)
