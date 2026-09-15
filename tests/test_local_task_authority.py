"""`llm_local_task` must not hand out a shell, or authority nobody asked for.

B1 and B2 of docs/ACTIONS_REMEDIATION_RUN.md, from the external gap review.

B1: `_run_check` ran `subprocess.run(check, shell=True, ...)`, so ONE string on an
MCP tool call was a general command-injection primitive — and `acceptance_check`
is exactly the parameter a caller composes. `run_command` inside the agent loop
had the right pattern all along (`shlex.split` + `shell=False`), two files away.

B2: `apply_writes` defaulted to True, and True ALSO set
`LLM_ROUTER_AGENT_COMMANDS=all`. `agent_writes.guard_command` returns True
immediately under `all`, skipping the entire inspection allowlist; what remained
was a regex catching `rm -rf /`, `mkfs`, `dd`, `curl|sh` — not `cp`, `mv`, `tee`
or `git`.
"""
from __future__ import annotations

import inspect
from pathlib import Path


from llm_router.tools import local_task as lt


class TestNoShell:

    def test_no_call_in_the_module_passes_shell_true(self):
        # Parsed, not grepped: the docstrings in this module quote the old
        # `shell=True` while explaining why it is gone, and a grep would match
        # the explanation and call it a regression.
        import ast

        tree = ast.parse(Path(lt.__file__).read_text())
        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "shell" and getattr(kw.value, "value", False) is True
        ]
        assert not offenders, (
            f"shell=True at line(s) {offenders}: an acceptance check reaching a "
            "shell makes one tool argument a command-injection primitive"
        )

    def test_a_second_command_after_a_semicolon_does_not_run(self, tmp_path):
        marker = tmp_path / "pwned.txt"
        ok, out = lt._run_check(
            f"python3 -c pass ; touch {marker}", tmp_path, timeout=20)
        assert not marker.exists(), (
            f"the text after ';' executed — {marker} was created"
        )

    def test_command_substitution_does_not_run(self, tmp_path):
        marker = tmp_path / "subst.txt"
        lt._run_check(f"python3 -c pass $(touch {marker})", tmp_path, timeout=20)
        assert not marker.exists(), "$(...) was evaluated by a shell"

    def test_a_redirection_does_not_create_a_file(self, tmp_path):
        marker = tmp_path / "redirected.txt"
        lt._run_check(f"python3 -c pass > {marker}", tmp_path, timeout=20)
        assert not marker.exists(), "> was interpreted by a shell"

    def test_a_genuine_check_still_passes_and_fails_correctly(self, tmp_path):
        ok, _ = lt._run_check(["python3", "-c", "raise SystemExit(0)"], tmp_path, 20)
        assert ok
        bad, _ = lt._run_check(["python3", "-c", "raise SystemExit(1)"], tmp_path, 20)
        assert not bad

    def test_a_string_check_is_still_accepted(self, tmp_path):
        ok, _ = lt._run_check("python3 -c pass", tmp_path, timeout=20)
        assert ok, "existing string callers must keep working"

    def test_an_empty_check_is_a_failure_not_a_pass(self, tmp_path):
        ok, why = lt._run_check("", tmp_path, timeout=5)
        assert not ok and why


class TestDefaultAuthority:

    def test_writes_are_not_applied_by_default(self):
        sig = inspect.signature(lt.llm_local_task)
        assert sig.parameters["apply_writes"].default is False, (
            "the default invocation of a consolidated front-door tool must not "
            "modify the caller's files"
        )

    def test_applying_writes_does_not_also_unlock_every_command(self):
        src = Path(lt.__file__).read_text()
        block = src.split("if apply_writes:", 1)[1].split("try:", 1)[0]
        assert 'LLM_ROUTER_AGENT_WRITES"] = "apply"' in block
        assert '"all"' not in block, (
            "asking for an edit on disk still buys the whole command allowlist"
        )

    def test_the_command_allowlist_is_what_apply_used_to_remove(self, monkeypatch):
        from llm_router.hooks import agent_writes
        monkeypatch.delenv("LLM_ROUTER_AGENT_COMMANDS", raising=False)
        allowed, _ = agent_writes.guard_command("git push --force")
        assert not allowed, "the allowlist is not actually guarding anything"
        monkeypatch.setenv("LLM_ROUTER_AGENT_COMMANDS", "all")
        allowed_all, _ = agent_writes.guard_command("git push --force")
        assert allowed_all, "under 'all' the guard is skipped — this is what B2 stops being the default"
