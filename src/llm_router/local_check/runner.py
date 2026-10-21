"""Serial, resumable local-model runner for ``llm-router local-check`` (LC-3, design §2/§3).

Properties:

* Serial, one attempt per item, no retries (a timeout is a result, not a reason to retry).
* Per-item timeout 240 s, scaled up on slow hardware by a 3-item rate probe. The probe's
  items are real items and their results are kept, so nothing is wasted.
* Results are appended to ``results.jsonl`` under the run dir, one fsynced line per item
  (file 0600, dir 0700). Resume = skip item ids already present; a torn last line from a
  crash is ignored and that item is rerun. ``meta.json`` pins model/digest/settings so a
  resume against a changed model refuses instead of mixing populations.
* Ctrl-C returns ``status="interrupted"`` with everything so far already on disk.
* Early stop is a helper with the bar as a parameter (the runner knows no statistics): stop
  once failures make ``min_pass`` unreachable.
* An optional lock file (``fcntl.flock``, non-blocking) is honoured so a local-check run
  does not collide with another process that owns the same Ollama (e.g. a benchmark harness).
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Protocol, Sequence

try:                                    # POSIX; on Windows a lock path is simply not enforced
    import fcntl
except ImportError:                     # pragma: no cover
    fcntl = None                        # type: ignore[assignment]

BASE_TIMEOUT_S = 240.0
PROBE_N = 3
MAX_TIMEOUT_SCALE = 4.0
SLOW_ITEM_S = 60.0                      # mean probe time at which the timeout starts scaling
DEFAULT_EXPECTED_OUT_TOKENS = 200
# O_NOFOLLOW is 0 where the OS lacks it (Windows); _refuse_symlink() is then the only guard on
# the append()/load() paths (open() always checks too).
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class Item(Protocol):
    """Minimum an item must offer. LC-1's bank item satisfies this structurally."""
    id: str
    prompt: str


@dataclass(frozen=True)
class SimpleItem:
    id: str
    prompt: str
    system: str | None = None


class Adapter(Protocol):
    """What the runner needs from a runtime. ``digest``, ``settings_hash`` and
    ``max_prompt_chars`` reach the runtime and may raise ``ProbeFailed`` (runtimes.py) when the
    probe failed in transit; that is not "unknown" (``None``). ``Runner()`` turns it into
    ``RunStartError``; a caller computing ``max_prompt_chars`` itself (LC-6 scope) must catch it."""
    def generate(self, model: str, prompt: str, *, system: str | None = ...,
                 timeout_s: float = ...): ...
    def digest(self, model: str) -> str | None: ...
    def settings_hash(self, model: str) -> str: ...
    def max_prompt_chars(self, model: str) -> int | None: ...


class LockHeld(Exception):
    pass


class RunMismatch(Exception):
    """Resume attempted against a run recorded for a different model/digest/settings."""


class RunDirError(RunMismatch):
    """The run dir cannot be trusted: unreadable/torn ``meta.json``, or a symlinked run file."""


class RunStartError(Exception):
    """The run could not start: the runtime's digest/settings (context window) could not be
    confirmed (``ProbeFailed``: runtime unreachable, timed out, 5xx). Raised by ``Runner()``
    before anything is written; retry when the runtime answers."""


# ------------------------------------------------------------------ scope / early stop

def partition_by_fit(items: Sequence[Item], max_chars: int | None
                     ) -> tuple[list[Item], list[Item]]:
    """(fits, excluded). ``max_chars`` None = window unknown, nothing excluded."""
    if max_chars is None:
        return list(items), []
    fits = [i for i in items if len(i.prompt) <= max_chars]
    return fits, [i for i in items if len(i.prompt) > max_chars]


def early_stop_reached(n_total: int, failures: int, min_pass: int | None) -> bool:
    """True when even passing every remaining item cannot reach ``min_pass`` (the bar's k).
    ``min_pass`` None disables it. Design §3: n=70, k=56 -> the 15th failure stops the run."""
    if min_pass is None:
        return False
    return n_total - failures < min_pass


def default_is_failure(rec: dict) -> bool:
    """Runner-level failure = no usable answer (error / timeout / empty). Quality failures
    are the grader's call; pass ``is_failure`` to count those. A truncated answer with no text
    (the model spent its whole budget thinking) is no usable answer either."""
    st = rec.get("status")
    if st == "truncated":
        return not str(rec.get("text") or "").strip()
    return st != "ok"


# ------------------------------------------------------------------ rate / timeout

@dataclass
class Rate:
    n: int
    prefill_chars_per_s: float | None
    decode_tok_per_s: float | None
    mean_item_s: float
    mean_out_tokens: float


def measure_rate(records: Iterable[dict]) -> Rate | None:
    """Prefill/decode estimate from completed records (time-to-first-token = prefill).
    Exclude the first (cold-load) record when at least two records exist.
    """
    records = list(records)
    warm = records[1:] if len(records) >= 2 else records
    recs = [r for r in warm if r.get("status") in ("ok", "truncated", "empty", "timeout")]
    if not recs:
        return None
    pf, dc, outs = [], [], []
    for r in recs:
        ttft, el = r.get("first_token_s"), r.get("elapsed_s") or 0.0
        toks = r.get("completion_tokens") or (r.get("raw_chars", 0) / 4 or None)
        if ttft and ttft > 0 and r.get("prompt_chars"):
            pf.append(r["prompt_chars"] / ttft)
        if ttft is not None and toks and el - ttft > 0:
            dc.append(toks / (el - ttft))
        if toks:
            outs.append(toks)
    mean = lambda xs: sum(xs) / len(xs) if xs else None      # noqa: E731
    return Rate(len(recs), mean(pf), mean(dc),
                sum(r.get("elapsed_s") or 0.0 for r in recs) / len(recs),
                mean(outs) or float(DEFAULT_EXPECTED_OUT_TOKENS))


def item_timeout(rate: Rate | None, base: float = BASE_TIMEOUT_S) -> float:
    """240 s, scaled by up to 4x when the probe shows items averaging over a minute."""
    if rate is None:
        return base
    return base * min(MAX_TIMEOUT_SCALE, max(1.0, rate.mean_item_s / SLOW_ITEM_S))


def estimate_seconds(items: Iterable[Item], rate: Rate | None) -> float | None:
    """Estimated time to run ``items`` at the probed rate; None when no rate is known
    (unknown stays None, not 0)."""
    if rate is None or not rate.prefill_chars_per_s or not rate.decode_tok_per_s:
        if rate is None:
            return None
        return rate.mean_item_s * sum(1 for _ in items)
    return sum(len(i.prompt) / rate.prefill_chars_per_s
               + rate.mean_out_tokens / rate.decode_tok_per_s for i in items)


# ------------------------------------------------------------------ store

class RunStore:
    """Append-only results for one run directory."""

    def __init__(self, run_dir: str | os.PathLike):
        self.dir = Path(run_dir)
        self.results_path = self.dir / "results.jsonl"
        self.meta_path = self.dir / "meta.json"

    def _refuse_symlink(self, path: Path) -> None:
        if not _NOFOLLOW and path.is_symlink():
            raise RunDirError(f"refusing symlinked run file {path}")

    def _read(self, path: Path) -> str:
        try:
            fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        except OSError as exc:
            if path.is_symlink():
                raise RunDirError(f"refusing symlinked run file {path}") from exc
            raise
        with os.fdopen(fd, "r") as f:
            return f.read()

    def open(self, meta: dict) -> None:
        """Create the dir (0700, only when we create it) and meta, or verify an existing run
        matches ``meta``. An existing directory's permissions are never changed."""
        created = not self.dir.exists()
        self.dir.mkdir(parents=True, exist_ok=True)
        if created:
            os.chmod(self.dir, 0o700)
        try:
            if self.meta_path.is_symlink() or self.results_path.is_symlink():
                raise RunDirError(f"refusing symlinked run file in {self.dir}")
            if self.meta_path.exists():
                try:
                    old = json.loads(self._read(self.meta_path))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise RunDirError(
                        f"{self.meta_path} is unreadable (torn write?): {exc}; "
                        "delete the run dir to start over") from exc
                if not isinstance(old, dict):
                    raise RunDirError(f"{self.meta_path} is not a JSON object; "
                                      "delete the run dir to start over")
                diff = {k: (old.get(k), meta.get(k)) for k in meta if old.get(k) != meta.get(k)}
                if diff:
                    raise RunMismatch(f"run dir was recorded for different settings: {diff}")
            else:
                self._write_meta(meta)
            if not self.results_path.exists():
                os.close(os.open(self.results_path,
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600))
        except OSError as exc:
            raise RunDirError(f"cannot use run dir {self.dir}: {exc}") from exc

    def _write_meta(self, meta: dict) -> None:
        """Atomic: temp file (0600) + fsync + rename + dir fsync, so a crash leaves either no
        meta.json or a whole one."""
        tmp = self.dir / f".meta.json.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(meta, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.meta_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError):
            dfd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)

    def load(self) -> list[dict]:
        """Intact records, last write per id wins; a torn trailing line is dropped."""
        out: dict[str, dict] = {}
        self._refuse_symlink(self.results_path)
        if not self.results_path.exists():
            return []
        for line in self._read(self.results_path).splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue                      # torn write from a crash
            if isinstance(rec, dict) and "item_id" in rec:
                out[rec["item_id"]] = rec
        return list(out.values())

    def append(self, rec: dict) -> None:
        self._refuse_symlink(self.results_path)
        # Start on a fresh line even if the previous write was torn mid-line.
        try:
            fd = os.open(self.results_path, os.O_RDWR | os.O_APPEND | os.O_CREAT | _NOFOLLOW,
                         0o600)
        except OSError as exc:
            if self.results_path.is_symlink():
                raise RunDirError(f"refusing symlinked run file {self.results_path}") from exc
            raise
        with os.fdopen(fd, "ab+") as f:
            f.seek(0, os.SEEK_END)
            if f.tell():
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    f.write(b"\n")
            f.write(json.dumps(rec, sort_keys=True).encode() + b"\n")
            f.flush()
            os.fsync(f.fileno())


@contextlib.contextmanager
def ollama_lock(lock_path: str | os.PathLike | None) -> Iterator[None]:
    """Non-blocking exclusive flock on ``lock_path`` if one is configured."""
    if not lock_path or fcntl is None:
        yield
        return
    p = Path(lock_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockHeld(f"lock held: {p}") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ------------------------------------------------------------------ run

@dataclass
class RunOutcome:
    status: str                 # complete | early_stop | interrupted
    done: int
    total: int
    failures: int
    rate: Rate | None
    timeout_s: float
    records: list[dict] = field(default_factory=list)


def _record(item: Item, res, timeout_s: float) -> dict:
    d = res.to_dict() if hasattr(res, "to_dict") else dict(res)
    d.update(item_id=item.id, prompt_chars=len(item.prompt), timeout_s=timeout_s,
             ts=time.time())
    return d


class Runner:
    """One resumable run. Construction reaches the runtime (digest, settings hash) and raises
    ``RunStartError`` (nothing written) when a probe fails in transit (``ProbeFailed``)."""

    def __init__(self, adapter: Adapter, model: str, items: Sequence[Item],
                 run_dir: str | os.PathLike, *, runtime: str | None = None,
                 lock_path: str | os.PathLike | None = None,
                 base_timeout_s: float = BASE_TIMEOUT_S, probe_n: int = PROBE_N,
                 min_pass: int | None = None,
                 is_failure: Callable[[dict], bool] = default_is_failure,
                 on_result: Callable[[dict], None] | None = None):
        self.adapter, self.model, self.items = adapter, model, list(items)
        self.store = RunStore(run_dir)
        self.lock_path, self.base_timeout_s, self.probe_n = lock_path, base_timeout_s, probe_n
        self.min_pass, self.is_failure, self.on_result = min_pass, is_failure, on_result
        # Both calls reach the runtime. A transient failure must not become digest/ctx None in
        # meta (a later resume would then mismatch), so it stops the run before anything is
        # written; the settings hash is built from the requested settings plus the confirmed ctx.
        try:
            digest = adapter.digest(model)
            settings = adapter.settings_hash(model)
        except Exception as exc:    # noqa: BLE001
            raise RunStartError(
                f"cannot confirm {model!r} digest/context window: {type(exc).__name__}: {exc}"
            ) from exc
        self.meta = {"model": model, "runtime": runtime, "digest": digest,
                     "settings_hash": settings,
                     "item_ids": [i.id for i in self.items]}

    # -- internals
    def _pending(self) -> list[Item]:
        done = {r["item_id"] for r in self.store.load()}
        return [i for i in self.items if i.id not in done]

    def _failures(self) -> int:
        ids = {i.id for i in self.items}
        return sum(1 for r in self.store.load() if r["item_id"] in ids and self.is_failure(r))

    def _one(self, item: Item, timeout_s: float) -> dict:
        res = self.adapter.generate(self.model, item.prompt,
                                    system=getattr(item, "system", None), timeout_s=timeout_s)
        rec = _record(item, res, timeout_s)
        self.store.append(rec)
        if self.on_result:
            self.on_result(rec)
        return rec

    # -- public
    def probe_rate(self) -> Rate | None:
        """Run the first ``probe_n`` pending items (real results, kept) and return the rate.
        Call before ``run`` to show a time estimate on the plan screen; ``run`` reuses it."""
        with ollama_lock(self.lock_path):
            self.store.open(self.meta)
            for item in self._pending()[: max(0, self.probe_n - len(self.store.load()))]:
                if self._stop():
                    break
                self._one(item, self.base_timeout_s)
        return measure_rate(self.store.load())

    def estimate_remaining_s(self) -> float | None:
        rate = measure_rate(self.store.load())
        return estimate_seconds(self._pending(), rate)

    def run(self) -> RunOutcome:
        with ollama_lock(self.lock_path):
            self.store.open(self.meta)
            status = "complete"
            try:
                # Probe first (no-op when resumed past probe_n results).
                for item in self._pending()[: max(0, self.probe_n - len(self.store.load()))]:
                    if self._stop():            # a resumed run may already be past the bar
                        status = "early_stop"
                        break
                    self._one(item, self.base_timeout_s)
                    if self._stop():
                        status = "early_stop"
                        break
                if status == "complete":
                    rate = measure_rate(self.store.load())
                    timeout_s = item_timeout(rate, self.base_timeout_s)
                    for item in self._pending():
                        if self._stop():
                            status = "early_stop"
                            break
                        self._one(item, timeout_s)
                        if self._stop():
                            status = "early_stop"
                            break
            except KeyboardInterrupt:
                status = "interrupted"
        recs = self.store.load()
        rate = measure_rate(recs)
        return RunOutcome(status, len(recs), len(self.items), self._failures(), rate,
                          item_timeout(rate, self.base_timeout_s), recs)

    def _stop(self) -> bool:
        return early_stop_reached(len(self.items), self._failures(), self.min_pass)
