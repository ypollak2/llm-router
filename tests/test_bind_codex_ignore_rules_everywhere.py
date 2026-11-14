"""BIND-8: every ``codex exec`` this repo starts carries ``--ignore-rules`` (one frozen study script excepted), and no
routing path starts one on a CLI that cannot honour it.

The hole (docs/bugs/BIND-6.md, reproduced live on codex-cli 0.161.0): ``--ignore-user-config`` does not stop Codex loading
execpolicy ``.rules`` files, and a rule such as ``prefix_rule(pattern=["curl"], decision="allow")`` makes Codex run a matching
command OUTSIDE its sandbox, with network, in every sandbox mode. The owner's own rules allow ``curl``, ``git push``,
``gh pr merge`` and ``gh run``. BIND-6 closed it for the delegated agent-route run. The router chain, the MCP tools and
every other ``run_codex`` caller passed no sandbox flag and no ``--ignore-rules``, so a rule-allowed command asked for by a
model they started was not sandboxed either (BIND-8).

Four layers; docs/bugs/BIND-8.md lists the mutation that turns each one red.

1. CENSUS   an AST walk of src/, scripts/ and hooks/ finds every ``codex exec`` argv list and command string. The set must
            equal the known one, so a new call site cannot appear without the flag and a pin in this file. One site is
            exempt by name: the frozen CLS-DEV study script (D-345, sha-pinned), which is not a routing path.
2. ARGV     the exact argv of each call site; a delegated run also carries ``-c web_search="disabled"`` (BIND-9).
3. CALLERS  the real callers (MCP tool, router chain, session broker, evaluator, calibration, agent-route hook, agentic
            adapter) start a stand-in ``codex`` and the launch carries the flag.
4. CHECK    a CLI whose ``exec --help`` does not list the flag is never started: the call fails with reason_code
            ``no_ignore_rules``, one WARNING, one ``CHZ-FO-CODEX-NO-IGNORE-RULES`` row, and routing falls back. The probe is
            asked once per binary, and a probe that cannot answer counts as "no".

The live case (the real binary, a stub model, a canary listener: no escape) is in tests/test_bind_codex_sandbox_live.py.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import sys
import types
from pathlib import Path

import pytest

from llm_router import codex_agent, failopen
from llm_router.codex_agent import IGNORE_RULES_FLAG, codex_exec_args, run_codex
from tests._bind_support import (
    delivered_text,
    AGENT_ROUTE,
    SUITABLE_PROMPT,
    agent_payload,
    decision_of,
    hook_env,
    init_repo,
    ledger_rows,
    run_hook,
    stub_calls,
    write_codex_stub,
)

REPO = Path(__file__).resolve().parents[1]
FAILOPEN_CODE = "CHZ-FO-CODEX-NO-IGNORE-RULES"
# What every run_codex launch starts with, as the stand-in records it (argv without the binary).
RUN_CODEX_HEAD = ["exec", "--json", "--ignore-user-config", "--ignore-rules"]

pytestmark = [pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell stub")]


# =============================================================================================== 1. CENSUS

#: Every argv list ``[<binary>, "exec", ...]`` in the shipped code: (file, enclosing function) -> "run" or "help".
#: "run" starts Codex and must carry the flag; "help" asks ``exec --help`` and starts nothing.
KNOWN_ARGV_SITES = {
    ("src/llm_router/codex_agent.py", "codex_exec_args"): "run",       # run_codex: router chain, MCP tool, broker, evaluator, calibrate, agent-route
    ("src/llm_router/agentic/adapters.py", "CodexAdapter._codex_argv"): "run",
    ("scripts/cls_send_lib.py", "codex_argv"): "run",                  # frozen CLS-DEV study script: UNPROTECTED_ON_PURPOSE
    ("scripts/bench_backend_quality.py", "run_codex"): "run",
    ("src/llm_router/codex_agent.py", "_probe_ignore_rules"): "help",  # the capability check itself
    ("scripts/cls_pins.py", "compute_static.codex"): "help",          # pins the help text; starts no run
}
#: Run sites that do NOT carry the flag, on purpose: (file, function) -> why. Exactly one. The census still finds it (it is in
#: KNOWN_ARGV_SITES) and its argv is pinned as it is (``test_argv_of_the_cls_sender``), so a change to the frozen script is
#: noticed, and any other ``codex exec`` without the flag fails.
UNPROTECTED_ON_PURPOSE = {
    ("scripts/cls_send_lib.py", "codex_argv"):
        "frozen CLS-DEV study script (D-345, sha-pinned); not a routing path; do not run CLS-DEV scripts",
}
#: Command LINES (strings that begin ``codex exec``) in code, outside docstrings: a recommendation shown to the model is a
#: call site too, because the model runs it.
KNOWN_COMMAND_STRINGS = {("src/llm_router/tools/routing.py", "llm_select_agent")}

# A command line starts with the codex binary: the literal word, or an f-string placeholder whose expression names it
# (``f"{codex_bin} exec ..."``; the census renders such a placeholder as ``{<expr>}``).
_COMMAND_RX = re.compile(r"^\s*(?:\S*/)?(?:codex|\{[^}]*codex[^}]*\})\s+exec\b", re.IGNORECASE)


def _via_name(name: ast.Name) -> ast.Constant:
    """A use of a module-level ``X = "exec"``: counts only behind a codex-named element, never by position alone."""
    const = ast.copy_location(ast.Constant("exec"), name)
    const.via_name = True  # type: ignore[attr-defined]
    return const


def _flat(elts: list[ast.expr]) -> list[ast.expr]:
    """Splice ``*[...]`` / ``*(...)`` of a literal into the sequence it sits in."""
    out: list[ast.expr] = []
    for e in elts:
        if isinstance(e, ast.Starred) and isinstance(e.value, (ast.List, ast.Tuple)):
            out += _flat(e.value.elts)
        else:
            out.append(e)
    return out


def _is_codexish(e: ast.expr) -> bool:
    """The codex binary as a literal path (``codex``, ``/x/bin/codex``) or as any expression that names it (``codex_bin``)."""
    if isinstance(e, ast.Constant):
        return isinstance(e.value, str) and Path(e.value).name == "codex"
    return "codex" in ast.unparse(e).lower()


def _is_exec_argv(elts: list[ast.expr], positional: bool) -> bool:
    """``[<binary>, "exec", ...]`` in a list or tuple (``positional``: any binary), or ``"exec"`` anywhere later behind an
    element that names codex (``["env", "A=1", "codex", "exec"]``, ``create_subprocess_exec("codex", "exec", ...)``)."""
    if positional and len(elts) >= 2 and isinstance(elts[1], ast.Constant) and elts[1].value == "exec" \
            and not getattr(elts[1], "via_name", False):
        return True
    return any(isinstance(e, ast.Constant) and e.value == "exec" and any(_is_codexish(x) for x in elts[:i])
               for i, e in enumerate(elts) if i >= 1)


class _Census(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel, self.stack, self.docs = rel, [], set()
        self.argv: list[tuple[str, str, int, list[ast.expr]]] = []
        self.strings: list[tuple[str, str, int, str]] = []

    def _docstring(self, node) -> None:
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            self.docs.add(id(body[0].value))

    def _scope(self, node) -> None:
        self._docstring(node)
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

    def visit_Module(self, node) -> None:
        self._docstring(node)
        # module-level ``SUB = "exec"``: read a use of SUB as the constant it holds
        names = {t.id for n in node.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
                 and n.value.value == "exec" for t in n.targets if isinstance(t, ast.Name)}
        for n in ast.walk(node):
            for field, child in ast.iter_fields(n):
                if isinstance(child, ast.Name) and child.id in names and isinstance(child.ctx, ast.Load):
                    setattr(n, field, _via_name(child))
                elif isinstance(child, list):
                    child[:] = [_via_name(c) if isinstance(c, ast.Name) and c.id in names
                                and isinstance(c.ctx, ast.Load) else c for c in child]
        self.generic_visit(node)

    def _where(self) -> str:
        return ".".join(self.stack) or "<module>"

    def visit_List(self, node) -> None:
        elts = _flat(node.elts)
        if _is_exec_argv(elts, positional=True):
            self.argv.append((self.rel, self._where(), node.lineno, elts))
        self.generic_visit(node)

    visit_Tuple = visit_List

    def visit_Call(self, node) -> None:  # asyncio.create_subprocess_exec("codex", "exec", ...): argv as positional arguments
        elts = _flat(node.args)
        if _is_exec_argv(elts, positional=False):
            self.argv.append((self.rel, self._where(), node.lineno, elts))
        self.generic_visit(node)

    def visit_BinOp(self, node) -> None:  # ["codex"] + ["exec", ...]
        if isinstance(node.op, ast.Add) and isinstance(node.left, (ast.List, ast.Tuple)) \
                and isinstance(node.right, (ast.List, ast.Tuple)):
            elts = _flat(node.left.elts) + _flat(node.right.elts)
            if node.right.elts and _is_exec_argv(elts, positional=False) \
                    and not _is_exec_argv(_flat(node.left.elts), positional=True) \
                    and not _is_exec_argv(_flat(node.right.elts), positional=True):
                self.argv.append((self.rel, self._where(), node.lineno, elts))
        self.generic_visit(node)

    def visit_JoinedStr(self, node) -> None:  # an f-string: judge its literal text once, not each part
        text = "".join(v.value if isinstance(v, ast.Constant) else "{" + ast.unparse(v.value) + "}" for v in node.values)
        if _COMMAND_RX.match(text):
            self.strings.append((self.rel, self._where(), node.lineno, text))

    def visit_Constant(self, node) -> None:
        if isinstance(node.value, str) and id(node) not in self.docs and _COMMAND_RX.match(node.value):
            self.strings.append((self.rel, self._where(), node.lineno, node.value))


def _census(root: Path = REPO):
    argv, strings, scanned = [], [], 0
    for tree_dir in ("src", "scripts", "hooks"):
        for path in sorted((root / tree_dir).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if "__pycache__" in path.parts or any(p.startswith(".") for p in path.relative_to(root).parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            scanned += 1
            if "exec" not in text:  # a cheap prefilter; it must not depend on the quote style or on the f-string text
                continue
            visitor = _Census(rel)
            visitor.visit(ast.parse(text, filename=rel))
            argv += visitor.argv
            strings += visitor.strings
    return argv, strings, scanned


def _carries_flag(elts: list[ast.expr]) -> bool:
    return any((isinstance(e, ast.Constant) and e.value == IGNORE_RULES_FLAG)
               or (isinstance(e, ast.Name) and e.id == "IGNORE_RULES_FLAG") for e in elts)


def _is_help_query(elts: list[ast.expr]) -> bool:
    return any(isinstance(e, ast.Constant) and e.value == "--help" for e in elts)


def test_the_census_finds_the_known_call_sites_and_nothing_else():
    argv, strings, scanned = _census()
    assert scanned > 300, f"the census read {scanned} files: it is not looking at the shipped tree"
    found = {(rel, where): ("help" if _is_help_query(elts) else "run") for rel, where, _line, elts in argv}
    assert found == KNOWN_ARGV_SITES, (
        "a `codex exec` argv appeared, moved or went away. Every site must carry --ignore-rules (or be an `exec --help` "
        f"query), and have its argv pinned below.\nnew: {sorted(set(found.items()) - set(KNOWN_ARGV_SITES.items()))}\n"
        f"gone: {sorted(set(KNOWN_ARGV_SITES.items()) - set(found.items()))}")
    assert {(rel, where) for rel, where, _l, _t in strings} == KNOWN_COMMAND_STRINGS, \
        f"a `codex exec ...` command line in code changed: {[(r, w, t) for r, w, _l, t in strings]}"


def test_every_run_site_and_command_line_carries_the_flag():
    argv, strings, _ = _census()
    runs = [(rel, where, line) for rel, where, line, elts in argv if not _is_help_query(elts)]
    assert len(runs) == 4, f"expected the four run sites, found {runs}"  # an empty list would pass everything
    unprotected = [(rel, where, line) for rel, where, line, elts in argv
                   if not _is_help_query(elts) and not _carries_flag(elts) and (rel, where) not in UNPROTECTED_ON_PURPOSE]
    assert not unprotected, f"`codex exec` without {IGNORE_RULES_FLAG}: {unprotected}"
    # an exemption must name a site that exists and still lacks the flag, or it is dead weight and goes
    exempt = {(rel, where): elts for rel, where, _line, elts in argv if (rel, where) in UNPROTECTED_ON_PURPOSE}
    assert set(exempt) == set(UNPROTECTED_ON_PURPOSE), f"an exemption names a site that is gone: {UNPROTECTED_ON_PURPOSE}"
    carrying = [site for site, elts in exempt.items() if _carries_flag(elts)]
    assert not carrying, f"{carrying} carries the flag now: drop it from UNPROTECTED_ON_PURPOSE"
    assert strings, "no command line found: the recommendation in tools/routing.py is not being checked"
    bare = [(rel, where, line) for rel, where, line, text in strings if IGNORE_RULES_FLAG not in text]
    assert not bare, f"a `codex exec ...` command line without {IGNORE_RULES_FLAG}: {bare}"


def test_a_help_query_starts_no_run():
    """The two sites exempted from the flag only ask ``exec --help``: nothing but the subcommand and ``--help`` in the list."""
    argv, _, _ = _census()
    helps = [(rel, where, [ast.unparse(e) for e in elts]) for rel, where, _l, elts in argv if _is_help_query(elts)]
    assert len(helps) == 2, helps
    for rel, where, elts in helps:
        assert elts[1:] == ["'exec'", "'--help'"], f"{rel}:{where} is exempt as a help query but carries more: {elts}"


#: Planted shapes (backlog item 7, the census missed 13 of 26 of these planted shapes before the fix; the 13-of-17 claim in review #818 cannot be checked, its corpus is not in the repo). Each is one realistic way Python in
#: src/ could start ``codex exec``; the census must report it. The five xfail rows are the shapes an AST walk of a single
#: expression cannot see (strict: if one starts passing, the marker goes). Data: the table in the PR description.
PLANTED = [
    ('01_list_literal', 'import subprocess\ndef f(p):\n    subprocess.run(["codex", "exec", "--json", p])\n'),
    ('02_list_plus_exec_in_head', 'import subprocess\ndef f(p):\n    subprocess.run(["codex", "exec"] + ["--json", p])\n'),
    ('03_binary_in_variable', 'import subprocess\ndef f(p, binp):\n    subprocess.run([binp, "exec", "--json", p])\n'),
    ('04_shlex_split_string', 'import shlex, subprocess\ndef f(p):\n    subprocess.run(shlex.split("codex exec --json " + p))\n'),
    ('05_popen', 'import subprocess\ndef f(p):\n    subprocess.Popen(["codex", "exec", p])\n'),
    ('06_check_output', 'import subprocess\ndef f(p):\n    return subprocess.check_output(["codex", "exec", p])\n'),
    ('07_asyncio_exec_positional', 'import asyncio\nasync def f(p):\n    await asyncio.create_subprocess_exec("codex", "exec", "--json", p)\n'),
    ('08_os_execvp', 'import os\ndef f(p):\n    os.execvp("codex", ["codex", "exec", p])\n'),
    ('09_exec_in_variable', 'import subprocess\nSUB = "exec"\ndef f(p):\n    subprocess.run(["codex", SUB, p])\n'),
    ('10_tuple', 'import subprocess\ndef f(p):\n    subprocess.run(("codex", "exec", p))\n'),
    ('11_fstring_shell_true', 'import subprocess\ndef f(p):\n    subprocess.run(f"codex exec --json {p}", shell=True)\n'),
    ('12_fstring_binvar_shell', 'import subprocess\ndef f(codex_bin, p):\n    subprocess.run(f"{codex_bin} exec --json {p}", shell=True)\n'),
    pytest.param('13_helper_wrapper_args', 'import subprocess\ndef _run(*a):\n    return subprocess.run(["codex", *a])\ndef f(p):\n    _run("exec", p)\n', marks=pytest.mark.xfail(strict=True, reason="the subcommand reaches the argv through a wrapper's *args: needs call-graph data flow")),
    ('14_alias_import', 'import subprocess as sp\ndef f(p):\n    sp.run(["codex", "exec", p])\n'),
    ('15_which_resolved', 'import shutil, subprocess\ndef f(p):\n    b = shutil.which("codex")\n    subprocess.run([b, "exec", p])\n'),
    ('16_single_quoted_exec', "import subprocess\ndef f(p):\n    subprocess.run(['codex', 'exec', p])\n"),
    ('17_exec_not_second', 'import subprocess\ndef f(p):\n    subprocess.run(["codex", "-C", "/w", "exec", p])\n'),
    ('18_env_prefix', 'import subprocess\ndef f(p):\n    subprocess.run(["env", "A=1", "codex", "exec", p])\n'),
    pytest.param('19_append_exec', 'import subprocess\ndef f(p):\n    cmd = ["codex"]\n    cmd.append("exec")\n    subprocess.run(cmd)\n', marks=pytest.mark.xfail(strict=True, reason='argv built by .append(): needs data flow from the list to its later mutation')),
    ('20_split_lists_concat', 'import subprocess\ndef f(p):\n    subprocess.run(["codex"] + ["exec", p])\n'),
    ('21_star_unpack', 'import subprocess\ndef f(p):\n    subprocess.run(["codex", *["exec", p]])\n'),
    ('22_join_list', 'import subprocess\ndef f(p):\n    subprocess.run(" ".join(["codex", "exec", p]), shell=True)\n'),
    pytest.param('23_str_format', 'import subprocess\ndef f(p):\n    subprocess.run("codex {} --json".format("exec"), shell=True)\n', marks=pytest.mark.xfail(strict=True, reason='the subcommand is a .format() argument: the literal text never reads `codex exec`')),
    pytest.param('24_percent_format', 'import subprocess\ndef f(p):\n    subprocess.run("codex %s --json" % "exec", shell=True)\n', marks=pytest.mark.xfail(strict=True, reason='the subcommand is a %-format argument: the literal text never reads `codex exec`')),
    ('25_implicit_concat_string', 'import subprocess\ndef f(p):\n    subprocess.run("codex " "exec --json", shell=True)\n'),
    ('26_path_constant_binary', 'import subprocess\ndef f(p):\n    subprocess.run(["/opt/homebrew/bin/codex", "exec", p])\n'),
]
#: Code that looks alike but starts no ``codex exec``: the census must stay quiet (an everything-matches detector passes all).
BENIGN = [
    "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('git', 'exec', 'x')\n",
    "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('ls', '-l', 'exec')\n",
    "def f(p):\n    return ['logging codex run'] + ['exec', p]\n",
    "def f(p):\n    return ['codex-note'] + ['exec', p]\n",
    "import subprocess\ndef f():\n    subprocess.run(['git', 'status'])\n",
    "def f(data, path):\n    return compile(data, str(path), 'exec')\n",
    "def f(p):\n    return ['ls', '-l', p]\n",
    "import subprocess\ndef f(p):\n    subprocess.run(['codex', '--version'])\n",
]


def _census_of(tmp_path: Path, source: str):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "planted.py").write_text(source, encoding="utf-8")
    argv, strings, _ = _census(tmp_path)
    return argv, strings


@pytest.mark.parametrize("name, source", PLANTED, ids=[(p.values if hasattr(p, "values") else p)[0] for p in PLANTED])
def test_the_census_catches_each_planted_variant(name, source, tmp_path):
    argv, strings = _census_of(tmp_path, source)
    assert argv or strings, f"variant {name} starts `codex exec` and the census did not see it"


@pytest.mark.parametrize("source", BENIGN)
def test_the_census_ignores_code_that_starts_no_codex_exec(source, tmp_path):
    argv, strings = _census_of(tmp_path, source)
    assert not argv and not strings, (argv, strings)


# ================================================================================================= 2. ARGV

_HEAD = ["/bin/codex", "exec", "--json", "--ignore-user-config", IGNORE_RULES_FLAG]
_TAIL = ["-m", "m", "-c", "model_provider=openai", "--color", "never", "--skip-git-repo-check", "-C", "/w", "p"]
_NO_SEARCH = ["-c", 'web_search="disabled"']  # BIND-9: the provider-side search tool is not offered to a delegated run
_WRITE = ["--sandbox", "workspace-write",
          "-c", "sandbox_workspace_write.network_access=false",
          "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
          "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true"] + _NO_SEARCH


@pytest.mark.parametrize("sandbox, middle", [
    (None, []),                      # the router chain, the MCP tool, the broker, the evaluator, calibrate: no sandbox flag
    ("read-only", ["--sandbox", "read-only"] + _NO_SEARCH),
    ("workspace-write", _WRITE),     # the delegated agent-route run (D-357)
])
def test_argv_of_run_codex_at_every_sandbox_mode(sandbox, middle):
    argv = codex_exec_args("/bin/codex", model="m", cwd="/w", prompt="p", sandbox=sandbox)
    assert argv == _HEAD + middle + _TAIL
    assert argv.index(IGNORE_RULES_FLAG) < argv.index("-m"), "the flag sits before the model, with the other safety flags"
    assert (_NO_SEARCH[1] in argv) is (sandbox is not None), "web_search is off in a delegated run and only there"


def test_argv_of_the_agentic_adapter():
    from llm_router.agentic.adapters import CodexAdapter

    base = ["/bin/codex", "exec", "--json", "--ignore-user-config", IGNORE_RULES_FLAG, "--color", "never",
            "--skip-git-repo-check"]
    assert CodexAdapter(tier=1)._codex_argv("/bin/codex", "p") == base + ["--sandbox", "workspace-write"] + _NO_SEARCH + ["p"]
    full = CodexAdapter(tier=1, model="gpt-5.5", cwd="/repo")._codex_argv("/bin/codex", "p")
    assert full == base + ["--sandbox", "workspace-write"] + _NO_SEARCH + [
        "-m", "gpt-5.5", "-c", "model_provider=openai", "-C", "/repo", "p"]
    read_only = CodexAdapter(tier=1, sandbox_mode="read-only")._codex_argv("/bin/codex", "p")
    assert read_only == base + ["--sandbox", "read-only"] + _NO_SEARCH + ["p"]
    assert CodexAdapter(tier=1, sandbox_mode="")._codex_argv("/bin/codex", "p") == base + ["p"]  # no sandbox: Codex's default


def test_argv_of_the_cls_sender():
    """The one exception, pinned as it is: the frozen CLS-DEV study script (D-345, sha-pinned) keeps its original argv, with
    no ``--ignore-rules``. It is not a routing path and its scripts are not to be run; changing it changes its pinned hash."""
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import cls_send_lib as S
    finally:
        sys.path.remove(str(REPO / "scripts"))
    head = ["/bin/codex", "exec", "-s", "read-only", "--skip-git-repo-check", "--ephemeral", "-C", "/cwd"]
    assert S.codex_argv("/cwd", "gpt-5.5", "/bin/codex") == head + ["--json", "-m", "gpt-5.5"]
    assert S.codex_argv("/cwd", "gpt-5.5", "/bin/codex", json_events=False) == head + ["-m", "gpt-5.5"]


def test_argv_of_the_backend_quality_bench(monkeypatch, tmp_path):
    import importlib.util

    monkeypatch.setattr(sys, "path", list(sys.path))  # the script prepends to sys.path on import: undone at teardown
    spec = importlib.util.spec_from_file_location("bench_backend_quality_bind8", REPO / "scripts" / "bench_backend_quality.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    seen = []
    monkeypatch.setattr(bench, "_cli", lambda args, sandbox, timeout, check=True: (seen.append(args), (None, "captured"))[1])
    assert bench.run_codex("p", tmp_path, "gpt-5.5", 5) == (None, "captured")
    assert seen == [["codex", "exec", "--json", IGNORE_RULES_FLAG, "-m", "gpt-5.5", "-c", "model_provider=openai",
                     "--color", "never", "--skip-git-repo-check", "--sandbox", "workspace-write", "-C", str(tmp_path), "p"]]


async def test_the_session_routing_recommendation_names_the_flag(monkeypatch):
    from llm_router.tools import routing
    from llm_router.types import Complexity, TaskType

    async def _classify(prompt):
        return types.SimpleNamespace(inferred_task_type=TaskType.CODE, complexity=Complexity.SIMPLE, confidence=0.9,
                                     reasoning="stub")

    monkeypatch.setattr(routing, "classify_for_routing", _classify)
    monkeypatch.setattr("llm_router.codex_agent.is_codex_available", lambda: True)
    out = await routing.llm_select_agent("write a haiku", profile="budget")
    assert re.search(r"```bash\ncodex exec --ignore-rules --model gpt-5\.5 ", out), out


# ============================================================================================ 3. CALLERS


@pytest.fixture
def stub(tmp_path, monkeypatch):
    """``make(**kw) -> (binary, argv_log)``: a stand-in ``codex`` that ``find_codex_binary`` returns, with an empty
    capability cache, so the first call asks the stand-in ``exec --help`` for real (the conftest stub is off:
    ``real_codex_probe``). ``<argv_log>.probes`` counts those questions; ``stub_calls`` counts launches."""
    def make(**kw):
        log = tmp_path / "argv.jsonl"
        binary = write_codex_stub(tmp_path / "bin", argv_log=log, **kw)
        monkeypatch.setenv("CODEX_PATH", str(binary))
        codex_agent._reset_codex_cache_for_tests()
        failopen.clear()
        return binary, log
    yield make
    codex_agent._reset_codex_cache_for_tests()
    failopen.clear()


def _probes(log: Path) -> int:
    p = Path(str(log) + ".probes")
    return len(p.read_text().splitlines()) if p.exists() else 0


def _refusals() -> int:
    return failopen.snapshot().by_code.get(FAILOPEN_CODE, 0)


async def _mcp_tool():
    from llm_router.tools.codex import llm_codex

    out = await llm_codex("say hi", model="gpt-5.5")
    return out.startswith("✅"), out


async def _session_broker():
    from llm_router.session_broker import codex_adapter

    res = await codex_adapter({"prompt": "say hi", "model": "codex/gpt-5.5"})
    return res["status"] == "ok", str(res)


async def _evaluator():
    from llm_router.model_evaluator import eval_codex_model

    score = await eval_codex_model("reasoning")
    return score is not None, str(score)


async def _calibration():
    from llm_router.resolver.calibrate import CloudCompleter

    completer = CloudCompleter(types.SimpleNamespace(route_kind="cli"))
    reply = await asyncio.to_thread(completer._cli, "codex/gpt-5.5", [{"role": "user", "content": "say hi"}], 20)
    return not reply.error, str(reply)


async def _run_codex_directly():
    res = await run_codex("say hi", model="gpt-5.5")
    return res.success, f"{res.reason_code}: {res.content}"


RUN_CODEX_CALLERS = [
    pytest.param(_mcp_tool, id="mcp-llm_codex"),
    pytest.param(_session_broker, id="session-broker"),
    pytest.param(_evaluator, id="model-evaluator"),
    pytest.param(_calibration, id="calibrate"),
    pytest.param(_run_codex_directly, id="run_codex"),
]


@pytest.mark.real_codex_probe
@pytest.mark.parametrize("caller", RUN_CODEX_CALLERS)
async def test_each_run_codex_caller_starts_codex_with_the_flag(stub, caller):
    _binary, log = stub()
    ok, text = await caller()
    assert ok, text
    calls = stub_calls(log)
    assert len(calls) == 1, f"the stand-in was launched {len(calls)} times"
    assert calls[0]["argv"][:4] == RUN_CODEX_HEAD, calls[0]["argv"]
    assert _probes(log) == 1 and _refusals() == 0


@pytest.mark.real_codex_probe
@pytest.mark.parametrize("caller", RUN_CODEX_CALLERS)
async def test_each_run_codex_caller_falls_back_on_a_cli_without_the_flag(stub, caller, caplog):
    _binary, log = stub(help_lists_ignore_rules=False)
    with caplog.at_level(logging.WARNING, logger="llm_router.codex_agent"):
        ok, text = await caller()
    assert not ok, f"the call reported success on a CLI that cannot ignore rules: {text}"
    assert stub_calls(log) == [], "Codex was started without --ignore-rules"
    assert _refusals() == 1, failopen.snapshot().by_code
    assert [r.getMessage() for r in caplog.records if "codex_unprotected_refused" in r.getMessage()], \
        "no WARNING line says why Codex was not used"


@pytest.mark.real_codex_probe
async def test_the_session_broker_and_run_codex_report_the_reason_code(stub):
    _binary, log = stub(help_lists_ignore_rules=False)
    res = await run_codex("say hi", model="gpt-5.5")
    assert (res.success, res.exit_code, res.reason_code) == (False, 1, "no_ignore_rules")
    assert IGNORE_RULES_FLAG in res.content and "BIND-8" in res.content
    from llm_router.session_broker import codex_adapter

    brokered = await codex_adapter({"prompt": "say hi", "model": "codex/gpt-5.5"})
    assert brokered["status"] == "error" and IGNORE_RULES_FLAG in brokered["error"]
    assert stub_calls(log) == []


@pytest.mark.real_codex_probe
async def test_the_router_chain_starts_codex_with_the_flag(stub, temp_db, mock_env, monkeypatch):
    from llm_router.router import route_and_call
    from llm_router.types import RoutingProfile, TaskType

    _binary, log = _chain_setup(stub, monkeypatch)
    resp = await route_and_call(TaskType.CODE, "implement a binary search function", profile=RoutingProfile.BALANCED)
    assert resp.provider == "codex" and resp.content == "STUB-CODEX-ANSWER"
    calls = stub_calls(log)
    assert len(calls) == 1 and calls[0]["argv"][:4] == RUN_CODEX_HEAD, calls


@pytest.mark.real_codex_probe
async def test_the_router_chain_skips_a_cli_without_the_flag(stub, temp_db, mock_env, monkeypatch):
    from llm_router.router import route_and_call
    from llm_router.types import LLMResponse, RoutingProfile, TaskType

    _binary, log = _chain_setup(stub, monkeypatch, help_lists_ignore_rules=False)
    fallback = LLMResponse(content="from the next provider", model="test/fallback", input_tokens=1, output_tokens=1,
                           cost_usd=0.0, latency_ms=1.0, provider="test")

    async def _next_provider(*a, **kw):
        return fallback

    monkeypatch.setattr("llm_router.providers.call_llm", _next_provider)
    resp = await route_and_call(TaskType.CODE, "implement a binary search function", profile=RoutingProfile.BALANCED)
    assert resp.provider != "codex" and resp.content == "from the next provider", (resp.provider, resp.content)
    assert stub_calls(log) == [], "the router started a CLI that cannot ignore rules"
    # the chain was built before the first refusal, so each Codex entry in it is refused once (two by default) ...
    refused = _refusals()
    assert refused >= 1 and _probes(log) == 1, (refused, _probes(log))
    # ... and the next request does not offer Codex at all
    again = await route_and_call(TaskType.CODE, "implement a binary search function", profile=RoutingProfile.BALANCED)
    assert again.content == "from the next provider" and _refusals() == refused, "the router kept offering a refused CLI"
    assert stub_calls(log) == [] and _probes(log) == 1


def _chain_setup(stub, monkeypatch, **stub_kw):
    """The conditions of tests/test_codex_routing.py's first case (subscription mode, Codex first in the chain), with a
    stand-in binary instead of a patched ``run_codex``."""
    binary, log = stub(**stub_kw)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
    monkeypatch.setattr("llm_router.router.is_codex_available", codex_agent.is_codex_available)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.1)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "")
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models", lambda: [])
    import llm_router.config as config_module

    config_module._config = None
    return binary, log


# --- the agent-route hook (a subprocess: it probes for itself, so its stand-in must answer ``exec --help``)

@pytest.fixture
def hook(tmp_path):
    project = init_repo(tmp_path / "project")
    log = tmp_path / "hook_argv.jsonl"

    class Hook:
        def run(self, *, help_lists_ignore_rules: bool = True):
            binary = write_codex_stub(tmp_path / "hookbin", argv_log=log, answer="HOOK-STUB-ANSWER",
                                      help_lists_ignore_rules=help_lists_ignore_rules)
            cwd = tmp_path / "hookproc"
            cwd.mkdir(exist_ok=True)
            return run_hook(AGENT_ROUTE, agent_payload(SUITABLE_PROMPT, cwd=str(project)),
                            hook_env(tmp_path, CODEX_PATH=str(binary)), cwd=cwd)

        argv_log = log
        home = tmp_path

    return Hook()


def test_the_agent_route_hook_delegates_with_the_flag(hook):
    decision = decision_of(hook.run())
    assert "HOOK-STUB-ANSWER" in delivered_text(decision)
    calls = stub_calls(hook.argv_log)
    assert len(calls) == 1 and calls[0]["argv"][:4] == RUN_CODEX_HEAD, calls
    assert "workspace-write" in calls[0]["argv"]
    assert _NO_SEARCH[1] in calls[0]["argv"], "the delegated run still offers the hosted web_search tool"


def test_the_agent_route_hook_leaves_the_task_to_claude_on_a_cli_without_the_flag(hook):
    proc = hook.run(help_lists_ignore_rules=False)
    decision = decision_of(proc)
    assert "decision" not in decision and decision["hookSpecificOutput"]["permissionDecision"] == "allow", decision
    assert stub_calls(hook.argv_log) == [], "the hook started a CLI that cannot ignore rules"
    failed = [r for r in ledger_rows(hook.home) if r.get("outcome") == "codex_failed"]
    assert len(failed) == 1 and failed[0]["reason_code"] == "no_ignore_rules", failed
    rows = (hook.home / ".llm-router" / "fail_open.jsonl").read_text()
    assert FAILOPEN_CODE in rows


# --- the agentic adapter (its own argv; it asks the same question before it starts the CLI)

def _milestone():
    from llm_router.agentic.ledger import AcceptanceResult, Milestone

    return Milestone("M1", "do it", lambda _a: AcceptanceResult(True))


@pytest.mark.real_codex_probe
def test_the_agentic_adapter_starts_codex_with_the_flag(stub, tmp_path):
    from llm_router.agentic.adapters import CodexAdapter

    binary, log = stub()
    res = CodexAdapter(tier=1, binary=str(binary), capture_diff=False).run(_milestone(), [], 5.0)
    assert res.artifacts["returncode"] == 0 and "STUB-CODEX-ANSWER" in res.artifacts["output"]
    calls = stub_calls(log)
    assert len(calls) == 1 and calls[0]["argv"][:4] == ["exec", "--json", "--ignore-user-config", IGNORE_RULES_FLAG], calls
    assert _NO_SEARCH[1] in calls[0]["argv"], "the adapter's sandboxed run still offers the hosted web_search tool"


@pytest.mark.real_codex_probe
def test_the_agentic_adapter_does_not_start_a_cli_without_the_flag(stub):
    from llm_router.agentic.adapters import CodexAdapter, ProcResult

    binary, log = stub(help_lists_ignore_rules=False)
    ran = []

    def _runner(argv, input_text):
        ran.append(argv)
        return ProcResult(0, "ran", "")

    res = CodexAdapter(tier=1, binary=str(binary), runner=_runner, capture_diff=True, cwd="/nonexistent").run(
        _milestone(), [], 5.0)
    assert ran == [], f"the adapter ran {ran}: not even the diff capture may run for a call that did not happen"
    assert res.artifacts["returncode"] == 127 and IGNORE_RULES_FLAG in res.artifacts["stderr"]
    assert stub_calls(log) == [] and _refusals() == 1


# ================================================================================================== 4. CHECK


def _help_script(tmp_path: Path, text: str, *, rc: int = 0, name: str = "codex") -> Path:
    """An executable that answers ``exec --help`` with ``text`` and exits ``rc``."""
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\ncat <<'EOF'\n{text}\nEOF\nexit {rc}\n")
    path.chmod(0o755)
    return path


@pytest.mark.real_codex_probe
@pytest.mark.parametrize("help_text, rc, expected", [
    ("      --ignore-rules\n          Do not load user or project execpolicy .rules files", 0, True),
    ("  --ignore-user-config   Do not load config.toml\n  --ignore-rules   skip rules", 0, True),
    ("      --ignore-user-config\n          Do not load config.toml", 0, False),   # an older CLI: the flag is not listed
    ("      --no-ignore-rules\n          something else", 0, False),                # a different flag is not this flag
    ("      --ignore-rules-file <FILE>\n          something else", 0, False),
    ("", 0, None),                                                                  # nothing to read: cannot say
    ("error: something broke\n      --ignore-rules", 2, None),                      # a failed help is not an answer
])
def test_the_probe_reads_the_flag_from_the_help_text(tmp_path, help_text, rc, expected):
    assert codex_agent._probe_ignore_rules(str(_help_script(tmp_path, help_text, rc=rc))) is expected


@pytest.mark.real_codex_probe
def test_a_missing_or_hung_binary_cannot_answer_and_counts_as_no(tmp_path, monkeypatch):
    assert codex_agent._probe_ignore_rules(str(tmp_path / "does-not-exist")) is None
    hung = tmp_path / "hung"
    hung.write_text("#!/bin/sh\nexec sleep 30\n")
    hung.chmod(0o755)
    monkeypatch.setattr(codex_agent, "_RULES_PROBE_TIMEOUT_S", 0.3)
    assert codex_agent._probe_ignore_rules(str(hung)) is None
    codex_agent._reset_codex_cache_for_tests()
    assert codex_agent.codex_supports_ignore_rules(str(hung)) is False  # fail closed


@pytest.mark.real_codex_probe
def test_the_answer_is_asked_once_and_an_unanswered_probe_is_not_repeated_for_a_minute(monkeypatch):
    asked = []
    answers = iter([None, True, False])

    def _probe(binary):
        asked.append(binary)
        return next(answers)

    monkeypatch.setattr(codex_agent, "_probe_ignore_rules", _probe)
    codex_agent._reset_codex_cache_for_tests()
    b = "/x/codex"
    assert codex_agent.codex_supports_ignore_rules(b) is False and asked == [b]      # could not answer: closed
    assert codex_agent.codex_supports_ignore_rules(b) is False and asked == [b]      # ... and not asked again now
    codex_agent._RULES_UNKNOWN_UNTIL[b] = 0.0                                        # a minute later
    assert codex_agent.codex_supports_ignore_rules(b) is True and asked == [b, b]
    assert codex_agent.codex_supports_ignore_rules(b) is True and asked == [b, b]    # a real answer is kept
    other = "/y/codex"
    assert codex_agent.codex_supports_ignore_rules(other) is False and asked == [b, b, other]  # per binary
    assert codex_agent.codex_supports_ignore_rules(other) is False and asked == [b, b, other]
    codex_agent._reset_codex_cache_for_tests()


@pytest.mark.real_codex_probe
async def test_the_probe_runs_off_the_event_loop_once_per_binary(stub, monkeypatch):
    _binary, log = stub()
    threads = []
    real = codex_agent._probe_ignore_rules

    def _spy(binary):
        import threading

        threads.append(threading.current_thread() is threading.main_thread())
        return real(binary)

    monkeypatch.setattr(codex_agent, "_probe_ignore_rules", _spy)
    for _ in range(3):
        assert (await run_codex("say hi", model="gpt-5.5")).success
    assert threads == [False], "the probe ran on the event loop's thread, or more than once"
    assert _probes(log) == 1 and len(stub_calls(log)) == 3


@pytest.mark.real_codex_probe
async def test_availability_and_the_mcp_message_follow_the_check(stub, monkeypatch):
    binary, log = stub(help_lists_ignore_rules=False)
    from llm_router.tools.codex import llm_codex

    assert codex_agent.is_codex_available() is True and codex_agent.codex_unsupported_binary() is None  # not asked yet
    first = await llm_codex("say hi")
    assert first.startswith("❌") and "no_ignore_rules" not in first and IGNORE_RULES_FLAG in first
    assert codex_agent.is_codex_available() is False, "the router would keep offering a CLI that is never started"
    assert codex_agent.codex_unsupported_binary() == str(binary)
    second = await llm_codex("say hi")
    assert "not found" not in second.lower() and str(binary) in second and IGNORE_RULES_FLAG in second, second
    assert stub_calls(log) == [] and _probes(log) == 1


@pytest.mark.real_codex_probe
async def test_the_refusal_is_a_warning_and_a_fail_open_row_with_the_binary_named(stub, caplog):
    binary, _log = stub(help_lists_ignore_rules=False)
    with caplog.at_level(logging.WARNING, logger="llm_router.codex_agent"):
        res = await run_codex("say hi", model="gpt-5.5")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "codex_unprotected_refused" in r.getMessage()]
    assert len(warnings) == 1 and str(binary) in warnings[0].getMessage() and IGNORE_RULES_FLAG in warnings[0].getMessage()
    rows = [json.loads(line) for line in failopen.store_path().read_text().splitlines() if line.strip()]
    assert [(r["c"], r.get("d")) for r in rows] == [(FAILOPEN_CODE, str(binary))]
    assert res.content in warnings[0].getMessage()


def test_the_flag_name_is_the_one_codex_documents():
    assert IGNORE_RULES_FLAG == "--ignore-rules"
    assert codex_agent._IGNORE_RULES_RX.search("   --ignore-rules   Do not load user or project execpolicy")
    assert not codex_agent._IGNORE_RULES_RX.search("--no-ignore-rules")
