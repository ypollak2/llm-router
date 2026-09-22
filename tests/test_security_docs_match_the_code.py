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
    assert "shlex.split" in AGENT_LOOP.read_text()


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
