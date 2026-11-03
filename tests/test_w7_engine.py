"""W7 text-job-bind: the decision. A placeholder command gets a rewrite or a deny, never a pass.

The model call is a stub here (no real model is ever called); everything else is real: a real git
repository, the real scanner and planner, real files on disk, and, for the parse of the router's
answer, the router's own ``llm_text_job`` with only ``route_and_call`` replaced.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm_router.text_job_bind import engine, limits
from llm_router.text_job_bind.engine import GenResult, decide

T = '"${LLM_TEXT:?}"'
SECRET_DIFF_LINE = "api_key_is_not_a_secret_here = 'zq-unique-diff-marker'"
MODEL_TEXT = "app: change a.py\n\nExplain the change in a.py with `ticks` and $(not run)."


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))
    monkeypatch.delenv("LLM_ROUTER_LOCAL_TEXT_JOBS", raising=False)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main", ".")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "a.py").write_text("print('a')\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "base")
    (r / "a.py").write_text(f"print('a')\n{SECRET_DIFF_LINE}\n")
    git(r, "add", "a.py")
    return r


class Gen:
    """A model call stub that records what it was asked and answers as told."""

    def __init__(self, *results: GenResult, raises: BaseException | None = None) -> None:
        self.results = list(results) or [GenResult("accepted", MODEL_TEXT, "ollama/stub", 1, "")]
        self.calls: list[tuple] = []
        self.raises = raises

    def __call__(self, job, text, hint, timeout_s):
        self.calls.append((job, text, hint, timeout_s))
        if self.raises:
            raise self.raises
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def payload(command: str, repo: Path, **extra) -> dict:
    return {
        "session_id": "sess-1", "tool_use_id": "toolu_1", "cwd": str(repo), "hook_event_name": "PreToolUse",
        "tool_name": "Bash", "tool_input": {"command": command, "description": "d", "timeout": 60000,
                                            "run_in_background": False}, **extra,
    }


def run(command: str, repo: Path, tmp_path: Path, gen: Gen | None = None, **kw):
    rows: list[dict] = []
    clock = kw.pop("now", lambda: 1_700_000_000.0)
    d = decide(payload(command, repo, **kw.pop("extra", {})), generate=gen or Gen(), root=tmp_path / "bind",
               record=rows.append, now=clock, **kw)
    return d, rows


def files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "bind").glob("*.txt")) if (tmp_path / "bind").exists() else []


# ---- the rewrite ---------------------------------------------------------------------------------------

def test_an_accepted_text_becomes_a_file_and_the_command_reads_it(repo, tmp_path):
    gen = Gen()
    d, rows = run(f"git commit -m {T} -m 'Co-Authored-By: X <x@y>'", repo, tmp_path, gen)
    assert d.kind == "rewrite" and d.reason is None
    (f,) = files(tmp_path)
    assert d.tool_input["command"] == f"git commit -F {f}"
    assert f.read_text() == MODEL_TEXT + "\n\nCo-Authored-By: X <x@y>\n"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    # every other field of the tool input survives: updatedInput replaces the whole object
    assert d.tool_input["description"] == "d" and d.tool_input["timeout"] == 60000
    assert d.tool_input["run_in_background"] is False
    # the model was shown the real staged diff, and only the diff of this repo
    job, text, hint, _t = gen.calls[0]
    assert job == "commit_message" and SECRET_DIFF_LINE in text and "diff --git a/a.py b/a.py" in text
    assert "base" in (hint or "")
    assert d.note and "ollama/stub" in d.note and "not written by Claude" in d.note


def test_the_rewritten_command_commits_the_text_with_real_git(repo, tmp_path):
    d, _ = run(f"git commit -m {T}", repo, tmp_path)
    out = subprocess.run(["bash", "-c", d.tool_input["command"]], cwd=repo, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert git(repo, "log", "-1", "--format=%B").strip() == MODEL_TEXT


def test_a_pr_body_is_bound_the_same_way(repo, tmp_path):
    git(repo, "checkout", "-q", "-b", "feat")
    git(repo, "commit", "-qm", "feat: a change to a.py")
    body = "## Summary\nChanges a.py.\n\n## Test plan\nRun the tests."
    gen = Gen(GenResult("accepted", body, "ollama/stub", 2, ""))
    d, rows = run(f'gh pr create --title "A change" --body {T}', repo, tmp_path, gen)
    assert d.kind == "rewrite"
    (f,) = files(tmp_path)
    assert d.tool_input["command"] == f'gh pr create --title "A change" --body-file {f}'
    assert f.read_text() == body + "\n"
    job, text, hint, _ = gen.calls[0]
    assert job == "pr_description" and "## Commits (1)" in text and "feat: a change to a.py" in text
    assert "A change" in hint
    assert rows[0]["job"] == "pr_description" and rows[0]["attempts"] == 2


def test_the_ledger_row_has_sizes_and_codes_and_no_text(repo, tmp_path):
    d, rows = run(f"git commit -m {T}", repo, tmp_path, extra={"agent_id": "a1", "agent_type": "general-purpose"})
    (row,) = rows
    assert row["kind"] == "bound" and row["outcome"] == "accepted" and row["job"] == "commit_message"
    assert row["out_chars"] == len(MODEL_TEXT) and row["in_chars"] > 50 and row["cmd_chars"] == len(f"git commit -m {T}")
    assert row["session_id"] == "sess-1" and row["tool_use_id"] == "toolu_1"
    assert row["subagent"] is True and row["agent_type"] == "general-purpose"
    assert isinstance(row["latency_ms"], int) and row["attempts"] == 1 and row["model"] == "ollama/stub"
    blob = json.dumps(row)
    for secret in (SECRET_DIFF_LINE, "ticks", "not run", "app: change", str(repo)):
        assert secret not in blob


def test_the_model_text_is_cleaned_before_it_is_written(repo, tmp_path):
    dirty = "```\r\napp: change a.py \r\n\r\nbody\x00 line  \r\n```\r\n"
    d, _ = run(f"git commit -m {T}", repo, tmp_path, Gen(GenResult("accepted", dirty, "m", 1, "")))
    assert d.kind == "rewrite"
    assert files(tmp_path)[0].read_text() == "app: change a.py\n\nbody line\n"


def test_clean_text_rules():
    assert engine.clean_text("a\r\nb\rc") == "a\nb\nc"
    assert engine.clean_text("\n\n  x\n\n") == "  x"
    assert engine.clean_text("```md\nx\n```") == "x"
    assert engine.clean_text("```\nx") == "```\nx"  # an unclosed fence is not unwrapped
    assert engine.clean_text("   \n") == ""


# ---- denials: never a pass, never a file -------------------------------------------------------------------

@pytest.mark.parametrize("result,outcome,reason", [
    (GenResult("rejected", "WIP", "m", 3, "generic_subject"), "rejected", "generic_subject"),
    (GenResult("rejected", "", "m", 3, "names_no_changed_file"), "rejected", "names_no_changed_file"),
    (GenResult("fallback", "", "none", 0, "no_lane"), "fallback", "no_lane"),
    (GenResult("fallback", "", "none", 0, "flag_off"), "fallback", "flag_off"),
    (GenResult("fallback", "", "none", 0, "lane_refused"), "fallback", "lane_refused"),
    (GenResult("error", "", "none", 0, "job_raised"), "error", "job_raised"),
    (GenResult("accepted", "   \n", "m", 1, ""), "rejected", "empty"),
])
def test_a_job_that_did_not_produce_a_checked_text_denies_and_writes_nothing(repo, tmp_path, result, outcome, reason):
    d, rows = run(f"git commit -m {T}", repo, tmp_path, Gen(result))
    assert d.kind == "deny" and d.tool_input is None
    assert "Nothing was run" in d.reason and "commit message yourself" in d.reason and "without the placeholder" in d.reason
    assert "WIP" not in d.reason  # model text is never echoed to the model
    assert files(tmp_path) == []
    (row,) = rows
    assert (row["kind"], row["outcome"], row["reason"]) == ("denied", outcome, reason)


def test_a_timeout_denies_and_pauses_the_job_for_a_while(repo, tmp_path):
    clock = [1_700_000_000.0]
    d, rows = run(f"git commit -m {T}", repo, tmp_path, Gen(GenResult("timeout", "", "none", 0, "timeout")), now=lambda: clock[0])
    assert d.kind == "deny" and "did not finish" in d.reason and rows[0]["outcome"] == "timeout"
    cooling = Gen()
    d, rows = run(f"git commit -m {T}", repo, tmp_path, cooling, now=lambda: clock[0] + 60)
    assert d.kind == "deny" and cooling.calls == [] and rows[0]["outcome"] == "cooldown"
    assert "paused" in d.reason
    after = Gen()
    d, _ = run(f"git commit -m {T}", repo, tmp_path, after, now=lambda: clock[0] + limits.COOLDOWN_SEC + 1)
    assert d.kind == "rewrite" and len(after.calls) == 1


def test_a_model_call_that_raises_denies_instead_of_letting_the_placeholder_through(repo, tmp_path):
    d, rows = run(f"git commit -m {T}", repo, tmp_path, Gen(raises=RuntimeError("boom")))
    assert d.kind == "deny" and "failed" in d.reason
    assert files(tmp_path) == [] and rows and rows[-1]["kind"] == "denied"


def test_an_exception_for_an_ordinary_command_is_a_pass(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "handwritten_job", lambda c: (_ for _ in ()).throw(RuntimeError("x")))
    d, _ = run("ls -la", repo, tmp_path)
    assert d.kind == "pass"


def test_no_time_left_means_no_call(repo, tmp_path):
    gen = Gen()
    d, rows = run(f"git commit -m {T}", repo, tmp_path, gen, started=0.0, mono=lambda: 98.0, budget_s=100.0)
    assert d.kind == "deny" and gen.calls == [] and rows[0]["reason"] == "no_time_left"


def test_the_budget_handed_to_the_model_is_what_is_left(repo, tmp_path):
    gen = Gen()
    run(f"git commit -m {T}", repo, tmp_path, gen, started=0.0, mono=lambda: 10.0, budget_s=100.0)
    assert [c[3] for c in gen.calls] == [90.0]


def test_an_unverified_rewrite_is_never_emitted(repo, tmp_path, monkeypatch):
    from llm_router.text_job_bind import plan as plan_mod

    monkeypatch.setattr(plan_mod.Plan, "rewrite", lambda self, path: self.command)  # leaves the placeholder in
    d, rows = run(f"git commit -m {T}", repo, tmp_path)
    assert d.kind == "deny" and rows[0]["outcome"] == "error" and files(tmp_path) == []

    monkeypatch.setattr(plan_mod.Plan, "rewrite", lambda self, path: "git commit")  # forgot the file
    d, _ = run(f"git commit -m {T}", repo, tmp_path)
    assert d.kind == "deny" and files(tmp_path) == []

    monkeypatch.setattr(plan_mod.Plan, "rewrite", lambda self, path: f"git commit -F {path} -m x")  # message option left
    d, _ = run(f"git commit -m {T}", repo, tmp_path)
    assert d.kind == "deny" and files(tmp_path) == []


def test_an_unwritable_state_directory_denies(repo, tmp_path):
    (tmp_path / "bind").write_text("a file where the directory should be")
    d, rows = run(f"git commit -m {T}", repo, tmp_path)
    assert d.kind == "deny" and rows[0]["reason"] == "write_failed"


# ---- shapes that cannot be bound -----------------------------------------------------------------------------

def test_a_stale_diff_is_refused_not_guessed(repo, tmp_path):
    (repo / "b.py").write_text("b\n")
    gen = Gen()
    d, rows = run(f"git add b.py && git commit -m {T}", repo, tmp_path, gen)
    assert d.kind == "deny" and gen.calls == []
    assert "git add" in d.reason and "own command" in d.reason
    assert (rows[0]["outcome"], rows[0]["reason"]) == ("unsupported", "mutating_predecessor")
    git(repo, "add", "b.py")  # the same commit, staged in its own command first
    d, _ = run(f"git commit -m {T}", repo, tmp_path, gen)
    assert d.kind == "rewrite" and "b.py" in gen.calls[0][1]


def test_nothing_staged_is_refused_with_the_next_step(repo, tmp_path):
    git(repo, "reset", "-q")
    gen = Gen()
    d, rows = run(f"git commit -m {T}", repo, tmp_path, gen)
    assert d.kind == "deny" and gen.calls == [] and "git add" in d.reason
    assert (rows[0]["outcome"], rows[0]["reason"]) == ("refused", "nothing_staged")


def test_cd_and_git_dash_c_find_the_right_repository(repo, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    for c in (f"cd {repo} && git commit -m {T}", f"git -C {repo} commit -m {T}"):
        gen = Gen()
        d = decide(payload(c, other), generate=gen, root=tmp_path / "bind", record=lambda r: None)
        assert d.kind == "rewrite" and SECRET_DIFF_LINE in gen.calls[0][1]


def test_an_unreadable_commit_command_is_denied_but_an_unreadable_doc_is_not(repo, tmp_path):
    d, rows = run(f"git commit -m {T} 'oops", repo, tmp_path)
    assert d.kind == "deny" and "could not be read" in d.reason and rows[0]["reason"] == "unreadable"
    d, rows = run(f"echo {T} 'oops", repo, tmp_path)
    assert d.kind == "pass" and rows == []


def test_the_placeholder_in_an_ordinary_command_is_left_to_the_shell(repo, tmp_path):
    gen = Gen()
    for c in (f"echo {T}", "cat <<'EOF'\ngit commit -m \"${LLM_TEXT:?}\"\nEOF"):
        d, rows = run(c, repo, tmp_path, gen)
        assert d.kind == "pass" and rows == []
    assert gen.calls == []


# ---- commands that are not ours -----------------------------------------------------------------------------

def test_other_tools_and_malformed_payloads_are_a_pass(repo, tmp_path):
    for p in ({}, {"tool_name": "Read", "tool_input": {"file_path": "x"}}, {"tool_name": "Bash"},
              {"tool_name": "Bash", "tool_input": {"command": ""}}, {"tool_name": "Bash", "tool_input": "nope"},
              {"tool_name": "Bash", "tool_input": {"command": 5}}):
        rows: list[dict] = []
        assert decide(p, generate=Gen(), root=tmp_path / "bind", record=rows.append).kind == "pass" and rows == []


def test_a_handwritten_commit_or_pr_command_leaves_one_row_and_passes(repo, tmp_path):
    cases = {
        "git commit -m 'fix: x'": "commit_message",
        "cd /r && git -C /r commit -am 'x'": "commit_message",
        "git push && gh pr create --title t --body 'b'": "pr_description",
        "git commit --amend --no-edit": "commit_message",
    }
    for cmd, job in cases.items():
        gen = Gen()
        d, rows = run(cmd, repo, tmp_path, gen)
        assert d.kind == "pass" and gen.calls == []
        (row,) = rows
        assert (row["kind"], row["job"], row["cmd_chars"]) == ("handwritten", job, len(cmd))
    for cmd in ("git status", "git log --oneline", "echo commit", "gh pr view 1", "ls"):
        d, rows = run(cmd, repo, tmp_path)
        assert d.kind == "pass" and (rows == [] or cmd == "echo commit")


# ---- the router's own text job, through the real tool -------------------------------------------------------

DIFF = "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n+x = 1\n"


def _resp(content: str):
    return SimpleNamespace(content=content, model="ollama/test-model", header=lambda: "> **ollama/test-model** 1 tokens")


def _ctx():
    return SimpleNamespace(info=AsyncMock(), report_progress=AsyncMock())


async def test_parse_reads_every_status_the_real_tool_returns(monkeypatch):
    from llm_router import eligibility_routing
    from llm_router.tools import text as text_tools

    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")
    monkeypatch.setattr(eligibility_routing, "passed_lane_available", lambda *a, **k: True)

    monkeypatch.setattr(text_tools, "route_and_call", AsyncMock(side_effect=[_resp("app: set x in app.py\n\nSecond paragraph.")]))
    r = engine.parse_tool_result(await text_tools.llm_text_job("commit_message", DIFF, _ctx()))
    assert r == GenResult("accepted", "app: set x in app.py\n\nSecond paragraph.", "ollama/test-model", 1, "")

    monkeypatch.setattr(text_tools, "route_and_call", AsyncMock(side_effect=[_resp("WIP")] * 3))
    r = engine.parse_tool_result(await text_tools.llm_text_job("commit_message", DIFF, _ctx()))
    assert (r.status, r.attempts, r.reason, r.text) == ("rejected", 3, "generic_subject", "")

    monkeypatch.setattr(text_tools, "route_and_call", AsyncMock(side_effect=[_resp("nothing relevant here at all")] * 3))
    r = engine.parse_tool_result(await text_tools.llm_text_job("commit_message", DIFF, _ctx()))
    assert (r.status, r.reason) == ("rejected", "names_no_changed_file")

    long_diff = DIFF + "+" + "y" * (50_000)
    monkeypatch.setattr(text_tools, "route_and_call", AsyncMock(side_effect=[_resp("app: more in app.py")]))
    r = engine.parse_tool_result(await text_tools.llm_text_job("commit_message", long_diff, _ctx()))
    assert (r.status, r.text) == ("accepted", "app: more in app.py")  # the truncation note is not part of the text

    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "0")
    r = engine.parse_tool_result(await text_tools.llm_text_job("commit_message", DIFF, _ctx()))
    assert (r.status, r.reason, r.attempts) == ("fallback", "flag_off", 0)

    monkeypatch.delenv("LLM_ROUTER_LOCAL_TEXT_JOBS")
    monkeypatch.setattr(eligibility_routing, "passed_lane_available", lambda *a, **k: False)
    r = engine.parse_tool_result(await text_tools.llm_text_job("commit_message", DIFF, _ctx()))
    assert (r.status, r.reason) == ("fallback", "no_lane")


def test_parse_never_turns_unexpected_output_into_a_text():
    assert engine.parse_tool_result("some other tool output") == GenResult("error", "", "unknown", 0, "unreadable_result")
    assert engine.parse_tool_result("").status == "error"


def test_generate_is_local_only_and_honours_the_off_switch(monkeypatch):
    from llm_router.tools import text as text_tools

    seen: list[str | None] = []

    async def fake(job, text, ctx, hint=None):
        seen.append(os.environ.get("LLM_ROUTER_LOCAL_TEXT_JOBS"))
        return "> **ollama/m** x\n\n_Structured: status=accepted, attempts=1_\n\nmsg text app.py"

    monkeypatch.setattr(text_tools, "llm_text_job", fake)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")  # the MCP tool would run the FULL chain
    r = engine.generate_with_text_job("commit_message", "x", None, 5)
    assert r.status == "accepted" and seen == [None], "a bound job must not see the full-chain flag"
    for off in ("0", "false", "no", "off", "disable", "nope"):
        monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", off)
        before = len(seen)
        assert engine.generate_with_text_job("commit_message", "x", None, 5).reason == "flag_off"
        assert len(seen) == before, "an off switch must make no call"


def test_generate_times_out_and_reports_it(monkeypatch):
    import asyncio

    from llm_router.tools import text as text_tools

    async def slow(*a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr(text_tools, "llm_text_job", slow)
    # The stub sleeps 30 s: returning at all (pytest's own per-test timeout is the clock) is the proof that the
    # call was cancelled at its budget and not awaited to the end.
    r = engine.generate_with_text_job("commit_message", "x", None, 0.2)
    assert r.status == "timeout"

    async def boom(*a, **k):
        raise RuntimeError("model fell over")

    monkeypatch.setattr(text_tools, "llm_text_job", boom)
    r = engine.generate_with_text_job("commit_message", "x", None, 5)
    assert (r.status, r.reason) == ("error", "job_raised")
    assert "fell over" not in repr(r)
