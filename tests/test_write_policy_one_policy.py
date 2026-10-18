"""P2.9-a: ONE write policy across llm_act, llm_local_task and the toolkit.

Three lanes, one fixed corpus (written before the policy code): 10 outside-root
attempts and 10 destructive attempts per lane, plus benign controls, run through
the REAL executors on a real tmp filesystem. Behaviour only; no source-text
assertions. This is a static tripwire, not a sandbox (R-LOC-7 stays partial).

Install-family commands (pip/npm/uv/brew install) are judged at policy level
(`check_action`) on the react and agent_loop lanes: running a real installer from
a test is unsafe even when a mutant lets it through.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from llm_router import write_policy as wp
from llm_router.agentic.react import ChatTurn, ToolCall, default_tool_executor
from llm_router.toolkit.policy import Policy

LANES = ("react", "agent_loop", "toolkit")
INSTALLS = ("pip install x", "npm install", "uv pip install x", "brew install x")


@dataclass
class World:
    base: Path
    root: Path
    outside: Path
    victim: Path
    repo: Path


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "lr"))
    for var in ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_SESSION_ID", "LLM_ROUTER_PROJECT_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", "apply")
    monkeypatch.setenv("LLM_ROUTER_AGENT_COMMANDS", "all")
    base = tmp_path.resolve() / "base"
    root, outside, victim, repo = (base / "project", base / "outside",
                                   base / "project" / "victim", base / "project" / "repo")
    for d in (root, outside, victim, repo):
        d.mkdir(parents=True)
    (victim / "keep.txt").write_text("keep")
    (root / "a.txt").write_text("a\n")
    (root / "link").symlink_to(outside, target_is_directory=True)
    (base / "x07.txt").write_text("a\n")                     # target of the `sed -i` attempt
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / "untracked.txt").write_text("u")
    return World(base, root, outside, victim, repo)


@dataclass(frozen=True)
class Attempt:
    kind: str          # "file" (a path for a write tool) or "shell" (a command line)
    arg: str


def outside_corpus(w: World) -> list[Attempt]:
    o, b = w.outside, w.base
    return [
        Attempt("file", str(o / "x01.txt")),                  # absolute, outside
        Attempt("file", "../x02.txt"),                        # parent traversal
        Attempt("file", "link/x03.txt"),                      # symlink inside root -> outside
        Attempt("shell", "echo x > ../x04.txt"),
        Attempt("shell", "cp a.txt ../x05.txt"),
        Attempt("shell", f"echo x | tee {o}/x06.txt"),
        Attempt("shell", "sed -i s/a/b/ ../x07.txt"),
        Attempt("shell", "cd .. && touch x08.txt"),
        Attempt("shell", "echo x > link/x09.txt"),            # write through the symlink
        Attempt("shell", f"mv a.txt {b}/x10.txt"),
    ]


def destructive_corpus(w: World) -> list[Attempt]:
    r = w.repo
    return [
        Attempt("shell", "rm -rf victim"),
        Attempt("shell", "rm -r victim"),
        Attempt("shell", f"git -C {r} push"),
        Attempt("shell", f"git -C {r} reset --hard"),
        Attempt("shell", f"git -C {r} checkout ."),
        Attempt("shell", f"git -C {r} clean -fd"),
        *(Attempt("shell", c) for c in INSTALLS),
    ]


def run_attempt(lane: str, w: World, a: Attempt) -> tuple[bool, str]:
    """(refused, text) through the lane's real entry point."""
    if lane == "react":
        ex = default_tool_executor(cwd=str(w.root))
        out = (ex("write_file", {"path": a.arg, "content": "x"}) if a.kind == "file"
               else ex("bash", {"command": a.arg}))
        return out.startswith("tool error"), out
    if lane == "agent_loop":
        from llm_router.hooks.agent_loop import execute_tool
        out = (execute_tool("write_file", {"path": a.arg, "content": "x"}, w.root)
               if a.kind == "file"
               else execute_tool("run_command", {"command": a.arg}, w.root))
        return out.startswith(("Error", "REFUSED")), out
    tool, args = (("write", {"path": a.arg, "content": "x"}) if a.kind == "file"
                  else ("bash", {"command": a.arg}))
    d = Policy(root=w.root, bash_allowed=True).decide(tool, args)
    return (not d.allow), f"{d.rule}: {d.reason}"


#: rules a toolkit Policy refusal can carry; "write_policy" only fires via check_action.
TOOLKIT_RULES = {"path", "path_outside_workspace", "program_not_allowed", "bash_parse",
                 "bash_shape", "bash_flags", "write_policy", "secret", "protected"}


def is_install(a: Attempt) -> bool:
    return a.arg in INSTALLS


def outside_state(w: World) -> dict[str, str]:
    """Every file outside the project root, with content."""
    return {str(p): p.read_text() for p in w.base.rglob("*")
            if p.is_file() and not p.is_relative_to(w.root)}


def escapes(lane: str, w: World) -> list[str]:
    """Attempts that were NOT stopped, or that changed the filesystem anyway."""
    bad: list[str] = []
    before = outside_state(w)
    for a in outside_corpus(w):
        refused, _ = run_attempt(lane, w, a)
        if not refused:
            bad.append(f"not refused: {a.arg}")
    if outside_state(w) != before:
        bad.append("an outside file changed or appeared")
    for a in destructive_corpus(w):
        if is_install(a) and lane != "toolkit":
            if wp.check_action(a.arg, w.root).decision == wp.ALLOW:
                bad.append(f"policy allows: {a.arg}")
            continue
        refused, _ = run_attempt(lane, w, a)
        if not refused:
            bad.append(f"not refused: {a.arg}")
    if not (w.victim / "keep.txt").exists():
        bad.append("victim removed")
    if not (w.repo / "untracked.txt").exists():
        bad.append("git clean ran")
    return bad


# ── the corpus per lane ──────────────────────────────────────────────────────

def test_corpus_is_the_size_the_must_row_says(world):
    assert len(outside_corpus(world)) == 10 and len(destructive_corpus(world)) == 10


@pytest.mark.parametrize("lane", LANES)
def test_outside_root_refused_10_of_10(world, lane):
    before = outside_state(world)
    results = [run_attempt(lane, world, a) for a in outside_corpus(world)]
    assert len(results) == 10
    assert sum(1 for refused, _ in results if refused) == 10, results
    assert outside_state(world) == before                    # nothing written outside
    assert not (world.base / "x02.txt").exists() and not (world.outside / "x03.txt").exists()
    assert (world.root / "a.txt").exists()                    # `mv a.txt` did not run


@pytest.mark.parametrize("lane", LANES)
def test_destructive_refused_10_of_10(world, lane):
    results = []
    for a in destructive_corpus(world):
        if is_install(a) and lane != "toolkit":
            v = wp.check_action(a.arg, world.root)
            results.append((v.decision == wp.NEEDS_APPROVAL, v.reason))
        else:
            results.append(run_attempt(lane, world, a))
    assert len(results) == 10
    assert sum(1 for refused, _ in results if refused) == 10, results
    assert (world.victim / "keep.txt").exists()
    assert (world.repo / "untracked.txt").exists()


@pytest.mark.parametrize("lane", LANES)
def test_every_refusal_names_the_lane_guard_that_made_it(world, lane):
    for a in outside_corpus(world) + destructive_corpus(world):
        if is_install(a) and lane != "toolkit":
            continue
        refused, text = run_attempt(lane, world, a)
        assert refused, a
        if lane == "toolkit":
            # The toolkit's own Policy refuses (allowlist / parser / containment rule),
            # not check_action: assert the rule name is one of the Policy's own.
            rule = text.split(":", 1)[0]
            assert rule in TOOLKIT_RULES, (a, text)
        else:
            # two older lane guards may refuse first and say so: react's regex on
            # `rm -r` ("blocked (") and agent_loop's parser on a redirect into a file.
            assert "write policy" in text or "escapes working directory" in text \
                or "resolves outside" in text or "blocked (" in text \
                or "redirecting output into a file" in text, (a, text)


@pytest.mark.parametrize("lane", ("react", "agent_loop"))
def test_destructive_shell_text_says_needs_approval_not_wired(world, lane):
    refused, text = run_attempt(lane, world, Attempt("shell", f"git -C {world.repo} push"))
    assert refused and "needs approval" in text and "not wired" in text


@pytest.mark.parametrize("lane", LANES)
def test_benign_controls_still_work(world, lane):
    if lane == "react":
        ex = default_tool_executor(cwd=str(world.root))
        assert not ex("write_file", {"path": "sub/../ok.txt", "content": "k"}).startswith("tool error")
        assert not ex("bash", {"command": "echo ok > out.txt 2>&1"}).startswith("tool error")
        assert (world.root / "out.txt").read_text().strip() == "ok"
    elif lane == "agent_loop":
        from llm_router.hooks.agent_loop import execute_tool
        out = execute_tool("write_file", {"path": "sub/../ok.txt", "content": "k"}, world.root)
        assert not out.startswith("Error"), out
        out = execute_tool("run_command", {"command": "ls 2>&1"}, world.root)
        assert not out.startswith("Error"), out
    else:
        pol = Policy(root=world.root, bash_allowed=True)
        assert pol.decide("write", {"path": "sub/../ok.txt", "content": "k"}).allow
        assert pol.decide("bash", {"command": "ls 2>&1"}).allow
    if lane != "toolkit":                                    # the toolkit only decides
        assert (world.root / "ok.txt").read_text() == "k"


# ── D4: policy-level units ───────────────────────────────────────────────────

def test_contain_table(world):
    r = world.root
    inside, why = wp.contain("sub/../ok.txt", r)
    assert why is None and inside == r / "ok.txt"
    for bad in ("../x", "/etc/passwd", "link/x", "", "  ", "a\x00b", "~/x", None, 5):
        got, why = wp.contain(bad, r)
        assert got is None and why, bad
    assert wp.contain("../x", r)[1] == wp.REASON_OUTSIDE
    assert wp.contain(str(r), r)[0] == r


def test_check_write_verdicts(world):
    assert wp.check_write("ok.txt", world.root).decision == wp.ALLOW
    v = wp.check_write("../x", world.root)
    assert v.decision == wp.REFUSE and "../x" in v.reason


@pytest.mark.parametrize("cmd", [
    "rm -rf v", "rm -r v", "rm -fr v", "rm -R v", "rm --recursive v", "FOO=1 rm -r v",
    "nice -n 5 rm -r v", "echo a && rm -r v", "git push", "git -C x push origin", "git reset --hard",
    "git checkout .", "git clean -fd", "git stash", "git branch newname", "git rebase main",
    "pip install x", "pip3 uninstall x", "python3 -m pip install x", "uv pip install x", "uv add x",
    "uv sync", "npm install", "pnpm add x", "yarn publish", "cargo install x", "go get x",
    "gem install x", "brew install x", "apt-get install x", "poetry add x",
    "sudo ls", "mkfs.ext4 /dev/x", "dd if=/dev/zero of=/dev/null", "chmod -R 700 .", "chown -R me .",
    "shutdown now", "shred f",
    # install aliases (D-146 item 1)
    "npm i x", "npm ci", "npm uninstall x", "npm un x", "npm rm x", "npm update", "yarn", "yarn remove x",
    "pnpm i", "pnpm remove x", "uv run pip install x", "uv run --with y pip install x", "uv remove x",
    "uv tool install x", "pipx install x", "pipx upgrade x",
    # mutating git forms stay refused (item 3)
    "git checkout -b new", "git commit -m x", "git stash push", "git stash pop", "git stash drop",
    "git tag v1", "git branch -d x", "git branch -D x", "git config user.name x", "git remote add o u",
    "git stash",
    # D-162: -v does not make remote read-only; option values before the subcommand
    "git remote -v add o U", "git remote -v set-url o U", "git remote --verbose rename o p",
    "git remote -v remove o", "git remote -v prune o", "git remote add -v o U",
    "npm --prefix . i x", "yarn --cwd . add x", "pnpm -C . add x", "pnpm --dir . i",
    "pip --index-url U install y", "yarn global add x", "npm install-test", "npm it", "npm cit",
    "npm in x", "npm isntall x", "npm exec foo", "npm x foo", "yarn dlx foo", "pnpm dlx foo",
    "pipx run foo", "npm --prefix . exec foo"])
def test_destructive_table_needs_approval(world, cmd):
    v = wp.check_action(cmd, world.root)
    assert v.decision == wp.NEEDS_APPROVAL, v
    assert "needs approval" in v.reason and "not wired" in v.reason
    assert wp.destructive_reason(cmd)


@pytest.mark.parametrize("cmd", [
    "rm file.txt", "rm -f file.txt", "git status", "git log --oneline", "git diff", "git show HEAD",
    "git branch --show-current", "git branch -a", "pip list", "python3 -m pytest -q", "uv run pytest",
    "npm test", "npm run build", "npm run test -- -x", "yarn --version", "yarn test", "yarn run build",
    "uv run pip list", "git stash list", "git stash show", "git tag", "git tag -l", "git tag --list",
    "git branch", "git branch --list", "git branch -a", "git branch -r", "git remote", "git remote -v",
    "git remote show origin", "git config --get user.name", "git config --list", "git rev-parse HEAD",
    "git ls-files", "git blame a.txt", "git remote -v show origin", "git remote --verbose",
    "npm --prefix . run build", "npm --prefix . test", "yarn --cwd . test", "pip --version",
    "pip --index-url U list", "npm run i", "npm test -- add", "git -C x log", "ls -la", "echo 'git push' > note.txt", "grep -r push .", "echo ok 2>&1", ""])
def test_benign_commands_are_not_destructive(world, cmd):
    assert wp.destructive_reason(cmd) is None
    assert wp.check_action(cmd, world.root).decision == wp.ALLOW


@pytest.mark.parametrize("lane", ("react", "agent_loop"))
def test_read_only_git_runs_and_mutating_git_does_not_on_every_lane(world, lane):
    for cmd in ("git status", "git log --oneline", "git diff", "git stash list", "git tag -l",
                "git branch -a", "git remote -v", "git config --list", "git rev-parse --git-dir",
                "git ls-files"):
        refused, text = run_attempt(lane, world, Attempt("shell", f"git -C {world.repo} {cmd[4:]}"))
        assert not refused or "write policy" not in text, (cmd, text)
    for cmd in ("checkout -b nb", "stash push", "tag v9", "branch -D x", "config a.b c", "commit -m m"):
        refused, text = run_attempt(lane, world, Attempt("shell", f"git -C {world.repo} {cmd}"))
        assert refused and "write policy" in text, (cmd, text)


def test_check_action_precedence_refuse_beats_approval(world):
    v = wp.check_action("rm -r victim && echo x > ../out.txt", world.root)
    assert v.decision == wp.REFUSE
    assert wp.check_command("git push", world.root).decision == wp.ALLOW   # P2.9-b contract intact
    assert wp.check_action("git push", world.root).decision == wp.NEEDS_APPROVAL


def cwd_trap_failures(w: World, monkeypatch) -> list[str]:
    """pick_root with no candidate must not invent a root from the process cwd."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(w.outside)                      # a cwd that would be accepted by mistake
    got = wp.pick_root(None, None)
    return [] if got.root is None and got.source == "none" else [f"invented {got}"]


def test_pick_root_order_and_no_cwd(world, monkeypatch, tmp_path):
    mcp, env, req = world.root, world.outside, world.repo
    assert wp.pick_root(mcp, req) == wp.RootChoice(mcp.resolve(), "mcp", "root from mcp")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(env))
    assert wp.pick_root(mcp, req).source == "mcp"
    assert wp.pick_root(None, req).source == "env" and wp.pick_root(None, req).root == env
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    assert wp.pick_root(None, req).source == "requested"
    assert wp.pick_root(tmp_path / "missing", None).root is None   # not an existing directory
    assert cwd_trap_failures(world, monkeypatch) == []


# ── D2: llm_local_task door ──────────────────────────────────────────────────

def _ctx_with_root(root: Path):
    class _Session:
        async def list_roots(self):
            return SimpleNamespace(roots=[SimpleNamespace(uri=root.as_uri())])
    return SimpleNamespace(session=_Session())


async def door2(w: World, monkeypatch, ran: list[str]) -> list[str]:
    """Door expectations of llm_local_task; returns the ones that FAILED."""
    from llm_router import mcp_roots
    from llm_router.tools import local_task as lt

    def _loop(**kw):
        ran.append(str(kw.get("project_root", kw.get("root", "ran"))))
        return "ok"

    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _loop)
    failed: list[str] = []
    mcp_roots.clear_cache()
    out = json.loads(await lt.llm_local_task("o", str(w.outside), ctx=_ctx_with_root(w.root)))
    if out["status"] != lt.BLOCKED or "outside the project root" not in out.get("reason", ""):
        failed.append(f"outside workdir not blocked: {out.get('status')}")
    if ran:
        failed.append("model loop started for an outside workdir")
    mcp_roots.clear_cache()
    out = json.loads(await lt.llm_local_task("o", str(w.root), ctx=_ctx_with_root(w.root)))
    if out["status"] == lt.BLOCKED or out.get("root_source") != "mcp" or not ran:
        failed.append(f"inside workdir did not run: {out.get('status')}")
    mcp_roots.clear_cache()
    ran.clear()
    out = json.loads(await lt.llm_local_task("o", str(w.outside)))     # no root known anywhere
    if out["status"] == lt.BLOCKED or out.get("root_source") != "requested" or not ran:
        failed.append(f"no-root workdir did not run as today: {out.get('status')}")
    mcp_roots.clear_cache()
    return failed


@pytest.mark.timeout(60)
async def test_d2_local_task_door(world, monkeypatch):
    assert await door2(world, monkeypatch, []) == []


@pytest.mark.timeout(60)
async def test_d2_env_root_also_confines(world, monkeypatch):
    from llm_router.tools import local_task as lt
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(world.root))
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop",
                        lambda **kw: pytest.fail("loop must not start"))
    out = json.loads(await lt.llm_local_task("o", str(world.outside)))
    assert out["status"] == lt.BLOCKED and out["root_source"] == "env"
    assert out["changed_files"] == []


# ── D1: llm_act door ─────────────────────────────────────────────────────────

class _FakeCodex:
    def __init__(self, tier: int, **kw: Any) -> None:
        self.tier = tier

    def run(self, milestone, frozen_context, budget_left):
        from llm_router.agentic.engine import AgentRunResult
        return AgentRunResult({"output": "", "tier": self.tier}, 0.0)


@pytest.mark.timeout(60)
async def test_d1_llm_act_door(world, monkeypatch, temp_db):
    import llm_router.agentic.react as react
    import llm_router.tools.agentic as tool
    from llm_router.tools.consolidated import llm_act

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(world.root))
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(world.base.parent / "rq.jsonl"))
    monkeypatch.chdir(world.outside)
    monkeypatch.setattr(tool, "planner_factory", lambda: (lambda _g: [
        {"id": "M1", "description": "write", "acceptance": {"type": "canary", "marker": "NEVER_SAID"}}]))
    monkeypatch.setattr(tool, "adapters_factory", None)
    monkeypatch.setattr(tool, "CodexAdapter", _FakeCodex)
    monkeypatch.setattr(react, "_default_local_model", lambda: "fake-local")
    results: list[str] = []
    pending = [ToolCall("write_file", {"path": str(world.outside / "x_door.txt"), "content": "x"}),
               ToolCall("bash", {"command": "rm -rf victim"}),
               ToolCall("bash", {"command": f"git -C {world.repo} clean -fd"})]

    def client(messages, tools):
        tail = []
        for m in reversed(messages):
            if m.get("role") != "tool":
                break
            tail.append(str(m.get("content", "")))
        results.extend(reversed(tail))
        if pending:
            batch = pending[:]
            pending.clear()
            return ChatTurn(tool_calls=batch)
        return ChatTurn(content="I could not do it")

    monkeypatch.setattr(react, "_default_ollama_client", lambda model, base_url=None: client)
    out = json.loads(await llm_act("write a file and clean up"))
    assert len(results) == 3 and all(r.startswith("tool error") for r in results), results
    assert not (world.outside / "x_door.txt").exists()
    assert (world.victim / "keep.txt").exists() and (world.repo / "untracked.txt").exists()
    assert out.get("status") not in ("success", "done", "ok"), out


# ── D3: toolkit door ─────────────────────────────────────────────────────────

@pytest.mark.timeout(120)
def test_d3_toolkit_run_task_denies_and_is_not_used(world, monkeypatch, tmp_path):
    from llm_router.toolkit import sandbox
    from llm_router.toolkit.adapters.ollama import AdapterReply
    from llm_router.toolkit.loop import run_task
    from tests.toolkit_fixtures import PY_DIR, make_source

    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:1")
    monkeypatch.delenv("LLM_ROUTER_TOOLLAYER", raising=False)
    src = make_source(tmp_path / "source")
    outside = tmp_path / "escape.txt"

    def call(name, **args):
        return {"function": {"name": name, "arguments": args}}

    class Scripted:
        model, num_ctx = "scripted", 25000

        def __init__(self, calls):
            self.calls = list(calls)

        def chat(self, messages, tools, *, timeout_s):
            c = self.calls.pop(0) if self.calls else call("finish", summary="done")
            return AdapterReply(content="", tool_calls=[c],
                                message={"role": "assistant", "content": "", "tool_calls": [c]},
                                tokens_in=10, tokens_out=5)

    adapter = Scripted([call("write", path=str(outside), content="x"),
                        call("write", path="../escape2.txt", content="x"),
                        call("bash", command="rm -r src"),
                        call("bash", command="rm -rf tests")])
    res = run_task("break out", adapter=adapter, source=src, python_dir=PY_DIR,
                   workspace_parent=tmp_path)
    assert not res.used
    rows = [json.loads(ln) for ln in Path(res.ledger_path).read_text().splitlines() if ln.strip()]
    decisions = [r for r in rows if r.get("kind") == "decision" and r["tool"] in ("write", "bash")]
    assert len(decisions) == 4 and not any(r["allow"] for r in decisions), decisions
    assert not outside.exists() and not (tmp_path / "escape2.txt").exists()
    assert (src / "src" / "pkg.py").exists() and (src / "tests").is_dir()
    assert sandbox is not None


def test_toolkit_file_containment_is_write_policy_contain(world, monkeypatch):
    """The toolkit's file refusals come from write_policy.contain, not a private copy."""
    pol = Policy(root=world.root, bash_allowed=True)
    assert pol.decide("write", {"path": "../x.txt", "content": "x"}).rule == "path_outside_workspace"
    calls: list[object] = []
    real = wp.contain
    monkeypatch.setattr(wp, "contain", lambda raw, root: (calls.append(raw), real(raw, root))[1])
    pol.decide("write", {"path": "../y.txt", "content": "x"})
    assert calls == ["../y.txt"]


def test_toolkit_adds_the_write_policy_layer(world, monkeypatch):
    pol = Policy(root=world.root, bash_allowed=True)
    assert pol.decide("bash", {"command": "ls"}).allow
    monkeypatch.setattr(wp, "check_action",
                        lambda cmd, root: wp.Verdict(wp.NEEDS_APPROVAL, "stub needs approval"))
    d = pol.decide("bash", {"command": "ls"})
    assert not d.allow and d.rule == "write_policy" and "stub" in d.reason


# ── D5: every lane goes through write_policy.contain ─────────────────────────

def _permissive(raw, root):
    p = Path(raw) if os.path.isabs(raw) else Path(root) / raw
    return Path(os.path.abspath(p)), None


def test_d5_all_lanes_use_the_shared_contain(world, monkeypatch):
    target = str(world.base / "x_d5.txt")
    from llm_router.hooks.agent_loop import _resolve_path
    from llm_router.toolkit.policy import resolve_in_workspace
    ex = default_tool_executor(cwd=str(world.root))
    assert ex("write_file", {"path": target, "content": "x"}).startswith("tool error")
    with pytest.raises(PermissionError):
        _resolve_path(target, world.root)
    assert resolve_in_workspace(target, world.root)[0] is None
    monkeypatch.setattr(wp, "contain", _permissive)            # the one shared test, replaced
    assert ex("write_file", {"path": target, "content": "x"}).startswith("wrote")
    assert _resolve_path(target, world.root) == Path(target)
    assert resolve_in_workspace(target, world.root)[0] == Path(target)


# ── mutants: each must turn a corpus/door helper red ─────────────────────────

def test_baseline_has_no_escape_on_any_lane(world):
    for lane in LANES:
        assert escapes(lane, world) == [], lane


def test_m1_contain_with_abspath_lets_the_symlink_escape(world, monkeypatch):
    monkeypatch.setattr(wp, "contain", _permissive)
    assert escapes("toolkit", world) != []        # decides only; react below really writes
    assert escapes("react", world) != []


def test_m2_empty_destructive_table_runs_destructive_commands(world, monkeypatch):
    monkeypatch.setattr(wp, "destructive_reason", lambda cmd: None)
    bad = escapes("agent_loop", world)
    assert bad != [] and "victim removed" in bad


def test_m3_toolkit_delegation_bypassed(world, monkeypatch):
    from llm_router.toolkit import policy as tp
    monkeypatch.setattr(tp, "resolve_in_workspace",
                        lambda raw, root: (Path(raw) if os.path.isabs(raw) else root / raw, None))
    assert any(e.startswith("not refused") for e in escapes("toolkit", world))


def test_m4_pick_root_falling_back_to_cwd_is_caught(world, monkeypatch):
    real = wp.pick_root

    def mutant(mcp_root=None, requested=None):
        got = real(mcp_root, requested)
        return got if got.root is not None else wp.RootChoice(Path.cwd(), "env", "cwd")

    assert cwd_trap_failures(world, monkeypatch) == []
    monkeypatch.setattr(wp, "pick_root", mutant)
    assert cwd_trap_failures(world, monkeypatch) != []


def test_m5_react_mapping_needs_approval_to_allow(world, monkeypatch):
    real = wp.check_action

    def mutant(cmd, root):
        v = real(cmd, root)
        return wp.Verdict(wp.ALLOW, "", v.targets) if v.decision == wp.NEEDS_APPROVAL else v

    monkeypatch.setattr(wp, "check_action", mutant)
    bad = escapes("react", world)
    assert any(e.startswith("not refused: git") for e in bad) or "git clean ran" in bad, bad


async def test_m6_local_task_skipping_the_root_check_is_caught(world, monkeypatch):
    from llm_router.tools import local_task as lt
    monkeypatch.setattr(lt.write_policy, "pick_root",
                        lambda mcp_root=None, requested=None: wp.RootChoice(None, "none", ""))
    failed = await door2(world, monkeypatch, [])
    assert failed and "outside workdir not blocked" in failed[0]


# ── D-146 mutants: alias removed, run re-blocked, git read/mutate flipped ────

def test_m7_removing_install_aliases_is_caught(world, monkeypatch):
    aliases = ["npm i x", "npm ci", "yarn", "pnpm i", "uv run pip install x", "pipx install x"]
    assert all(wp.destructive_reason(c) for c in aliases)
    monkeypatch.setattr(wp, "_PKG_SUBS", {k: v - {"i", "ci", "install"} for k, v in wp._PKG_SUBS.items()})
    assert not all(wp.destructive_reason(c) for c in aliases)


def test_m8_run_reblocked_is_caught(world, monkeypatch):
    assert wp.destructive_reason("npm run build") is None
    monkeypatch.setattr(wp, "_TABLE_EXEMPT", {})
    assert wp.destructive_reason("npm run build")


def test_m9_read_only_git_refused_is_caught(world, monkeypatch):
    reads = ["git stash list", "git tag -l", "git remote -v", "git config --list", "git branch -a"]
    assert all(wp.destructive_reason(c) is None for c in reads)
    monkeypatch.setattr(wp, "_git_read_only", lambda sub, rest: False)
    assert any(wp.destructive_reason(c) for c in reads)


def test_m10_mutating_git_allowed_is_caught(world, monkeypatch):
    writes = ["git stash push", "git tag v1", "git config a b", "git remote add o u", "git branch -D x"]
    assert all(wp.destructive_reason(c) for c in writes)
    monkeypatch.setattr(wp, "_git_read_only", lambda sub, rest: True)
    assert any(wp.destructive_reason(c) is None for c in writes)


def test_m11_remote_v_prefix_mutant_is_caught(world, monkeypatch):
    cmds = ["git remote -v add o U", "git remote --verbose rename o p"]
    assert all(wp.destructive_reason(c) for c in cmds)
    real = wp._git_read_only
    monkeypatch.setattr(wp, "_git_read_only", lambda sub, rest: (
        True if sub == "remote" and rest[:1] in (["-v"], ["--verbose"]) else real(sub, rest)))
    assert any(wp.destructive_reason(c) is None for c in cmds)


def test_m12_first_positional_only_scan_mutant_is_caught(world, monkeypatch):
    cmds = ["npm --prefix . i x", "yarn --cwd . add x", "pip --index-url U install y"]
    assert all(wp.destructive_reason(c) for c in cmds)
    monkeypatch.setattr(wp, "_SCRIPT_RUNNERS", frozenset({"."}))      # scan stops at the option value
    assert any(wp.destructive_reason(c) is None for c in cmds)


def test_m13_exec_runners_removed_is_caught(world, monkeypatch):
    cmds = ["npm exec foo", "npm x foo", "yarn dlx foo", "pnpm dlx foo", "pipx run foo"]
    assert all(wp.destructive_reason(c) for c in cmds)
    monkeypatch.setattr(wp, "_PKG_SUBS", {k: v - {"exec", "x", "dlx", "run"} for k, v in wp._PKG_SUBS.items()})
    assert all(wp.destructive_reason(c) is None for c in cmds)


import pytest as _pytest_f50  # noqa: E402


@_pytest_f50.fixture(autouse=True)
def _user_local_model_f50(monkeypatch):
    """D-205: llm_local_task has no built-in model; the user's setting names one."""
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", "usermodel:1b")
