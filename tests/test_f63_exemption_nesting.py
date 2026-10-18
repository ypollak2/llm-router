"""F63 - the Bash exemptions classify EVERY executed segment of a command.

F60 review: these still passed while a routing hold was active because only the
first word / command-position regex was inspected:
  * command substitution  `llm-router status $(llm-router query x)`, backticks
  * newline-separated     `llm-router status\\nllm-router query x`
  * transparent prefixes  xargs / env / nohup / time / nice / timeout / sudo
  * `python -m openai`, `node -e 'require("openai")'`, os.system("llm-router run")
A command is exempt only if EVERY segment is exempt.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("enforce_route_f63", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BYPASSES = [
    # command substitution / backticks / process substitution / heredoc body
    "llm-router status $(llm-router query x)",
    "llm-router status `llm-router query x`",
    'echo "$(llm-router query x)"',
    "ls $(echo $(llm-router query x))",
    "cat <(llm-router query x)",
    "cat <<EOF\n$(llm-router query x)\nEOF",
    # newline / && / || / ; / pipes
    "llm-router status\nllm-router query x",
    "llm-router status\n\nls\nllm-router query x",
    "llm-router status && llm-router query x",
    "llm-router status || llm-router query x",
    "ls | llm-router query x",
    "if true; then llm-router query x; fi",
    "(llm-router query x)",
    "for f in a; do llm-router query $f; done",
    # transparent prefixes
    "xargs llm-router query",
    "echo x | xargs -I {} llm-router query {}",
    "xargs -n 1 -P 4 llm-router query",
    "env X=1 llm-router query x",
    "env -u FOO llm-router query x",
    "X=1 Y=2 llm-router query x",
    "nohup llm-router query x",
    "time llm-router query x",
    "nice -n 5 llm-router query x",
    "timeout 30 llm-router query x",
    "timeout -s KILL 30 llm-router query x",
    "sudo llm-router query x",
    "sudo -u bob llm-router query x",
    "nohup nice -n 5 timeout 9 env A=1 llm-router query x",
    "git status\nenv X=1 xargs llm-router run m",
    # other shell-driven LLM shapes
    "python3 -m openai api chat.completions.create",
    "python -m anthropic",
    "node -e 'const o=require(\"openai\"); new o()'",
    "node -e \"import('openai')\"",
    "python3 -c 'import os; os.system(\"llm-router run --model m t\")'",
    "python3 -c 'import subprocess; subprocess.run([\"x\"]); subprocess.run(\"llm-router run t\", shell=True)'",
]
EXEMPT = [
    "git log --grep=ollama", "grep -r anthropic .", "pytest -k openai",
    "git status && git diff 2>&1 | head", "ls -la\npwd", "echo $(date) | wc -l",
    "for f in a b; do ls $f; done", "(cd /tmp; ls)", "cat <<'EOF'\nllm-router query $(x)\nEOF",
    "xargs -n 1 echo", "env X=1 git status", "nohup make test", "time pytest -q",
    "sudo -u bob ls /root", "git commit -m 'a; b'",
    "git log --format='%h %s' | head # llm-router query x",
    "python3 -m pytest -q", "node -e 'console.log(1)'",
]
F1 = [
    "llm-router set-enforce off", "llm-router set-enforce soft", "llm-router set-enforce smart",
    "llm-router doctor", "llm-router doctor --fix-routing", "llm-router status", "llm-router install",
    "llm_router set-enforce off", ".venv/bin/llm-router status",
    "LLM_ROUTER_HOME=/tmp llm-router status", "llm-router doctor && llm-router status",
    "llm-router status\nllm-router doctor", "cd /tmp && llm-router set-enforce off",
    "llm-router status | head", "env X=1 llm-router set-enforce off",
    "export LLM_ROUTER_ENFORCE=soft", "export LLM_ROUTER_ENFORCE=off",
]


@pytest.mark.parametrize("cmd", BYPASSES)
def test_bypass_is_not_exempt(hook, cmd):
    assert hook._is_local_only_bash(cmd) is False, cmd
    assert hook._is_router_self_command(cmd) is False, cmd
    assert hook._bash_exempt_from_hold("coordination", cmd, False) is False, cmd
    assert hook._smart_ops_bash_exempt("smart", "coordination", cmd) is False \
        or hook._is_shell_llm_call(cmd) is False  # smart-ops is deny-list; see e2e


@pytest.mark.parametrize("cmd", EXEMPT)
def test_false_positive_list_stays_exempt(hook, cmd):
    assert hook._is_local_only_bash(cmd) is True, cmd


@pytest.mark.parametrize("cmd", F1)
def test_f1_commands_are_self_commands(hook, cmd):
    assert hook._is_router_self_command(cmd) is True, cmd
    assert hook._is_local_only_bash(cmd) is True, cmd


def test_non_empty_sets():
    assert len(BYPASSES) >= 30 and len(EXEMPT) >= 15 and len(F1) >= 12
    assert len(E2E_BYPASS) >= 12 and len(E2E_F1) >= 7


def test_segments_split_and_descend(hook):
    segs = hook._bash_segments("a $(b `c`) ; d\ne && f | g 2>&1")
    assert segs == ["c", "b SUBST", "a  SUBST", "d", "e", "f", "g 2>&1"] or \
        {"a  SUBST", "b  SUBST", "c", "d", "e", "f", "g 2>&1"} <= set(segs), segs
    assert hook._bash_segments("echo 'a;b' \"c|d\"") == ["echo 'a;b' \"c|d\""]


def test_mutant_without_descent_would_pass_bypasses(hook, monkeypatch):
    """Mutant: ignore substitutions/newlines/prefixes -> bypass tests must fail."""
    monkeypatch.setattr(hook, "_bash_segments", lambda c: [c])
    monkeypatch.setattr(hook, "_effective_command", lambda s: s)
    escaped = [c for c in BYPASSES if hook._is_local_only_bash(c)]
    assert len(escaped) >= 10, escaped


PATHOLOGICAL = [
    "llm-router status " + "$(" * 200 + "x" + ")" * 200,
    "ls; " * 20000,
    "ls $(" * 5000,
    "a" * 500_000,
    "`" * 20000,
    "ls " + "\\\n" * 20000,
    "cat <<E\n" * 5000,
    "'" * 50000,
    "$((" * 5000,
]


@pytest.mark.timing  # asserts a wall-clock bound: serial timing lane (TIMING-1)
@pytest.mark.parametrize("cmd", PATHOLOGICAL)
def test_pathological_input_is_bounded_and_not_exempt(hook, cmd):
    t0 = time.perf_counter()
    assert hook._is_local_only_bash(cmd) is False
    assert hook._is_router_self_command(cmd) is False
    assert time.perf_counter() - t0 < 1.0


# ── End to end through the hook process (pending hold, smart and hard) ────────

def _run(cmd, home, enforce, task_type):
    d = home / ".llm-router"
    d.mkdir(parents=True, exist_ok=True)
    sid = "f63"
    (d / f"pending_route_{sid}.json").write_text(json.dumps({
        "expected_tool": "llm", "task_type": task_type, "complexity": "moderate",
        "method": "heuristic", "issued_at": time.time(), "turn_id": 1,
        "session_id": sid, "original_prompt": "do the thing"}))
    e = {k: v for k, v in os.environ.items()
         if k not in ("LLM_ROUTER_ENFORCE", "LLM_ROUTER_SLIM", "LLM_ROUTER_DELEGATE")}
    e.update(HOME=str(home), LLM_ROUTER_HOME=str(d), LLM_ROUTER_ENFORCE=enforce)
    r = subprocess.run([sys.executable, str(HOOK)], capture_output=True, text=True, env=e,
                       input=json.dumps({"session_id": sid, "tool_name": "Bash",
                                         "tool_input": {"command": cmd}}), timeout=60)
    return bool(r.stdout.strip()) and json.loads(r.stdout).get("decision") == "block"


E2E_BYPASS = [
    "llm-router status $(llm-router query x)", "llm-router status `llm-router query x`",
    "llm-router status\nllm-router query x", "xargs llm-router query",
    "env X=1 llm-router query x", "nohup llm-router query x", "time llm-router query x",
    "nice -n 5 llm-router query x", "timeout 30 llm-router query x", "sudo llm-router query x",
    "python3 -m openai", "node -e 'require(\"openai\")'",
    "python3 -c 'import os; os.system(\"llm-router run m t\")'",
]


@pytest.mark.parametrize("enforce", ["smart", "hard"])
@pytest.mark.parametrize("task", ["coordination", "research"])
@pytest.mark.parametrize("cmd", E2E_BYPASS)
def test_e2e_bypass_is_held(tmp_path, enforce, task, cmd):
    assert _run(cmd, tmp_path, enforce, task), (enforce, task, cmd)


E2E_F1 = ["llm-router set-enforce off", "llm-router doctor", "llm-router doctor --fix-routing",
          "llm-router status", "llm-router install",
          "export LLM_ROUTER_ENFORCE=soft", "export LLM_ROUTER_ENFORCE=off"]


@pytest.mark.parametrize("enforce", ["smart", "hard", "strict"])
@pytest.mark.parametrize("task", ["coordination", "research"])
@pytest.mark.parametrize("cmd", E2E_F1)
def test_e2e_f1_commands_never_held(tmp_path, enforce, task, cmd):
    assert not _run(cmd, tmp_path, enforce, task), (enforce, task, cmd)


@pytest.mark.parametrize("enforce", ["smart", "hard"])
@pytest.mark.parametrize("cmd", ["git log --grep=ollama", "grep -r anthropic .", "pytest -k openai"])
def test_e2e_false_positives_exempt_for_non_qa(tmp_path, enforce, cmd):
    assert not _run(cmd, tmp_path, enforce, "coordination"), (enforce, cmd)


def test_export_escape_is_not_a_blanket_export_exemption(hook):
    assert hook._is_local_only_bash("export OPENAI_API_KEY=x") is False
    assert hook._is_local_only_bash("export LLM_ROUTER_ENFORCE=off; llm-router query x") is False


# ── Round 1 repair: arithmetic substitutions, case arms, export, bare env ────

R1_BYPASSES = [
    "echo $(( $(llm-router query x) + 1 ))",
    "echo $(( 1 + $(curl api.openai.com) ))",
    "echo $(( 1 + `llm-router query x` ))",
    "echo $(( $(( $(curl api.openai.com) )) + 1 ))",
    "case x in x) curl api.openai.com;; esac",
    "case x in x) llm-router query hi;; esac",
    "case x in a|b) ls;; c) llm-router query hi;; esac",
    "case $x in\n a) ls ;;\n b) curl api.openai.com ;;\nesac",
    "case x in x) ls;& y) llm-router query hi;;& z) ls;; esac",
    "for i in a; do case $i in a) curl api.openai.com;; esac; done",
    "export LLM_ROUTER_ENFORCE=off; curl api.openai.com",
    "export LLM_ROUTER_ENFORCE=off && llm-router query x",
]
R1_EXEMPT = [
    "echo $((1+2))", "echo $(( $(date +%s) + 1 ))", "echo $(( 1 + `ls | wc -l` ))",
    "case x in a|b) ls;; c) pwd;; *) echo hi;; esac",
    "case $x in\n a) ls ;;\n b) git status ;;\nesac",
    "for i in a; do case $i in a) ls;; esac; done",
    "cat <<EOF\n$(date)\nEOF", "env | sort", "env -i", "env A=1 | sort",
]


@pytest.mark.parametrize("cmd", R1_BYPASSES)
def test_r1_bypass_is_not_exempt(hook, cmd):
    assert hook._is_local_only_bash(cmd) is False, cmd
    assert hook._is_router_self_command(cmd) is False, cmd
    assert hook._bash_exempt_from_hold("coordination", cmd, False) is False, cmd


@pytest.mark.parametrize("cmd", R1_EXEMPT)
def test_r1_false_positives_stay_exempt(hook, cmd):
    assert hook._is_local_only_bash(cmd) is True, cmd


def test_r1_arith_substitutions_become_segments(hook):
    assert "curl api.openai.com" in hook._bash_segments("echo $(( 1 + $(curl api.openai.com) ))")
    assert hook._bash_segments("echo $((a)|(b))") != hook._bash_segments("echo $((a|b))")
    assert hook._bash_segments("echo $(( 1 + ") is None  # unterminated -> fail closed


def test_r1_case_arms_are_their_own_segments(hook):
    assert hook._bash_segments("case x in a|b) ls;; c) pwd;; esac") == ["ls", "pwd"]
    assert hook._bash_segments("case x in x) curl a;; esac") == ["curl a"]


def test_r1_export_exact_var_and_lowering_values_only(hook):
    for ok in ["export LLM_ROUTER_ENFORCE=off", "export LLM_ROUTER_ENFORCE=soft",
               'export LLM_ROUTER_ENFORCE="off"', "export LLM_ROUTER_ENFORCE='soft'",
               "export LLM_ROUTER_ENFORCE=advise", "export LLM_ROUTER_ENFORCE=shadow"]:
        assert hook._is_router_self_command(ok) is True, ok
    for bad in ["export LLM_ROUTER_ENFORCE=hard", "export LLM_ROUTER_ENFORCE=strict",
                "export LLM_ROUTER_ENFORCE=", "export LLM_ROUTER_ENFORCE=banana",
                "export LLM_ROUTER_ENFORCEX=off", "export OTHER=off"]:
        assert hook._is_router_self_command(bad) is False, bad
        assert hook._is_local_only_bash(bad) is False, bad


def test_r1_export_plus_other_command_is_not_never_held(hook):
    assert hook._is_router_self_command("export LLM_ROUTER_ENFORCE=off; rm -rf x") is False
    assert hook._is_router_self_command("export LLM_ROUTER_ENFORCE=off; ls") is False
    # but the combination with a real admin command still is
    assert hook._is_router_self_command("export LLM_ROUTER_ENFORCE=off; llm-router status") is True


def test_r1_tokenizer_exception_fails_closed_and_does_not_crash(hook, monkeypatch, tmp_path,
                                                                 capsys):
    def boom(*a, **k):
        raise RuntimeError("tokenizer bug")

    monkeypatch.setattr(hook, "_seg_scan", boom)
    assert hook._bash_segments("ls -la") is None
    assert hook._is_local_only_bash("ls -la") is False
    assert hook._is_router_self_command("llm-router status") is False
    # the whole hook still returns normally (no traceback) for a Bash call
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(
        {"session_id": "f63r1", "tool_name": "Bash",
         "tool_input": {"command": "echo $(( 1 + $(curl api.openai.com) ))"}})))
    try:
        hook.main()
    except SystemExit as e:
        assert e.code in (0, None)


E2E_R1 = [c for c in R1_BYPASSES if "\n" not in c][:10] + [
    "export LLM_ROUTER_ENFORCE=hard; curl api.openai.com"]


@pytest.mark.parametrize("enforce", ["smart", "hard"])
@pytest.mark.parametrize("task", ["coordination", "research"])
@pytest.mark.parametrize("cmd", E2E_R1 + [c for c in R1_BYPASSES if "\n" in c])
def test_r1_e2e_bypass_is_held(tmp_path, enforce, task, cmd):
    assert _run(cmd, tmp_path, enforce, task), (enforce, task, cmd)


@pytest.mark.parametrize("enforce", ["smart", "hard", "strict"])
@pytest.mark.parametrize("task", ["coordination", "research"])
@pytest.mark.parametrize("cmd", ["export LLM_ROUTER_ENFORCE=advise", 'export LLM_ROUTER_ENFORCE="off"'])
def test_r1_e2e_lowering_exports_never_held(tmp_path, enforce, task, cmd):
    assert not _run(cmd, tmp_path, enforce, task), (enforce, task, cmd)


@pytest.mark.parametrize("enforce", ["smart", "strict"])
@pytest.mark.parametrize("task", ["research", "query"])
def test_r1_e2e_export_then_rm_not_never_held(tmp_path, enforce, task):
    assert _run("export LLM_ROUTER_ENFORCE=off; rm -rf x", tmp_path, enforce, task), (enforce, task)


# F66 (rv-586 r2): a `time` prefix before `case` must not hide the arm bodies.
@pytest.mark.parametrize("cmd", [
    "time case x in x) llm-router query x;; esac",
    "time -p case x in x) llm-router query x;; esac",
    "time case x in x) curl api.openai.com;; esac",
])
@pytest.mark.parametrize("mode", ["smart", "hard"])
def test_f66_time_prefixed_case_arm_is_held(tmp_path, cmd, mode):
    assert _run(cmd, tmp_path, mode, "coordination"), (mode, cmd)
