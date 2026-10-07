"""Mutation check for the tool layer's containment: remove each check in turn and
a test must go red.

Each case copies the package, deletes ONE containment check in the copy, and runs the
tests that are supposed to guard it in a subprocess pointed at the copy. A control run
of the unmutated copy must be green first, otherwise a "red" would prove nothing (and
a mutation whose text is not found fails loudly: no silent no-op mutations).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from llm_router.toolkit import sandbox

ROOT = Path(__file__).resolve().parents[1]
SRC_PKG = ROOT / "src" / "llm_router"
pytestmark = pytest.mark.timeout(240)

SB = sandbox.prove_sandbox().proven
SAFETY = "tests/test_toolkit_safety.py"
LOOP = "tests/test_toolkit_loop.py"
INTEG = "tests/test_toolkit_verifier_integrity.py"
PARITY = "tests/test_agent_loop_hook_parity.py"

P, S, T, V, L = ("toolkit/policy.py", "toolkit/sandbox.py", "toolkit/tools.py", "toolkit/verify.py",
                 "toolkit/loop.py")

# (id, file, old text, new text, test file, -k expression, needs the OS sandbox)
MUTATIONS = [
    ("path-containment", P, "if real != root and root not in real.parents:", "if False:",
     SAFETY, "traversal or symlink", False),
    ("secret-deny-read", P, 'if rel != "." and is_secret_relpath(rel):\n            return _deny("secret", "that path is not available")\n        if tool == "search":',
     'if False:\n            return _deny("secret", "that path is not available")\n        if tool == "search":',
     SAFETY, "secret_file_read", False),
    ("secret-deny-write", P, '        if is_secret_relpath(rel):\n            return _deny("secret", "that path is not available")\n        if real in self.protected',
     '        if False:\n            return _deny("secret", "that path is not available")\n        if real in self.protected',
     SAFETY, "secret_write_and_edit", False),
    ("frozen-tests", P, "if real in self.protected or any(p in real.parents for p in self.protected):", "if False:",
     SAFETY, "frozen_verifier", True),
    ("write-no-clobber", P, 'if real.exists() and args.get("overwrite") is not True:', "if False:",
     SAFETY, "refuses_to_overwrite", False),
    ("bytes-budget", P, "if self.bytes_written + size > self.max_bytes_written:", "if False:",
     SAFETY, "bytes_written_budget", False),
    ("bash-off-gate", P, "if not self.bash_allowed:", "if False:", SAFETY, "not_proven", False),
    ("program-allowlist", P, "if prog not in ALLOWED_PROGRAMS:", "if False:", SAFETY, "test_bash_is_denied", False),
    ("shell-tricks", P, "for bad in _FORBIDDEN_SUBSTRINGS:", "for bad in ():", SAFETY, "test_bash_is_denied", False),
    ("arg-path-containment", P, "pathlike = value.startswith((\"/\", \"~\", \".\")) or \"/\" in value or \"..\" in value",
     "pathlike = False", SAFETY, "test_bash_is_denied", False),
    ("inline-python", P, "if a == \"-\" or (not a.startswith(\"--\") and \"c\" in a[1:] and not a.startswith(\"-W\")):",
     "if False:", SAFETY, "test_bash_is_denied", False),
    ("kill-switch-policy", P, "        why = sandbox.kill_switch_reason()\n        if why:\n            return _deny(\"kill\"",
     "        why = None\n        if why:\n            return _deny(\"kill\"", SAFETY, "env_kill_switch", False),
    ("kill-switch-running-command", T, "if launcher is not None and sandbox.kill_switch_reason():\n                    status = \"kill\"",
     "if False:\n                    status = \"kill\"", SAFETY, "kill_switch_file", True),
    ("sandbox-network-deny", S, '"(deny network*)",', '"",', SAFETY, "proven_on_this_mac or detects_a_profile", True),
    ("sandbox-write-deny", S, '"(deny file-write*)",', '"",', SAFETY, "proven_on_this_mac or detects_a_profile", True),
    ("sandbox-proof-skips-control", S, 'checks["network_denied"] = rc != 0 and accepted() == 0',
     'checks["network_denied"] = True', SAFETY, "detects_a_profile", True),
    ("sandbox-launchservices-deny", S, 'lines.append("(deny mach-lookup " + " ".join(', 'lines.append(";(deny mach-lookup " + " ".join(',
     SAFETY, "launchservices", True),
    ("sandbox-secret-read-deny", S, "    for rx in _SECRET_READ_REGEXES:\n        lines.append(", "    for rx in ():\n        lines.append(",
     SAFETY, "secret_named_files_anywhere or detects_a_profile", True),
    ("memory-watchdog", T, "if sandbox.tree_rss_kb(final.pid) > launcher.max_rss_kb:", "if False:",
     SAFETY, "memory_hog", True),
    ("child-group-kill", S, "os.killpg(proc.pid, signal.SIGKILL)", "pass", SAFETY, "fork_bomb or setsid or sigint or long_running", True),
    ("output-cap", T, "if overflow.is_set():\n                    status = \"overflow\"", "if False:\n                    status = \"overflow\"",
     SAFETY, "huge_output", True),
    ("timeout", T, "if time.monotonic() >= deadline:\n                    status = \"timeout\"",
     "if False:\n                    status = \"timeout\"", SAFETY, "long_running", True),
    ("search-secret-skip", T, "        if deny_secrets and is_secret_relpath(rel):\n            continue\n        try:\n            if fpath.stat()",
     "        if False:\n            continue\n        try:\n            if fpath.stat()", SAFETY, "never_surface_secrets", False),
    ("list-secret-skip", T, "            if is_secret_relpath(rel):\n                continue\n            if p.is_dir():",
     "            if False:\n                continue\n            if p.is_dir():", SAFETY, "never_surface_secrets", False),
    ("copy-excludes-secrets", S, "        if is_secret_relpath(rel):\n            ws.skipped_secret += 1",
     "        if False:\n            ws.skipped_secret += 1", SAFETY, "copy_excludes_secrets", False),
    ("edit-exact-once-gate", T, "new, reasons = apply_edits({rel: original}, instr)",
     "new, reasons = ({rel: original.replace(instr[0].old_string, instr[0].new_string)}, [])", SAFETY,
     "apply_edits_exact_once", False),
    ("weakened-tests", V, "    v.weakened = weakened_tests(workspace.baseline, workspace.root, frozen)",
     "    v.weakened = []", SAFETY, "deleting or weakening or skipping or rewrites", True),
    ("harness-tamper-flag", V, "    v.tampered = harness_tampering(workspace.baseline, workspace.root)", "    v.tampered = []",
     INTEG, "every_harness_tamper or really_works", True),
    ("harness-tamper-scans-config", V, "        if is_control_relpath(rel):\n            if rel not in now_files:",
     "        if False:\n            if rel not in now_files:", INTEG, "every_harness_tamper", True),
    ("harness-tamper-scans-hooks", V, "            if len(now) > len(was):", "            if False:", INTEG,
     "every_harness_tamper", True),
    ("control-file-policy", P, '        if is_control_relpath(rel):\n            return _deny("protected"',
     '        if False:\n            return _deny("protected"', INTEG, "told_up_front", True),
    ("hook-secret-deny", "hooks/agent_loop.py", "    if rel != \".\" and is_secret_relpath(rel):\n        raise PermissionError",
     "    if False:\n        raise PermissionError", PARITY, "secret", False),
    ("hook-command-guard", "hooks/agent_loop.py", "            allowed, refusal = _writes.guard_command(seg[\"argv\"])\n            if not allowed:",
     "            allowed, refusal = True, ''\n            if not allowed:", PARITY, "decisions_match", False),
    ("hook-blocklist", "hooks/agent_loop.py", "    if _BLOCKED_COMMANDS.search(cmd):\n        return f\"Error: Command blocked", 
     "    if False:\n        return f\"Error: Command blocked", PARITY, "decisions_match", False),
    ("verifier-runs-after", V, "    elif after.rc != 0:", "    elif False:", LOOP, "does_not_fix", True),
    ("safety-flag-blocks-used", L, "if verdict.ok and res.safety_flags:", "if False:", LOOP, "frozen_test", True),
    ("used-needs-verifier", L, "res.used = True if verdict.ok else (False if verdict.ran else None)",
     "res.used = True", LOOP, "without_a_verifier or does_not_fix or models_own_done", True),
]


def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex[:8]


_CONTROLS: dict[tuple[str, str], bool] = {}


@pytest.fixture(scope="module")
def pristine_copy(tmp_path_factory):
    dest = tmp_path_factory.mktemp("mut") / "pkg"
    shutil.copytree(SRC_PKG, dest / "llm_router", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest


def _ps() -> dict[int, str]:
    out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,command="], capture_output=True, text=True).stdout
    return {int(ln.split(None, 1)[0]): ln.split(None, 1)[1] for ln in out.splitlines() if ln.strip()}


def _reap_leftovers(tag: str) -> None:
    """The kill-path mutations intentionally leave children alive; kill exactly the ones THIS run
    started (their scripts carry the run's unique tag), never another worker's."""
    import signal
    for pid, cmd in _ps().items():
        if tag in cmd:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def _run(pkg_root: Path, tests: str, k: str, tmp: Path, tag: str = "") -> subprocess.CompletedProcess:
    env = {"LLMR_TEST_RUN_TAG": tag, "PATH": os.environ["PATH"], "HOME": str(tmp), "LLM_ROUTER_HOME": str(tmp / "lr"),
           "PYTHONPATH": str(pkg_root), "OLLAMA_HOST": "127.0.0.1:1", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-m", "pytest", tests, "-k", k, "-x", "-q", "-p", "no:cacheprovider",
                           "-p", "no:xdist", "--no-header", "-o", "addopts="],
                          cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=200)


@pytest.mark.skipif(sys.platform != "darwin", reason="control run uses the macOS sandbox tests")
def test_the_mutation_harness_runs_the_copy_not_the_original(pristine_copy, tmp_path):
    (pristine_copy / "llm_router" / "toolkit" / "sentinel_probe.py").write_text("X = 1\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONPATH": str(pristine_copy)}
    out = subprocess.run([sys.executable, "-c", "import llm_router, sys; print(llm_router.__file__)"],
                         cwd=str(ROOT), env=env, capture_output=True, text=True).stdout.strip()
    assert out.startswith(str(pristine_copy)), out


@pytest.mark.parametrize("mid,rel,old,new,tests,k,needs_sb", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_removing_a_containment_check_turns_a_test_red(pristine_copy, tmp_path, mid, rel, old, new, tests, k,
                                                       needs_sb):
    if needs_sb and not SB:
        pytest.skip("this mutation is guarded by tests that need the proven OS sandbox")
    target = pristine_copy / "llm_router" / rel
    original = target.read_text()
    assert original.count(old) == 1, f"mutation {mid}: the text to remove was not found exactly once"
    if (tests, k) not in _CONTROLS:
        control = _run(pristine_copy, tests, k, tmp_path, "mc" + uuid_hex())
        assert control.returncode == 0, (
            f"control run is not green, a red would prove nothing:\n{control.stdout[-1500:]}")
        assert "passed" in control.stdout, "the control selected no tests (vacuous)"
        _CONTROLS[(tests, k)] = True
    import uuid
    tag = "mt" + uuid.uuid4().hex[:8]
    try:
        target.write_text(original.replace(old, new))
        mutated = _run(pristine_copy, tests, k, tmp_path, tag)
    finally:
        target.write_text(original)
        _reap_leftovers(tag)
    assert mutated.returncode != 0, f"mutation {mid} survived: no test went red\n{mutated.stdout[-800:]}"
    assert "failed" in mutated.stdout or "error" in mutated.stdout.lower(), mutated.stdout[-800:]
