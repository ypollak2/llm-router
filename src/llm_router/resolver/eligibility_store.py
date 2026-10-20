"""Competence writer and fitness adapter for ``local_eligibility.json``.

``record_competence`` validates graded lanes; ``record_fitness`` merges fitness results.
Both preserve unrelated entries and write through the same lock.
Keys are bare Ollama names, as read by ``resource_map``.
"""

from __future__ import annotations

import calendar
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
    source = lane.get("source")
    if source is not None and source not in ("prereg", "local_check"):
        raise ValueError("invalid source")
    if source == "local_check":
        # User-run local-check provenance (LC-0). Other optional fields (model_digest, runtime,
        # judge, run_id, date, scope, attempts, rule_concentration) are not validated here and
        # unknown extra fields are ignored by readers.
        for key in ("bank_version", "settings_hash"):
            if not isinstance(lane.get(key), str) or not lane[key]:
                raise ValueError(f"local_check lane needs {key}")
        bank_sha = lane.get("bank_sha256")
        if not isinstance(bank_sha, str) or not _SHA256.fullmatch(bank_sha):
            raise ValueError("local_check lane needs bank_sha256")


_DIGEST_UNVERIFIED_DAYS = 30


def _lane_epoch(lane: Mapping[str, Any]) -> float | None:
    """UTC midnight of the lane's ``date`` (or ``recorded_at``), or None if absent/unparseable."""
    for key in ("date", "recorded_at"):
        value = lane.get(key)
        if isinstance(value, str):
            try:
                return calendar.timegm(time.strptime(value[:10], "%Y-%m-%d"))
            except ValueError:
                return None
    return None


def lane_is_fresh(lane: Mapping[str, Any], current_digest: str | None, now: float) -> bool:
    """Whether a text_job lane still describes the model now installed (LC-0; no routing use yet).

    A lane with ``model_digest`` is fresh iff ``current_digest`` equals it; a mismatch is stale
    (the lane is kept for history and reads as unknown for routing). A lane with no stored
    digest, or a stored digest that cannot be checked (``current_digest`` None), is
    digest_unverified and valid for 30 days from its date; no or unparseable date is stale.
    ``now`` is epoch seconds.
    """
    stored = lane.get("model_digest") if isinstance(lane, Mapping) else None
    if isinstance(stored, str) and stored and isinstance(current_digest, str) and current_digest:
        return stored == current_digest
    when = _lane_epoch(lane) if isinstance(lane, Mapping) else None
    return when is not None and 0 <= now - when <= _DIGEST_UNVERIFIED_DAYS * 86400


def _bank_label(version: str) -> str:
    return "bank v" + (version[1:] if version[:1] in ("v", "V") else version)


def text_job_lines(*, path: Path | None = None,
                   current_digests: Mapping[str, str] | None = None,
                   now: float | None = None) -> list[str]:
    """One line per (model, prereg, verdict): the text jobs a pre-registered run qualified.

    Visibility only; nothing here grants routing. Lanes whose stored grade disagrees with
    their own counts, or that lack provenance, are skipped (unknown is not shown as either).
    User-run ``local_check`` lanes are shown as ``<model> \u2014 jobs passed k/n (bank vN, date)``;
    when ``current_digests`` has the model's digest and ``lane_is_fresh`` says stale, the line
    reads ``stale \u2014 model changed``. Without the model's current digest no staleness is shown.
    """
    try:
        data = json.loads(Path(path or eligibility_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    groups: dict[tuple, list[str]] = {}
    lc_groups: dict[tuple, list[str]] = {}
    clock = time.time() if now is None else now
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
            if lane.get("source") == "local_check":
                cur = current_digests.get(model) if current_digests else None
                stale = bool(cur) and not lane_is_fresh(lane, cur, clock)
                lc_key = (model, lane["grade"], lane["k"], lane["n"], lane["bank_version"],
                          lane.get("date") or lane.get("recorded_at") or "", stale)
                lc_groups.setdefault(lc_key, []).append(job)
                continue
            key = (model, lane["prereg"], lane["grade"], lane["k"], lane["n"])
            groups.setdefault(key, []).append(job)
    word = {"passed": "qualified", "failed": "failed"}
    lines = [f"text_job: {', '.join(jobs)} \u2014 {word[g]} ({prereg}, {k}/{n}) [{model}]"
             for (model, prereg, g, k, n), jobs in groups.items()]
    for (model, g, k, n, bank, date, stale), jobs in lc_groups.items():
        names = ", ".join(jobs)
        if stale:
            lines.append(f"{model} \u2014 {names} stale \u2014 model changed")
        else:
            when = f", {date}" if date else ""
            lines.append(f"{model} \u2014 {names} {'passed' if g == 'passed' else 'failed'} "
                         f"{k}/{n} ({_bank_label(bank)}{when})")
    return lines


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


_PROVENANCE_KEYS = ("bank_version", "bank_sha256", "settings_hash", "model_digest", "runtime",
                    "judge", "run_id", "date", "scope", "attempts")
_RECORDABLE = {"PASS": "passed", "FAIL": "failed"}


def record_local_check(
    model: str, jobs_result: Mapping[str, Mapping[str, Any]], provenance: Mapping[str, Any],
    *, path: Path | None = None,
) -> bool:
    """Record a user-run local-check result as ``{model}.text_job.{job}`` lanes (LC-0).

    ``jobs_result`` maps job -> ``{"k", "n", "verdict": "PASS"|"FAIL"|"INCONCLUSIVE"|"INVALID"}``
    (optional ``rule_concentration``). Jobs with an INCONCLUSIVE or INVALID verdict are never
    written; returns False when nothing was written. A PASS/FAIL whose counts grade otherwise
    under D-241 (``eligibility.grade_text_job``) is refused with ValueError, and nothing is
    written. ``provenance`` needs ``bank_version``, ``bank_sha256`` and ``settings_hash`` and may
    carry ``model_digest``, ``runtime``, ``judge``, ``run_id``, ``date``, ``scope``, ``attempts``.
    Goes through ``record_competence``: same lock, atomic 0600 write. No prompt text is stored.
    """
    if not isinstance(jobs_result, Mapping) or not isinstance(provenance, Mapping):
        raise ValueError("jobs_result and provenance must be mappings")
    prov = {k: v for k, v in provenance.items() if k in _PROVENANCE_KEYS}
    bank_sha = prov.get("bank_sha256")
    lanes: dict[str, dict[str, Any]] = {}
    for job, res in jobs_result.items():
        if job not in TEXT_JOBS:
            raise ValueError("invalid job")
        verdict = res.get("verdict") if isinstance(res, Mapping) else None
        if verdict in ("INCONCLUSIVE", "INVALID"):
            continue
        if verdict not in _RECORDABLE:
            raise ValueError("invalid verdict")
        k, n = res.get("k"), res.get("n")
        grade_ = grade_text_job({"k": k, "n": n})
        if grade_ != _RECORDABLE[verdict]:
            raise ValueError("grade does not match counts")
        lane: dict[str, Any] = {
            **{k_: v for k_, v in prov.items() if v is not None},
            "grade": grade_, "verdict": "eligible" if grade_ == "passed" else "failed",
            "eligible": grade_ == "passed", "source": "local_check",
            "prereg": f"local-check/{prov.get('bank_version')}", "manifest_sha256": bank_sha,
            "k": k, "n": n,
        }
        lane.setdefault("attempts", 1)
        lane.setdefault("date", time.strftime("%Y-%m-%d", time.gmtime()))
        if isinstance(res.get("rule_concentration"), (dict, list, int, float, str)):
            lane["rule_concentration"] = res["rule_concentration"]
        lanes[job] = lane
    for job, lane in lanes.items():
        _check_text_job_lane(job, lane)
    if not lanes:
        return False
    return record_competence(model, "text_job", lanes, path=path)
