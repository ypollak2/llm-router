"""Competence writer and fitness adapter for ``local_eligibility.json``.

``record_competence`` validates graded lanes; ``record_fitness`` merges fitness results.
Both preserve unrelated entries and write through the same lock.
Keys are bare Ollama names, as read by ``resource_map``.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from llm_router import paths
from llm_router.eligibility import TEXT_JOBS, grade, grade_text_job
from llm_router.file_lock import exclusive_lock


def eligibility_path() -> Path:
    return paths.state_path("local_eligibility.json")


def record_fitness(model: str, results: dict[str, Any], *, path: Path | None = None) -> bool:
    """Merge the conclusive ``results`` (task_type -> FitnessResult) under ``{model}.fitness``.

    Returns False, writing nothing, when no result is conclusive.
    """
    conclusive = {t: r for t, r in results.items() if r.n > 0}
    if not conclusive:
        return False
    target = Path(path) if path else eligibility_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(target.with_name(target.name + ".lock")):
        try:
            data = json.loads(target.read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        entry = data.get(model)
        entry = entry if isinstance(entry, dict) else {}
        fitness = entry.get("fitness")
        fitness = fitness if isinstance(fitness, dict) else {}
        fitness.update({t: {k: v for k, v in asdict(r).items() if k != "task_type"}
                        for t, r in conclusive.items()})
        entry["fitness"] = fitness
        entry["fitness_at"] = time.time()
        data[model] = entry
        tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
    return True


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _check_text_job_lane(job: Any, lane: Any) -> None:
    if job not in TEXT_JOBS:
        raise ValueError("invalid job")
    if not isinstance(lane, Mapping) or "grade" not in lane or grade_text_job(lane) != lane["grade"]:
        raise ValueError("mismatched grade")
    if lane["grade"] not in ("passed", "failed"):
        raise ValueError("invalid grade")
    verdict = lane.get("verdict")
    if verdict not in ("eligible", "failed"):
        raise ValueError("invalid verdict")
    if lane.get("eligible") is not (verdict == "eligible"):
        raise ValueError("inconsistent eligible")
    if (verdict == "eligible") != (lane["grade"] == "passed"):
        raise ValueError("verdict does not match grade")
    prereg, sha = lane.get("prereg"), lane.get("manifest_sha256")
    if not isinstance(prereg, str) or not prereg or not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        raise ValueError("text_job lane needs prereg id and manifest sha256")


def text_job_lines(*, path: Path | None = None) -> list[str]:
    """One line per (model, prereg, verdict): the text jobs a pre-registered run qualified.

    Visibility only; nothing here grants routing. Lanes whose stored grade disagrees with
    their own counts, or that lack provenance, are skipped (unknown is not shown as either).
    """
    try:
        data = json.loads(Path(path or eligibility_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    groups: dict[tuple, list[str]] = {}
    for model, entry in sorted(data.items()):
        task = entry.get("text_job") if isinstance(entry, dict) else None
        if not isinstance(task, dict):
            continue
        for job in TEXT_JOBS:
            lane = task.get(job)
            try:
                _check_text_job_lane(job, lane)
            except ValueError:
                continue
            key = (model, lane["prereg"], lane["grade"], lane["k"], lane["n"])
            groups.setdefault(key, []).append(job)
    word = {"passed": "qualified", "failed": "failed"}
    return [f"text_job: {', '.join(jobs)} \u2014 {word[g]} ({prereg}, {k}/{n}) [{model}]"
            for (model, prereg, g, k, n), jobs in groups.items()]


def record_competence(
    model: str, task_type: str, lanes: Mapping[str, Mapping[str, Any]],
    *, path: Path | None = None,
) -> bool:
    """Merge validated competence lanes, preserving fitness and unrelated entries.

    ``text_job`` (F124) is keyed by job (``eligibility.TEXT_JOBS``) instead of tier, and its
    lane is graded ``passed``/``failed`` by ``eligibility.grade_text_job`` (D-241) with the
    prereg provenance stored; no router reader looks up that task type.
    """
    if task_type not in ("qa", "tech_op", "code", "text_job"):
        raise ValueError("invalid task_type")
    if not isinstance(lanes, Mapping):
        raise ValueError("lanes must be a mapping")
    for tier, lane in lanes.items():
        if task_type == "text_job":
            _check_text_job_lane(tier, lane)
            continue
        if tier not in ("simple", "moderate"):
            raise ValueError("invalid tier")
        if not isinstance(lane, Mapping) or "grade" not in lane or grade(lane) != lane["grade"]:
            raise ValueError("mismatched grade")
        verdict = lane.get("verdict")
        if verdict not in ("eligible", "failed", "inconclusive", "not_informative"):
            raise ValueError("invalid verdict")
        if lane.get("eligible") is not (verdict == "eligible"):
            raise ValueError("inconsistent eligible")
        if verdict == "eligible" and lane["grade"] != "eligible":
            raise ValueError("eligible verdict requires eligible grade")
    if not lanes:
        return False
    target = Path(path) if path else eligibility_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.close(fd)
    lock_path.chmod(0o600)
    with exclusive_lock(lock_path) as locked:
        if not locked:
            raise OSError("eligibility lock unavailable")
        try:
            data = json.loads(target.read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        entry = data.get(model)
        entry = entry if isinstance(entry, dict) else {}
        task = entry.get(task_type)
        task = task if isinstance(task, dict) else {}
        task.update({tier: dict(lane) for tier, lane in lanes.items()})
        entry[task_type] = task
        data[model] = entry
        tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(data, stream, indent=2, sort_keys=True)
            os.replace(tmp, target)
            target.chmod(0o600)
        finally:
            tmp.unlink(missing_ok=True)
    return True
