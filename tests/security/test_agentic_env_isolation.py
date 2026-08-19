"""RED6-01 (P0) — a delegated subprocess must not inherit the parent's secrets.

The delegation surface (`llm_act` / `llm_delegate`) spawns processes that run
model-authored commands. Those processes were started with no `env=`, so they
inherited the whole parent environment: every provider API key the router holds.
One `printenv` in a command the model was talked into writing was enough.

The canary here is `FAKE_KEY`. That name is chosen, not arbitrary: it matches
**nothing** in `safe_subprocess._SECRET_ENV_VARS`, so the pre-existing denylist
forwards it happily. It is the shape of every credential the denylist has not
been taught about yet — which, historically, has included `AWS_ACCESS_KEY_ID`,
`GH_PAT` and `DATABASE_URL`, per that module's own comment.

The tests below assert the *allowlist* property — "only these variables are
present" — rather than "the canary is absent". Absence of one name is what a
denylist can also achieve, right up until the day it cannot.
"""

from __future__ import annotations

import pytest

from llm_router.safe_subprocess import _ENV_ALLOWLIST, get_delegated_env, get_safe_env

CANARY = "FAKE_KEY"
CANARY_VALUE = "sk-NOTREAL-000"

REAL_LOOKING_SECRETS = {
    CANARY: CANARY_VALUE,
    "ANTHROPIC_API_KEY": "sk-ant-NOTREAL",
    "OPENAI_API_KEY": "sk-NOTREAL",
    "AWS_ACCESS_KEY_ID": "AKIANOTREAL",
    "GH_PAT": "ghp_NOTREAL",
    "DATABASE_URL": "postgres://u:p@h/db",
    "MY_COMPANY_INTERNAL_CRED": "NOTREAL",
}


@pytest.fixture
def parent_env_with_secrets(monkeypatch):
    for key, value in REAL_LOOKING_SECRETS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/tmp")
    return REAL_LOOKING_SECRETS


def test_delegated_env_contains_only_allowlisted_names(parent_env_with_secrets):
    """The property that matters: nothing is present unless it was named."""
    env = get_delegated_env()
    from llm_router.safe_subprocess import _ENV_ALLOWLIST_PREFIXES

    unexpected = [
        k
        for k in env
        if k not in _ENV_ALLOWLIST and not k.startswith(_ENV_ALLOWLIST_PREFIXES)
    ]
    assert unexpected == [], f"non-allowlisted variables reached the child: {unexpected}"


@pytest.mark.parametrize("secret", sorted(REAL_LOOKING_SECRETS))
def test_no_secret_reaches_the_delegated_env(parent_env_with_secrets, secret):
    assert secret not in get_delegated_env()


def test_the_denylist_leaks_the_canary_and_the_allowlist_does_not(parent_env_with_secrets):
    """Pins WHY this is an allowlist, so nobody 'simplifies' it back.

    If this ever fails because the denylist grew a pattern for FAKE_KEY, that is
    not a fix — it is the same whack-a-mole one round later. Rename the canary.
    """
    assert CANARY in get_safe_env(), (
        "the denylist no longer leaks the canary; pick a fresh unrecognised name "
        "rather than concluding the denylist is now sufficient"
    )
    assert CANARY not in get_delegated_env()


def test_child_process_cannot_read_a_secret_the_blocklist_never_sees(
    parent_env_with_secrets, tmp_path, monkeypatch
):
    """End to end through the real executor, with the blocklist deliberately bypassed.

    The variable is named `NOTES`, not `FAKE_KEY`. That is the whole test. A
    credential-shaped name trips the command blocklist, so using one would prove
    only that the blocklist works — and the blocklist is not the boundary. An
    innocuous name is the denylist's exact blind spot (`AWS_ACCESS_KEY_ID`,
    `GH_PAT` and `DATABASE_URL` all leaked in production for the same reason),
    and `echo $NOTES` sails past every pattern in `_bash_block_reason`.

    It still leaks nothing, because the value is not in the child to be read.
    """
    from llm_router.agentic.react import _bash_block_reason, default_tool_executor

    monkeypatch.setenv("NOTES", CANARY_VALUE)
    command = "echo [$NOTES]"

    assert _bash_block_reason(command) is None, (
        "this test is only meaningful while the command is NOT blocked — "
        "pick another bypass"
    )

    execute = default_tool_executor(cwd=str(tmp_path))
    out = execute("bash", {"command": command})

    assert CANARY_VALUE not in out, f"secret leaked to the child: {out!r}"
    assert "[]" in out, f"expected an empty expansion, got: {out!r}"


def test_child_process_can_still_do_useful_work(parent_env_with_secrets, tmp_path):
    """Isolation that breaks the tool is not a fix, it is an outage.

    PATH and HOME must survive or the child cannot exec anything.
    """
    from llm_router.agentic.react import default_tool_executor

    execute = default_tool_executor(cwd=str(tmp_path))
    out = execute("bash", {"command": "echo hello && pwd"})

    assert "hello" in out
    assert "[exit 0]" in out


def test_codex_adapter_runner_passes_an_allowlisted_env(parent_env_with_secrets, monkeypatch):
    """RED6-01, CodexAdapter arm.

    `subprocess_runner` bypassed `codex_agent.run_codex()` — the guarded path —
    so the protection that existed in this codebase was routed around rather
    than absent. Asserted at the subprocess boundary because that is where the
    bypass happened.
    """
    import subprocess as _subprocess

    from llm_router.agentic import adapters

    captured: dict = {}

    def _fake_run(argv, **kwargs):
        captured.update(kwargs)

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(_subprocess, "run", _fake_run)
    adapters.subprocess_runner(["codex", "exec"], "do the thing")

    env = captured.get("env")
    assert env is not None, "subprocess_runner spawned a child with an inherited environment"
    for secret in REAL_LOOKING_SECRETS:
        assert secret not in env, f"{secret} reached the agent CLI"


def test_agent_cli_passthrough_carries_no_credentials():
    """The explicit passthrough is where a key would most plausibly creep back in."""
    from llm_router.agentic.adapters import _AGENT_CLI_PASSTHROUGH

    for name in _AGENT_CLI_PASSTHROUGH:
        upper = name.upper()
        assert not any(
            marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CRED")
        ), f"{name} looks like a credential and must not be passed to a delegated CLI"
