"""Adapter that records fitness results in ``local_eligibility.json``.

The single writer for that file is meant to be #441's ``grade`` / ``record_competence``,
which is not on this base. This adapter touches only the ``fitness`` sub-key of a model's
entry and keeps every other key, so the two converge on the same file. When #441 lands,
replace the body of ``record_fitness`` with a call to it.

Keys are bare Ollama names (``qwen2.5-coder:7b``), the key ``resource_map`` reads.
Only conclusive task results (``n > 0``) are written, merged per task type, so a run
that hit transport errors never overwrites an earlier measurement.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from llm_router import paths
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
