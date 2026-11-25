"""Contain only the check subprocess, never edit application or host-side Git.

macOS uses (allow default): deny-default breaks pytest on current macOS. Reads of
/Volumes, /Users, /private/var/folders and /private/tmp are denied except ROOT,
this run's TEMP and the runtime grants (bin/lib of a runtime prefix, never its
root); other system paths stay readable. The Mach broker denylist is a probed,
best-effort list (read-only bootstrap_look_up from inside, Darwin 25.5, n=7 names),
not a complete XPC boundary: best effort, not a security boundary against a hostile
repo. launchd job submission itself was NOT tested (a copied launchctl is killed by
the OS outside the sandbox, so there is no positive control). Descendants inherit
confinement; apply_tool kills setsid() descendants after the check by pgrep -P walk
and an env-marker sweep (best effort: SIP binaries hide their env, a hostile process
can clear it). Resource limits (apply_tool, via check_sandbox.resource_limiter):
RLIMIT_FSIZE 256 MB, RLIMIT_NPROC = this uid's process count + 128 (per-uid on
macOS and Linux, so it also bounds the user's other processes while the check
runs), 16 MB of captured output. No CPU/memory limit beyond the 60 s wall clock.
Executables and their interpreter installations must be trusted.
The Linux backend (bubblewrap) is exercised on a real ubuntu-24.04 host by the
CI job `linux-sandbox` (.github/workflows/ci.yml): every non-macOS test in
tests/test_check_sandbox.py must run, and the job fails if any is skipped. The
macOS behavioural tests (test_macos_*) are deselected there and run only locally.
ROOT must not be HOME, an ancestor of HOME, or "/", and ROOT/.git is read-only
inside the sandbox on both backends (a check cannot plant hooks or git config).
Known difference: when ROOT/.git does not exist, macOS denies creating it (the
profile denies the path) while Linux allows it (nothing to ro-bind); wrap() never
creates anything in the user's repository to hide that.
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
import threading


MARKER_VAR = "LLM_CHECK_ID"


class SandboxUnavailable(RuntimeError):
    """No usable containment backend; callers must explicitly choose fallback."""


ENV_ALLOWLIST = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TERM", "TMPDIR",
                 "VIRTUAL_ENV", "PYTHONDONTWRITEBYTECODE", "CI", "NO_COLOR"}


_run = threading.local()


def set_marker(marker):
    """Per-run id (this thread) that environment() puts in the check's env so a sweep can find it."""
    _run.marker = marker


def current_marker():
    return getattr(_run, "marker", None)


def environment():
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST or k.startswith("LC_")}
    if current_marker():
        env[MARKER_VAR] = current_marker()
    return env


FSIZE_BYTES = 256 * 1024 * 1024   # RLIMIT_FSIZE: largest file any process of the check may write
NPROC_HEADROOM = 128              # RLIMIT_NPROC = this uid's current process count + headroom


def _uid_process_count():
    """Processes of this uid now. RLIMIT_NPROC is per-uid (not per-tree) on macOS and Linux:
    the kernel compares the uid-wide count with the limit, so a bare small number would
    stop every fork of the user's desktop, and a bare large one would not bound anything."""
    try:
        listing = subprocess.run(["/bin/ps", "-U", str(os.getuid()), "-o", "pid="],
                                 capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return len(listing.split())


def resource_limiter():
    """preexec_fn that bounds file size and the uid's process count in the check subprocess.

    Inherited through sandbox-exec and bwrap. Not a CPU/memory limit (60 s wall clock only).
    Returns None when the process count is unavailable: FSIZE alone is then applied.
    """
    import resource

    count = _uid_process_count()
    nproc = count + NPROC_HEADROOM if count is not None else None
    fsize = FSIZE_BYTES

    def apply():
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        if nproc is not None:
            hard = resource.getrlimit(resource.RLIMIT_NPROC)[1]
            limit = nproc if hard == resource.RLIM_INFINITY else min(nproc, hard)
            resource.setrlimit(resource.RLIMIT_NPROC, (limit, limit))
    return apply


def _children(pid):
    try:
        out = subprocess.run(["/usr/bin/pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(p) for p in out.split()]


def descendants(root_pid):
    """Pids below root_pid found by walking pgrep -P (macOS). Snapshot it BEFORE killing the
    group: once a parent dies its setsid() children are reparented and can no longer be found."""
    if platform.system() != "Darwin":
        return set()
    found, pending = set(), [root_pid]
    while pending and len(found) < 5000:
        for child in _children(pending.pop()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def kill_survivors(known, marker):
    """macOS: SIGKILL check descendants that left the process group (setsid), after killpg.

    Best effort, two sources: `known` (descendants() snapshot taken while the root lived;
    finds children even if they scrubbed their env) and a sweep of this uid's processes
    whose environment carries the per-run marker (finds reparented descendants; `ps -E`
    shows no env for SIP-protected binaries and a hostile process can clear its env).
    Returns the sorted pids that were still alive and signalled.
    """
    import signal

    if platform.system() != "Darwin":
        return []
    found = set(known)
    if marker:
        try:
            listing = subprocess.run(["/bin/ps", "-E", "-ww", "-U", str(os.getuid()), "-o", "pid=,command="],
                                     capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            listing = ""
        needle = f"{MARKER_VAR}={marker}"
        for line in listing.splitlines():
            head, _, rest = line.strip().partition(" ")
            if head.isdigit() and needle in rest and int(head) != os.getpid():
                found.add(int(head))
    killed = []
    for pid in sorted(found):
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except OSError:
            pass
    return killed


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


def _under_system(path):
    """macOS /System (incl. /System/Volumes/Data, which firmlinks to /Users); case-insensitive."""
    return len(path.parts) > 1 and path.parts[1].lower() == "system"


def _strip_data_volume(path):
    parts = path.parts
    if [p.lower() for p in parts[1:4]] == ["system", "volumes", "data"]:
        return Path("/", *parts[4:])
    return path


def _check_root(root):
    """Refuse a ROOT that would re-grant HOME (the allow ROOT rule outranks the HOME deny).

    Compared by file identity, not by string: on case-insensitive APFS '/users/me'
    names the same directory as '/Users/me'. Fewer than 2 path components
    ('/', '/usr', '/etc', '/Users', '/home', '/opt', ...) is always refused.
    """
    import pwd

    refusal = "root covers HOME or a system directory"
    named = Path(os.path.abspath(os.fspath(root)))  # lexical: /etc stays /etc, no symlink followed
    if len(named.parts) < 3:
        raise SandboxUnavailable(f"{refusal}: {named}")
    if _under_system(named):
        raise SandboxUnavailable(f"{refusal}: {named}")
    path = Path(_path(root))
    if _under_system(path):
        raise SandboxUnavailable(f"{refusal}: {path}")
    # macOS /etc, /var, /tmp resolve into /private: treat '/private' like '/'.
    floor = 4 if len(path.parts) > 1 and path.parts[1].lower() == "private" else 3
    if len(path.parts) < floor:
        raise SandboxUnavailable(f"{refusal}: {path}")
    homes = {Path(_path(os.environ.get("HOME") or Path.home())),
             Path(_path(pwd.getpwuid(os.getuid()).pw_dir))}
    bare = _strip_data_volume(path)
    for home in homes:
        if _inside(_strip_data_volume(home), bare):  # firmlink view of the same tree
            raise SandboxUnavailable(f"{refusal}: {path}")
        for ancestor in (home, *home.parents):
            try:
                same = os.path.samefile(path, ancestor)
            except OSError:
                same = path == ancestor
            if same:
                raise SandboxUnavailable(f"root covers HOME or a system directory: {path}")


def _inside(path, root):
    return root is not None and (path == root or root in path.parents)


_SYSTEM_DIRS = ("/usr", "/bin", "/etc", "/lib", "/lib32", "/lib64")


def _repo_owned(original, root):
    """True when any lexical ancestor of the path is, or resolves into, ROOT.

    Resolving the whole path first would erase provenance: a repository can make
    `.venv/bin` a symlink to `/usr/bin` and still own `.venv/pyvenv.cfg`.
    """
    if root is None:
        return False
    return any(_inside(anc, root) or _inside(Path(_path(anc)), root)
               for anc in (original, *original.parents))


def _is_runtime_prefix(prefix):
    """True when an existing directory is shaped like a Python or Node install."""
    return ((prefix / "pyvenv.cfg").is_file() or any(prefix.glob("lib/python3*/os.py"))
            # Homebrew formula prefix: stdlib lives in the nested framework.
            or any(prefix.glob("Frameworks/Python.framework/Versions/*/lib/python3*/os.py"))
            or (prefix / "bin/node").is_file() or (prefix / "lib/node_modules").is_dir())


def _grant(path, root, untrusted, prefix=None):
    """Grant a runtime dir; repository-derived grants outside ROOT must be real installs.

    A repository controls its own symlink targets, shebang lines and pyvenv.cfg text,
    so any of them can name an arbitrary existing directory (for example ~/.ssh).
    """
    path = Path(_path(path))
    # System dirs are visible regardless: outside HOME on macOS, mounted wholesale on Linux.
    system = any(_inside(path, Path(d)) for d in _SYSTEM_DIRS)
    if untrusted and not system and not _inside(path, root):
        prefix = Path(_path(prefix if prefix is not None else path))
        if not _is_runtime_prefix(prefix):
            raise SandboxUnavailable(f"repository names a non-runtime dir: {path}")
    return _runtime_grant(path)


def _interpreter_dirs(interpreter, root=None):
    """Inspect installation metadata without ever executing the interpreter.

    pyvenv.cfg text and interpreter paths are always validated as real installs,
    whoever supplied them.
    """
    untrusted = True
    original = Path(os.path.abspath(interpreter))
    resolved = Path(_path(original))
    for candidate in (original, resolved):
        venv = candidate.parent.parent
        config = venv / "pyvenv.cfg"
        if config.is_file():
            for line in config.read_text().splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() == "home" and value.strip():
                    return {_grant(venv, root, untrusted),
                            _grant(Path(value.strip()).parent, root, untrusted)}
            raise SandboxUnavailable(f"Python venv home missing: {config}")
    return {_grant(resolved.parent.parent, root, untrusted)}


def _runtime_dirs(executable, root=None):
    """Derive directory access from executable/shebang metadata only.

    An executable inside ROOT is repository-controlled: every grant it implies
    outside ROOT must be an existing Python/Node installation.
    """
    root = Path(_path(root)) if root is not None else None
    if ".." in Path(executable).parts:
        # abspath() would collapse it and hide that the path started inside ROOT.
        raise SandboxUnavailable("executable path contains '..'")
    original = Path(os.path.abspath(executable))
    resolved = Path(_path(original))
    untrusted = _repo_owned(original, root) or _inside(resolved, root)
    dirs = {_grant(resolved.parent, root, untrusted, prefix=resolved.parent.parent)}
    if original.name.startswith("python"):
        dirs.update(_interpreter_dirs(original, root))
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
            dirs.update(_interpreter_dirs(node, root))
        else:
            dirs.update(_interpreter_dirs(parts[0], root))
    if original.name in {"npm", "pnpm", "node"}:
        dirs.add(_grant(resolved.parent.parent, root, untrusted))
    return sorted(dirs)


def _narrow_grants(dirs, root):
    """S7: grant only the bin/lib/Frameworks subtrees of a runtime prefix, never its root.

    A prefix root holds files a check has no business reading (.npmrc, pip.conf, etc/npmrc,
    credentials next to a conda/nvm install). The one root file python needs is pyvenv.cfg,
    returned separately as a single-file grant. Dirs that are not prefix-shaped (a bare
    bin dir, system dirs, anything inside ROOT) pass through unchanged.
    Returns (dirs, files).
    """
    out, files = set(), set()
    for name in dirs:
        path = Path(name)
        system = any(_inside(path, Path(d)) for d in _SYSTEM_DIRS)
        if system or _inside(path, root) or not _is_runtime_prefix(path):
            out.add(name)
            continue
        for sub in ("bin", "lib", "lib64", "Frameworks"):
            real = (path / sub)
            if real.is_dir():
                target = Path(_path(real))
                if _inside(target, Path(_path(path))):
                    out.add(_runtime_grant(target))
        if (path / "pyvenv.cfg").is_file():
            files.add(_path(path / "pyvenv.cfg"))
    return sorted(out), sorted(files)


def _symlink_hops(path):
    """Every symlink met while resolving *path*, as (link path, raw target), in resolution order."""
    hops, pending, done = [], list(Path(os.path.abspath(path)).parts[1:]), Path("/")
    while pending:
        if len(hops) > 40:
            raise SandboxUnavailable(f"too many symlinks resolving {path}")
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            # `..` applies to the resolved prefix, after any symlink before it was followed.
            done = done.parent
            continue
        current = done / part
        if current.is_symlink():
            target = os.readlink(current)
            hops.append((str(current), target))
            if os.path.isabs(target):
                done, rest = Path("/"), Path(target).parts[1:]
            else:
                rest = Path(target).parts
            pending = list(rest) + pending
        else:
            done = current
    return hops


def _exec_paths(executable):
    """Paths the runtime opens by their unresolved name: the executable, its shebang
    interpreter and any venv `home`. Linux must recreate their symlinks inside bwrap."""
    paths = [Path(os.path.abspath(executable))]
    try:
        with open(paths[0], "rb") as stream:
            first = stream.readline(4096).decode("utf-8", errors="replace").strip()
    except OSError:
        first = ""
    if first.startswith("#!/") and Path(first[2:].split()[0]).name != "env":
        paths.append(Path(first[2:].split()[0]))
    for interpreter in list(paths):
        config = interpreter.parent.parent / "pyvenv.cfg"
        if config.is_file():
            for line in config.read_text().splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() == "home" and value.strip():
                    paths.append(Path(value.strip()))
    return paths


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
; Last match wins: keep .git read-only so a check cannot plant hooks or config.
(deny file-write* (subpath (param "GIT")))
(deny file-read-data (subpath (param "HOME")) (subpath (param "USER_HOME"))
    (subpath "/Volumes") (subpath "/Users") (subpath "/private/var/folders") (subpath "/private/tmp"))
(allow file-read-data (subpath (param "ROOT")) (subpath (param "TEMP")) {runtime})
(deny process-exec (literal "/usr/bin/open") (literal "/usr/bin/osascript")
    (literal "/usr/bin/pbcopy") (literal "/usr/bin/pbpaste") (literal "/usr/bin/launchctl"))
; Best-effort broker restriction, not an exhaustive Mach/XPC boundary.
(deny mach-lookup (global-name "com.apple.coreservices.appleevents")
    (global-name "com.apple.lsd.mapdb") (global-name "com.apple.windowserver.active")
    (global-name "com.apple.coreservices.launchservicesd")
    (global-name "com.apple.pasteboard.1") (global-name "com.apple.securityd")
    (global-name "com.apple.securityd.xpc")
    ; S2 probe (bootstrap_look_up from inside the sandbox, 2026-11-25, Darwin 25.5, n=7 names):
    ; these were still reachable: Keychain, quarantine, ServiceManagement job submission.
    (global-name "com.apple.SecurityServer") (global-name "com.apple.secd")
    (global-name "com.apple.coreservices.quarantine-resolver")
    (global-name "com.apple.xpc.smd") (global-name "com.apple.pboard"))
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
    _check_root(root)  # before anything resolves or touches the directory
    root = _path(root)
    home = _path(os.environ.get("HOME", Path.home()))
    try:
        if system == "Linux":
            _scan_root_sockets(root)
        runtimes, runtime_files = _narrow_grants(_runtime_dirs(argv[0], root), Path(root))
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

            params = {"ROOT": root, "HOME": home, "USER_HOME": _path(pwd.getpwuid(os.getuid()).pw_dir), "TEMP": scratch,
                      "GIT": root + "/.git"}
            params.update({f"R{i}": value for i, value in enumerate(runtimes)})
            params.update({f"F{i}": value for i, value in enumerate(runtime_files)})
            rules = " ".join([f'(subpath (param "R{i}"))' for i in range(len(runtimes))]
                             + [f'(literal (param "F{i}"))' for i in range(len(runtime_files))])
            command = [backend]
            for key, value in params.items():
                command.extend(["-D", f"{key}={value}"])
            command += ["-p", _MAC_PROFILE.format(runtime=rules), "/usr/bin/env", "-i"]
            command += [f"{k}={v}" for k, v in env.items()] + list(argv)
        else:
            # --new-session detaches from the controlling terminal: no /dev/tty writes
            # and no TIOCSTI input injection (CVE-2017-5226).
            command = [backend, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
            mounts = {"/usr", "/bin", "/etc", *glob.glob("/lib*"), *runtimes, *runtime_files}
            for path in sorted(mounts):
                if Path(path).exists():
                    command += ["--ro-bind", _path(path), path]
            # Symlinks on the way to the interpreter (uv: cpython-3.12-... -> cpython-3.12.16-...)
            # are not inside any mount; recreate them. A link grants no read access by itself.
            visible = [Path(m) for m in (*mounts, root, scratch)]
            made = set()
            for exec_path in _exec_paths(argv[0]):
                for link, target in _symlink_hops(exec_path):
                    if link in made or any(_inside(Path(link), m) for m in visible):
                        continue
                    made.add(link)
                    command += ["--symlink", target, link]
            command += ["--dev", "/dev", "--proc", "/proc", "--bind", root, root,
                        "--bind", scratch, scratch]
            if os.path.lexists(root + "/.git"):
                # After the ROOT bind so it overrides it; a linked-worktree .git file works too.
                command += ["--ro-bind", root + "/.git", root + "/.git"]
            command += ["--chdir", root]
            for key, value in env.items():
                command += ["--setenv", key, value]
            command += ["--", *argv]
        return command, temp.cleanup
    except BaseException:
        temp.cleanup()
        raise
