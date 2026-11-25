"""Real platform denial checks paired with unsandboxed positive controls."""
import errno
import glob
import json
import os
from pathlib import Path
import platform
import socket
import shutil
import subprocess
import sys

import pytest

from llm_router import apply_tool as apply
from llm_router import check_sandbox as sandbox


@pytest.fixture
def mac(tmp_path, monkeypatch):
    if platform.system() != "Darwin":
        pytest.skip("behavioural containment tests require macOS sandbox-exec")
    reason = sandbox._probe("Darwin", "/usr/bin/sandbox-exec")
    if reason:
        pytest.skip(reason)
    root = tmp_path / "root"
    home = tmp_path / "fake-home"
    root.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return root, home


@pytest.fixture
def linux(tmp_path, monkeypatch):
    if platform.system() != "Linux":
        pytest.skip("behavioural containment tests require Linux bubblewrap")
    backend = shutil.which("bwrap")
    if not backend:
        pytest.skip("bwrap missing: real bubblewrap probe unavailable")
    reason = sandbox._probe("Linux", backend)
    if reason:
        pytest.skip(reason)
    root = tmp_path / "root"
    home = tmp_path / "fake-home"
    root.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return root, home


def run_python(root, code, contained):
    argv = [sys.executable, "-c", code]
    cleanup = None
    if contained:
        argv, cleanup = sandbox.wrap(argv, root)
    try:
        return subprocess.run(argv, cwd=root, env=sandbox.environment(), capture_output=True, text=True, timeout=15)
    finally:
        if cleanup:
            cleanup()


def test_macos_writable_root_private_tmp_and_cleanup(mac):
    root, _ = mac
    result = run_python(root, "import os; from pathlib import Path; "
                        "Path('inside').write_text('yes'); "
                        "p=Path(os.environ['TMPDIR']); (p/'inside').write_text('yes'); print(p)", True)
    assert result.returncode == 0, result.stderr
    assert (root / "inside").read_text() == "yes"
    assert not Path(result.stdout.strip()).exists()


@pytest.mark.parametrize("action", ["write", "read"])
def test_macos_home_canary_denied(mac, action):
    root, home = mac
    canary = home / "canary"
    canary.write_text("secret")
    code = f"from pathlib import Path; p=Path({str(canary)!r}); "
    code += "p.write_text('changed')" if action == "write" else "print(p.read_text())"
    baseline = run_python(root, code, False)
    assert baseline.returncode == 0, baseline.stderr
    canary.write_text("secret")
    result = run_python(root, code, True)
    assert result.returncode != 0 and ("PermissionError" in result.stderr), result
    assert canary.read_text() == "secret"


@pytest.mark.parametrize("port", [0, 11434])
def test_macos_tcp_denied_with_positive_control(mac, port):
    root, _ = mac
    listener = socket.socket()
    try:
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen(4)
            port = listener.getsockname()[1]
        except OSError as exc:
            # An existing Ollama listener is a usable positive control too.
            if port != 11434 or exc.errno != errno.EADDRINUSE:
                raise
        host = "localhost" if port == 11434 else "127.0.0.1"
        code = f"import socket; socket.create_connection(({host!r}, {port}), timeout=2).close()"
        baseline = run_python(root, code, False)
        assert baseline.returncode == 0, baseline.stderr
        result = run_python(root, code, True)
        assert result.returncode != 0 and "PermissionError" in result.stderr, result
    finally:
        listener.close()


def test_macos_osascript_exec_denied(mac):
    root, _ = mac
    code = "import subprocess; subprocess.run(['/usr/bin/osascript', '-e', 'return 1'], check=True)"
    baseline = run_python(root, code, False)
    assert baseline.returncode == 0, baseline.stderr
    result = run_python(root, code, True)
    assert result.returncode != 0 and "PermissionError" in result.stderr, result



def test_macos_copied_osascript_service_denied(mac):
    root, _ = mac
    copied = root / "osascript-copy"
    # copy2 would try to copy the system file flags, which macOS refuses.
    shutil.copyfile("/usr/bin/osascript", copied)
    copied.chmod(0o755)
    argv = [str(copied), "-e", "return 1"]
    baseline = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=15)
    if baseline.returncode:
        pytest.skip(f"copied osascript positive control failed: {baseline.stderr}")
    wrapped, cleanup = sandbox.wrap(argv, root)
    try:
        result = subprocess.run(wrapped, cwd=root, capture_output=True, text=True, timeout=15)
        assert result.returncode != 0, result.stdout + result.stderr
    finally:
        cleanup()


def test_macos_tty_writes_denied_with_positive_control(mac):
    root, _ = mac
    available = []
    for path in glob.glob("/dev/tty*"):
        code = f"import os; fd=os.open({path!r}, os.O_WRONLY | os.O_NONBLOCK); os.close(fd)"
        baseline = run_python(root, code, False)
        if baseline.returncode == 0:
            available.append((path, code))
    if not available:
        pytest.skip("no tty available for outside-sandbox write-open positive control")
    for path, code in available:
        result = run_python(root, code, True)
        assert result.returncode != 0 and "PermissionError" in result.stderr, (path, result)

def test_macos_pytest_passes(mac):
    root, _ = mac
    (root / "test_tiny.py").write_text("def test_tiny():\n    assert 2 + 2 == 4\n")
    argv, cleanup = sandbox.wrap([sys.executable, "-m", "pytest", "test_tiny.py", "-q", "-p", "no:cacheprovider"], root)
    try:
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
    finally:
        cleanup()


@pytest.mark.parametrize("bad", ['"', '\\', '\n', '\0'])
def test_path_injection_rejected(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")
    with pytest.raises(sandbox.SandboxUnavailable, match="unsafe character"):
        sandbox.wrap([sys.executable], str(tmp_path / ("bad" + bad)))


@pytest.mark.parametrize("mode,forced,ran", [("require", False, False), ("auto", False, True), ("off", True, False), ("off", False, True)])
def test_probe_failure_policy(tmp_path, monkeypatch, mode, forced, ran):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sandbox, "_probe", lambda *a: "probe failed deliberately")
    monkeypatch.setenv("LLM_ROUTER_APPLY_SANDBOX", mode)
    seen = []
    monkeypatch.setattr(apply, "_execute_check", lambda *a: (seen.append(a), (0, "ok"))[1])
    if ran:
        rc, text, contained = apply._run_check([sys.executable], tmp_path, require_sandbox=forced)
        assert rc == 0 and not contained and "check: NOT sandboxed (" in text
    else:
        with pytest.raises(sandbox.SandboxUnavailable, match="probe failed deliberately"):
            apply._run_check([sys.executable], tmp_path, require_sandbox=forced)
    assert bool(seen) == ran


@pytest.mark.parametrize("mode", ["require", "auto"])
def test_unavailable_reply_and_ledger(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("LLM_ROUTER_APPLY", "on")
    monkeypatch.setenv("LLM_ROUTER_APPLY_SANDBOX", mode)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "ledger"))
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sandbox, "_probe", lambda *a: "probe failed deliberately")
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.py").write_text("x = 1\n")
    seen = []
    monkeypatch.setattr(apply, "_execute_check", lambda *a: (seen.append(a), (0, "ok"))[1])
    result = apply.llm_apply(json.dumps([dict(file="a.py", old_string="1", new_string="2")]), "python -m pytest", str(root))
    assert "NOT sandboxed (probe failed deliberately)" in result if mode == "auto" else \
        "Check not run: no usable sandbox (probe failed deliberately)" in result
    row = json.loads((tmp_path / "ledger/apply_ledger.jsonl").read_text())
    assert row["sandboxed"] is False
    assert row["check_ran"] == (mode == "auto") == bool(seen)
    if mode == "require":
        assert "Check rc=not run" in result


def test_probe_failure_cached(monkeypatch):
    sandbox._probe.cache_clear()
    calls = []
    def fail(*a, **kw):
        calls.append(a)
        raise OSError("cannot start")
    monkeypatch.setattr(sandbox.subprocess, "run", fail)
    try:
        assert "cannot start" in sandbox._probe("Darwin", "/missing")
        assert "cannot start" in sandbox._probe("Darwin", "/missing")
        assert len(calls) == 1
    finally:
        sandbox._probe.cache_clear()


def test_linux_argv_and_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "_probe", lambda *a: "")
    monkeypatch.setenv("SECRET_CANARY", "private")
    argv, cleanup = sandbox.wrap([sys.executable, "-m", "pytest"], tmp_path)
    try:
        for flag in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv", "--ro-bind", "--bind"):
            assert flag in argv
        assert "SECRET_CANARY" not in " ".join(argv)
        assert str(tmp_path.resolve()) in argv
    finally:
        cleanup()


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_missing_backend_refuses(tmp_path, monkeypatch, system):
    monkeypatch.setattr(sandbox.platform, "system", lambda: system)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.wrap([sys.executable], tmp_path)


def test_mac_profile_parameters_and_scrub(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sandbox, "_probe", lambda *a: "")
    monkeypatch.setenv("SECRET_CANARY", "private")
    unrelated = tmp_path / "unrelated-bin"
    unrelated.mkdir()
    monkeypatch.setenv("PATH", str(unrelated))
    argv, cleanup = sandbox.wrap([sys.executable], tmp_path)
    try:
        profile = argv[argv.index("-p") + 1]
        assert str(tmp_path) not in profile
        assert '(param "ROOT")' in profile and '(param "TEMP")' in profile
        assert '(deny network*)' in profile and "allow network" not in profile
        assert "SECRET_CANARY" not in " ".join(argv)
        runtime_values = [arg.split("=", 1)[1] for arg in argv if arg.startswith("R") and "=" in arg and arg[1:2].isdigit()]
        assert runtime_values and str(unrelated) not in runtime_values
        home = Path(os.environ["HOME"]).resolve()
        assert all(Path(grant) != home and Path(grant) not in home.parents
                   for grant in runtime_values)
        assert str(tmp_path) not in runtime_values
        # S1: these paths now appear in the read DENY rule; they must never be in an allow rule.
        allows = " ".join(line for line in profile.splitlines() if line.startswith("(allow"))
        assert '/private/tmp' not in allows and '/private/var/folders' not in allows
        assert profile.index('(subpath "/private/tmp")') < profile.index('(allow file-read-data')
    finally:
        cleanup()


def test_sandbox_reply_and_ledger_wiring_only(tmp_path, monkeypatch):
    """Check reply/ledger wiring only; wrapping and execution are mocked."""
    monkeypatch.setenv("LLM_ROUTER_APPLY", "on")
    monkeypatch.setenv("LLM_ROUTER_APPLY_SANDBOX", "require")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "ledger"))
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.py").write_text("x = 1\n")
    cleaned = []
    monkeypatch.setattr(sandbox, "wrap", lambda argv, root: (argv, lambda: cleaned.append(True)))
    monkeypatch.setattr(sandbox, "backend_name", lambda: "macOS sandbox-exec")
    monkeypatch.setattr(apply, "_execute_check", lambda *a: (0, "ok"))
    result = apply.llm_apply(json.dumps([dict(file="a.py", old_string="1", new_string="2")]), "python -m pytest", str(root))
    assert "check: sandboxed (macOS sandbox-exec)" in result
    assert cleaned == [True]
    row = json.loads((tmp_path / "ledger/apply_ledger.jsonl").read_text())
    assert row["sandboxed"] is True and row["check_ran"] is True


def test_start_failure_never_retries_unsandboxed(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_APPLY_SANDBOX", "auto")
    cleaned, calls = [], []
    monkeypatch.setattr(sandbox, "wrap", lambda argv, root: (argv, lambda: cleaned.append(True)))
    def fail(*args):
        calls.append(args)
        raise OSError("backend vanished")
    monkeypatch.setattr(apply, "_execute_check", fail)
    with pytest.raises(sandbox.SandboxUnavailable, match="sandbox failed to start"):
        apply._run_check([sys.executable], tmp_path)
    assert len(calls) == 1 and cleaned == [True]


def test_runtime_discovery_never_executes(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    base = tmp_path / "base"
    (venv / "bin").mkdir(parents=True)
    (base / "bin").mkdir(parents=True)
    interpreter = venv / "bin/python"
    interpreter.write_text("#!/" + str(base / "bin/python").lstrip("/") + "\n")
    (base / "bin/python").write_bytes(b"binary")
    (base / "lib/python3.12").mkdir(parents=True)
    (base / "lib/python3.12/os.py").write_text("")
    (venv / "pyvenv.cfg").write_text(f"home = {base / 'bin'}\n")
    interpreter.chmod(0o755)
    def forbidden(*args, **kwargs):
        pytest.fail("runtime discovery executed a subprocess")
    monkeypatch.setattr(sandbox.subprocess, "run", forbidden)
    grants = sandbox._runtime_dirs(interpreter)
    assert str(venv.resolve()) in grants and str(base.resolve()) in grants
    assert all(Path(grant).is_dir() for grant in grants)


def test_python_symlink_venv_and_base_prefix(tmp_path):
    base = tmp_path / "base"
    venv = tmp_path / "venv"
    (base / "bin").mkdir(parents=True)
    (venv / "bin").mkdir(parents=True)
    interpreter = base / "bin/python3"
    interpreter.write_bytes(b"binary")
    (base / "lib/python3.12").mkdir(parents=True)
    (base / "lib/python3.12/os.py").write_text("")
    link = venv / "bin/python"
    link.symlink_to(interpreter)
    (venv / "pyvenv.cfg").write_text(f"home = {base / 'bin'}\n")
    grants = sandbox._runtime_dirs(link)
    assert {str(base.resolve()), str(venv.resolve())} <= set(grants)
    assert str(base.resolve()) in sandbox._runtime_dirs(interpreter)


def test_home_prefix_runtime_refused(tmp_path, monkeypatch):
    home = tmp_path / "fakehome"
    (home / "bin").mkdir(parents=True)
    executable = home / "bin/npm"
    executable.write_bytes(b"trusted executable")
    executable.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(sandbox.SandboxUnavailable, match=f"runtime dir too broad: {home.resolve()}"):
        sandbox._runtime_dirs(executable)


@pytest.mark.parametrize("grant", ["/", "/Users", "/home"])
def test_broad_runtime_grants_refused(grant):
    with pytest.raises(sandbox.SandboxUnavailable, match="runtime dir too broad"):
        sandbox._runtime_grant(grant)


def test_linux_socket_scan_refuses_before_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    def forbidden(*args):
        pytest.fail("backend probed before socket refusal")
    monkeypatch.setattr(sandbox, "_probe", forbidden)
    monkeypatch.chdir(tmp_path)
    with socket.socket(socket.AF_UNIX) as listener:
        try:
            listener.bind("host.sock")
        except PermissionError as exc:
            pytest.skip(f"host sandbox prevents Unix socket positive control: {exc}")
        with pytest.raises(sandbox.SandboxUnavailable, match="socket in ROOT"):
            sandbox.wrap([sys.executable], tmp_path)


def test_socket_scan_does_not_follow_symlinks(tmp_path, monkeypatch):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(outside)
    with socket.socket(socket.AF_UNIX) as listener:
        try:
            listener.bind("host.sock")
        except PermissionError as exc:
            pytest.skip(f"host sandbox prevents Unix socket positive control: {exc}")
        sandbox._scan_root_sockets(root)


def test_socket_scan_entry_limit(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from contextlib import nullcontext
    import stat

    entry = SimpleNamespace(stat=lambda **kw: SimpleNamespace(st_mode=stat.S_IFREG))
    monkeypatch.setattr(sandbox.os, "scandir", lambda path: nullcontext(iter([entry] * 50_001)))
    with pytest.raises(sandbox.SandboxUnavailable, match="50000 entries"):
        sandbox._scan_root_sockets(tmp_path)


def test_socket_scan_rejects_socket_entry(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from contextlib import nullcontext
    import stat

    entry = SimpleNamespace(path=str(tmp_path / "host.sock"),
                            stat=lambda **kw: SimpleNamespace(st_mode=stat.S_IFSOCK))
    monkeypatch.setattr(sandbox.os, "scandir", lambda path: nullcontext(iter([entry])))
    with pytest.raises(sandbox.SandboxUnavailable, match="socket in ROOT"):
        sandbox._scan_root_sockets(tmp_path)


def test_linux_writable_root_private_tmp_and_cleanup(linux):
    root, _ = linux
    result = run_python(root, "import os; from pathlib import Path; "
                        "Path('inside').write_text('yes'); "
                        "p=Path(os.environ['TMPDIR']); (p/'inside').write_text('yes'); print(p)", True)
    assert result.returncode == 0, result.stderr
    assert (root / "inside").read_text() == "yes"
    assert not Path(result.stdout.strip()).exists()


def test_linux_outside_write_denied(linux):
    root, _ = linux
    outside = root.parent / "outside"
    outside.mkdir()
    canary = outside / "canary"
    code = f"from pathlib import Path; Path({str(canary)!r}).write_text('changed')"
    baseline = run_python(root, code, False)
    assert baseline.returncode == 0, baseline.stderr
    canary.write_text("unchanged")
    result = run_python(root, code, True)
    assert result.returncode != 0 and any(
        error in result.stderr for error in ("FileNotFoundError", "PermissionError", "OSError")
    ), result
    assert canary.read_text() == "unchanged"


def test_linux_home_read_denied(linux):
    root, home = linux
    canary = home / "canary"
    canary.write_text("secret")
    canary.chmod(0o644)
    code = "import os; from pathlib import Path; print((Path(os.environ['HOME'])/'canary').read_text())"
    baseline = run_python(root, code, False)
    assert baseline.returncode == 0 and baseline.stdout.strip() == "secret", baseline
    result = run_python(root, code, True)
    assert result.returncode != 0 and any(
        error in result.stderr for error in ("FileNotFoundError", "PermissionError")
    ), result
    assert "secret" not in result.stdout


def test_linux_tcp_denied_with_positive_control(linux):
    root, _ = linux
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        code = f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=2).close()"
        baseline = run_python(root, code, False)
        assert baseline.returncode == 0, baseline.stderr
        result = run_python(root, code, True)
        assert result.returncode != 0 and any(
            error in result.stderr for error in ("ConnectionRefusedError", "TimeoutError", "OSError", "PermissionError")
        ), result


def test_linux_unix_socket_refuses_with_positive_control(linux, monkeypatch):
    root, _ = linux
    # Relative pathname keeps the socket below AF_UNIX's pathname length limit.
    monkeypatch.chdir(root)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind("host.sock")
        listener.listen(4)
        code = "import socket; s=socket.socket(socket.AF_UNIX); s.connect('host.sock'); s.close()"
        baseline = run_python(root, code, False)
        assert baseline.returncode == 0, baseline.stderr
        with pytest.raises(sandbox.SandboxUnavailable, match="socket in ROOT"):
            sandbox.wrap([sys.executable, "-c", code], root)


def test_linux_tty_write_denied_with_positive_control(linux):
    root, _ = linux
    code = "import os; fd=os.open('/dev/tty', os.O_WRONLY | os.O_NONBLOCK); os.close(fd)"
    baseline = run_python(root, code, False)
    if baseline.returncode != 0:
        pytest.skip(f"/dev/tty write-open unavailable outside sandbox: {baseline.stderr}")
    result = run_python(root, code, True)
    assert result.returncode != 0 and any(
        error in result.stderr for error in ("FileNotFoundError", "PermissionError", "OSError")
    ), result


def test_linux_pytest_passes(linux):
    root, _ = linux
    (root / "test_tiny.py").write_text("def test_tiny():\n    assert 2 + 2 == 4\n")
    argv, cleanup = sandbox.wrap([sys.executable, "-m", "pytest", "test_tiny.py", "-q", "-p", "no:cacheprovider"], root)
    try:
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
    finally:
        cleanup()


@pytest.mark.parametrize("mode", ["require", "auto"])
def test_linux_apparmor_probe_failure_policy(tmp_path, monkeypatch, mode):
    error = "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"
    sandbox._probe.cache_clear()
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 1, b"", error.encode()))
    monkeypatch.setenv("LLM_ROUTER_APPLY_SANDBOX", mode)
    calls = []
    monkeypatch.setattr(apply, "_execute_check", lambda *a: (calls.append(a), (0, "ok"))[1])
    try:
        with pytest.raises(sandbox.SandboxUnavailable, match="Failed RTM_NEWADDR"):
            sandbox.wrap([sys.executable], tmp_path)
        if mode == "require":
            with pytest.raises(sandbox.SandboxUnavailable, match="Failed RTM_NEWADDR"):
                apply._run_check([sys.executable], tmp_path)
            assert not calls
        else:
            rc, text, contained = apply._run_check([sys.executable], tmp_path)
            assert rc == 0 and not contained and len(calls) == 1
            assert "NOT sandboxed" in text and error in text
    finally:
        sandbox._probe.cache_clear()


def _fake_install(prefix):
    (prefix / "bin").mkdir(parents=True)
    (prefix / "lib/python3.12").mkdir(parents=True)
    (prefix / "lib/python3.12/os.py").write_text("")
    (prefix / "bin/python3").write_bytes(b"binary")
    (prefix / "bin/python3").chmod(0o755)
    return prefix


def _repo_venv(root, home_value, base):
    (root / ".venv/bin").mkdir(parents=True)
    interpreter = root / ".venv/bin/python"
    interpreter.write_text(f"#!{base / 'bin/python3'}\n")
    interpreter.chmod(0o755)
    (root / ".venv/pyvenv.cfg").write_text(f"home = {home_value}\n")
    return interpreter


def test_repo_pyvenv_home_cannot_grant_unrelated_home_subtree(tmp_path, monkeypatch):
    home = tmp_path / "fakehome"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh/id_rsa").write_text("secret")
    monkeypatch.setenv("HOME", str(home))
    base = _fake_install(tmp_path / "install")
    root = tmp_path / "repo"
    interpreter = _repo_venv(root, home / ".ssh/bin", base)
    with pytest.raises(sandbox.SandboxUnavailable, match="non-runtime dir"):
        sandbox._runtime_dirs(interpreter, root)
    # Positive control: the same repo pointing at a real-looking install is granted.
    (root / ".venv/pyvenv.cfg").write_text(f"home = {base / 'bin'}\n")
    grants = sandbox._runtime_dirs(interpreter, root)
    assert str(base.resolve()) in grants
    assert not any(str((home / ".ssh").resolve()) in g for g in grants)


def test_repo_symlinked_executable_cannot_grant_unrelated_dir(tmp_path, monkeypatch):
    home = tmp_path / "fakehome"
    (home / ".ssh").mkdir(parents=True)
    tool = home / ".ssh/tool"
    tool.write_bytes(b"binary")
    tool.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "repo"
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/pytest").symlink_to(tool)
    with pytest.raises(sandbox.SandboxUnavailable, match="non-runtime dir"):
        sandbox._runtime_dirs(root / ".venv/bin/pytest", root)
    # Positive control: a symlink into a real-looking install is granted.
    base = _fake_install(tmp_path / "install")
    (root / ".venv/bin/pytest").unlink()
    (root / ".venv/bin/pytest").symlink_to(base / "bin/python3")
    assert str((base / "bin").resolve()) in sandbox._runtime_dirs(root / ".venv/bin/pytest", root)


def test_repo_bin_symlink_does_not_erase_provenance(tmp_path, monkeypatch):
    home = tmp_path / "fakehome"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    outside_bin = tmp_path / "sysbin"
    outside_bin.mkdir()
    (outside_bin / "python3").write_bytes(b"binary")
    (outside_bin / "python3").chmod(0o755)
    root = tmp_path / "repo"
    (root / ".venv").mkdir(parents=True)
    (root / ".venv/bin").symlink_to(outside_bin)
    (root / ".venv/pyvenv.cfg").write_text(f"home = {home / '.ssh/bin'}\n")
    assert sandbox._repo_owned(Path(os.path.abspath(root / ".venv/bin/python3")), Path(sandbox._path(root)))
    with pytest.raises(sandbox.SandboxUnavailable, match="non-runtime dir"):
        sandbox._runtime_dirs(root / ".venv/bin/python3", root)
    # Same layout without a repository ROOT (PATH-trusted) still validates pyvenv home.
    with pytest.raises(sandbox.SandboxUnavailable, match="non-runtime dir"):
        sandbox._runtime_dirs(root / ".venv/bin/python3")


def test_dotdot_executable_path_refused(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(sandbox.SandboxUnavailable, match=r"contains '\.\.'"):
        sandbox._runtime_dirs(str(root / ".." / "elsewhere" / "runner"), root)


def test_homebrew_formula_prefix_is_a_runtime(tmp_path):
    formula = tmp_path / "Cellar/python@3.12/3.12.7"
    stdlib = formula / "Frameworks/Python.framework/Versions/3.12/lib/python3.12"
    stdlib.mkdir(parents=True)
    assert not sandbox._is_runtime_prefix(formula)
    (stdlib / "os.py").write_text("")
    assert sandbox._is_runtime_prefix(formula)


def test_symlink_hops_cover_uv_minor_version_link(tmp_path):
    store = tmp_path / "uv/python"
    real = store / "cpython-3.12.16-linux"
    (real / "bin").mkdir(parents=True)
    (real / "bin/python3.12").write_bytes(b"binary")
    (store / "cpython-3.12-linux").symlink_to(real)
    venv = tmp_path / "proj/.venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(store / "cpython-3.12-linux/bin/python3.12")
    (venv / "pyvenv.cfg").write_text(f"home = {store / 'cpython-3.12-linux/bin'}\n")
    links = {link for path in sandbox._exec_paths(venv / "bin/python") for link, _ in sandbox._symlink_hops(path)}
    assert str(venv / "bin/python") in links
    assert str(store / "cpython-3.12-linux") in links
    # Positive control: a plain file has no hops of its own.
    assert not any(link.startswith(str(real)) for path in [real / "bin/python3.12"]
                   for link, _ in sandbox._symlink_hops(path))


def test_symlink_hops_refuse_loops(tmp_path):
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    with pytest.raises(sandbox.SandboxUnavailable, match="too many symlinks"):
        sandbox._symlink_hops(tmp_path / "a/python")


def test_symlink_hops_follow_link_before_dotdot(tmp_path):
    deep = tmp_path / "other/deep"
    deep.mkdir(parents=True)
    runtime = tmp_path / "other/runtime/bin"
    runtime.mkdir(parents=True)
    (runtime / "python").write_bytes(b"binary")
    (tmp_path / "store").mkdir()
    (tmp_path / "store/alias").symlink_to(deep)
    (tmp_path / "entry").symlink_to(tmp_path / "store/alias/../runtime/bin/python")
    links = [link for link, _ in sandbox._symlink_hops(tmp_path / "entry")]
    assert str(tmp_path / "store/alias") in links
    # Positive control: the resolution lands where the OS resolves it.
    assert (tmp_path / "entry").resolve() == (runtime / "python").resolve()


# --- fail-closed fixes M1-M4 (REVIEW-SANDBOX-SECURITY-20261125) ---

def test_default_mode_is_require_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_APPLY_SANDBOX", raising=False)
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    seen = []
    monkeypatch.setattr(apply, "_execute_check", lambda *a: (seen.append(a), (0, "ok"))[1])
    with pytest.raises(sandbox.SandboxUnavailable, match="bwrap missing"):
        apply._run_check([sys.executable], tmp_path)
    assert not seen


@pytest.mark.parametrize("which", ["home", "parent", "root"])
@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_root_covering_home_refused(tmp_path, monkeypatch, system, which):
    home = tmp_path / "users" / "me"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sandbox.platform, "system", lambda: system)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "_probe", lambda *a: "")
    root = {"home": home, "parent": home.parent, "root": Path("/")}[which]
    with pytest.raises(sandbox.SandboxUnavailable, match="root covers HOME"):
        sandbox.wrap([sys.executable], root)


def test_root_beside_home_allowed(tmp_path, monkeypatch):
    home = tmp_path / "users" / "me"
    root = tmp_path / "work" / "repo"
    home.mkdir(parents=True)
    root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "_probe", lambda *a: "")
    argv, cleanup = sandbox.wrap([sys.executable], root)
    cleanup()
    assert str(root.resolve()) in argv


def test_linux_git_dir_read_only_after_root_bind(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "_probe", lambda *a: "")
    (tmp_path / ".git").mkdir()
    argv, cleanup = sandbox.wrap([sys.executable], tmp_path)
    cleanup()
    git = str(tmp_path.resolve()) + "/.git"
    i = argv.index("--ro-bind", argv.index("--bind"))
    assert argv[i:i + 3] == ["--ro-bind", git, git]
    assert argv.index("--bind") < i


def test_mac_profile_denies_git_writes_last(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sandbox, "_probe", lambda *a: "")
    argv, cleanup = sandbox.wrap([sys.executable], tmp_path)
    cleanup()
    profile = argv[argv.index("-p") + 1]
    assert f"GIT={tmp_path.resolve()}/.git" in argv
    assert profile.index('(deny file-write* (subpath (param "GIT")))') > profile.index('(allow file-write* (subpath (param "ROOT"))')


def test_macos_git_dir_write_denied_source_write_allowed(mac):
    root, _ = mac
    (root / ".git" / "hooks").mkdir(parents=True)
    code = ("import pathlib\n"
            "pathlib.Path('src.txt').write_text('ok')\n"
            "for p in ('.git/hooks/pre-commit', '.git/config'):\n"
            "    try:\n        pathlib.Path(p).write_text('x'); print('WROTE', p)\n"
            "    except OSError: print('DENIED', p)\n")
    argv, cleanup = sandbox.wrap([sys.executable, "-c", code], root)
    try:
        out = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=60).stdout
    finally:
        cleanup()
    assert "DENIED .git/hooks/pre-commit" in out and "DENIED .git/config" in out
    assert (root / "src.txt").read_text() == "ok" and not (root / ".git/config").exists()


def test_docstring_names_ci_job_not_unverified():
    doc = sandbox.__doc__
    assert "unverified" not in doc and "linux-sandbox" in doc


# --- review round 2: identity-based ROOT check ---

def _wrap_linux(monkeypatch, root):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "_probe", lambda *a: "")
    return sandbox.wrap([sys.executable], root)


@pytest.mark.parametrize("which", ["home", "parent"])
def test_macos_case_variant_root_of_home_refused(tmp_path, monkeypatch, which):
    home = tmp_path / "Users" / "Me"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    target = home if which == "home" else home.parent
    variant = Path(str(target.resolve()).swapcase())
    if not variant.exists() or variant == target.resolve():
        pytest.skip("case-sensitive filesystem: case variants name different directories")
    with pytest.raises(sandbox.SandboxUnavailable, match="root covers HOME"):
        _wrap_linux(monkeypatch, variant)


def test_relative_root_covering_home_refused(tmp_path, monkeypatch):
    home = tmp_path / "a" / "b" / "me"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path / "a" / "b")
    for rel in (".", "..", "me", "../b/me"):
        with pytest.raises(sandbox.SandboxUnavailable, match="root covers HOME"):
            _wrap_linux(monkeypatch, rel)


def test_home_unset_falls_back_to_pw_dir(tmp_path, monkeypatch):
    import pwd

    monkeypatch.delenv("HOME", raising=False)
    fake = tmp_path / "x" / "y" / "pwhome"
    fake.mkdir(parents=True)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: type("P", (), {"pw_dir": str(fake)})())
    assert "HOME" not in os.environ
    with pytest.raises(sandbox.SandboxUnavailable, match="root covers HOME"):
        _wrap_linux(monkeypatch, fake)


@pytest.mark.parametrize("root", ["/usr", "/etc", "/opt", "/"])
def test_system_roots_refused(tmp_path, monkeypatch, root):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    with pytest.raises(sandbox.SandboxUnavailable, match="root covers HOME or a system directory"):
        _wrap_linux(monkeypatch, root)


def test_refusal_message_names_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_APPLY", "on")
    monkeypatch.delenv("LLM_ROUTER_APPLY_SANDBOX", raising=False)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "ledger"))
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sandbox, "_probe", lambda *a: "probe failed deliberately")
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.py").write_text("x = 1\n")
    result = apply.llm_apply(json.dumps([dict(file="a.py", old_string="1", new_string="2")]), "python -m pytest", str(root))
    assert "no usable sandbox (probe failed deliberately)" in result
    assert "LLM_ROUTER_APPLY_SANDBOX=auto" in result


@pytest.mark.parametrize("root", ["/tmp", "/var", "/etc", "/private", "/private/etc", "/private/var", "/private/tmp"])
def test_private_alias_roots_refused(tmp_path, monkeypatch, root):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    with pytest.raises(sandbox.SandboxUnavailable, match="root covers HOME or a system directory"):
        _wrap_linux(monkeypatch, root)


def test_macos_symlink_to_private_var_refused(tmp_path, monkeypatch):
    if not Path("/private/var").is_dir():
        pytest.skip("no /private/var on this OS")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    link = tmp_path / "work" / "link"
    link.parent.mkdir()
    link.symlink_to("/private/var")
    with pytest.raises(sandbox.SandboxUnavailable, match="root covers HOME or a system directory"):
        _wrap_linux(monkeypatch, link)


@pytest.mark.parametrize("root", ["/System", "/System/Volumes", "/System/Volumes/Data", "/system/volumes/data"])
def test_system_volume_roots_refused(tmp_path, monkeypatch, root):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    with pytest.raises(sandbox.SandboxUnavailable, match="root covers HOME or a system directory"):
        _wrap_linux(monkeypatch, root)


def test_data_volume_prefix_stripped_before_home_compare(tmp_path, monkeypatch):
    # Isolates the /System/Volumes/Data strip only on Linux (no /System there, so the
    # ancestor walk falls back to string compare). On macOS the samefile walk already
    # refuses this case; the strip is defence in depth there.
    monkeypatch.setenv("HOME", "/System/Volumes/Data" + str(tmp_path / "h"))
    with pytest.raises(sandbox.SandboxUnavailable, match="root covers HOME or a system directory"):
        _wrap_linux(monkeypatch, tmp_path)


def test_normal_temp_repo_still_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    repo = tmp_path / "repo"
    repo.mkdir()
    argv, cleanup = _wrap_linux(monkeypatch, repo)
    cleanup()
    assert str(repo.resolve()) in argv


def test_macos_reads_outside_home_denied(mac, tmp_path):
    """S1: canaries in /Users/Shared, this user's tmp parent, /private/tmp, /Volumes stay unreadable."""
    import tempfile

    root, _ = mac
    made = []
    try:
        canaries = []
        for parent in (Path("/Users/Shared"), Path(tempfile.gettempdir()), Path("/private/tmp")):
            try:
                path = Path(tempfile.mkdtemp(prefix="s1-canary-", dir=parent)) / "secret"
            except OSError:
                continue
            made.append(path.parent)
            path.write_text("secret")
            canaries.append(path)
        assert any(str(c).startswith(("/private/var/folders", "/var/folders")) or
                   str(c).startswith(tempfile.gettempdir()) for c in canaries)
        for path in canaries:
            code = f"print(open({str(path)!r}).read())"
            assert run_python(root, code, False).returncode == 0   # positive control
            result = run_python(root, code, True)
            assert result.returncode != 0 and "PermissionError" in result.stderr, (path, result)
        # /Volumes: open the directory itself (no listing of its contents).
        code = "import os; os.close(os.open('/Volumes', os.O_RDONLY))"
        assert run_python(root, code, False).returncode == 0
        result = run_python(root, code, True)
        assert result.returncode != 0 and "PermissionError" in result.stderr, result
    finally:
        for parent in made:
            shutil.rmtree(parent, ignore_errors=True)


_LOOKUP = ("import ctypes,sys; lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib'); port=ctypes.c_uint32(); "
           "bp=ctypes.c_uint32.in_dll(lib,'bootstrap_port'); lib.bootstrap_look_up.argtypes=["
           "ctypes.c_uint32,ctypes.c_char_p,ctypes.POINTER(ctypes.c_uint32)]; "
           "sys.exit(lib.bootstrap_look_up(bp.value, sys.argv[1].encode(), ctypes.byref(port)))")


@pytest.mark.parametrize("service", ["com.apple.pasteboard.1", "com.apple.SecurityServer",
                                     "com.apple.coreservices.quarantine-resolver", "com.apple.xpc.smd"])
def test_macos_mach_lookup_denied_with_positive_control(mac, service):
    """S2: read-only bootstrap_look_up (no job submitted, nothing written) is refused inside."""
    root, _ = mac
    argv = [sys.executable, "-c", _LOOKUP, service]
    base = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    if base.returncode != 0:
        pytest.skip(f"{service} not reachable outside the sandbox on this macOS")
    wrapped, cleanup = sandbox.wrap(argv, root)
    try:
        result = subprocess.run(wrapped, cwd=root, capture_output=True, text=True, timeout=15)
    finally:
        cleanup()
    assert result.returncode != 0, f"{service} reachable inside the sandbox"


def _run_limited(tmp_path, code):
    return apply._execute_check([sys.executable, "-c", code], tmp_path)


def test_fsize_limit_stops_disk_fill(tmp_path, monkeypatch):
    """S3: a check writing without end is cut at RLIMIT_FSIZE (2 MB here, 256 MB shipped)."""
    monkeypatch.setattr(sandbox, "FSIZE_BYTES", 2 * 1024 * 1024)
    code = ("import os\nf=open('fill','wb')\nfor _ in range(4096):\n"
            "    try: f.write(b'y'*65536); f.flush()\n    except OSError as e: print('EFBIG', e.errno); break\n")
    rc, tail = _run_limited(tmp_path, code)
    assert "EFBIG" in tail and (tmp_path / "fill").stat().st_size <= 2 * 1024 * 1024
    assert sandbox.FSIZE_BYTES == 2 * 1024 * 1024


@pytest.mark.timing
@pytest.mark.skipif(platform.system() != "Darwin", reason="RLIMIT_NPROC is applied on macOS only (check_sandbox.py: Linux gets no NPROC limit)")
def test_macos_nproc_limit_bounds_fork_canary(tmp_path, monkeypatch):
    """S3: bounded fork canary (<=200 children, killed at the end) hits EAGAIN under the per-uid cap."""
    count = sandbox._uid_process_count()
    assert count, "ps must report this uid's processes"
    monkeypatch.setattr(sandbox, "NPROC_HEADROOM", 40)
    code = ("import os, time, signal\nkids=[]\nresult='no-limit'\n"
            "for _ in range(200):\n"
            "    try: pid=os.fork()\n    except OSError as e: result='EAGAIN' if e.errno==%d else repr(e)" % errno.EAGAIN + "; break\n"
            "    if pid==0:\n        time.sleep(30); os._exit(0)\n    kids.append(pid)\n"
            "for k in kids:\n    os.kill(k, signal.SIGKILL)\n    os.waitpid(k, 0)\n"
            "print(result, len(kids))\n")
    started = __import__("time").monotonic()
    rc, tail = _run_limited(tmp_path, code)
    assert "EAGAIN" in tail, tail
    assert int(tail.split()[-1]) < 200 and __import__("time").monotonic() - started < 30


@pytest.mark.timing
def test_output_cap_kills_flooding_check(tmp_path, monkeypatch):
    """S3: captured output is capped (1 MB here, 16 MB shipped) and the group is killed."""
    monkeypatch.setattr(apply, "_OUTPUT_CAP", 1024 * 1024)
    code = "import sys, time\nwhile True: sys.stdout.write('z'*65536); sys.stdout.flush(); time.sleep(0.002)\n"
    started = __import__("time").monotonic()
    rc, tail = _run_limited(tmp_path, code)
    assert rc == -9 and "output exceeded" in tail
    assert __import__("time").monotonic() - started < 30


def test_macos_limits_survive_sandbox_exec(mac, monkeypatch):
    """S3: the limits are inherited through sandbox-exec into the contained check."""
    monkeypatch.setattr(sandbox, "FSIZE_BYTES", 1024 * 1024)
    root, _ = mac
    code = "import resource; print(resource.getrlimit(resource.RLIMIT_FSIZE)[0], resource.getrlimit(resource.RLIMIT_NPROC)[0])"
    argv, cleanup = sandbox.wrap([sys.executable, "-c", code], root)
    try:
        rc, tail = apply._execute_check(argv, root)
    finally:
        cleanup()
    fsize, nproc = tail.split()
    assert rc == 0 and int(fsize) == 1024 * 1024 and 0 < int(nproc) < 100_000


def _detached_child_code(pidfile):
    return ("import os, sys, time\n"
            "if os.fork() == 0:\n"
            "    os.setsid()\n"
            f"    open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "    time.sleep(60); os._exit(0)\n"
            "time.sleep(0.5)\n")   # parent exits normally; the child is reparented to launchd


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_macos_setsid_child_killed_after_check(mac):
    """S4: a setsid() descendant outlives killpg; the marker sweep must kill it. Positive control first."""
    root, _ = mac
    pidfile = root / "child.pid"
    # positive control: the unmodified group teardown leaves the detached child running
    proc = subprocess.Popen([sys.executable, "-c", _detached_child_code(pidfile)], cwd=root, start_new_session=True)
    proc.wait()
    control = int(pidfile.read_text())
    try:
        assert _alive(control), "control: setsid child should survive killpg-only teardown"
    finally:
        os.kill(control, 9)
    pidfile.unlink()
    sandbox.set_marker("s4marker0123")
    argv, cleanup = sandbox.wrap([sys.executable, "-c", _detached_child_code(pidfile)], root)
    try:
        apply._execute_check(argv, root)
    finally:
        sandbox.set_marker(None)
        cleanup()
    child = int(pidfile.read_text())
    try:
        import time
        for _ in range(20):
            if not _alive(child):
                break
            time.sleep(0.1)
        assert not _alive(child)
    finally:
        if _alive(child):
            os.kill(child, 9)


def test_runtime_prefix_grants_only_bin_and_lib(tmp_path):
    """S7: the prefix root (and its .npmrc / etc/npmrc) is never granted; pyvenv.cfg is a single-file grant."""
    prefix = _fake_install(tmp_path / "toolchain")
    (prefix / ".npmrc").write_text("//registry:_authToken=canary")
    (prefix / "etc").mkdir()
    (prefix / "etc/npmrc").write_text("canary")
    (prefix / "pyvenv.cfg").write_text("home = x\n")
    root = tmp_path / "repo"
    root.mkdir()
    dirs, files = sandbox._narrow_grants([str(prefix.resolve())], root)
    resolved = prefix.resolve()
    assert str(resolved) not in dirs
    assert set(dirs) == {str(resolved / "bin"), str(resolved / "lib")}
    assert files == [str(resolved / "pyvenv.cfg")]


def test_non_prefix_and_in_root_grants_pass_through(tmp_path):
    bare = tmp_path / "just-bin"
    bare.mkdir()
    root = tmp_path / "repo"
    (root / ".venv/lib/python3.12").mkdir(parents=True)
    (root / ".venv/lib/python3.12/os.py").write_text("")
    dirs, files = sandbox._narrow_grants([str(bare.resolve()), str((root / ".venv").resolve())], root.resolve())
    assert set(dirs) == {str(bare.resolve()), str((root / ".venv").resolve())} and files == []


def test_macos_prefix_canary_unreadable_while_runtime_works(mac, tmp_path):
    """S7 behavioural: a venv-shaped prefix outside ROOT runs python, its .npmrc canary stays unreadable."""
    root, _ = mac
    prefix = tmp_path / "toolchain"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/python").symlink_to(sys.executable)
    (prefix / "pyvenv.cfg").write_text(f"home = {Path(sys.executable).resolve().parent}\n")
    canary = prefix / ".npmrc"
    canary.write_text("//registry:_authToken=canary")
    code = (f"import sys\nprint('prefix', sys.prefix)\n"
            f"try: print(open({str(canary)!r}).read())\nexcept PermissionError: print('DENIED')\n")
    argv, cleanup = sandbox.wrap([str(prefix / "bin/python"), "-c", code], root)
    try:
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=30)
    finally:
        cleanup()
    assert result.returncode == 0, result.stderr
    assert "DENIED" in result.stdout and "authToken" not in result.stdout


def test_nproc_limit_is_macos_only(monkeypatch):
    """S3: Linux counts threads per uid (ps counts processes), so only FSIZE is set there."""
    import resource

    calls = []
    monkeypatch.setattr(resource, "setrlimit", lambda which, value: calls.append(which))
    monkeypatch.setattr(sandbox, "_uid_process_count", lambda: 500)
    for system, expect_nproc in (("Linux", False), ("Darwin", True)):
        calls.clear()
        monkeypatch.setattr(sandbox.platform, "system", lambda s=system: s)
        sandbox.resource_limiter()()
        assert (resource.RLIMIT_NPROC in calls) is expect_nproc
        assert resource.RLIMIT_FSIZE in calls
