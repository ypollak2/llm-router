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
    p = subprocess.Popen(["git", "archive", "--format=tar", head], cwd=cwd, env=env,
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
        left = budget - (time.monotonic() - started)
        if left < _MIN_VERIFY_S:
            return _unavailable("timeout")
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
