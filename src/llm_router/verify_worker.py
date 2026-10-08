"""``python -m llm_router.verify_worker``: drains the pending_verify queue (verifier PR C, SHADOW).

Detached from any turn (spawned by the SessionStart/Stop hooks, see ``verify_queue``):

* at most ``verify_queue.MAX_WORKERS`` (2) run at once (flock slots, released by the kernel if a
  worker dies);
* a unit is claimed by an atomic rename, so two workers never process the same marker;
* each unit gets ``LLM_ROUTER_VERIFY_BUDGET_S`` seconds (default 120, capped at 300) for the whole
  job: building the pristine checkout at the marker's HEAD, ``verify_unit``, recording;
* the baseline is ``git archive <head>`` of the user's repo, extracted into a private temp dir. The
  user's tree is only READ, never written; because the baseline is the recorded HEAD, a HEAD that
  moved since dispatch cannot make the recorded patch fail to apply;
* ANY failure records ``unavailable`` with a reason code (never free text), and the patch is deleted
  once the unit is settled. A marker past its 24 h TTL records ``unavailable``/``verify_expired``.

SHADOW: only ``northstar.record_verify`` is called; no outcome, NS, D1 or D2 changes (PR E).
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

from llm_router import verify_queue as Q
from llm_router.toolkit import sandbox
from llm_router.toolkit import verify as V
from llm_router.toolkit import verify_unit as VU

DEFAULT_BUDGET_S = VU.DEFAULT_BUDGET_S      # 120
MAX_BUDGET_S = VU.MAX_BUDGET_S              # 300
MAX_UNITS_PER_RUN = 25
_GRACE_S = 30.0                             # hard watchdog = budget + grace
_MAX_CHECKOUT_BYTES = 200 * 1024 * 1024
_MIN_VERIFY_S = 5.0


def budget_s() -> float:
    try:
        v = float(os.environ.get("LLM_ROUTER_VERIFY_BUDGET_S", DEFAULT_BUDGET_S))
    except ValueError:
        v = DEFAULT_BUDGET_S
    if v != v:                              # NaN
        v = DEFAULT_BUDGET_S
    return min(max(v, 1.0), MAX_BUDGET_S)


class _Watchdog(BaseException):
    """Raised by SIGALRM. A BaseException on purpose: ``verify_unit`` swallows ``Exception``."""


class _Fail(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _unavailable(reason: str):
    return VU.UnitResult(VU.UNAVAILABLE, reason)


def _checkout(cwd: str, head: str, dest: Path, deadline: float) -> None:
    """``git archive <head>`` of `cwd` extracted into `dest` (read-only on the user's repo)."""
    if not os.path.isdir(cwd):
        raise _Fail("verify_repo_missing")
    if not hasattr(tarfile, "data_filter"):      # Python < 3.11.4: no safe extraction filter
        raise _Fail("verify_checkout_failed")
    env = Q._git_env()
    if not Q._HEAD_RX.fullmatch(head):                # defence in depth: _valid already refuses this
        raise _Fail("marker_invalid")
    p = subprocess.Popen(["git", "archive", "--format=tar", "--end-of-options", head], cwd=cwd, env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    timer = threading.Timer(max(1.0, deadline - time.monotonic()), p.kill)
    timer.start()
    total = 0
    try:
        with tarfile.open(fileobj=p.stdout, mode="r|") as tar:
            for member in tar:
                total += max(member.size, 0)
                if total > _MAX_CHECKOUT_BYTES:
                    raise _Fail("verify_repo_too_large")
                tar.extract(member, dest, filter="data")
        # tarfile stops at the end-of-archive blocks; git still writes the record padding. Closing
        # the pipe before that makes git die of SIGPIPE (rc != 0) on a timing race: a good checkout
        # recorded as verify_head_unavailable. Drain to EOF first (the timer bounds it).
        while p.stdout.read(65536):
            pass
    except tarfile.TarError:
        try:                                     # an empty/short stream: did git itself refuse?
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        raise _Fail("verify_head_unavailable" if p.returncode else "verify_checkout_failed") from None
    finally:
        timer.cancel()
        if p.poll() is None:
            p.kill()
        p.stdout.close()
        p.wait()
    if p.returncode != 0:
        raise _Fail("verify_head_unavailable")


# ── the repo's own interpreter ───────────────────────────────────────────────
#
# Repo tests need the repo's dependencies, so when ``<repo>/.venv/bin/python`` exists the tests run
# with it, through two shims in a private dir (PATH puts the shim dir first inside the sandbox).
#
# EDITABLE INSTALLS ARE DISABLED, NOT DETECTED. An editable install reaches the user's REAL tree only
# through site-packages: ``.pth`` files, egg-links and the ``sys.meta_path`` finders those register.
# Detecting every installer format is an arms race (namespace packages, renamed package dirs, lazy
# submodules, strict-editable finders...), so the venv python runs with ``-S`` (no site processing:
# no ``.pth`` line, no finder ever runs) and PYTHONPATH is built explicitly:
#
#   the sandbox copy ($PWD = baseline or patched copy): $PWD, $PWD/src, $PWD/lib, and the parent dir
#   of every top-level package found in the copy (monorepos, any depth), THEN the venv's own
#   purelib/platlib (queried with ``python -S`` itself), THEN the shim dir (the load-check plugin).
#
# Regular installed dependencies still import from purelib; the user's package can only come from the
# copy. COST (fail closed, documented): a dependency that needs a ``.pth`` to import (legacy namespace
# packages, distutils-precedence) fails to import, the baseline fails, and the verdict is
# unavailable (baseline_failing / tests_error), never a verdict on the wrong code.
#
# Belt and braces, a pytest plugin (``-p _llmr_loadcheck``) checks at session end that no loaded module
# file lives in the user's real tree (outside the copy and the venv); if one does, the ``pytest`` shim
# deletes the junit report and exits 97, so the run is never a pass (unavailable/no_junit). Any failure to set the venv up (cannot run it, probe timeout or
# crash) is ``unavailable``, never a silent fallback to the default interpreter; the one fallback is a
# venv that simply has no pytest.
_SKIP_WALK = {".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages"}
_MAX_PKG_ROOTS = 80

_LOADCHECK = """\
import os, sys

REAL = {real!r}
SITES = {sites!r}


def _under(p, root):
    return p == root or p.startswith(root + os.sep)


def pytest_sessionfinish(session, exitstatus):
    here = os.path.realpath(os.getcwd())
    venv = [os.path.realpath(d) for d in SITES]
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not isinstance(f, str):
            continue
        r = os.path.realpath(f)
        if _under(r, REAL) and not _under(r, here) and not any(_under(r, v) for v in venv):
            with open(os.path.join(os.environ.get("TMPDIR", "/tmp"), "llmr-outside"), "w") as fh:
                fh.write(r)
            return
"""


def _path_ok(p: str) -> bool:
    """Any real path is fine (the shims quote every piece with ``shlex.quote``); only what cannot be
    one PYTHONPATH entry is refused: a ``:`` (the separator), a NUL or a newline."""
    return bool(p) and not any(c in p for c in (":", "\0", "\n"))


def _pythonpath_expr(rels: list[str], absolutes: list[str]) -> str:
    """A shell word for PYTHONPATH: ``"$PWD"`` (the sandbox copy) and ``"$PWD"/<quoted rel>`` entries,
    then quoted absolute dirs, joined with ``:``. No character allowlist: quoting does the work."""
    parts = ['"$PWD"', *(f'"$PWD"/{shlex.quote(r)}' for r in dict.fromkeys(rels)), *map(shlex.quote, absolutes)]
    return ":".join(parts)


def _package_roots(checkout: Path) -> list[str]:
    """Relative dirs (of the copy) that should be on PYTHONPATH: the parent of every top-level package
    (a dir with __init__.py whose parent has none) and every src/ or lib/ dir, to any depth."""
    out: list[str] = []
    for n, (dirpath, dirnames, files) in enumerate(os.walk(checkout)):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_WALK and not d.startswith("."))
        if n > 5000:
            break
        rel = os.path.relpath(dirpath, checkout)
        parent = os.path.dirname(rel)
        cands = []
        if rel != "." and os.path.basename(rel) in ("src", "lib"):
            cands.append(rel)
        if "__init__.py" in files and rel != "." and parent \
                and not os.path.exists(os.path.join(checkout, parent, "__init__.py")):
            cands.append(parent)
            while parent:                         # namespace packages: every ancestor may be the root
                parent = os.path.dirname(parent)
                if parent:
                    cands.append(parent)
        out += [c for c in cands if c not in out and _path_ok(c)]
    return out[:_MAX_PKG_ROOTS]


def _venv_site_dirs(py: Path) -> list[str] | None:
    """The venv's purelib/platlib, asked of ``python -S`` itself. Under -S ``sys.prefix`` is the BASE
    prefix (venv detection lives in ``site``), so the venv dir is passed as the sysconfig base."""
    venv = str(py.parent.parent)
    code = ("import sysconfig, json, sys; v = {'base': sys.argv[1], 'platbase': sys.argv[1]}; "
            "print(json.dumps([sysconfig.get_path('purelib', vars=v), sysconfig.get_path('platlib', vars=v)]))")
    try:
        proc = subprocess.run([str(py), "-S", "-c", code, venv], env=Q._git_env(), capture_output=True, text=True,
                              timeout=10, stdin=subprocess.DEVNULL)
        dirs = json.loads(proc.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    out = []
    for d in dirs:
        if isinstance(d, str) and _path_ok(d) and d not in out:
            out.append(d)
    return out or None


def _repo_python_dir(real_repo: str, checkout: Path, tmp: Path, budget: float) -> tuple[str | None, str | None]:
    """``(python_dir, None)`` to run the tests with the repo's venv, ``(None, None)`` to keep the
    default interpreter (no venv, or the venv has no pytest), ``(None, reason)`` when the venv cannot
    be used safely. See the block comment above: editable installs are disabled with ``-S``."""
    py = Path(real_repo) / ".venv" / "bin" / "python"
    if not (py.is_file() and os.access(py, os.X_OK)) or sandbox.kill_switch_reason() \
            or not sandbox.prove_sandbox().proven:
        return None, None
    sites = _venv_site_dirs(py)
    if sites is None:
        return None, "venv_probe_failed"
    shim = tmp / "shim"
    shim.mkdir(mode=0o700)
    rels = ["src", "lib", *_package_roots(checkout)]
    if not _path_ok(str(shim)):
        return None, "venv_probe_failed"
    path = _pythonpath_expr(rels, [*sites, str(shim)])
    files = {
        "llmr-py": f'#!/bin/sh\nexport PYTHONPATH={path}\nexec {shlex.quote(str(py))} -S "$@"\n',
        "pytest": ('#!/bin/sh\nrm -f "$TMPDIR/llmr-outside"\n'
                   f'{shlex.quote(str(shim / "llmr-py"))} -m pytest -p _llmr_loadcheck "$@"\n'
                   'rc=$?\nif [ -e "$TMPDIR/llmr-outside" ]; then\n'
                   '  for a in "$@"; do case "$a" in --junit-xml=*) rm -f "${a#--junit-xml=}";; esac; done\n'
                   f'  exit {VU.OUTSIDE_COPY_RC}\nfi\nexit $rc\n'),
        "_llmr_loadcheck.py": _LOADCHECK.format(real=os.path.realpath(real_repo), sites=sites),
    }
    for name, body in files.items():
        (shim / name).write_text(body)
        os.chmod(shim / name, 0o700)
    probe_tmp = tmp / "probe"
    probe_tmp.mkdir(mode=0o700)
    probe = "import sys\ntry:\n import pytest\nexcept ImportError:\n sys.exit(96)\n"
    run = V.run_command(f"llmr-py -c {shlex.quote(probe)}", checkout, probe_tmp, python_dir=str(shim),
                        timeout_s=min(30.0, max(budget, 1.0)), tag="probe")
    if run.timed_out:
        return None, "venv_probe_timeout"
    if run.rc == 0:
        return str(shim), None
    if run.rc == 96:
        return None, None                       # a venv without pytest: the default interpreter
    return None, "venv_probe_failed"


def _run_unit(m: Q.Marker, verify) -> "VU.UnitResult":
    data = m.data
    pp = Q.patch_path(m.unit_id)
    try:
        if pp.stat().st_size > Q.MAX_PATCH_BYTES:
            return _unavailable("patch_too_large")
        patch = pp.read_bytes().decode("utf-8", "replace")
    except OSError:
        return _unavailable("verify_patch_missing")
    started = time.monotonic()
    budget = budget_s()
    tmp = Path(tempfile.mkdtemp(prefix="llmr-verify-"))
    try:
        os.chmod(tmp, 0o700)
        repo = tmp / "repo"
        repo.mkdir(mode=0o700)
        try:
            _checkout(data["cwd"], data["head"], repo, started + budget)
        except _Fail as f:
            return _unavailable(f.reason)
        python_dir, why = _repo_python_dir(data["cwd"], repo, tmp, budget - (time.monotonic() - started))
        if why:
            return _unavailable(why)
        left = budget - (time.monotonic() - started)
        if left < _MIN_VERIFY_S:
            return _unavailable("timeout")
        if python_dir:
            res = verify(repo, patch, budget_s=left, python_dir=python_dir)
            # A dependency that only imports through a .pth fails under -S and looks like
            # baseline_failing: the flag tells the two apart in the record.
            if res.verify_status != VU.PASS_F2P and "repo_venv_s" not in res.flags:
                res.flags.append("repo_venv_s")
            return res
        return verify(repo, patch, budget_s=left)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _alarm(seconds: float):
    def handler(signum, frame):
        raise _Watchdog()
    signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)


def _settle(m: Q.Marker, result, record) -> None:
    """Record the verdict, then delete the marker and its patch (the patch is the user's source)."""
    try:
        record(m.unit_id, result)
    finally:
        Q.discard(m)


def process(m: Q.Marker, *, verify=VU.verify_unit, record=None) -> str:
    """One claimed unit -> one verify record. Never raises; returns the verify_status written."""
    if record is None:
        from llm_router import northstar
        record = northstar.record_verify
    result = None
    try:
        _alarm(budget_s() + _GRACE_S)
        try:
            result = _run_unit(m, verify)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    except _Watchdog:
        result = _unavailable("verify_worker_timeout")
    except Exception as exc:                     # noqa: BLE001 -- any failure is a recorded verdict
        _failopen("CHZ-FO-VERIFY-WORKER", exc)
        result = _unavailable("verify_worker_error")
    try:
        _settle(m, result, record)
    except Exception as exc:                     # noqa: BLE001 -- the ledger write itself failed
        _failopen("CHZ-FO-VERIFY-RECORD", exc)
    return result.verify_status


def _failopen(code: str, exc: BaseException) -> None:
    try:
        from llm_router import failopen
        failopen.record(code, exc)
    except Exception:  # noqa: BLE001
        pass


def drain(*, verify=VU.verify_unit, record=None, now: float | None = None,
          process_units: bool = True) -> dict:
    """Expire, recover, then process claimed units until the queue is empty (or the per-run cap).
    Returns counts, for the tests and the log line."""
    if record is None:
        from llm_router import northstar
        record = northstar.record_verify
    now = time.time() if now is None else now
    counts = {"expired": 0, "processed": 0, "crashed": 0}
    for m in Q.recover_stale_claims(now):
        _settle_unavailable(m, "verify_worker_crashed", record)
        counts["crashed"] += 1
    for uid in Q.take_invalid():
        try:
            record(uid, _unavailable("marker_invalid"))
        except Exception as exc:                 # noqa: BLE001
            _failopen("CHZ-FO-VERIFY-RECORD", exc)
    for m in Q.pending():
        if Q.is_expired(m, now):
            c = Q.claim(m)
            if c is not None:
                _settle_unavailable(c, "verify_expired", record)
                counts["expired"] += 1
    Q.sweep_orphan_patches(now)
    if not process_units:
        return counts
    while counts["processed"] < MAX_UNITS_PER_RUN:
        claimed = None
        for m in Q.pending():
            claimed = Q.claim(m)
            if claimed is not None:
                break
        if claimed is None:
            break
        process(claimed, verify=verify, record=record)
        counts["processed"] += 1
    return counts


def _settle_unavailable(m: Q.Marker, reason: str, record) -> None:
    try:
        _settle(m, _unavailable(reason), record)
    except Exception as exc:                     # noqa: BLE001
        _failopen("CHZ-FO-VERIFY-RECORD", exc)


def main(argv: list[str] | None = None) -> int:
    slot = Q.acquire_slot()
    if slot is None:
        return 0                                  # two workers already running (or no flock)
    try:
        drain(process_units=Q.enabled())
    finally:
        slot.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
