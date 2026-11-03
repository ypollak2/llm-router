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

from llm_router.text_job_bind import engine, ledger, limits, sanitize
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
    monkeypatch.setenv("LLM_ROUTER_TEXT_JOB_BODY", "model")  # the tests that are not about body modes want the model's text
    monkeypatch.setattr(engine, "load_env", lambda: None)  # no .env of this checkout; its own test lifts this


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
    assert d.tool_input["timeout"] == 60000 and d.tool_input["run_in_background"] is False
    # the approver sees a file path, so the description says what is being committed and that it is local text
    assert d.tool_input["description"] == "git commit (local text): app: change a.py"
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
    # the model's guess at a test plan is not published as if it were a result
    assert f.read_text() == "## Summary\nChanges a.py.\n\n## Test plan\n\n" + sanitize.TEST_PLAN_PLACEHOLDER + "\n"
    assert d.tool_input["description"] == "gh pr create (local text): A change"
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
    d, rows = run(f"echo {T}", repo, tmp_path, gen)
    assert d.kind == "pass" and rows == []
    assert gen.calls == []


@pytest.mark.parametrize("cmd", [
    "git commit -m '${LLM_TEXT:?}'", "git commit -m \\${LLM_TEXT:?}", "git commit -m $LLM_TEXT",
    'git commit -m "${LLM_TEXT}"', "git commit -F - <<'EOF'\n${LLM_TEXT:?}\nEOF",
    "cat <<'EOF'\ngit commit -m \"${LLM_TEXT:?}\"\nEOF", "bash -c 'git commit -m \"${LLM_TEXT:?}\"'",
])
def test_a_placeholder_the_shell_would_not_replace_is_denied_never_passed_through(repo, tmp_path, cmd):
    """F2/F3: written in single quotes, after a backslash, in a quoted here-document, or without the abort guard,
    the text would be committed as it stands (or, for the unguarded forms, an empty message posted). It is
    denied with a message that says how to write it, and nothing reaches the model."""
    gen = Gen()
    d, rows = run(cmd, repo, tmp_path, gen)
    assert d.kind == "deny" and gen.calls == [] and files(tmp_path) == []
    assert 'exactly as "${LLM_TEXT:?}"' in d.reason and "git commit -F <file>" in d.reason
    assert (rows[0]["outcome"], rows[0]["reason"]) == ("unsupported", "placeholder_spelling")


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




# ---- F5: what the published text may not do ----------------------------------------------------------------

def test_mentions_closing_keywords_and_a_test_plan_are_neutralised_in_what_is_published(repo, tmp_path):
    git(repo, "checkout", "-q", "-b", "feat")
    git(repo, "commit", "-qm", "feat: a change to a.py")
    body = ("## Summary\nThanks @alice and @org/team for a.py. Fixes #12, closes owner/repo#3 and "
            "resolves https://github.com/o/r/issues/9. Mail me@example.com.\n\n## Test plan\nRan pytest, all 4000 passed.\n")
    d, rows = run(f'gh pr create --title T --body {T}', repo, tmp_path, Gen(GenResult("accepted", body, "m", 1, "")))
    assert d.kind == "rewrite"
    published = files(tmp_path)[0].read_text()
    assert "@alice" not in published and "@org" not in published and "alice and org/team" in published
    assert "me@example.com" in published  # an address is not a mention
    for closing in ("Fixes #12", "closes owner/repo#3", "resolves https://"):
        assert closing not in published
    assert "refs #12" in published and "refs owner/repo#3" in published and "refs https://github.com/o/r/issues/9" in published
    assert "Ran pytest" not in published and sanitize.TEST_PLAN_PLACEHOLDER in published
    (row,) = rows
    assert (row["stripped_mentions"], row["stripped_closing"], row["replaced_test_plan"]) == (2, 3, 1)
    assert "alice" not in json.dumps(row)


def test_a_commit_message_is_cleaned_the_same_way_but_keeps_its_prose(repo, tmp_path):
    text = "fix(a): handle a.py, thanks @bob\n\nCloses #7 and keeps `@property`."
    d, rows = run(f"git commit -m {T}", repo, tmp_path, Gen(GenResult("accepted", text, "m", 1, "")))
    assert files(tmp_path)[0].read_text() == "fix(a): handle a.py, thanks bob\n\nrefs #7 and keeps `@property`.\n"
    assert rows[0]["stripped_mentions"] == 1 and rows[0]["stripped_closing"] == 1  # a code span does not notify anyone


@pytest.mark.parametrize("text,expected", [
    ("a @x b", "a x b"), ("a@b.com", "a@b.com"), ("(@x)", "(x)"), ("@@x", "@@x"), ("`@x`", "`@x`"),
    ("Fix #1", "refs #1"), ("fix: thing", "fix: thing"), ("fixes the bug", "fixes the bug"), ("Resolved: #4", "refs: #4"),
    ("closed #5, fixed #6", "refs #5, refs #6"), ("fixes#8", "refs #8"),
])
def test_the_neutral_forms(text, expected):
    assert sanitize.clean(text, pr_body=False).text == expected


def test_an_empty_test_plan_section_is_left_and_other_sections_are_not_touched():
    out = sanitize.clean("## Summary\nS\n\n## Test plan\n\n## Notes\nN", pr_body=True)
    assert out.test_plan == 0 and out.text == "## Summary\nS\n\n## Test plan\n\n## Notes\nN"
    out = sanitize.clean("## Summary\nS\n\n### Test plan\n- ran it\n\n## Notes\nN", pr_body=True)
    assert out.test_plan == 1 and "ran it" not in out.text and out.text.endswith("## Notes\nN")


# ---- F17: what the body of a commit message holds ----------------------------------------------------------

def test_the_default_body_is_a_list_of_changed_files_with_no_model_text(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_TEXT_JOB_BODY")
    assert limits.COMMIT_BODY_MODE == "files"
    (repo / "b.py").write_text("b\nb\n")
    git(repo, "add", "b.py")
    model = "app: change a.py\n\nThis body says something false about a.py and is never published."
    d, rows = run(f"git commit -m {T} -m 'Co-Authored-By: X <x@y>'", repo, tmp_path, Gen(GenResult("accepted", model, "m", 1, "")))
    out = files(tmp_path)[0].read_text()
    assert out == ("app: change a.py\n\nChanged files:\n- a.py (+1/-0)\n- b.py (+2/-0)\n\nCo-Authored-By: X <x@y>\n")
    assert "false" not in out and rows[0]["body_mode"] == "files"
    assert d.tool_input["description"] == "git commit (local text): app: change a.py"


@pytest.mark.parametrize("mode,expected", [
    ("none", "app: change a.py\n"),
    ("model", MODEL_TEXT + "\n"),
    ("files", "app: change a.py\n\nChanged files:\n- a.py (+1/-0)\n"),
    ("bogus", "app: change a.py\n\nChanged files:\n- a.py (+1/-0)\n"),  # an unknown value falls back to the constant
])
def test_the_body_mode_is_one_constant_and_an_environment_override(repo, tmp_path, monkeypatch, mode, expected):
    monkeypatch.setenv("LLM_ROUTER_TEXT_JOB_BODY", mode)
    d, rows = run(f"git commit -m {T}", repo, tmp_path)
    assert files(tmp_path)[0].read_text() == expected


def test_a_long_file_list_is_counted_not_dropped(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_TEXT_JOB_BODY", "files")
    for i in range(limits.FILES_BODY_MAX + 5):
        (repo / f"m{i:03d}.py").write_text("x\n")
    git(repo, "add", ".")
    d, _ = run(f"git commit -m {T}", repo, tmp_path)
    body = files(tmp_path)[0].read_text()
    assert body.count("\n- ") == limits.FILES_BODY_MAX + 1 and "- ... and 6 more files" in body  # a.py plus 45 new ones


def test_a_pr_body_is_not_affected_by_the_commit_body_mode(repo, tmp_path, monkeypatch):
    git(repo, "checkout", "-q", "-b", "feat")
    git(repo, "commit", "-qm", "feat: a")
    monkeypatch.setenv("LLM_ROUTER_TEXT_JOB_BODY", "none")
    body = "## Summary\nChanges a.py.\n\n## Test plan\n"
    d, _ = run(f"gh pr create --title T --body {T}", repo, tmp_path, Gen(GenResult("accepted", body, "m", 1, "")))
    assert files(tmp_path)[0].read_text().startswith("## Summary\nChanges a.py.")


# ---- F6: the approver sees what is being committed ------------------------------------------------------------

def test_description_is_one_clean_line_and_every_other_field_is_kept(repo, tmp_path):
    text = "app: change a.py\x1b[31m with\ttabs " + "x" * 300
    d, _ = run(f"git commit -m {T}", repo, tmp_path, Gen(GenResult("accepted", text, "m", 1, "")))
    desc = d.tool_input["description"]
    assert desc.startswith("git commit (local text): app: change a.py") and "\x1b" not in desc and "\t" not in desc
    assert len(desc) <= len("git commit (local text): ") + 100
    assert set(d.tool_input) == {"command", "description", "timeout", "run_in_background"}


# ---- F7: the .env the MCP server loads is loaded here too ---------------------------------------------------------

def test_the_router_home_dotenv_reaches_the_hook_process(repo, tmp_path, monkeypatch):
    monkeypatch.undo()  # drop the autouse stubs, then set up this test's own environment
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    home = tmp_path / "router_home"
    home.mkdir()
    (home / ".env").write_text("LLM_ROUTER_TEXT_JOB_PROBE=from-dotenv\nLLM_ROUTER_LOCAL_TEXT_JOBS=0\n")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.delenv("LLM_ROUTER_LOCAL_TEXT_JOBS", raising=False)
    monkeypatch.delenv("LLM_ROUTER_TEXT_JOB_PROBE", raising=False)
    monkeypatch.chdir(tmp_path)
    seen: list[str | None] = []

    def gen(job, text, hint, t):
        seen.append(os.environ.get("LLM_ROUTER_TEXT_JOB_PROBE"))
        return GenResult("accepted", MODEL_TEXT, "m", 1, "")

    d, _ = run(f"git commit -m {T}", repo, tmp_path, gen)  # type: ignore[arg-type]
    assert d.kind == "rewrite" and seen == ["from-dotenv"]
    # and the documented off switch, set in that file, is what generate_with_text_job sees
    assert os.environ.get("LLM_ROUTER_LOCAL_TEXT_JOBS") == "0"
    assert engine.generate_with_text_job("commit_message", "x", None, 5).reason == "flag_off"
    monkeypatch.delenv("LLM_ROUTER_LOCAL_TEXT_JOBS", raising=False)
    monkeypatch.delenv("LLM_ROUTER_TEXT_JOB_PROBE", raising=False)


# ---- F11: the router import is not the model's wait ----------------------------------------------------------------

def test_the_router_is_loaded_before_the_model_phase_not_inside_it(repo, tmp_path, monkeypatch):
    events: list[str] = []

    class Phase:
        def __enter__(self):
            events.append("model-phase-enter")

        def __exit__(self, *a):
            events.append("model-phase-exit")
            return False

    monkeypatch.setattr(engine, "_model_phase", lambda: Phase())

    def gen(job, text, hint, t):
        events.append("generate")
        return GenResult("accepted", MODEL_TEXT, "m", 1, "")

    d, _ = run(f"git commit -m {T}", repo, tmp_path, gen, prepare=lambda: events.append("prepare"))  # type: ignore[arg-type]
    assert d.kind == "rewrite"
    assert events == ["prepare", "model-phase-enter", "generate", "model-phase-exit"]


def test_a_router_that_cannot_be_loaded_denies_before_any_model_call(repo, tmp_path):
    def boom():
        raise ImportError("no litellm")

    gen = Gen()
    d, rows = run(f"git commit -m {T}", repo, tmp_path, gen, prepare=boom)
    assert d.kind == "deny" and gen.calls == [] and rows[0]["reason"] == "router_import_failed"


def test_prepare_defaults_to_the_router_loader_exactly_for_the_real_generator(repo, tmp_path, monkeypatch):
    loaded: list[str] = []
    monkeypatch.setattr(engine, "load_router", lambda: loaded.append("load"))
    real = lambda *a: GenResult("accepted", MODEL_TEXT, "m", 1, "")  # noqa: E731 -- stands in for generate_with_text_job
    monkeypatch.setattr(engine, "generate_with_text_job", real)
    d = decide(payload(f"git commit -m {T}", repo), generate=real, root=tmp_path / "b1", record=lambda r: None)
    assert d.kind == "rewrite" and loaded == ["load"]
    loaded.clear()
    other = Gen()  # any other generator is a stub: the router is not imported for it
    decide(payload(f"git commit -m {T}", repo), generate=other, root=tmp_path / "b2", record=lambda r: None)
    assert loaded == []


# ---- F15: one job at a time per machine, and "busy" instead of a queue past the budget --------------------

def test_a_second_job_that_finds_the_lock_taken_is_told_to_write_it_itself(repo, tmp_path):
    import fcntl

    root = tmp_path / "bind"
    root.mkdir(mode=0o700)
    fd = os.open(root / "job.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        gen = Gen()
        d, rows = run(f"git commit -m {T}", repo, tmp_path, gen, lock_wait_s=0.2)
    finally:
        os.close(fd)
    assert d.kind == "deny" and gen.calls == [] and "Another commit-message or PR job" in d.reason
    assert "yourself" in d.reason and files(tmp_path) == []
    assert (rows[0]["outcome"], rows[0]["reason"]) == ("busy", "lock_held")
    # busy is a capacity answer, not a failure of the model: no cooldown, so the next job is not refused for it
    d, _ = run(f"git commit -m {T}", repo, tmp_path, Gen())
    assert d.kind == "rewrite"


def test_jobs_in_parallel_run_one_at_a_time(repo, tmp_path):
    import threading
    import time

    active = [0]
    peak = [0]
    guard = threading.Lock()

    def gen(job, text, hint, t):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.15)
        with guard:
            active[0] -= 1
        return GenResult("accepted", MODEL_TEXT, "m", 1, "")

    results: list[str] = []

    def one():
        d = decide(payload(f"git commit -m {T}", repo), generate=gen, root=tmp_path / "bind", record=lambda r: None,
                   lock_wait_s=10.0)
        results.append(d.kind)

    threads = [threading.Thread(target=one) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert results == ["rewrite"] * 4 and peak[0] == 1


def test_the_lock_is_released_when_the_job_ends_even_on_an_exception(repo, tmp_path):
    d, _ = run(f"git commit -m {T}", repo, tmp_path, Gen(raises=RuntimeError("x")))
    assert d.kind == "deny"
    d, _ = run(f"git commit -m {T}", repo, tmp_path, Gen(), lock_wait_s=0.0)
    assert d.kind == "rewrite"


def test_the_wait_for_the_lock_counts_against_the_budget(repo, tmp_path):
    import fcntl

    root = tmp_path / "bind"
    root.mkdir(mode=0o700)
    fd = os.open(root / "job.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        # 96 of 100 s are gone: the wait may use at most what leaves five seconds for the model, which is none
        d, rows = run(f"git commit -m {T}", repo, tmp_path, Gen(), started=0.0, mono=lambda: 96.0, budget_s=100.0, lock_wait_s=30.0)
    finally:
        os.close(fd)
    assert d.kind == "deny" and rows[0]["outcome"] == "busy"


# ---- F10: the deadline guard leaves what an engine timeout leaves --------------------------------------------

def test_the_deadline_guard_records_a_row_and_starts_the_cooldown(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))
    monkeypatch.setattr(engine.time, "monotonic", lambda: 100.0)  # a fixed clock: the model call began 3 s ago
    engine._model_started[:] = [97.0]
    try:
        open_phases = engine.record_deadline(payload(f"git commit -m {T}", repo))
    finally:
        engine._model_started.clear()
    (row,) = ledger.read_rows()
    assert (row["kind"], row["outcome"], row["reason"], row["job"]) == ("denied", "timeout", "deadline_guard", "commit_message")
    assert engine._cooldown_remaining(tmp_path / "router_home" / "text_job_bind", engine.time.time()) > 0
    assert open_phases == {"text_job": 3000.0}  # still open: handed to the recorder
    assert engine.record_deadline(payload("ls", repo)) == {}  # no model call was in flight


# ---- F16: the sweep, and the reasons the model is told -----------------------------------------------------

def test_old_message_files_are_swept_and_young_ones_are_not(tmp_path):
    root = tmp_path / "bind"
    root.mkdir()
    old, young, other = root / "1-old.txt", root / "2-young.txt", root / "keep.lock"
    for f in (old, young, other):
        f.write_text("x")
    now = 1_700_000_000.0
    os.utime(old, (now - limits.FILE_TTL_SEC - 1, now - limits.FILE_TTL_SEC - 1))
    os.utime(young, (now - limits.FILE_TTL_SEC + 60, now - limits.FILE_TTL_SEC + 60))
    os.utime(other, (now - 10 * limits.FILE_TTL_SEC, now - 10 * limits.FILE_TTL_SEC))
    engine._sweep(root, now)
    assert not old.exists() and young.exists() and other.exists()
    engine._write_message_file(root, "new", now)  # writing a message sweeps too
    assert young.exists() and len(list(root.glob("*.txt"))) == 2


@pytest.mark.parametrize("reason,needle", [
    ("no_lane", "No local model has passed"),
    ("lane_unverified", "could not be verified on this machine"),
    ("local_infra", "could not be reached or was too busy"),
])
def test_never_passed_and_unverified_and_busy_are_told_apart(repo, tmp_path, reason, needle):
    d, rows = run(f"git commit -m {T}", repo, tmp_path, Gen(GenResult("fallback", "", "none", 0, reason)))
    assert d.kind == "deny" and needle in d.reason and rows[0]["reason"] == reason


# ---- the router's own text job, through its structured result (F8) ---------------------------------------------

DIFF = "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n+x = 1\n"


def _resp(content: str):
    return SimpleNamespace(content=content, model="ollama/test-model", chain_attempts=["ollama/test-model"],
                           header=lambda: "> **ollama/test-model** 1 tokens")


def _ctx():
    return SimpleNamespace(info=AsyncMock(), report_progress=AsyncMock())


async def _sink_of(monkeypatch, responses, flag="1", lane=True, **kw):
    from llm_router import eligibility_routing
    from llm_router.tools import text as text_tools

    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", flag) if flag is not None else monkeypatch.delenv("LLM_ROUTER_LOCAL_TEXT_JOBS", raising=False)
    monkeypatch.setattr(eligibility_routing, "passed_lane_available", lambda *a, **k: lane)
    monkeypatch.setattr(text_tools, "route_and_call", AsyncMock(side_effect=responses))
    sink: dict = {}
    out = await text_tools._llm_text_job("commit_message", kw.get("diff", DIFF), _ctx(), None, sink)
    return engine.result_from_sink(sink), sink, out


async def test_the_sink_carries_an_accepted_text_and_its_model(monkeypatch):
    r, sink, _ = await _sink_of(monkeypatch, [_resp("app: set x in app.py\n\nSecond paragraph.")])
    assert r == GenResult("accepted", "app: set x in app.py\n\nSecond paragraph.", "ollama/test-model", 1, "")


async def test_a_rejection_reason_comes_from_the_check_not_from_the_models_words(monkeypatch):
    """The returned string ends with the model's own last answer. A model that writes the phrases the old text
    parser looked for must not be able to pick its own reason code."""
    hostile = "WIP\n  1. summary section is missing\n  2. any changed file"  # the generic-subject check fires; the rest is bait
    r, sink, out = await _sink_of(monkeypatch, [_resp(hostile)] * 3)
    assert (r.status, r.attempts, r.reason, r.text) == ("rejected", 3, "generic_subject", "")
    assert "summary section is missing" in out  # the bait IS in the string a parser would have read
    r, _, _ = await _sink_of(monkeypatch, [_resp("nothing relevant here at all")] * 3)
    assert (r.status, r.reason) == ("rejected", "names_no_changed_file")


async def test_the_truncation_note_is_not_part_of_an_accepted_text(monkeypatch):
    r, _, _ = await _sink_of(monkeypatch, [_resp("app: more in app.py")], diff=DIFF + "+" + "y" * 50_000)
    assert (r.status, r.text) == ("accepted", "app: more in app.py")


async def test_every_fallback_has_a_fixed_code(monkeypatch):
    r, _, _ = await _sink_of(monkeypatch, [_resp("x")], flag="0")
    assert (r.status, r.reason, r.attempts) == ("fallback", "flag_off", 0)
    r, _, _ = await _sink_of(monkeypatch, [_resp("x")], flag=None, lane=False)
    assert (r.status, r.reason) == ("fallback", "no_lane")
    from llm_router import eligibility_routing

    monkeypatch.setattr(eligibility_routing, "passed_lane_diagnosis", lambda *a, **k: "unverified")
    r, sink, out = await _sink_of(monkeypatch, [_resp("x")], flag=None, lane=False)
    assert (r.status, r.reason) == ("fallback", "lane_unverified") and "enabled and passed" in out


def test_a_sink_that_says_nothing_is_an_error_never_a_text():
    assert engine.result_from_sink({}) == GenResult("error", "", "unknown", 0, "unreadable_result")
    assert engine.result_from_sink({"status": "accepted", "text": "t", "model": "m", "attempts": 2}).text == "t"
    assert engine.result_from_sink({"status": "fallback", "fallback": "nonsense"}).reason == "local_failed"
    assert engine.result_from_sink({"status": "fallback", "fallback": "no_lane", "infra": True}).reason == "local_infra"
    assert engine.result_from_sink({"status": "rejected", "codes": ["subject_too_long", "x"]}).reason == "subject_too_long"
    assert engine.result_from_sink({"status": "rejected"}).reason == "failed_checks"


def test_generate_is_local_only_and_honours_the_off_switch(monkeypatch):
    from llm_router.tools import text as text_tools

    seen: list[str | None] = []

    async def fake(job, text, ctx, hint, sink, attempt_timeout_s=None):
        seen.append(os.environ.get("LLM_ROUTER_LOCAL_TEXT_JOBS"))
        sink.update(status="accepted", attempts=1, model="ollama/m", text="msg text app.py", codes=[], reasons=[], fallback="", infra=False)
        return "ignored"

    monkeypatch.setattr(text_tools, "_llm_text_job", fake)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")  # the MCP tool would run the FULL chain
    r = engine.generate_with_text_job("commit_message", "x", None, 5)
    assert r.status == "accepted" and seen == [None], "a bound job must not see the full-chain flag"
    for off in ("0", "false", "no", "off", "disable", "nope"):
        monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", off)
        before = len(seen)
        assert engine.generate_with_text_job("commit_message", "x", None, 5).reason == "flag_off"
        assert len(seen) == before, "an off switch must make no call"


def test_each_attempt_is_capped_by_the_mcp_doors_own_constants(monkeypatch):
    from llm_router import mcp_door
    from llm_router.tools import text as text_tools

    caps: list[float | None] = []

    async def fake(job, text, ctx, hint, sink, attempt_timeout_s=None):
        caps.append(attempt_timeout_s)
        sink.update(status="accepted", attempts=1, model="m", text="t")
        return ""

    monkeypatch.setattr(text_tools, "_llm_text_job", fake)
    engine.generate_with_text_job("commit_message", "x", None, 5)
    assert caps == [mcp_door.MCP_DEADLINE_DEFAULT_S * mcp_door.ATTEMPT_SHARE] == [engine.attempt_cap_s()]
    assert 0 < caps[0] < limits.BUDGET_SEC  # one slow attempt cannot use the whole budget
    monkeypatch.setattr(mcp_door, "ATTEMPT_SHARE", "not a number")
    assert engine.attempt_cap_s() is None  # unreadable constants mean no cap, not a crash


def test_generate_times_out_and_reports_it(monkeypatch):
    import asyncio

    from llm_router.tools import text as text_tools

    async def slow(*a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr(text_tools, "_llm_text_job", slow)
    # The stub sleeps 30 s: returning at all (pytest's own per-test timeout is the clock) is the proof that the
    # call was cancelled at its budget and not awaited to the end.
    r = engine.generate_with_text_job("commit_message", "x", None, 0.2)
    assert r.status == "timeout"

    async def boom(*a, **k):
        raise RuntimeError("model fell over")

    monkeypatch.setattr(text_tools, "_llm_text_job", boom)
    r = engine.generate_with_text_job("commit_message", "x", None, 5)
    assert (r.status, r.reason) == ("error", "job_raised")
    assert "fell over" not in repr(r)
