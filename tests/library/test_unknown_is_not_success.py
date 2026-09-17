"""An outcome nobody observed is not a successful one.

`_outcome` reads a tool event and answers what happened. Its final line was
``return "ok", 0``, reached whenever `tool_response` is not a dict, or is a
dict carrying no `exit_code`, no `returnCode`, no `is_error` and no `error`.

That is the "I could not tell" branch, and it answered "it worked".

The library is the memory this project reasons from later. A command whose
result was never observed, recorded as `ok`, becomes evidence that a fix
succeeded — and the whole point of the engineering-memory layer is that a
remembered false diagnosis is repeated more confidently than a forgotten one.
`sealer.is_seal_event` already refuses to seal a chapter on anything but `ok`,
so the fallback was also quietly deciding that unverified git commits sealed
chapters.

`unknown` is a third answer, not a shade of failure. It says the event
happened and its result was not observed, which is exactly true and is what a
later reader needs in order to go and check.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HARVEST = (Path(__file__).resolve().parent.parent.parent
            / "src" / "llm_router" / "hooks" / "library-harvest.py")
_SPEC = importlib.util.spec_from_file_location("library_harvest", _HARVEST)
harvest = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(harvest)


@pytest.mark.parametrize("response,expected,why", [
    ({"exit_code": 0}, "ok", "an observed success"),
    ({"exit_code": 1}, "error", "an observed failure"),
    ({"returnCode": 0}, "ok", "the other spelling of exit code"),
    ({"is_error": True}, "error", "an explicit error flag"),
    ({"error": "boom"}, "error", "an error message"),
    ({"interrupted": True}, "interrupted", "the user stopped it"),
    ({}, "unknown", "a dict that says nothing about the result"),
    ({"stdout": "done"}, "unknown", "output is not an exit code"),
    (None, "unknown", "no tool_response at all"),
    ("some string", "unknown", "a response that is not a dict"),
    ({"exit_code": "0"}, "unknown", "a STRING exit code is not an int — the "
                                    "old code fell through to ok here too"),
])
def test_an_unobserved_result_is_unknown(response, expected, why):
    outcome, _code = harvest._outcome({"tool_response": response})
    assert outcome == expected, why


def test_unknown_carries_no_exit_code():
    """Zero would be a claim. None is the absence of one."""
    _outcome, code = harvest._outcome({"tool_response": {}})
    assert code is None, (
        f"an unobserved outcome reported exit code {code!r}, which reads as "
        f"a result somebody saw"
    )


def test_an_unknown_outcome_does_not_seal_a_chapter():
    """Sealing is an assertion that a milestone happened and held."""
    from llm_router.library.sealer import is_seal_event

    observed = {"tool": "Bash", "outcome": "ok", "cmd": "git commit -m x"}
    unobserved = {"tool": "Bash", "outcome": "unknown", "cmd": "git commit -m x"}

    assert is_seal_event(observed) == "commit"
    assert is_seal_event(unobserved) is None, (
        "a commit nobody watched succeed sealed a chapter as though it had"
    )


def test_no_consumer_treats_unknown_as_success():
    """The guard: `!= "error"` is the easy way to reintroduce this.

    Anything that asks "did it work?" by ruling out failure will count
    `unknown` as a yes. The question has three answers now.
    """
    src = Path(__import__("llm_router").__file__).resolve().parent
    offenders = []
    for path in list(src.rglob("*.py")) + list(src.rglob("*-*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("#", '"""', "'''", "*")):
                continue
            if 'outcome' in stripped and (
                '!= "error"' in stripped or "!= 'error'" in stripped
            ):
                offenders.append(f"{path.relative_to(src)}:{i}")
    assert not offenders, (
        "these decide success by ruling out failure, which counts an "
        "unobserved outcome as a success: " + ", ".join(offenders)
    )
