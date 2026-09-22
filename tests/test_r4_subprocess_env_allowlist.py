"""A model-chosen command must not inherit the operator's credentials — R4.

`agent_loop.execute_tool("run_command", ...)` ran `subprocess.run(argv, ...)`
with no `env=`, so the child inherited the FULL parent environment: every
provider key, OAuth token and cloud credential in the process.

The command allowlist cannot mitigate this. It permits `python`, `python3`,
`node`, `awk`, `sed`, `find`, `go`, `cargo` and `git` — ten of its 28 entries
are general-purpose interpreters, every one of which can read `os.environ`.
So A-01 (execution) and A-02 (credentials) compose into one chain.

Measured against the real `execute_tool`:

    before:  LEAKED: ['ANTHROPIC_API_KEY', 'TOTALLY_UNKNOWN_SECRET']
    after:   LEAKED: nothing

`TOTALLY_UNKNOWN_SECRET` is the point of the allowlist. A denylist can only
strip names it knows; an allowlist carries nothing that was not named, so a
credential whose variable name nobody anticipated is absent by construction.

THE ENUMERATION IS THE DELIVERABLE. Fixing this one call site without a test
that finds the next one is how `verifiers.py` got the allowlist on
2026-09-22 and this sibling did not.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

#: Call sites that execute MODEL- OR CALLER-SUPPLIED argv. These are the ones
#: that matter: a hardcoded `["git", "rev-parse"]` inheriting the environment
#: is normal and often necessary, while a command a model chose is not.
#:
#: 51 of the ~60 subprocess sites in src/ inherit the environment. Asserting on
#: all of them would produce a failure list long enough that the only practical
#: response is to allowlist it wholesale — and then the allowlist IS the blind
#: spot. (Same reasoning as the provider-family scan in R11, which was narrowed
#: after it flagged `_CHEAP_PROVIDERS`.)
#:
#: ADD TO THIS LIST when a new site executes argv the caller supplies.
MODEL_ARGV_SITES = {
    "llm_router/hooks/agent_loop.py": "run_command tool: argv chosen by the model",
    "llm_router/tools/local_task.py": "acceptance check: argv from the caller",
}


def _subprocess_sites():
    """Every subprocess.run/Popen in src/, with whether it passes `env=`."""
    out = []
    for f in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = ast.unparse(node.func)
            if fn not in ("subprocess.run", "subprocess.Popen",
                          "subprocess.check_output", "subprocess.call"):
                continue
            kwargs = {k.arg for k in node.keywords if k.arg}
            passes_env = "env" in kwargs
            out.append((str(f.relative_to(SRC)), node.lineno, fn, passes_env))
    return out


def test_the_scan_finds_subprocess_sites():
    """Anti-vacuity: a scan that finds nothing permits anything."""
    sites = _subprocess_sites()
    assert len(sites) >= 20, (
        f"only {len(sites)} subprocess call sites found — the AST scan has "
        "stopped matching src/"
    )


def test_the_agent_loop_does_not_inherit_the_environment():
    """The specific site the audit found leaking."""
    sites = [s for s in _subprocess_sites()
             if s[0] == "llm_router/hooks/agent_loop.py"]
    assert sites, "no subprocess call in agent_loop.py — did it move?"
    for rel, lineno, fn, passes_env in sites:
        assert passes_env, (
            f"{rel}:{lineno} calls {fn} without env= — a model-chosen command "
            "would inherit every credential in the process"
        )


def test_the_delegated_env_carries_no_provider_keys(monkeypatch):
    """The allowlist's defining property, including for an unknown name."""
    from llm_router.safe_subprocess import get_delegated_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-AUDITcanary")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-AUDITcanary")
    monkeypatch.setenv("TOTALLY_UNKNOWN_SECRET", "AUDITcanary")

    env = get_delegated_env()
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TOTALLY_UNKNOWN_SECRET"):
        assert name not in env, f"{name} crossed into the delegated environment"


def test_the_delegated_env_is_still_usable():
    """Anti-vacuity, and it caught a real mistake.

    An empty environment passes the test above and breaks every agent command.
    A first probe of this fix reported `command not found: python` and looked
    like a regression; PATH was in fact intact and `python` simply is not on
    it. The child must keep enough to run.
    """
    from llm_router.safe_subprocess import get_delegated_env

    env = get_delegated_env()
    assert env.get("PATH"), "the child has no PATH and can exec nothing"
    assert "HOME" in env


@pytest.mark.parametrize("rel", sorted(MODEL_ARGV_SITES))
def test_model_supplied_argv_never_inherits_the_environment(rel):
    """Every site that runs caller-chosen argv passes an allowlisted env.

    This is the part that makes R4 a class fix rather than a point fix. The
    agent_loop site was found by an auditor; `local_task` was found by this
    test on its first run.
    """
    sites = [s for s in _subprocess_sites() if s[0] == rel]
    assert sites, f"no subprocess call found in {rel} — did it move?"
    offenders = [f"{r}:{ln} {fn}" for r, ln, fn, env_ok in sites if not env_ok]
    assert not offenders, (
        f"{rel} executes caller-supplied argv without env=:\n  "
        + "\n  ".join(offenders)
        + f"\n\n({MODEL_ARGV_SITES[rel]})"
    )


def test_the_broader_inheritance_count_is_recorded():
    """Informational, and a ratchet on the rest.

    51 sites inherited at the time of writing. Most are hardcoded commands
    where that is correct. The number is pinned so a large jump is visible
    without pretending every one of them is a defect.
    """
    inheriting = [s for s in _subprocess_sites() if not s[3]]
    assert len(inheriting) <= 55, (
        f"{len(inheriting)} subprocess sites inherit the environment, up from "
        "51. If a new one runs caller-supplied argv, add it to "
        "MODEL_ARGV_SITES; if not, raise this number deliberately."
    )
