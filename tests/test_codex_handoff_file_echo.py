"""A successful Codex handoff is saved to a private file and the Agent call is ALLOWED with a one-line echo prompt,
so Claude Code does not label it "Error:" (docs/BUGS.md ECHO-1). The deny stays only as the fallback when the file
cannot be written. Real ``agent-route.py`` per scenario, a stub ``codex`` executable, state under a tmp HOME."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from tests._bind_support import (
    AGENT_ROUTE,
    SUITABLE_PROMPT,
    agent_payload,
    break_results_dir,
    decision_of,
    echo_path_of,
    hook_env,
    init_repo,
    ledger_rows,
    run_hook,
    write_codex_stub,
)

pytestmark = [
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell stub and file modes"),
    pytest.mark.timeout(180),
]

ANSWER = "STUB-CODEX-ANSWER-9d2f: the cursor skips the last row.\nSecond line with <b>markup</b>."


@pytest.fixture
def ctx(tmp_path):
    project = init_repo(tmp_path / "project")
    stub = write_codex_stub(tmp_path / "bin", argv_log=tmp_path / "argv.jsonl", answer=ANSWER)

    class Ctx:
        home = tmp_path

        def run(self, **payload_kw):
            payload_kw.setdefault("cwd", str(project))
            payload = agent_payload(SUITABLE_PROMPT, **payload_kw)
            cwd = tmp_path / "hookproc"
            cwd.mkdir(exist_ok=True)
            return run_hook(AGENT_ROUTE, payload, hook_env(tmp_path, CODEX_PATH=str(stub)), cwd=cwd)

    return Ctx()


def _delegated_row(home):
    return [r for r in ledger_rows(home) if r["outcome"] == "delegated"][-1]


def test_delegation_is_an_allow_with_updated_input_not_a_deny(ctx):
    d = decision_of(ctx.run(description="find the paging bug", run_in_background=False))
    assert "decision" not in d and "reason" not in d  # no legacy block pair
    hso = d["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse" and hso["permissionDecision"] == "allow"
    ui = hso["updatedInput"]
    assert ui["model"] == "haiku" and ui["subagent_type"] == "general-purpose"
    assert ui["run_in_background"] is False
    assert ui["description"] == "find the paging bug"  # kept
    path = echo_path_of(d)
    assert path in hso["permissionDecisionReason"] and "Codex" in hso["permissionDecisionReason"]


def test_result_file_is_private_and_holds_the_exact_answer(ctx):
    d = decision_of(ctx.run())
    path = echo_path_of(d)
    assert os.path.isabs(path) and path.startswith(str((ctx.home / ".llm-router" / "agent-results").resolve()))
    assert path.endswith(".md")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700


def test_result_file_content_is_exact(ctx):
    d = decision_of(ctx.run())
    got = open(echo_path_of(d), encoding="utf-8").read()
    assert got.strip() == ANSWER


def test_echo_prompt_names_the_path_and_asks_for_one_line(ctx):
    d = decision_of(ctx.run(description="find the paging bug"))
    prompt = d["hookSpecificOutput"]["updatedInput"]["prompt"]
    path = echo_path_of(d)
    assert f"Codex result saved at {path} (task: find the paging bug). Read that file for the full result." in prompt
    assert "no tools" in prompt.lower() and "nothing else" in prompt.lower()
    assert "STUB-CODEX-ANSWER" not in prompt and SUITABLE_PROMPT not in prompt  # no content, no original brief
    assert len(prompt) < 400, len(prompt)


def test_background_spawn_and_isolation_keys(ctx):
    d = decision_of(ctx.run(description="bg task", run_in_background=True, isolation="worktree"))
    ui = d["hookSpecificOutput"]["updatedInput"]
    assert ui["run_in_background"] is False  # the echo is a foreground one-liner either way
    assert "isolation" not in ui and "worktree" not in ui
    assert "Codex result saved at" in ui["prompt"]


def test_deny_fallback_when_the_directory_is_unwritable(ctx):
    break_results_dir(ctx.home)  # a regular file stands where the directory must be
    d = decision_of(ctx.run())
    assert d["decision"] == "block" and d["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "STUB-CODEX-ANSWER-9d2f" in d["reason"]
    assert _delegated_row(ctx.home)["delivery"] == "deny_fallback"


def test_ledger_delivery_field_and_unchanged_row(ctx):
    ctx.run(tool_use_id="toolu_echo")
    row = _delegated_row(ctx.home)
    assert row["delivery"] == "file_echo"
    assert row["lever"] == "agent_route_codex" and row["outcome"] == "delegated" and row["tool_use_id"] == "toolu_echo"
    assert row["answer_chars"] == len(ANSWER)
    # D-281: the model output is in the result file only, never in a ledger row
    ledger_text = "".join(json.dumps(r) for r in ledger_rows(ctx.home))
    assert "STUB-CODEX-ANSWER" not in ledger_text


def test_an_allowed_echo_is_not_counted_as_a_void_deny(ctx):
    import subprocess
    from tests._bind_support import HOOKS_SRC
    from llm_router import codex_handoff

    ctx.run(tool_use_id="toolu_e1")
    post = {"hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_use_id": "toolu_e1",
            "session_id": "bind-test-session", "tool_input": {"prompt": "x"}, "tool_response": {}}
    subprocess.run([sys.executable, str(HOOKS_SRC / "agent-depth-release.py")], input=json.dumps(post),
                   capture_output=True, text=True, cwd=str(ctx.home), env=hook_env(ctx.home))
    assert not [r for r in ledger_rows(ctx.home) if r["outcome"] == "delegation_void"]
    os.environ["LLM_ROUTER_HOME"] = str(ctx.home / ".llm-router")
    try:
        assert codex_handoff.voids_in_session("bind-test-session") == 0
    finally:
        del os.environ["LLM_ROUTER_HOME"]
