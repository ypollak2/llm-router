"""Retention for ``~/.llm-router/local_check/runs/`` (LC-7, design section 5; ``docs/RETENTION.md``).

* Keep the newest :data:`KEEP_RUNS` run directories; delete the older ones.
* A run that is an unexpired ``open`` reservation in ``attempts.jsonl`` is never deleted (the next run of that
  configuration must resume it), on top of the newest three.
* Answers (the ``text`` of each ``results.jsonl`` record) are removed from a kept run once its results file is
  older than :data:`ANSWER_DAYS` days; counts, statuses and timings stay. The rewrite is atomic and 0600.
* ``attempts.jsonl`` is not touched (no answer text; the anti-retry rules read all of it).
* Symlinked run entries are skipped, never followed or deleted through.

Called from ``llm-router doctor``; the CLI may call :func:`prune` after a run. It never raises.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from llm_router import paths
from llm_router.local_check.attempts import LOG_NAME, RESERVATION_TTL_S

KEEP_RUNS = 3
ANSWER_DAYS = 30


@dataclass
class PruneReport:
    deleted: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    answers_pruned: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def default_root() -> Path:
    return paths.state_path("local_check", "runs")


def _open_reservations(root: Path, now: float) -> set[str]:
    latest: dict[str, dict] = {}
    try:
        lines = (root / LOG_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and isinstance(e.get("run_id"), str):
            latest[e["run_id"]] = e
    return {rid for rid, e in latest.items() if e.get("state") == "open"
            and isinstance(e.get("ts"), (int, float)) and now - e["ts"] <= RESERVATION_TTL_S}


def _mtime(run: Path) -> float:
    try:
        return max([run.stat().st_mtime] + [f.stat().st_mtime for f in run.iterdir() if f.is_file() and not f.is_symlink()])
    except OSError:
        return 0.0


def _strip_answers(results: Path) -> bool:
    """Blank ``text`` in every record; True when anything changed. Atomic, 0600."""
    out, changed = [], False
    for line in results.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            changed = True              # a torn line is dropped with the answers
            continue
        if isinstance(rec, dict) and rec.get("text"):
            rec["text"], rec["answer_pruned"] = "", True
            changed = True
        out.append(json.dumps(rec, sort_keys=True))
    if changed:
        st = results.stat()
        tmp = results.with_name(f".{results.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8", opener=paths.private_opener) as fh:
            fh.write("".join(x + "\n" for x in out))
        os.utime(tmp, (st.st_atime, st.st_mtime))      # pruning answers does not make the run look new
        os.replace(tmp, results)
    return changed


def prune(root: Path | None = None, *, now: float | None = None, keep: int = KEEP_RUNS,
          answer_days: float = ANSWER_DAYS) -> PruneReport:
    rep = PruneReport()
    root = Path(root) if root is not None else default_root()
    clock = time.time() if now is None else now
    try:
        runs = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
    except OSError:
        return rep
    protected = _open_reservations(root, clock)
    runs.sort(key=_mtime, reverse=True)
    for i, run in enumerate(runs):
        if i < keep or run.name in protected:
            rep.kept.append(run.name)
            continue
        try:
            shutil.rmtree(run)
            rep.deleted.append(run.name)
        except OSError as exc:
            rep.errors.append(f"{run.name}: {type(exc).__name__}")
    for name in rep.kept:
        results = root / name / "results.jsonl"
        try:
            if results.is_file() and not results.is_symlink() and clock - results.stat().st_mtime > answer_days * 86400:
                if _strip_answers(results):
                    rep.answers_pruned.append(name)
        except OSError as exc:
            rep.errors.append(f"{name}: {type(exc).__name__}")
    return rep
