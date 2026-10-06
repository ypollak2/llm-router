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
# with it, through two shims in a private dir (PATH puts the shim dir first inside the sandbox):
#
#   llmr-py   sets PYTHONPATH="$PWD/src:$PWD" and execs the venv python
#   pytest    llmr-py -m pytest
#
# $PWD is the sandbox copy being tested (baseline or patched), so the copy shadows the user's REAL
# tree. That matters because a venv with an editable install puts the real tree on sys.path: unless
# the copy comes first, baseline == after (no f2p ever) or the wrong code is tested. A probe inside
# the sandbox proves it for this repo: any sys.path entry (or package) that lives under the real tree
# and is NOT shadowed by the copy means ``editable_points_outside`` (unavailable, never a verdict).
_PROBE = r"""
import importlib, os, signal, sys
real = os.path.realpath(sys.argv[1]); here = os.path.realpath(os.getcwd())
try:
    import pytest  # noqa: F401
except Exception:
    sys.exit(96)
def under(p, root):
    return p == root or p.startswith(root + os.sep)
prefix = os.path.realpath(sys.prefix)
# 1. sys.path entries in the real tree that the copy does not shadow (cheap, catches .pth / egg-link)
paths = [os.path.realpath(p) for p in sys.path if p and os.path.exists(p)]
for i, p in enumerate(paths):
    if under(p, real) and not under(p, here) and not under(p, prefix):
        twin = os.path.normpath(os.path.join(here, os.path.relpath(p, real)))
        if twin not in paths[:i]:
            sys.exit(97)
# 2. the authoritative check: import every top-level package/module of the copy, with this very
#    interpreter, env and PYTHONPATH, and see where it RESOLVES (meta_path finders, strict editable
#    installs and anything else included)
SKIP = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__", "site-packages",
        "tests", "test", "docs", "examples", "scripts"}
def skipped(n):
    return n in SKIP or n.startswith((".", "test", "conftest", "setup", "_"))
names = set()
def add_modules(d):
    try:
        for n in os.listdir(d):
            if n.endswith(".py") and n[:-3].isidentifier() and not skipped(n[:-3]):
                names.add(n[:-3])
    except OSError:
        pass
roots = [here, os.path.join(here, "src"), os.path.join(here, "lib")]
pk = os.path.join(here, "packages")
if os.path.isdir(pk):
    for n in os.listdir(pk):
        roots += [os.path.join(pk, n), os.path.join(pk, n, "src")]
for r in roots:
    if os.path.isdir(r):
        add_modules(r)
for dirpath, dirnames, files in os.walk(here):
    depth = os.path.relpath(dirpath, here).count(os.sep)
    dirnames[:] = [d for d in dirnames if not skipped(d) and depth < 4]
    if "__init__.py" in files and not skipped(os.path.basename(dirpath)):
        parent = os.path.dirname(dirpath)
        if not os.path.exists(os.path.join(parent, "__init__.py")):
            names.add(os.path.basename(dirpath))
class _Slow(BaseException):
    pass
def _alarm(sig, frame):
    raise _Slow()
signal.signal(signal.SIGALRM, _alarm)
for name in sorted(names):
    if not name.isidentifier():
        continue
    signal.alarm(5)
    try:
        mod = importlib.import_module(name)
    except BaseException:
        continue                          # not importable here: the tests cannot import it either
    finally:
        signal.alarm(0)
    origin = getattr(mod, "__file__", None) or (list(getattr(mod, "__path__", []) or [""])[0])
    if origin and not under(os.path.realpath(origin), here):
        sys.exit(97)                      # resolves outside the sandbox copy
"""


def _repo_python_dir(real_repo: str, checkout: Path, tmp: Path, budget: float) -> tuple[str | None, str | None]:
    """``(python_dir, None)`` to run the tests with the repo's venv, ``(None, None)`` to keep the
    default interpreter, ``(None, reason)`` when the venv would test the wrong tree."""
    py = Path(real_repo) / ".venv" / "bin" / "python"
    if not (py.is_file() and os.access(py, os.X_OK)) or sandbox.kill_switch_reason() \
            or not sandbox.prove_sandbox().proven:
        return None, None
    shim = tmp / "shim"
    shim.mkdir(mode=0o700)
    for name, body in (("llmr-py", f'#!/bin/sh\nexport PYTHONPATH="$PWD/src:$PWD${{PYTHONPATH:+:$PYTHONPATH}}"\n'
                                   f'exec {shlex.quote(str(py))} "$@"\n'),
                       ("pytest", f'#!/bin/sh\nexec {shlex.quote(str(shim / "llmr-py"))} -m pytest "$@"\n')):
        (shim / name).write_text(body)
        os.chmod(shim / name, 0o700)
    probe_tmp = tmp / "probe"
    probe_tmp.mkdir(mode=0o700)
    cmd = f"llmr-py -c {shlex.quote(_PROBE)} {shlex.quote(real_repo)}"
    run = V.run_command(cmd, checkout, probe_tmp, python_dir=str(shim), timeout_s=min(30.0, max(budget, 1.0)),
                        tag="probe")
    if run.rc == 97:
        return None, "editable_points_outside"
    if run.rc == 0:
        return str(shim), None
    return None, None                           # no pytest in the venv / venv unusable: default interpreter


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
            return verify(repo, patch, budget_s=left, python_dir=python_dir)
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
