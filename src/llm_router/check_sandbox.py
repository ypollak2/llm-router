"""Contain only the check subprocess, never edit application or host-side Git.

macOS uses (allow default): deny-default breaks pytest on current macOS. Reads
outside HOME remain possible. The short Mach broker denylist is best-effort,
not a complete XPC boundary. Descendants inherit confinement, but setsid can
escape the caller's process-group teardown. This is not a VM/resource boundary.
Executables and their interpreter installations must be trusted.
The Linux backend is unverified on a real Linux host.
"""
from __future__ import annotations

from functools import lru_cache
import glob
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import tempfile


class SandboxUnavailable(RuntimeError):
    """No usable containment backend; callers must explicitly choose fallback."""


ENV_ALLOWLIST = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TERM", "TMPDIR",
                 "VIRTUAL_ENV", "PYTHONDONTWRITEBYTECODE", "CI", "NO_COLOR"}


def environment():
    return {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST or k.startswith("LC_")}


def _path(value):
    value = os.fspath(value)
    if any(c in value for c in ('"', '\\', '\n', '\r', '\0')):
        raise SandboxUnavailable("unsafe character in sandbox path")
    resolved = str(Path(value).resolve())
    if any(c in resolved for c in ('"', '\\', '\n', '\r', '\0')):
        raise SandboxUnavailable("unsafe character in resolved sandbox path")
    return resolved


def _runtime_grant(path):
    """Reject directory grants that could expose a user's entire home."""
    import pwd

    path = Path(_path(path))
    homes = {Path(_path(Path.home())), Path(_path(pwd.getpwuid(os.getuid()).pw_dir))}
    if (not path.is_dir() or path in {Path(p).resolve() for p in ("/", "/Users", "/home")}
            or any(path == home or path in home.parents for home in homes)):
        raise SandboxUnavailable(f"runtime dir too broad: {path}")
    return str(path)


def _interpreter_dirs(interpreter):
    """Inspect installation metadata without ever executing the interpreter."""
    original = Path(os.path.abspath(interpreter))
    resolved = Path(_path(original))
    for candidate in (original, resolved):
        root = candidate.parent.parent
        config = root / "pyvenv.cfg"
        if config.is_file():
            for line in config.read_text().splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() == "home" and value.strip():
                    return {_runtime_grant(root), _runtime_grant(Path(value.strip()).parent)}
            raise SandboxUnavailable(f"Python venv home missing: {config}")
    return {_runtime_grant(resolved.parent.parent)}


def _runtime_dirs(executable):
    """Derive directory access from executable/shebang metadata only."""
    original = Path(os.path.abspath(executable))
    resolved = Path(_path(original))
    dirs = {_runtime_grant(resolved.parent)}
    if original.name.startswith("python"):
        dirs.update(_interpreter_dirs(original))
    with resolved.open("rb") as stream:
        first = stream.readline(4096).decode("utf-8", errors="replace").strip()
    if first.startswith("#!/"):
        parts = first[2:].split()
        if Path(parts[0]).name == "env":
            if len(parts) != 2 or parts[1] != "node":
                raise SandboxUnavailable("unsupported env shebang interpreter")
            node = next((parent / "bin/node" for parent in resolved.parents
                         if (parent / "bin/node").is_file()), None)
            if node is None:
                raise SandboxUnavailable("node interpreter unavailable in executable prefix")
            dirs.update(_interpreter_dirs(node))
        else:
            dirs.update(_interpreter_dirs(parts[0]))
    if original.name in {"npm", "pnpm", "node"}:
        dirs.add(_runtime_grant(resolved.parent.parent))
    return sorted(dirs)


def _scan_root_sockets(root):
    """Refuse host pathname sockets, bounding the walk without following links."""
    pending = [root]
    count = 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > 50_000:
                    raise SandboxUnavailable("ROOT scan exceeds 50000 entries")
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISSOCK(mode):
                    raise SandboxUnavailable(f"socket in ROOT: {entry.path}")
                if stat.S_ISDIR(mode):
                    pending.append(entry.path)


_MAC_PROFILE = '''(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-write* (subpath (param "ROOT")) (subpath (param "TEMP"))
    (literal "/dev/null") (literal "/dev/zero") (literal "/dev/dtracehelper")
    (literal "/dev/urandom") (literal "/dev/random"))
(deny file-read-data (subpath (param "HOME")) (subpath (param "USER_HOME")))
(allow file-read-data (subpath (param "ROOT")) (subpath (param "TEMP")) {runtime})
(deny process-exec (literal "/usr/bin/open") (literal "/usr/bin/osascript")
    (literal "/usr/bin/pbcopy") (literal "/usr/bin/pbpaste") (literal "/usr/bin/launchctl"))
; Best-effort broker restriction, not an exhaustive Mach/XPC boundary.
(deny mach-lookup (global-name "com.apple.coreservices.appleevents")
    (global-name "com.apple.lsd.mapdb") (global-name "com.apple.windowserver.active")
    (global-name "com.apple.coreservices.launchservicesd")
    (global-name "com.apple.pasteboard.1") (global-name "com.apple.securityd")
    (global-name "com.apple.securityd.xpc"))
'''


@lru_cache(maxsize=None)
def _probe(system, backend):
    command = ([backend, "-p", '(version 1)(allow default)(deny network*)', "/usr/bin/true"]
               if system == "Darwin" else
               [backend, "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/", "--", "/bin/true"])
    try:
        result = subprocess.run(command, capture_output=True, timeout=5, env=environment(), close_fds=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{backend} probe failed: {exc}"
    return "" if result.returncode == 0 else f"{backend} probe failed: {result.stderr.decode(errors='replace').strip()}"


def backend_name():
    return "macOS sandbox-exec" if platform.system() == "Darwin" else "Linux bubblewrap"


def wrap(argv, root):
    """Return (wrapped argv, cleanup); never change the parent's environment."""
    system = platform.system()
    backend = "/usr/bin/sandbox-exec" if system == "Darwin" else shutil.which("bwrap") if system == "Linux" else None
    if not backend:
        raise SandboxUnavailable("bwrap missing" if system == "Linux" else f"unsupported OS: {system}")
    root = _path(root)
    home = _path(os.environ.get("HOME", Path.home()))
    try:
        if system == "Linux":
            _scan_root_sockets(root)
        runtimes = _runtime_dirs(argv[0])
        reason = _probe(system, backend)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SandboxUnavailable(str(exc)) from exc
    if reason:
        raise SandboxUnavailable(reason)
    temp = tempfile.TemporaryDirectory(prefix="llm-check-")
    try:
        scratch = _path(temp.name)
        env = environment()
        env.update(TMPDIR=scratch, HOME=home)
        if system == "Darwin":
            import pwd

            params = {"ROOT": root, "HOME": home, "USER_HOME": _path(pwd.getpwuid(os.getuid()).pw_dir), "TEMP": scratch}
            params.update({f"R{i}": value for i, value in enumerate(runtimes)})
            rules = " ".join(f'(subpath (param "R{i}"))' for i in range(len(runtimes)))
            command = [backend]
            for key, value in params.items():
                command.extend(["-D", f"{key}={value}"])
            command += ["-p", _MAC_PROFILE.format(runtime=rules), "/usr/bin/env", "-i"]
            command += [f"{k}={v}" for k, v in env.items()] + list(argv)
        else:
            # --new-session detaches from the controlling terminal: no /dev/tty writes
            # and no TIOCSTI input injection (CVE-2017-5226).
            command = [backend, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
            mounts = {"/usr", "/bin", "/etc", *glob.glob("/lib*"), *runtimes}
            for path in sorted(mounts):
                if Path(path).exists():
                    command += ["--ro-bind", _path(path), path]
            command += ["--dev", "/dev", "--proc", "/proc", "--bind", root, root,
                        "--bind", scratch, scratch, "--chdir", root]
            for key, value in env.items():
                command += ["--setenv", key, value]
            command += ["--", *argv]
        return command, temp.cleanup
    except BaseException:
        temp.cleanup()
        raise
