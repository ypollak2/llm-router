"""Program-only application: real temporary git work trees and subprocesses."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from llm_router import apply_tool as apply

HANDOFF = "Nothing more will be applied by this tool for this attempt. Make the remaining changes yourself with your edit tools, then run the check."


@pytest.fixture(autouse=True)
def no_dependency_bytecode(monkeypatch):
    # The supplied interpreter lives in protected real state; lazy imports must
    # not try to cache third-party bytecode there.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    from llm_router import zero_claude_edit
    monkeypatch.setattr(zero_claude_edit, "edit_model", lambda **kwargs: None)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / ".llm-router"))
    monkeypatch.setenv("LLM_ROUTER_APPLY", "on")
    work = tmp_path / "repo"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "a.py").write_text("value = 1\n")
    return work


REAL_RUN_CHECK = apply._run_check


def edit(file="a.py", old="value = 1", new="value = 2"):
    return dict(file=file, old_string=old, new_string=new)


def call(repo, edits=None, check=""):
    return apply.llm_apply(json.dumps(edits if edits is not None else [edit()]), check, str(repo))


def rows():
    return [json.loads(line) for line in (Path(os.environ["LLM_ROUTER_HOME"]) / "apply_ledger.jsonl").read_text().splitlines()]


def runner(repo, monkeypatch, code):
    bin_dir = repo.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    executable = bin_dir / "pytest"
    executable.write_text(f"#!{sys.executable}\n" + code)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])


def test_json_composes_and_reports_counts(repo):
    result = call(repo, [edit(), edit(old="value = 2", new="value = 3")])
    assert result.startswith("APPLIED_NOT_CHECKED\n")
    assert "a.py: +1/-1 lines" in result
    assert (repo / "a.py").read_text() == "value = 3\n"
    assert rows()[0]["n_blocks"] == 2


def test_newline_only_diff_counts(repo):
    result = call(repo, [edit(old="value = 1\n", new="value = 1")])
    assert result.startswith("APPLIED_NOT_CHECKED")
    assert "a.py: +1/-1 lines" in result


def test_blocks_new_append_replace_and_newline_fallback(repo):
    (repo / "notes.txt").write_text("first")
    result = apply.llm_apply('''FILE: a.py
FIND:
```python
value = 1
```
REPLACE:
```python
value = 2
```
NEW FILE: sub/new.py
```python
x = 1
```
FILE: sub/new.py
FIND:
```
x = 1
```
REPLACE:
```
x = 2
```
APPEND TO: notes.txt
```
second
```
APPEND TO: fresh.txt
```
created
```
''', cwd=str(repo))
    assert result.startswith("APPLIED_NOT_CHECKED\n")
    assert (repo / "a.py").read_text() == "value = 2\n"
    assert (repo / "sub/new.py").read_text() == "x = 2\n"
    assert (repo / "notes.txt").read_text() == "first\nsecond\n"
    assert (repo / "fresh.txt").read_text() == "created\n"
    (repo / "notes.txt").write_text("first\nsecond")
    result = apply.llm_apply("FILE: notes.txt\nFIND:\n```\nsecond\n```\nREPLACE:\n```\nlast\n```", cwd=str(repo))
    assert result.startswith("APPLIED_NOT_CHECKED")
    assert (repo / "notes.txt").read_text() == "first\nlast"


@pytest.mark.parametrize("flag", [None, "off", "0", "false", "no"])
def test_off_switch_read_per_call(repo, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("LLM_ROUTER_APPLY")
    else:
        monkeypatch.setenv("LLM_ROUTER_APPLY", flag)
    result = call(repo)
    assert result.startswith("REJECTED\n")
    assert "[llm_apply credit]" not in result
    assert "disabled" in result and "LLM_ROUTER_APPLY=on" in result
    assert result.endswith(HANDOFF)
    assert (repo / "a.py").read_text() == "value = 1\n"
    assert not (Path(os.environ["LLM_ROUTER_HOME"]) / "apply_ledger.jsonl").exists()
    monkeypatch.setenv("LLM_ROUTER_APPLY", "yes")
    assert call(repo).startswith("APPLIED_NOT_CHECKED")
    assert len(rows()) == 1


@pytest.mark.parametrize("flag", ["on", "1", "true", "yes", " TRUE "])
def test_enable_values(repo, monkeypatch, flag):
    monkeypatch.setenv("LLM_ROUTER_APPLY", flag)
    assert call(repo).startswith("APPLIED_NOT_CHECKED")


def test_all_or_nothing_and_every_reason(repo):
    result = call(repo, [edit(), edit(old="missing"), edit(file="missing.py")], "pytest")
    assert result.startswith("REJECTED") and result.endswith(HANDOFF)
    assert "appears 0 times" in result and "no such file" in result
    assert (repo / "a.py").read_text() == "value = 1\n"
    assert not rows()[0]["check_ran"]


@pytest.mark.parametrize("bad", ["../outside.py", "link.py", "escape/new.py"])
def test_outside_cwd_and_symlinks_refused(repo, bad):
    outside = repo.parent / "outside.py"
    outside.write_text("value = 1\n")
    (repo / "link.py").symlink_to(outside)
    (repo / "escape").symlink_to(repo.parent, target_is_directory=True)
    result = call(repo, [edit(), edit(file=bad)])
    assert result.startswith("REJECTED") and "outside cwd" in result
    assert outside.read_text() == "value = 1\n"
    assert (repo / "a.py").read_text() == "value = 1\n"
    assert not (repo.parent / "new.py").exists()


def test_cwd_is_boundary_even_inside_larger_repo(repo):
    sub = repo / "sub"
    sub.mkdir()
    result = call(sub, [edit(file="../a.py")])
    assert result.startswith("REJECTED") and "outside cwd" in result


def test_new_file_overwrite_and_duplicate_refused(repo):
    for text in ["NEW FILE: a.py\n```\nx = 1\n```", "NEW FILE: new.txt\n```\nx\n```\nNEW FILE: new.txt\n```\ny\n```"]:
        result = apply.llm_apply(text, cwd=str(repo))
        assert result.startswith("REJECTED") and "overwrite" in result
    assert (repo / "a.py").read_text() == "value = 1\n"
    assert not (repo / "new.txt").exists()


@pytest.mark.parametrize("new", ["value = (", " "])
def test_final_syntax_gate(repo, new):
    # Empty Python is valid, but an unmatched parenthesis must reject the batch.
    result = call(repo, [edit(new=new)])
    assert result.startswith("REJECTED" if new == "value = (" else "APPLIED_NOT_CHECKED")


def test_syntax_checked_after_composition(repo):
    assert call(repo, [edit(new="value = ("), edit(old="value = (", new="value = (2)")]).startswith("APPLIED_NOT_CHECKED")


@pytest.mark.parametrize("raw", ["[]", "not edits", '[{"file": "a.py"}]', "FILE: a.py\nFIND:\n```\nx\n```", json.dumps([edit(), {"file": "a.py"}])])
def test_bad_input_rejects_entire_batch(repo, raw):
    assert apply.llm_apply(raw, cwd=str(repo)).startswith("REJECTED")
    assert (repo / "a.py").read_text() == "value = 1\n"


def test_non_git_refused_and_default_cwd(repo, monkeypatch):
    assert call(repo.parent).startswith("REJECTED")
    monkeypatch.chdir(repo)
    assert apply.llm_apply(json.dumps([edit()])).startswith("APPLIED_NOT_CHECKED")


def test_real_pytest_check(repo, monkeypatch):
    (repo / "test_smoke.py").write_text("def test_value():\n    from a import value\n    assert value == 2\n")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    result = call(repo, check="python -m pytest -q test_smoke.py")
    assert result.startswith("APPLIED_AND_PASSED"), result
    assert "Check rc=0" in result and "1 passed" in result


def test_allowed_runner_and_failed_check_tail(repo, monkeypatch):
    runner(repo, monkeypatch, "import sys\nprint('x' * 2000 + 'THE_END')\nsys.exit(7)\n")
    result = call(repo, check="pytest -q")
    assert result.startswith("APPLIED_CHECK_FAILED") and result.endswith(HANDOFF)
    assert "Check rc=7" in result and "THE_END" in result
    tail = result.split("Check rc=7\n")[1].split("\n" + HANDOFF)[0]
    assert len(tail) == 1500
    assert (repo / "a.py").read_text() == "value = 2\n"
    assert rows()[0]["check_rc"] == 7 and rows()[0]["check_ran"]


@pytest.mark.parametrize("command", ["pytest; rm -rf x", "bash -c 'echo no'", "pytest | cat", "pytest && echo x", "pytest $X", "pytest `id`", "pytest > out", "pytest < in", "pytest\necho x", "python -c 'print(1)'", "npm install", "pytest 'unterminated"])
def test_disallowed_check_does_not_run(repo, monkeypatch, command):
    monkeypatch.setattr(apply, "_run_check", lambda *args: pytest.fail("unsafe check ran"))
    result = call(repo, check=command)
    assert result.startswith("APPLIED_NOT_CHECKED") and "Check not run:" in result
    assert not rows()[0]["check_ran"]


@pytest.mark.parametrize("command", ["pytest -q", "python -m pytest -q", "python3 -m pytest", "ruff check .", "mypy src", "pnpm test", "go test ./pkg", "go test -run TestX -count=1 ./...", "cargo test", "pytest -q tests/test_x.py -k name", "pytest -qx tests/test_x.py::test_a", "pytest --tb=short -m 'not slow'"])
def test_allowlist(repo, monkeypatch, command):
    for path in ("src", "pkg", "tests"):
        (repo / path).mkdir(exist_ok=True)
    (repo / "tests/test_x.py").write_text("def test_a():\n    pass\n")
    monkeypatch.setattr(apply.shutil, "which", lambda name: "/trusted/bin/" + name)
    assert apply._check_argv(command, repo)[0] is not None


# Round-4 review bypasses: each made a runner read config/plugins/files from outside the repo.
@pytest.mark.parametrize("command", ["pytest @/tmp/evil.args", "pytest @args.txt", "python -m pytest --pyargs requests",
    "pytest --inifile=external.ini", "mypy @/tmp/evil.args", "go test -modfile=x.mod", "go test '-gcflags=all=-importcfg /tmp/e'",
    "npm test -- --run", "pytest -k -pevil", "pytest missing_dir", "pytest link_out", "ruff check --config x.toml"])
def test_allowlist_refuses(repo, monkeypatch, command):
    (repo / "external.ini").write_text("[pytest]\n")
    (repo / "args.txt").write_text("-p evil\n")
    (repo / "link_out").symlink_to("/tmp")
    monkeypatch.setattr(apply.shutil, "which", lambda name: "/trusted/bin/" + name)
    argv, reason = apply._check_argv(command, repo)
    assert argv is None and reason.startswith("Check not run:"), (argv, reason)


@pytest.mark.parametrize("parent_exits", [False, True])
def test_kills_started_descendant(repo, monkeypatch, parent_exits):
    ready = repo / "child.ready"
    heartbeat = repo / "child.heartbeat"
    child = ("import time; from pathlib import Path\n"
             f"heartbeat = Path({str(heartbeat)!r})\n"
             "heartbeat.touch()\n"
             f"Path({str(ready)!r}).write_text('ready')\n"
             "deadline = time.monotonic() + 60\n"
             "while time.monotonic() < deadline:\n"
             "    heartbeat.touch()\n"
             "    time.sleep(0.1)\n")
    runner(repo, monkeypatch, f"import subprocess, sys, time\nfrom pathlib import Path\nsubprocess.Popen([sys.executable, '-c', {child!r}])\ndeadline = time.monotonic() + 10\nwhile not Path({str(ready)!r}).exists():\n    if time.monotonic() > deadline: raise RuntimeError('child never ready')\n    time.sleep(0.01)\n" + ("" if parent_exits else "time.sleep(60)\n"))
    monkeypatch.setattr(apply, "_CHECK_TIMEOUT", 12.0 if parent_exits else 5.0)
    result = call(repo, check="pytest")
    assert ready.exists(), result
    if parent_exits and "sandboxed (Linux bubblewrap)" in result:
        # bwrap's PID namespace dies with its init, taking the child with it:
        # nothing survives to report, which is the stronger outcome.
        assert result.startswith("APPLIED_AND_PASSED"), result
    else:
        assert result.startswith("APPLIED_CHECK_FAILED"), result
        assert "still alive" in result and "process group killed" in result
        assert rows()[0]["check_rc"] == -9
    modified = heartbeat.stat().st_mtime_ns
    time.sleep(0.5)
    assert heartbeat.stat().st_mtime_ns == modified


def test_ledger_schema(repo):
    call(repo)
    row = rows()[0]
    assert set(row) == {"ts", "status", "n_blocks", "n_files", "check_ran", "check_rc", "sandboxed", "duration_s", "source",
                        "repair_attempted", "repair_passed", "repair_model", "repair_s", "repair_reason",
                        "repair_check_rc", "repair_sandboxed", "program_replaced", "local_replaced", "files", "repair_trigger"}
    assert row["status"] == "APPLIED_NOT_CHECKED" and row["source"] == "program"
    assert row["n_files"] == row["n_blocks"] == 1
    assert row["ts"].endswith("+00:00") and row["duration_s"] >= 0
    assert row["check_rc"] is None


@pytest.mark.parametrize("error", [OSError, RuntimeError, TypeError, KeyboardInterrupt, SystemExit])
def test_write_error_rolls_back(repo, monkeypatch, error):
    (repo / "b.py").write_text("value = 1\n")
    replace = os.replace
    failed = False

    def fail_second(source, target, **kwargs):
        nonlocal failed
        if Path(target).name == "b.py" and not failed:
            failed = True
            raise error("injected write failure")
        return replace(source, target, **kwargs)

    monkeypatch.setattr(os, "replace", fail_second)
    if issubclass(error, Exception):
        result = call(repo, [edit(), edit(file="b.py")])
        assert result.startswith("REJECTED") and "injected write failure" in result
    else:
        with pytest.raises(error, match="injected write failure"):
            call(repo, [edit(), edit(file="b.py")])
    assert (repo / "a.py").read_text() == (repo / "b.py").read_text() == "value = 1\n"
    assert not list(repo.glob(".llm-apply-*"))


def test_registration_excludes_slim_tiers():
    from llm_router.tools import text
    from llm_router.tool_tiers import make_should_register

    class Registry:
        def __init__(self):
            self.names = set()

        def tool(self):
            return lambda fn: self.names.add(fn.__name__)

    for tier in ("off", "core", "routing", "consolidated"):
        registry = Registry()
        text.register(registry, make_should_register(tier))
        assert ("llm_apply" in registry.names) == (tier == "off")


def test_async_tool_wrapper(repo):
    from llm_router.tools.text import llm_apply
    assert asyncio.run(llm_apply(json.dumps([edit()]), cwd=str(repo))).startswith("APPLIED_NOT_CHECKED")


def test_fenced_json_with_malformed_trailer(repo):
    raw = "```json\n" + json.dumps([edit()]) + "\n```\nFILE: a.py\nFIND:\n```\nvalue = 2\n```"
    assert apply.llm_apply(raw, cwd=str(repo)).startswith("REJECTED")
    assert (repo / "a.py").read_text() == "value = 1\n"


@pytest.mark.parametrize("field", ["file", "old_string", "new_string"])
@pytest.mark.parametrize("bad", [None, [], {}, 1, False])
def test_non_string_json_fields(repo, field, bad):
    invalid = edit()
    invalid[field] = bad
    assert call(repo, [edit(), invalid]).startswith("REJECTED")
    assert (repo / "a.py").read_text() == "value = 1\n"


@pytest.mark.parametrize("swap_at", [1, 3])
def test_parent_swapped_outside_before_write(repo, monkeypatch, swap_at):
    sub = repo / "sub"
    sub.mkdir()
    (sub / "a.py").write_text("value = 1\n")
    outside = repo.parent / "outside"
    outside.mkdir()
    (outside / "a.py").write_text("value = 1\n")
    count = 0
    def swap(path):
        nonlocal count
        count += 1
        if count == swap_at and sub.is_dir() and not sub.is_symlink():
            sub.rename(repo / "saved")
            sub.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(apply, "_before_write", swap)
    result = call(repo, [edit(file="sub/a.py")])
    assert result.startswith("REJECTED")
    assert (outside / "a.py").read_text() == "value = 1\n"
    assert not list(outside.glob(".llm-apply-*"))


@pytest.mark.parametrize("create_at", [1, 2])
def test_new_file_concurrent_creation(repo, monkeypatch, create_at):
    target = repo / "new.txt"
    count = 0
    def create(path):
        nonlocal count
        count += 1
        if count == create_at and not target.exists():
            target.write_text("concurrent")
    monkeypatch.setattr(apply, "_before_write", create)
    result = apply.llm_apply("NEW FILE: new.txt\n```\nnew\n```", cwd=str(repo))
    assert result.startswith("REJECTED")
    assert target.read_text() == "concurrent"


def test_existing_file_identity_changed(repo, monkeypatch):
    target = repo / "a.py"
    def swap(path):
        monkeypatch.setattr(apply, "_before_write", lambda path: None)
        target.rename(repo / "old.py")
        target.write_text("value = 99\n")
    monkeypatch.setattr(apply, "_before_write", swap)
    assert call(repo).startswith("REJECTED")
    assert target.read_text() == "value = 99\n"


@pytest.mark.parametrize("error", [OSError, RuntimeError])
def test_restore_failure_keeps_all_backups(repo, monkeypatch, error):
    (repo / "b.py").write_text("value = 1\n")
    replace = os.replace
    count = 0
    def fail(source, target, **kwargs):
        nonlocal count
        count += 1
        if count >= 2:
            raise error("injected failure")
        return replace(source, target, **kwargs)
    monkeypatch.setattr(os, "replace", fail)
    result = call(repo, [edit(), edit(file="b.py")])
    assert result.startswith("REJECTED") and "ROLLBACK INCOMPLETE" in result
    backups = list(repo.glob(".llm-apply-*"))
    assert len(backups) == 2
    for backup in backups:
        assert str(backup) in result
        assert backup.read_text() == "value = 1\n"


def test_arbitrary_python_path_not_run(repo, monkeypatch):
    monkeypatch.setattr(apply, "_run_check", lambda *args: pytest.fail("unsafe runner"))
    assert "Check not run:" in call(repo, check="/tmp/x/python -m pytest")


def test_local_venv_python_inside_cwd(repo):
    binary = repo / ".venv/bin/python"
    binary.parent.mkdir(parents=True)
    binary.write_text(f"#!{sys.executable}\nprint('local check')\n")
    binary.chmod(0o755)
    result = call(repo, check=".venv/bin/python -m pytest")
    assert result.startswith("APPLIED_AND_PASSED"), result
    assert "local check" in result


def test_local_venv_python_outside_cwd(repo):
    binary = repo / ".venv/bin/python"
    binary.parent.mkdir(parents=True)
    binary.symlink_to(sys.executable)
    assert "Check not run:" in call(repo, check=".venv/bin/python -m pytest")


@pytest.mark.parametrize("flag", ["-p", "-c", "-C", "-o", "--override-ini", "--rootdir", "--confcutdir", "--config", "--config-file", "--prefix", "--manifest-path", "--import-mode", "--script-shell", "-exec", "--exec", "-toolexec", "--toolexec", "--basetemp"])
@pytest.mark.parametrize("suffix", [" x", "=x"])
def test_forbidden_flags(repo, monkeypatch, flag, suffix):
    monkeypatch.setattr(apply, "_run_check", lambda *args: pytest.fail("forbidden flag ran"))
    assert "Check not run:" in call(repo, check="pytest " + flag + suffix)


@pytest.mark.parametrize("arg", ["/tmp/tests", "../tests", "foo/../bar", "--option=../x", "~/tests", "--option=/tmp/tests", "--option=~/tests", "-k/tmp/tests", "-k~/tests"])
def test_forbidden_path_arguments(repo, monkeypatch, arg):
    monkeypatch.setattr(apply, "_run_check", lambda *args: pytest.fail("forbidden path ran"))
    assert "Check not run:" in call(repo, check="pytest " + arg)


def test_child_environment_scrub(repo, monkeypatch):
    keys = ["PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONSTARTUP", "PYTHONPATH", "NODE_OPTIONS", "NPM_CONFIG_PREFIX", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "npm_config_script_shell", "LD_LIBRARY_PATH", "GOFLAGS", "RUSTC_WRAPPER"]
    runner(repo, monkeypatch, "import os, json\nprint('PYTEST_ADDOPTS=' + str(os.getenv('PYTEST_ADDOPTS')))\nprint(json.dumps({key: os.getenv(key) for key in " + repr(keys) + "}))\n")
    # Capture the environment passed to the child as well: loaders consume some
    # variables before Python can inspect them.
    popen = apply.subprocess.Popen
    def capture(*args, **kwargs):
        if "env" in kwargs:
            assert all(key not in kwargs["env"] for key in keys)
            allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TERM", "TMPDIR",
                       "VIRTUAL_ENV", "PYTHONDONTWRITEBYTECODE", "CI", "NO_COLOR"}
            assert all(key in allowed or key.startswith("LC_") for key in kwargs["env"])
            assert kwargs["env"]["CI"] == "true"
            assert kwargs["env"]["LC_MESSAGES"] == "C"
        return popen(*args, **kwargs)
    monkeypatch.setattr(apply.subprocess, "Popen", capture)
    for key in keys:
        monkeypatch.setenv(key, "injected")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("LC_MESSAGES", "C")
    result = call(repo, check="pytest")
    assert result.startswith("APPLIED_AND_PASSED"), result
    assert "PYTEST_ADDOPTS=None" in result


def test_ledger_write_failure_reported(repo, monkeypatch):
    blocked = repo / "blocked"
    blocked.write_text("file")
    monkeypatch.setattr(apply, "llm_router_home", lambda: blocked)
    result = call(repo)
    assert result.startswith("REJECTED")
    assert "ledger not writable: " in result
    assert (repo / "a.py").read_text() == "value = 1\n"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_json_nonstandard_constants_rejected(repo, constant):
    raw = json.dumps([edit()])[:-2] + ', "extra": ' + constant + '}]'
    assert apply.llm_apply(raw, cwd=str(repo)).startswith("REJECTED")
    assert (repo / "a.py").read_text() == "value = 1\n"


def test_malformed_block_cannot_consume_later_fences(repo):
    raw = "FILE: a.py\nFIND:\n```\nvalue = 1\n```\ngarbage\n```\nREPLACE:\n```\nvalue = 2\n```"
    # Make the permissive regex's cross-fence capture a unique valid match.
    original = "value = 1\n```\ngarbage\n"
    (repo / "a.py").write_text(original)
    assert apply.llm_apply(raw, cwd=str(repo)).startswith("REJECTED")
    assert (repo / "a.py").read_text() == original


@pytest.mark.parametrize("command", ["pytest -pevil", "pytest -c/tmp/evil.ini",
    "pytest -o addopts=-pevil", "npm test --script-shell=./evil", "go test -exec=./evil",
    "pytest -qc/tmp/evil.ini", "pytest -qpplugin", "python -m pytest -xp evil", "ruff check -qc x"])
def test_attached_runner_overrides_not_run(repo, monkeypatch, command):
    monkeypatch.setattr(apply, "_run_check", lambda *args: pytest.fail("unsafe runner argument"))
    result = call(repo, check=command)
    assert result.startswith("APPLIED_NOT_CHECKED") and "Check not run:" in result
    assert not rows()[0]["check_ran"]


@pytest.mark.parametrize("command", ["pytest -qq", "pytest -qx", "pytest -q -k name"])
def test_harmless_grouped_switches_still_run(repo, monkeypatch, command):
    runner(repo, monkeypatch, "")
    seen = []
    monkeypatch.setattr(apply, "_run_check", lambda argv, root: (seen.append(argv), (0, "ok", False))[1])
    result = call(repo, check=command)
    assert seen, result
    assert result.startswith("APPLIED_AND_PASSED")


@pytest.mark.parametrize("leave_symlink", [False, True])
def test_parent_relocated_after_identity_check(repo, monkeypatch, leave_symlink):
    sub = repo / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("value = 1\n")
    outside = repo.parent / "moved"

    def relocate(path):
        if path.name == "b.py":
            sub.rename(outside)
            if leave_symlink:
                sub.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(apply, "_before_replace", relocate)
    result = call(repo, [edit(), edit(file="sub/b.py")])
    assert result.startswith("REJECTED"), result
    assert "path moved outside cwd during write" in result
    assert (repo / "a.py").read_text() == (outside / "b.py").read_text() == "value = 1\n"
    assert not list(repo.rglob(".llm-apply-*"))
    assert not list(outside.glob(".llm-apply-*"))


def test_ledger_final_append_failure_reported(repo, monkeypatch):
    real_open = open

    class BrokenAppend:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def write(self, text):
            raise OSError("injected final append failure")

    def ledger_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return BrokenAppend(handle) if Path(path).name == "apply_ledger.jsonl" else handle

    monkeypatch.setattr(apply, "open", ledger_open, raising=False)
    result = call(repo)
    assert result.startswith("APPLIED_NOT_CHECKED")
    assert "ledger: not written (injected final append failure)" in result
    assert (repo / "a.py").read_text() == "value = 2\n"


def test_ledger_open_failure_rejects_before_any_writes(repo, monkeypatch):
    real_open = open

    def deny_ledger(path, *args, **kwargs):
        if Path(path).name == "apply_ledger.jsonl":
            raise PermissionError("injected append open denial")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(apply, "open", deny_ledger, raising=False)
    monkeypatch.setattr(apply, "_write_batch", lambda *args: pytest.fail("write before ledger open"))
    result = call(repo)
    assert result.startswith("REJECTED")
    assert "ledger not writable: injected append open denial" in result
    assert (repo / "a.py").read_text() == "value = 1\n"


@pytest.fixture
def repair_setup(repo, monkeypatch):
    from llm_router import zero_claude_edit as local
    events = []
    runner(repo, monkeypatch, "")

    def model(*, allow_probe):
        assert allow_probe is False
        return "test-local"

    def wrap(argv, root):
        events.append("probe")
        return argv, lambda: events.append("cleanup")

    def generate(task, contents, model, *, deadline_s):
        assert events == ["probe", "cleanup"]
        assert model == "test-local" and deadline_s > time.monotonic()
        assert "a.py" in contents
        assert ("apply the intent of these edits" in task and "Rejection reasons:" in task) or (
            "make this check pass with minimal edits" in task and "a.py: +1/-1 lines" in task
            and "Check command: pytest" in task and "failed" in task)
        events.append("generate")
        return {"a.py": "value = 3\n"}, [apply.EditInstruction("a.py", contents["a.py"].strip(), "value = 3")], None

    def check(argv, root, *, require_sandbox=False):
        events.append("repair-check" if require_sandbox else "initial-check")
        if not require_sandbox:
            events.clear()  # Generation must be after probe cleanup.
            return 7, "check: initial\nfailed", False
        assert (root / "a.py").read_bytes() == b"value = 3\n"
        return 0, "check: sandboxed\npassed", True

    monkeypatch.delenv("LLM_ROUTER_APPLY_REPAIR", raising=False)
    monkeypatch.setattr(local, "edit_model", model)
    monkeypatch.setattr(local, "generate_edits", generate)
    monkeypatch.setattr(apply.check_sandbox, "wrap", wrap)
    monkeypatch.setattr(apply, "_run_check", check)
    return local, events


def assert_repair_row(attempted, passed, reason):
    row = rows()[-1]
    assert row["repair_attempted"] is attempted
    assert row["repair_passed"] is passed
    assert row["repair_reason"] == reason
    assert row["source"] == ("local" if passed else "program")
    assert (row["repair_s"] is not None) is attempted
    assert "value =" not in json.dumps(row)
    return row


@pytest.mark.parametrize("rejected", [False, True])
def test_local_repair_passes(repo, repair_setup, rejected):
    result = call(repo, [edit(old="missing")] if rejected else None, "pytest")
    assert result.startswith("REPAIRED_BY_LOCAL_AND_PASSED"), result
    assert "local model test-local" in result and "Claude should review the diff" in result
    assert "a.py: +1/-1 lines" in result and HANDOFF not in result
    assert (repo / "a.py").read_bytes() == b"value = 3\n"
    row = assert_repair_row(True, True, "passed")
    assert row["repair_model"] == "test-local" and row["sandboxed"] and row["check_rc"] == 0


@pytest.mark.parametrize("failure", ["check", "sandbox", "no edits", "outside", "syntax", "timeout", "exception"])
@pytest.mark.parametrize("rejected", [False, True])
def test_failed_local_repair_restores(repo, monkeypatch, repair_setup, failure, rejected):
    local, events = repair_setup
    if failure in {"check", "sandbox"}:
        def check(argv, root, *, require_sandbox=False):
            if require_sandbox and failure == "sandbox":
                raise apply.check_sandbox.SandboxUnavailable("lost backend")
            return 7, "check: test\nfailed", require_sandbox
        monkeypatch.setattr(apply, "_run_check", check)
    else:
        def generate(task, contents, model, *, deadline_s):
            if failure == "exception":
                raise TimeoutError("model timeout")
            if failure == "no edits":
                return None, [], "empty"
            if failure == "timeout":
                monkeypatch.setattr(apply.time, "monotonic", lambda: deadline_s + 1)
            target = "b.py" if failure == "outside" else "a.py"
            new = "value = (" if failure == "syntax" else "value = 3"
            return {}, [apply.EditInstruction(target, contents["a.py"].strip(), new)], None
        monkeypatch.setattr(local, "generate_edits", generate)
    (repo / "b.py").write_text("value = 99\n")
    result = call(repo, [edit(old="missing")] if rejected else None, "pytest")
    assert result.startswith("REJECTED" if rejected else "APPLIED_CHECK_FAILED"), result
    assert "local repair: failed (" in result and result.endswith(HANDOFF)
    assert (repo / "a.py").read_bytes() == (b"value = 1\n" if rejected else b"value = 2\n")
    assert (repo / "b.py").read_bytes() == b"value = 99\n"
    row = rows()[-1]
    assert_repair_row(True, False, row["repair_reason"])


@pytest.mark.parametrize("gate", ["off", "0", "false", "no", "no model", "sandbox", "no check", "bad check", "disabled"])
def test_local_repair_gates(repo, monkeypatch, repair_setup, gate):
    local, events = repair_setup
    monkeypatch.setattr(local, "generate_edits", lambda *a, **kw: pytest.fail("generation despite gate"))
    check = "pytest"
    if gate in {"off", "0", "false", "no"}:
        monkeypatch.setenv("LLM_ROUTER_APPLY_REPAIR", gate)
    elif gate == "no model":
        monkeypatch.setattr(local, "edit_model", lambda **kw: None)
    elif gate == "sandbox":
        def unavailable(*args):
            raise apply.check_sandbox.SandboxUnavailable("missing backend")
        monkeypatch.setattr(apply.check_sandbox, "wrap", unavailable)
    elif gate == "disabled":
        monkeypatch.setenv("LLM_ROUTER_APPLY", "off")
    else:
        check = "" if gate == "no check" else "bash -c nope"
    result = call(repo, [edit(old="missing")], check)
    assert result.startswith("REJECTED") and result.endswith(HANDOFF)
    assert (repo / "a.py").read_text() == "value = 1\n"
    if gate != "disabled":
        reason = ("disabled" if gate in {"off", "0", "false", "no"} else
                  "no local model" if gate == "no model" else
                  "sandbox unavailable" if gate == "sandbox" else "no allowlisted check")
        assert_repair_row(False, False, reason)
    if gate == "sandbox":
        assert "sandbox unavailable: missing backend" in result


@pytest.mark.parametrize("seconds,expected", [("1", 10), ("999", 120), ("42.5", 42.5), ("bad", 60), ("inf", 120), ("-inf", 10)])
def test_repair_seconds_clamp(monkeypatch, seconds, expected):
    monkeypatch.setenv("LLM_ROUTER_APPLY_REPAIR_S", seconds)
    assert apply._repair_seconds() == expected


def test_repair_seconds_default(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_APPLY_REPAIR_S", raising=False)
    assert apply._repair_seconds() == 60


@pytest.mark.parametrize("file", ["missing.py", "../outside.py"])
def test_rejected_batch_requires_existing_contained_files(repo, monkeypatch, repair_setup, file):
    local, _ = repair_setup
    (repo.parent / "outside.py").write_text("value = 1\n")
    monkeypatch.setattr(local, "generate_edits", lambda *a, **kw: pytest.fail("unsafe repair"))
    result = call(repo, [edit(file=file)], "pytest")
    assert result.startswith("REJECTED")
    assert_repair_row(False, False, None)


def test_local_repair_requires_sandbox_even_when_off(repo, monkeypatch, repair_setup):
    monkeypatch.setenv("LLM_ROUTER_APPLY_SANDBOX", "off")
    result = call(repo, check="pytest")
    assert result.startswith("REPAIRED_BY_LOCAL_AND_PASSED"), result
    assert "repair-check" in repair_setup[1]


def test_local_repair_restore_failure_reported(repo, monkeypatch, repair_setup):
    write = apply._write_batch
    calls = 0

    def write_batch(*args):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected restore failure")
        return write(*args)

    monkeypatch.setattr(apply, "_write_batch", write_batch)
    monkeypatch.setattr(apply, "_run_check", lambda *a, **kw: (7, "check: failed", True))
    result = call(repo, check="pytest")
    assert result.startswith("APPLIED_CHECK_FAILED") and result.endswith(HANDOFF)
    assert "ROLLBACK INCOMPLETE: injected restore failure" in result
    assert_repair_row(True, False, "check still failed")


def test_local_repair_restores_exact_crlf_bytes(repo, monkeypatch, repair_setup):
    (repo / "a.py").write_bytes(b"value = 1\r\n")
    monkeypatch.setattr(apply, "_run_check", lambda *a, **kw: (7, "check: failed", True))
    result = call(repo, check="pytest")
    assert result.startswith("APPLIED_CHECK_FAILED") and result.endswith(HANDOFF)
    assert (repo / "a.py").read_bytes() == b"value = 2\r\n"


def test_local_repair_refuses_entire_out_of_batch_response(repo, monkeypatch, repair_setup):
    local, _ = repair_setup
    outside = repo.parent / "outside.py"
    outside.write_text("value = 99\n")
    monkeypatch.setattr(local, "generate_edits", lambda *a, **kw: ({}, [
        apply.EditInstruction("a.py", "value = 2", "value = 3"),
        apply.EditInstruction("../outside.py", "value = 99", "value = 3"),
    ], None))
    result = call(repo, check="pytest")
    assert result.startswith("APPLIED_CHECK_FAILED")
    assert (repo / "a.py").read_text() == "value = 2\n"
    assert outside.read_text() == "value = 99\n"
    assert_repair_row(True, False, "file outside repair batch")


def test_restore_survives_non_utf8_left_by_failed_check(repo, monkeypatch, repair_setup):
    def check(argv, root, *, require_sandbox=False):
        if require_sandbox:
            (root / "a.py").write_bytes(b"\xff\xfe broken")
        return 7, "check: test\nfailed", require_sandbox

    monkeypatch.setattr(apply, "_run_check", check)
    result = call(repo, check="pytest")
    assert result.startswith("APPLIED_CHECK_FAILED") and "ROLLBACK INCOMPLETE" not in result, result
    assert (repo / "a.py").read_bytes() == b"value = 2\n"


def _plain_generate(monkeypatch, local):
    monkeypatch.setattr(local, "generate_edits", lambda task, contents, model, *, deadline_s: (
        {"a.py": "value = 3\n"}, [apply.EditInstruction("a.py", contents["a.py"].strip(), "value = 3")], None))


def test_ledger_keeps_original_check_after_failed_repair(repo, monkeypatch, repair_setup):
    _plain_generate(monkeypatch, repair_setup[0])
    monkeypatch.setattr(apply, "_run_check",
                        lambda argv, root, *, require_sandbox=False: (9 if require_sandbox else 7, "check: x\nfailed", require_sandbox))
    result = call(repo, check="pytest")
    assert result.startswith("APPLIED_CHECK_FAILED")
    row = rows()[-1]
    assert (row["check_rc"], row["sandboxed"]) == (7, False)
    assert (row["repair_check_rc"], row["repair_sandboxed"]) == (9, True)
    # Positive control: a passing repair reports the repaired state's check.
    monkeypatch.setattr(apply, "_run_check",
                        lambda argv, root, *, require_sandbox=False: (0 if require_sandbox else 7, "check: x\nok", require_sandbox))
    (repo / "a.py").write_text("value = 1\n")
    assert call(repo, check="pytest").startswith("REPAIRED_BY_LOCAL_AND_PASSED")
    row = rows()[-1]
    assert (row["check_rc"], row["sandboxed"], row["repair_check_rc"]) == (0, True, 0)


def test_rejected_batch_task_carries_validated_fields_only(repo, monkeypatch, repair_setup):
    local, _ = repair_setup
    seen = []
    real = local.generate_edits
    monkeypatch.setattr(local, "generate_edits", lambda task, *a, **kw: (seen.append(task), real(task, *a, **kw))[1])
    result = call(repo, [{**edit(old="missing"), "private_note": "SECRET-TOKEN"}], "pytest")
    assert result.startswith("REPAIRED_BY_LOCAL_AND_PASSED"), result
    assert len(seen) == 1 and "SECRET-TOKEN" not in seen[0]
    assert "FIND:\nmissing" in seen[0] and "REPLACE:\nvalue = 2" in seen[0]


def _real_check_setup(monkeypatch, wrap_results):
    """Restore the real _run_check; mock only the sandbox wrapper and the process run."""
    monkeypatch.setattr(apply, "_run_check", REAL_RUN_CHECK)
    wraps, runs = [], []

    def wrap(argv, root):
        outcome = wrap_results[len(wraps)]
        wraps.append(argv)
        if isinstance(outcome, Exception):
            raise outcome
        return ["sandboxed", *argv], lambda: None

    def execute(argv, root):
        runs.append(argv)
        passed = (root / "a.py").read_text() == "value = 3\n"
        return (0, "ok") if passed else (7, "failed")

    monkeypatch.setattr(apply.check_sandbox, "wrap", wrap)
    monkeypatch.setattr(apply, "_execute_check", execute)
    return wraps, runs


def test_repair_check_is_sandboxed_by_the_real_run_check_even_when_off(repo, monkeypatch, repair_setup):
    _plain_generate(monkeypatch, repair_setup[0])
    monkeypatch.setenv("LLM_ROUTER_APPLY_SANDBOX", "off")
    wraps, runs = _real_check_setup(monkeypatch, [None, None])
    result = call(repo, check="pytest")
    assert result.startswith("REPAIRED_BY_LOCAL_AND_PASSED"), result
    # Initial check unwrapped (mode off); probe + repair re-run wrapped.
    assert len(wraps) == 2 and runs[0][0] != "sandboxed" and runs[1][0] == "sandboxed"
    assert rows()[-1]["sandboxed"] is True


def test_backend_lost_after_probe_restores_and_runs_nothing(repo, monkeypatch, repair_setup):
    _plain_generate(monkeypatch, repair_setup[0])
    monkeypatch.setenv("LLM_ROUTER_APPLY_SANDBOX", "off")
    lost = apply.check_sandbox.SandboxUnavailable("backend lost")
    wraps, runs = _real_check_setup(monkeypatch, [None, lost])
    result = call(repo, check="pytest")
    assert result.startswith("APPLIED_CHECK_FAILED") and result.endswith(HANDOFF), result
    assert "sandbox unavailable at re-run" in result
    assert len(runs) == 1  # only the initial check ran; local edits never ran unsandboxed
    assert (repo / "a.py").read_text() == "value = 2\n"


def test_successful_overwrite_records_no_cleanup_failopen(repo, monkeypatch):
    codes = []
    monkeypatch.setattr(apply, "_failopen", lambda code, exc: codes.append(code))
    assert call(repo).startswith("APPLIED")
    assert (repo / "a.py").read_text() == "value = 2\n"
    assert "CHZ-FO-APPLY-TEMP-CLEANUP" not in codes
    assert not [p for p in repo.iterdir() if p.name.startswith(".llm-apply-")]


@pytest.mark.parametrize("status,trigger,program,local", [
    ("APPLIED_AND_PASSED", None, 3, 0),
    ("REPAIRED_BY_LOCAL_AND_PASSED", "B", 3, 1),
    ("REPAIRED_BY_LOCAL_AND_PASSED", "A", 0, 1),
    ("REJECTED", "A", 0, 0),
    ("APPLIED_NOT_CHECKED", None, 0, 0),
    ("APPLIED_CHECK_FAILED", "B", 0, 0),
])
def test_credit_matrix(repo, monkeypatch, repair_setup, status, trigger, program, local):
    edits = [edit(), edit(old="value = 2", new="value = 4")]
    if trigger == "A":
        edits[0] = edit(old="missing")
    check = "" if status == "APPLIED_NOT_CHECKED" else "pytest"
    if status == "APPLIED_AND_PASSED":
        monkeypatch.setattr(apply, "_run_check", lambda *a, **k: (0, "check: test\npassed", False))
    elif status in {"REJECTED", "APPLIED_CHECK_FAILED"}:
        # Failed repair earns nothing, whether or not the program applied the batch.
        monkeypatch.setattr(repair_setup[0], "generate_edits", lambda *a, **k: (None, [], "failed"))
    result = call(repo, edits, check)
    lines = result.splitlines()
    assert lines[0] == status
    assert lines[1].startswith("[llm_apply credit] ")
    credit = json.loads(lines[1][len("[llm_apply credit] "):])
    expected_files = [] if status == "REJECTED" else [str((repo / "a.py").resolve())]
    assert credit == {"program": program, "local": local, "files": expected_files}
    assert list(credit) == ["program", "local", "files"]
    row = rows()[-1]
    assert (row["program_replaced"], row["local_replaced"], row["files"]) == (program, local, expected_files)
    assert row["repair_trigger"] == trigger


def test_credit_files_are_sorted_actual_changes(repo, monkeypatch):
    (repo / "z.py").write_text("value = 1\n")
    (repo / "unchanged.py").write_text("value = 1\n")
    runner(repo, monkeypatch, "")
    monkeypatch.setattr(apply, "_run_check", lambda *a, **k: (0, "check: test\npassed", False))
    result = call(repo, [edit("z.py"), edit(), edit("unchanged.py", new="value = 1")], "pytest")
    credit = json.loads(result.splitlines()[1].split("] ", 1)[1])
    assert credit == {"program": 4, "local": 0, "files": sorted(str((repo / f).resolve()) for f in ("z.py", "a.py"))}
