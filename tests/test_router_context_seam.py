"""router.py is the last path that attaches context by hand. Two defects.

ONE — the AST branch was unreachable from the router.

`context_prep.prepare_prompt` gates its code-context branch on
``task_type in (CODE, ANALYZE) and project_dir``. router.py called it without
`project_dir`, so for every routed CODE task the branch evaluated false and the
model got no source. The router resolves a perfectly good scope forty lines
further down, for OKF; it just never handed it over. Nothing failed, nothing
logged — the feature reported `context_source="none"` and looked like a
retrieval miss.

TWO — it injected OKF itself.

`tests/test_okf_choke_point.py` exists because injection reached 2 of 7
execution paths, and its rule is that every path crosses
`context_injection.inject()`. router.py was exempted by name in the skip tuple.
That exemption is removed with this change, so the rule now has no exceptions.

WHAT THE SWAP ACTUALLY CHANGES, measured before it was made

`inject()` composes three sources where router.py composed one: OKF concepts,
repo facts (branch, HEAD, dirty state — read from git), and session context
when a session_id is given. `scripts/shadow_diff_router_injection.py` built
both forms for 12 router-shaped prompts:

    OKF half byte-identical            12/12
    delta                              uniform across all 12 prompts
    that delta                         the <repo_state> block, nothing else

So retrieval does not change. What changes is that a routed prompt now carries
the repository state every other execution path already carried.

The delta's MAGNITUDE is not fixed and this docstring used to say it was
("+252 bytes, constant"). `<repo_state>` renders the branch name, the last
commit subject and the dirty-file list, so it measured ~211 bytes on a clean
tree and ~260 on a dirty one. An audit caught it. What is stable, and what the
swap actually rests on, is that the delta is the same for every prompt at a
given moment and that the OKF half does not move at all.

The session block is deliberately NOT adopted here. router.py has an
`agent_session_id` and passing it would be a second, larger behaviour change
riding on a deduplication — exactly the thing the shadow diff was run to avoid.

The call-site checks below are source assertions, like the choke-point test
they extend, and they are paired with a behavioural test that the branch they
enable actually fires. Neither is sufficient alone: the source check cannot
prove the feature works, and the behavioural check cannot prove the router is
the one calling it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__import__("llm_router").__file__).resolve().parent
ROUTER = (SRC / "router.py").read_text(encoding="utf-8")


def test_the_router_tells_context_prep_which_project_it_is_in():
    """Without this argument the AST branch in context_prep cannot run."""
    calls = re.findall(r"_prepare\(\s*(.*?)\n\s*\)", ROUTER, re.DOTALL)
    assert calls, "router.py no longer calls _prepare — update this test"
    for args in calls:
        assert "project_dir=" in args, (
            "router.py calls context_prep without project_dir, so "
            "`task_type in (CODE, ANALYZE) and project_dir` is false for every "
            f"routed call and the code-context branch is dead:\n{args}"
        )


def test_the_router_passes_a_normalised_root_not_a_raw_one():
    """The MCP client reports whatever it likes; result_cache hashes it.

    `root_from_ctx` returns the client's reported workspace root with no `.git`
    walk. `result_cache._get_db_path` turns its `project_dir` into a FILE PATH,
    and a second spelling of one project there orphans a database that is never
    reopened and therefore never purged. Normalise once, at the seam.
    """
    assert "resolve_scope(" in ROUTER, (
        "router.py hands a raw client-reported root downstream instead of "
        "normalising it through semantic.scope.resolve_scope"
    )


def test_the_router_no_longer_attaches_okf_itself():
    """The choke-point rule, now without its one exemption."""
    offenders = [
        f"router.py:{i}"
        for i, line in enumerate(ROUTER.splitlines(), 1)
        if ".inject_context(" in line and not line.strip().startswith(("#", "*"))
    ]
    assert not offenders, (
        "router.py still attaches OKF context itself rather than calling "
        "context_injection.inject(): " + ", ".join(offenders)
    )


def test_the_router_does_not_quietly_start_sending_session_context():
    """`inject()` will add the session block the moment a session_id appears.

    That is a bigger change than the one this seam is for, and it must be a
    decision rather than a side effect of the swap. If it is adopted later, it
    gets its own commit, its own shadow diff and its own line here.
    """
    # Any alias — the module is imported as `_ctx_inject` at the call site, and
    # a test that only knows one spelling stops guarding the moment it changes.
    call = re.compile(r"\b\w*\.?inject\(\s*(.*?)\n\s*\)", re.DOTALL)
    calls = [m.group(1) for m in call.finditer(ROUTER)]
    assert calls, "router.py no longer calls the choke point — update this test"
    for args in calls:
        assert "session_id=" not in args, (
            "the choke-point swap started passing a session_id, which adds the "
            f"session-context block to every routed prompt:\n{args}"
        )


def test_intent_gates_measure_the_user_prompt_not_the_attached_context():
    """Injected context is not intent, and length gates must not count it.

    `_short_prompt` decided whether to escalate on quality by measuring
    `len(prompt)` — the prompt AFTER attachment. Latent while attachment only
    happened when OKF matched; live the moment it became unconditional, because
    every prompt then gained a constant ~250 bytes of repository state and
    "say OK" stopped being a short prompt. Five tests failed on it, and they
    were right to.

    The rule: what gets SENT is `prompt` and that is correct for cost and token
    estimates. What the user MEANT is `user_prompt`, and that is what an intent
    gate reads.
    """
    assert "user_prompt: str | None = None" in ROUTER, (
        "_dispatch_model_loop no longer receives the pre-attachment prompt, so "
        "every length-based intent gate inside it is measuring injected context"
    )
    body = ROUTER[ROUTER.index("_short_prompt ="):][:400]
    assert "len(prompt)" not in body, (
        "the short-prompt guard is measuring the attached prompt again:\n" + body
    )


class TestTheBranchThisEnablesActuallyFires:
    """Behavioural half: a project_dir must produce source context."""

    def test_code_task_with_a_project_dir_gets_code_context(self, tmp_path):
        from llm_router.classify import Complexity, TaskType
        from llm_router.context_prep import prepare_prompt

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "reconciler.py").write_text(
            "def reconcile_invoice(a, b):\n"
            "    '''Match invoice lines against ledger entries.'''\n"
            "    return a == b\n",
            encoding="utf-8",
        )

        with_dir = prepare_prompt(
            user_prompt="fix reconcile_invoice in reconciler.py",
            task_type=TaskType.CODE,
            complexity=Complexity.MODERATE,
            target_model="qwen3-coder:30b",
            project_dir=str(repo),
        )
        without_dir = prepare_prompt(
            user_prompt="fix reconcile_invoice in reconciler.py",
            task_type=TaskType.CODE,
            complexity=Complexity.MODERATE,
            target_model="qwen3-coder:30b",
        )

        assert "ast" in with_dir.context_source, (
            f"a CODE task with a real project_dir got no source context "
            f"(context_source={with_dir.context_source!r})"
        )
        assert "ast" not in without_dir.context_source, (
            "the control case already had source context, so this test would "
            "pass whether or not the router passes project_dir"
        )
        assert "reconcile_invoice" in with_dir.context
