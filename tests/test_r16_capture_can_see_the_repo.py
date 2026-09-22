"""R16 — the audit's diagnosis was wrong, and the test premise caught it.

The finding said: `prompt_capture.capture()` has no `cwd`/`tools`/`external`
parameters, so repo- and tool-bound tasks are **permanently ineligible — a
missing signature, not a policy.**

The second half is false. Writing the premise assertion for the fix disproved
it:

    envelope.build(..., cwd=None)  ->  RepoState(commit=..., reconstructable=True)

`envelope.build` already falls back to `os.getcwd()`, so a repo reference was
being captured all along. The parameter was never what blocked repo tasks.

The actual blocker is `eligibility.replay_available()`, which returns **False**:

    assess(repo task, has_repo_state=True, envelope_complete=True)
      -> replayable=False, reasons=['no-replayer-for-required-state']

That is H-08, and it is a POLICY — a deliberate, documented refusal to admit
work the runner cannot execute. `scripts/groundtruth/` contains zero
`git checkout` / `git apply` / worktree call sites, so a captured repo task
could never be graded. The gate is tied to the capability rather than a flag,
so repo tasks become eligible automatically once a replayer exists.

So R16's remediation is the SECOND branch of its own acceptance criteria:
state that Ground Truth covers state-free prompts only, and say what the gap
is. The signature fix is kept because it is independently correct — an
explicit `cwd` beats the process working directory, which is not reliably the
task's repo, and `tool_names`/`external` have no fallback at all — but it is
not what unblocks anything, and this file exists partly to stop anyone
believing it was.

THE GAP CANNOT BE QUANTIFIED AS A SHARE OF TRAFFIC TODAY. There is exactly one
captured prompt on this machine and no pool, because capture has been off. A
percentage derived from n=1 is precisely the figure this audit exists to stop.
What IS exact: 100% of tasks requiring repo state are excluded, by design,
until a replayer exists.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import subprocess
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC.parent / "scripts"))


def test_capture_accepts_and_forwards_the_state_parameters():
    """The signature fix. Correct, and not what unblocks repo tasks."""
    from llm_router.prompt_capture import capture

    params = inspect.signature(capture).parameters
    for needed in ("cwd", "tool_names", "external", "test_command"):
        assert needed in params, f"capture() has no {needed!r} parameter"

    tree = ast.parse((SRC / "llm_router" / "prompt_capture.py").read_text(encoding="utf-8"))
    call = next(
        c for c in ast.walk(tree)
        if isinstance(c, ast.Call) and ast.unparse(c.func).endswith("_accumulate")
    )
    passed = {kw.arg for kw in call.keywords}
    for needed in ("cwd", "tool_names", "external", "test_command"):
        assert needed in passed, (
            f"capture() accepts {needed!r} and drops it before accumulate() — "
            "the same defect with a more convincing surface"
        )


def test_the_router_cwd_expression_is_actually_in_scope():
    """The bug the first draft of this fix had, kept as a regression.

    It passed `project_root`, which `_finalize_successful_route` does not
    receive. That is a NameError on every capture, landing in the enclosing
    `except Exception: capture never breaks routing` — so the feature would
    have been silently dead in exactly the way the defect it fixes was.
    """
    import llm_router.router as router

    import textwrap

    fn_src = textwrap.dedent(inspect.getsource(router._finalize_successful_route))
    tree = ast.parse(fn_src)
    fn = tree.body[0]
    call = next(
        (c for c in ast.walk(fn)
         if isinstance(c, ast.Call)
         and any(kw.arg == "cwd" for kw in getattr(c, "keywords", []))),
        None,
    )
    assert call is not None, "the router no longer passes a cwd to capture()"
    expr = next(kw.value for kw in call.keywords if kw.arg == "cwd")
    names = {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}
    bound = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    bound |= {
        (a.asname or a.name).split(".")[0]
        for n in ast.walk(fn)
        if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names
    }
    bound |= {
        t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }
    unbound = names - bound
    assert not unbound, (
        f"the cwd expression uses name(s) not bound in the function: "
        f"{sorted(unbound)}. That is a NameError swallowed by the capture "
        "path's own except clause — the feature would be silently dead."
    )


def test_the_envelope_never_needed_the_parameter_to_find_a_repo(tmp_path):
    """The premise assertion that disproved the audit's diagnosis.

    If this ever starts failing, `envelope.build` has stopped falling back to
    the process cwd — and the R16 finding becomes true after all.
    """
    from groundtruth import envelope as envmod

    bare = envmod.build(
        prompt="what does a.txt contain?", required_state=["repo"],
        route_id="r", session_id="s", prompt_sha256="h", task_type="query",
    )
    assert bare.repo is not None and bare.repo.reconstructable, (
        "envelope.build with no cwd no longer resolves a repo. R16's original "
        "diagnosis — 'a missing signature' — would now be correct, and this "
        "file's reasoning needs revisiting."
    )


def test_repo_tasks_are_refused_by_policy_not_by_accident():
    """The REAL blocker, asserted so it cannot be misdiagnosed again."""
    from groundtruth.eligibility import assess, replay_available

    assert replay_available() is False, (
        "a replayer now exists — repo tasks should become eligible "
        "automatically, and the docs' 'state-free prompts only' scoping "
        "should be revisited in the same commit"
    )
    e = assess(
        "fix the off-by-one in src/llm_router/cost.py line 42",
        task_type="code", has_repo_state=True, envelope_complete=True,
    )
    assert not e.replayable
    assert "no-replayer-for-required-state" in e.ineligibility_reasons, (
        f"a repo task with perfect state is refused for some other reason: "
        f"{e.ineligibility_reasons}. The documented cause is the absence of a "
        "replayer; if that changed, the scoping in the docs is now wrong."
    )


def test_a_replayer_would_flip_the_gate_without_a_code_change(monkeypatch):
    """The gate is tied to the CAPABILITY, not to a hand-maintained flag.

    This is what makes "state-free only" a scoped claim rather than a
    permanent one — and it is worth pinning, because a flag would silently rot.
    """
    from groundtruth import eligibility

    monkeypatch.setattr(eligibility, "replay_available", lambda: True)
    e = eligibility.assess(
        "fix the off-by-one in src/llm_router/cost.py line 42",
        task_type="code", has_repo_state=True, envelope_complete=True,
    )
    assert "no-replayer-for-required-state" not in e.ineligibility_reasons, (
        "with a replayer available the task is STILL refused for lack of one"
    )


def test_git_is_available_for_the_premise_test():
    """Anti-vacuity: the envelope test proves nothing without git."""
    r = subprocess.run(["git", "--version"], capture_output=True)
    assert r.returncode == 0, "git is unavailable; the envelope premise is untested"
