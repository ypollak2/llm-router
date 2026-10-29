"""The security documentation must describe the code that exists.

B3 of docs/ACTIONS_REMEDIATION_RUN.md. Both README.md and SECURITY.md stated that
`run_command` "executes through a shell", via `subprocess.run(cmd, shell=True)`.
It does not, and it did not at the reviewed commit either — `hooks/agent_loop.py`
has used `shlex.split` + `shell=False` throughout, with a comment saying why.

The docs were also wrong in the safe direction, which is its own problem:
SECURITY.md listed twelve commands and claimed three were blocked. Six of them are
refused today by `agent_writes.guard_command`, a second layer the document never
mentioned, and `echo $OPENAI_API_KEY` cannot leak anything because there is no
shell to expand it.

A security document that is wrong in both directions is worse than none: a reader
hardens against a threat that does not exist and trusts a layer they were never
told about.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENT_LOOP = ROOT / "src/llm_router/hooks/agent_loop.py"
DOCS = {"README.md": ROOT / "README.md", "SECURITY.md": ROOT / "SECURITY.md"}


def test_run_command_really_does_not_use_a_shell():
    """The fact the docs must agree with. If this ever fails, fix the CODE."""
    tree = ast.parse(AGENT_LOOP.read_text())
    bad = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
           for kw in n.keywords
           if kw.arg == "shell" and getattr(kw.value, "value", False) is True]
    assert not bad, f"agent_loop.py now uses a shell at line(s) {bad}"
    # The tokenizer moved to the toolkit (agent_loop.py calls it): one executor. So the
    # shell-free property is checked on BOTH files, plus the call that joins them.
    tools_tree = ast.parse((ROOT / "src/llm_router/toolkit/tools.py").read_text())
    bad_tools = [n.lineno for n in ast.walk(tools_tree) if isinstance(n, ast.Call)
                 for kw in n.keywords if kw.arg == "shell" and getattr(kw.value, "value", False) is True]
    assert not bad_tools, f"toolkit/tools.py (the executor the hook calls) uses a shell at line(s) {bad_tools}"
    assert _tokenizes_with_shlex((ROOT / "src/llm_router/toolkit/tools.py").read_text())
    hook_calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "_kit.run_pipelines" in hook_calls and "_writes.guard_command" in hook_calls, (
        "agent_loop.py no longer calls the toolkit executor behind the allowlist guard: this test's "
        "premise (the hook path is the toolkit path) is gone")


@pytest.mark.parametrize("name", sorted(DOCS))
def test_no_doc_claims_run_command_uses_a_shell(name):
    text = DOCS[name].read_text()
    claims = [ln.strip() for ln in text.splitlines()
              if re.search(r"run_command.{0,80}(through a shell|shell=True)", ln)
              and "does **not**" not in ln and "not execute through a shell" not in ln]
    assert not claims, f"{name} still claims run_command uses a shell: {claims[:2]}"


@pytest.mark.parametrize("name", sorted(DOCS))
def test_the_allowlist_layer_is_documented(name):
    text = DOCS[name].read_text()
    assert "guard_command" in text or "allowlist" in text, (
        f"{name} describes only the regex blocklist and never mentions the "
        "allowlist, which is the stronger of the two layers"
    )


def test_the_allowlist_blocks_what_the_old_table_called_unblocked(monkeypatch):
    """Two bugs fixed here, both found while doing R3.

    1. It passed a STRING to `guard_command`, which takes an argv LIST.
       `argv[0]` was therefore `"g"`, and every command was refused on
       *"'g' is not in the inspection allowlist"* — the right answer for
       entirely the wrong reason. It would have passed unchanged if `git push`
       had been explicitly allowed. Measured:

           as a string:  REFUSED: 'g' is not in the inspection allowlist
           as an argv:   REFUSED: 'git push' changes state rather than reading it

       The refusal REASON is now asserted, not just the boolean, so the test
       cannot go on being satisfied by an accident of input shape.

    2. `from llm_router.hooks import agent_writes` leaves a fileless module
       stub on the `llm_router.hooks` package, which the suite's own T-01 guard
       rejects — it answers for the real module in every test that follows.
       Loaded by path instead.
    """
    import importlib.util

    monkeypatch.delenv("LLM_ROUTER_AGENT_COMMANDS", raising=False)
    path = DOCS["SECURITY.md"].parent / "src/llm_router/hooks/agent_writes.py"
    spec = importlib.util.spec_from_file_location("_aw_docs_probe", path)
    agent_writes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent_writes)

    for argv, because in (
        (["git", "push", "--force"], "changes state"),
        (["npm", "install"], "not in the inspection allowlist"),
        (["pip", "install", "requests"], "not in the inspection allowlist"),
        (["rm", "-rf", "./src"], "not in the inspection allowlist"),
        (["git", "reset", "--hard"], "changes state"),
    ):
        allowed, msg = agent_writes.guard_command(argv)
        assert not allowed, f"{argv!r} is not actually blocked; the docs claim it is"
        assert because in msg, (
            f"{argv!r} was refused, but for the wrong reason: {msg!r}. Expected "
            f"a refusal mentioning {because!r}."
        )


def test_security_md_names_the_population_its_table_measures():
    """R3 replaced this test's original contract, and why is worth keeping.

    It used to require SECURITY.md to say the twelve-command table
    "UNDERSTATES" the real protection — i.e. that the allowlist blocks MORE
    than the table shows. That was true and it was the misleading half. The
    table's twelve commands are all obviously destructive, so a high refusal
    rate over them says nothing about what an agent can actually do; measured
    against the same capabilities reached through allowlisted interpreters,
    10 of 10 are ALLOWED.

    So the contract is now the opposite: the document must name BOTH
    populations, because the difference between them is the finding. Asking it
    to advertise that the allowlist blocks more than the table shows is asking
    it to undersell nothing and oversell the boundary.

    `tests/test_r3_allowlist_is_not_containment.py` owns the numbers; this
    asserts the framing survives.
    """
    text = DOCS["SECURITY.md"].read_text()
    assert "10 of 12" in text, "the obvious-destructive population is not stated"
    assert "10 of 10" in text, (
        "the interpreter population is not stated. Quoting only the flattering "
        "row is what made an accurate number misleading."
    )
    assert "not a containment" in text.lower(), (
        "SECURITY.md no longer says what the allowlist is NOT"
    )


def _tokenizes_with_shlex(src: str) -> bool:
    """AST, not text (K7): a call to shlex.split or shlex.shlex exists. S
    (2026-09-24) moved run_command to a shlex.shlex tokenizer so `&&`/`|` are
    split and chained without a shell; either form keeps the argv guarantee."""
    import ast as _ast
    return any(isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
               and n.func.attr in ("split", "shlex")
               and getattr(n.func.value, "id", "") == "shlex"
               for n in _ast.walk(_ast.parse(src)))


# ── UN-15 / W3-4: the refuse-only safety contract (NFR-SAFE amended, D-321) ──
#
# SECURITY.md "Local action safety contract (refuse-only)" says which local actions
# the router refuses and that v16 has no approval flow. These tests read that
# section's tables and run every row through the REAL lane executors, with only the
# final execution step (`subprocess.run` for llm_act, `run_pipelines` for
# llm_local_task) replaced by a recorder, so nothing destructive runs even when a
# regression lets a row through. "Not run" means the recorder saw nothing.

CONTRACT_HEADING = "## Local action safety contract (refuse-only)"
LANES = ("llm_act", "llm_local_task", "toolkit")
CONFIGS = {
    "default": {},
    "LLM_ROUTER_AGENT_WRITES=apply": {"LLM_ROUTER_AGENT_WRITES": "apply"},
    "LLM_ROUTER_AGENT_COMMANDS=all": {"LLM_ROUTER_AGENT_COMMANDS": "all"},
    "most-permissive": {"LLM_ROUTER_AGENT_WRITES": "apply", "LLM_ROUTER_AGENT_COMMANDS": "all"},
}


def _contract_section() -> str:
    text = DOCS["SECURITY.md"].read_text()
    if CONTRACT_HEADING not in text:
        return ""
    after = text.split(CONTRACT_HEADING, 1)[1]
    return after.split("\n## ", 1)[0]


def _cell(raw: str) -> str:
    """A table cell without its backticks; `\\|` is a literal pipe."""
    raw = raw.strip()
    m = re.fullmatch(r"`([^`]*)`(.*)", raw)
    return (m.group(1) if m else raw).replace("\\|", "|")


def _table(first_header: str, *more: str, raw: bool = False) -> list[list[str]]:
    """Rows of the section's table whose header row starts with these cells
    (cells unwrapped by `_cell`, or as written when *raw*)."""
    want = [first_header, *more]
    rows: list[list[str]] = []
    inside = False
    for line in _contract_section().splitlines():
        if not line.startswith("|"):
            inside = False
            continue
        cells = [c for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if [c.strip() for c in cells[:len(want)]] == want:
            inside = True
            continue
        if inside and not set(line) <= set("|-: "):
            rows.append([c.strip() if raw else _cell(c) for c in cells])
    return rows


def _refused_rows():
    return [(r[0], r[1]) for r in _table("command", "verdict", "class")]


def _file_rows():
    return [(r[0], r[1]) for r in _table("path", "verdict")]


def _by_design_rows():
    return [(r[0], r[1]) for r in _table("lane", "command")]


def _gap_rows():
    """(lane, configuration, command): one per lane a row names (`llm_act`, `toolkit`)."""
    out = []
    for r in _table("lane", "configuration", "command", "effect", raw=True):
        for lane in re.findall(r"`([^`]+)`", r[0]):
            out.append((lane, _cell(r[1]), _cell(r[2])))
    return out


def _fill(cmd: str, w: dict) -> str:
    """The gaps table writes `{OUT}` (outside the root) and `{HOME}`."""
    return cmd.replace("{OUT}", str(w["outside"])).replace("{HOME}", str(w["home"]))


@pytest.fixture
def contract_world(tmp_path, monkeypatch):
    """A project root, a directory outside it, a symlink out, and recorders."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "lr"))
    for var in ("CLAUDE_PROJECT_DIR", "LLM_ROUTER_PROJECT_ROOT",
                "LLM_ROUTER_AGENT_WRITES", "LLM_ROUTER_AGENT_COMMANDS"):
        monkeypatch.delenv(var, raising=False)
    base = tmp_path.resolve() / "base"
    root, outside = base / "project", base / "outside"
    (root / "src").mkdir(parents=True)
    outside.mkdir()
    (root / "a.txt").write_text("a\n")
    (root / "src" / "keep.py").write_text("x = 1\n")
    (root / "link").symlink_to(outside, target_is_directory=True)

    import subprocess as _sp

    from llm_router.toolkit import tools as kit

    ran: list[object] = []

    def record_run(argv, *a, **k):
        ran.append(argv)
        return _sp.CompletedProcess(argv, 0, "", "")

    def record_pipelines(parsed, **k):
        ran.append(parsed)
        return "[recorded, not run]"

    def refuse_to_execute(*a, **k):
        # Not AssertionError: a gap xfail (raises=AssertionError) must not absorb it.
        raise RuntimeError("a lane reached Popen/os.system: this test would have executed it")

    monkeypatch.setattr(_sp, "run", record_run)
    monkeypatch.setattr(kit, "run_pipelines", record_pipelines)
    # Fail closed: if a refactor moves a lane off the two recorded calls, nothing
    # runs for real; the test errors instead.
    monkeypatch.setattr(_sp, "Popen", refuse_to_execute)
    monkeypatch.setattr(os, "system", refuse_to_execute)
    return {"root": root, "outside": outside, "base": base, "home": home, "ran": ran}


def _use_config(monkeypatch, config: str) -> None:
    for var in ("LLM_ROUTER_AGENT_WRITES", "LLM_ROUTER_AGENT_COMMANDS"):
        monkeypatch.delenv(var, raising=False)
    for var, value in CONFIGS[config].items():
        monkeypatch.setenv(var, value)


def _run_command(lane: str, cmd: str, w: dict) -> tuple[bool, str]:
    """(executed, lane output) for one shell command through the lane's real entry."""
    w["ran"].clear()
    root = w["root"]
    if lane == "llm_act":
        from llm_router.agentic.react import default_tool_executor
        out = default_tool_executor(cwd=str(root))("bash", {"command": cmd})
        return bool(w["ran"]), out
    if lane == "llm_local_task":
        from llm_router.hooks.agent_loop import execute_tool
        out = execute_tool("run_command", {"command": cmd}, root)
        return bool(w["ran"]), out
    from llm_router.toolkit.policy import Policy
    d = Policy(root=root, bash_allowed=True).decide("bash", {"command": cmd})
    return d.allow, f"{d.rule}: {d.reason}"


def _refused_output(lane: str, out: str) -> bool:
    if lane == "llm_act":
        return out.startswith("tool error")
    if lane == "llm_local_task":
        return out.startswith(("Error", "REFUSED"))
    return not out.startswith("ok:")


def test_contract_section_exists_with_every_table():
    """Anti-vacuity: every parametrized test below is empty if a table goes missing."""
    section = _contract_section()
    assert section, f"SECURITY.md has no {CONTRACT_HEADING!r} section"
    assert "no approval flow in v16" in section.lower()
    assert len(_refused_rows()) >= 20, _refused_rows()
    assert len(_file_rows()) >= 4, _file_rows()
    assert _by_design_rows(), "the runs-by-design table is gone"
    assert _gap_rows(), "the known-gaps table is gone; if every gap is fixed, delete this line"
    assert {lane for lane, _c, _cmd in _gap_rows()} <= set(LANES), _gap_rows()
    assert {c for _l, c, _cmd in _gap_rows()} <= set(CONFIGS), _gap_rows()
    lanes_named = {r[0] for r in _table("lane", "entry point", "code")}
    assert lanes_named == set(LANES), lanes_named


def test_contract_verdict_tokens_are_the_policy_constants():
    from llm_router import write_policy as wp
    documented = {r[0] for r in _table("verdict", "what every lane does")}
    assert documented == {wp.REFUSE, wp.NEEDS_APPROVAL, wp.ALLOW}, documented
    used = {v for _c, v in _refused_rows()} | {v for _p, v in _file_rows()}
    assert used <= {wp.REFUSE, wp.NEEDS_APPROVAL}, used


@pytest.mark.parametrize("cmd,verdict", _refused_rows(), ids=lambda v: str(v)[:40])
def test_documented_verdict_is_what_check_action_returns(contract_world, cmd, verdict):
    from llm_router import write_policy as wp
    got = wp.check_action(cmd, contract_world["root"])
    assert got.decision == verdict, (cmd, got)
    if verdict == wp.NEEDS_APPROVAL:
        # No approval flow exists: the policy itself says the command is refused.
        assert "approval is not wired, so treated as refused" in got.reason, got


@pytest.mark.parametrize("config", ["default", "most-permissive"])
@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("cmd,verdict", _refused_rows(), ids=lambda v: str(v)[:40])
def test_documented_refusal_never_runs_on_any_lane(contract_world, monkeypatch, lane, config,
                                                   cmd, verdict):
    _use_config(monkeypatch, config)
    executed, out = _run_command(lane, cmd, contract_world)
    assert not executed, f"{lane} ({config}) RAN {cmd!r}: {out[:200]}"
    assert _refused_output(lane, out), f"{lane} ({config}) did not say it refused {cmd!r}: {out[:200]}"


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("path,verdict", _file_rows(), ids=lambda v: str(v)[:30])
def test_documented_file_write_refusal_holds_on_every_lane(contract_world, monkeypatch, lane,
                                                           path, verdict):
    from llm_router import write_policy as wp
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", "apply")   # containment, not propose, decides
    root, base = contract_world["root"], contract_world["base"]
    raw = path.split(" ", 1)[0]
    assert wp.check_write(raw, root).decision == verdict
    before = sorted(str(p) for p in base.parent.rglob("*") if not p.is_relative_to(root))
    if lane == "llm_act":
        from llm_router.agentic.react import default_tool_executor
        out = default_tool_executor(cwd=str(root))("write_file", {"path": raw, "content": "x"})
        assert out.startswith("tool error"), out
    elif lane == "llm_local_task":
        from llm_router.hooks.agent_loop import execute_tool
        out = execute_tool("write_file", {"path": raw, "content": "x"}, root)
        assert out.startswith(("Error", "REFUSED")), out
    else:
        from llm_router.toolkit.policy import Policy
        assert not Policy(root=root, bash_allowed=True).decide(
            "write", {"path": raw, "content": "x"}).allow
    after = sorted(str(p) for p in base.parent.rglob("*") if not p.is_relative_to(root))
    assert after == before, "a file appeared outside the project root"


@pytest.mark.parametrize("lane,cmd", _by_design_rows(), ids=lambda v: str(v)[:30])
def test_documented_runs_by_design_really_run(contract_world, lane, cmd):
    """The section must not overstate the refusals either."""
    executed, out = _run_command(lane, cmd, contract_world)
    assert executed, f"{lane} now refuses {cmd!r} ({out[:160]}): move it out of 'Runs by design'"


def _gap_params():
    return [pytest.param(lane, config, cmd, id=f"{lane}|{config}|{cmd}"[:70],
                         marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
                             f"UN-15 finding: {lane} runs {cmd!r} under {config} "
                             "(SECURITY.md known gaps)")))
            for lane, config, cmd in _gap_rows()]


@pytest.mark.parametrize("lane,config,cmd", _gap_params())
def test_known_gap_is_refused(contract_world, monkeypatch, lane, config, cmd):
    """Strict xfail: each documented gap really runs. A fix XPASSes and fails here,
    so the row has to move out of the known-gaps table in the same change."""
    _use_config(monkeypatch, config)
    executed, out = _run_command(lane, _fill(cmd, contract_world), contract_world)
    assert not executed, f"{lane} ({config}) ran {cmd!r}"


def test_llm_act_without_a_root_refuses_bash_and_writes(contract_world):
    from llm_router.agentic.react import default_tool_executor
    ex = default_tool_executor(cwd=None)
    assert ex("bash", {"command": "echo hi"}).startswith("tool error")
    assert ex("write_file", {"path": "x.txt", "content": "x"}).startswith("tool error")
    assert contract_world["ran"] == []


def test_llm_local_task_default_proposes_and_writes_nothing(contract_world):
    from llm_router.hooks.agent_loop import execute_tool
    root = contract_world["root"]
    out = execute_tool("write_file", {"path": "a.txt", "content": "changed\n"}, root)
    assert out.startswith("PROPOSED — NOT APPLIED"), out
    assert (root / "a.txt").read_text() == "a\n"


@pytest.mark.parametrize("lane,config", [("llm_act", "default"),
                                         ("llm_local_task", "most-permissive")])
def test_mutant_needs_approval_runs_is_caught(contract_world, monkeypatch, lane, config):
    """Negative control: if a lane ran a needs_approval command, the lane test above
    would see it. The mutant maps needs_approval to allow inside check_action."""
    from llm_router import write_policy as wp
    real = wp.check_action

    def mutant(cmd, root):
        v = real(cmd, root)
        return wp.Verdict(wp.ALLOW, "", v.targets) if v.decision == wp.NEEDS_APPROVAL else v

    _use_config(monkeypatch, config)
    rows = [c for c, v in _refused_rows() if v == wp.NEEDS_APPROVAL]
    assert rows
    assert not any(_run_command(lane, c, contract_world)[0] for c in rows)
    monkeypatch.setattr(wp, "check_action", mutant)
    leaked = [c for c in rows if _run_command(lane, c, contract_world)[0]]
    assert leaked, f"the mutant let nothing through on {lane}: the lane test cannot see a leak"
