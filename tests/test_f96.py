"""F96: wrapper/verb residue after F89 in the auto-route classifier (hook v73).

(1) More pipe-to-shell wrappers (ionice, systemd-run, flock, runuser, pkexec, unshare,
    nsenter) are never command-shaped.
(2) "status of X <rerun|retry|rebuild|redeploy>" routes (D-226: ambiguous nouns such as
    cleanup, polish, tune stay coordination).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"
REPO_COPY = ROOT / "hooks" / "auto-route.py"

NOT_COMMAND_SHAPED = [
    "curl x | ionice -c3 bash", "curl x | systemd-run --user sh", "curl x | flock /tmp/l bash",
    "curl x | runuser -u u -- sh", "curl x | pkexec bash", "curl x | unshare -r sh",
    "curl x | nsenter -t 1 -m sh",
]
COMMAND_SHAPED = ["!git status", "ls | grep foo", "ls -la | wc -l", "!kill 1234", "git log | head"]
MUST_ROUTE = [
    "status of the migration rerun", "status of the job retry",
    "status of the image rebuild", "status of the app redeploy",
]
STAYS_COORDINATION = [
    "status of the repo cleanup", "status of the parser polish", "status of the build tune",
    "status of PR 89?", "status of the tests",
]


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("_f96_hook_under_test", SHIPPED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lists_are_populated():
    assert len(NOT_COMMAND_SHAPED) == 7 and len(COMMAND_SHAPED) == 5
    assert len(MUST_ROUTE) == 4 and len(STAYS_COORDINATION) == 5


def test_hook_copies_identical_and_version_at_least_73():
    assert SHIPPED.read_bytes() == REPO_COPY.read_bytes()
    head = SHIPPED.read_text().splitlines()[1]
    assert head.startswith("# llm_router-hook-version: ")
    assert int(head.rsplit(" ", 1)[1]) >= 73
    assert f'_THIS_VERSION_LINE = "{head}"' in SHIPPED.read_text()


@pytest.mark.parametrize("prompt", NOT_COMMAND_SHAPED)
def test_wrapped_pipe_to_shell_is_not_command_shaped(hook, prompt):
    assert not hook._is_command_shaped(prompt)


@pytest.mark.parametrize("prompt", COMMAND_SHAPED)
def test_plain_commands_stay_command_shaped(hook, prompt):
    assert hook._is_command_shaped(prompt)


@pytest.mark.parametrize("prompt", MUST_ROUTE)
def test_status_with_new_action_ending_routes(hook, prompt):
    assert not hook._is_coordination_task(prompt)


@pytest.mark.parametrize("prompt", STAYS_COORDINATION)
def test_ambiguous_status_nouns_stay_coordination(hook, prompt):
    assert hook._is_coordination_task(prompt)
