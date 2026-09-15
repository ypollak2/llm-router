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


def test_the_allowlist_blocks_what_the_old_table_called_unblocked():
    from llm_router.hooks import agent_writes
    import os

    os.environ.pop("LLM_ROUTER_AGENT_COMMANDS", None)
    for cmd in ("git push --force", "npm install", "pip install requests",
                "rm -rf ./src", "git reset --hard"):
        allowed, _ = agent_writes.guard_command(cmd)
        assert not allowed, f"{cmd!r} is not actually blocked; the docs now claim it is"


def test_security_md_says_its_table_measures_only_one_layer():
    text = DOCS["SECURITY.md"].read_text()
    assert "UNDERSTATES" in text or "understates" in text, (
        "the twelve-command table measures the regex only; without saying so it "
        "reads as the complete picture and undersells the real protection"
    )
