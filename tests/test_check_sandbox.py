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
    assert "check: NOT sandboxed (probe failed deliberately)" in result
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
        assert '/private/tmp' not in profile and '/private/var/folders' not in profile
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
