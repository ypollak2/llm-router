"""Pinned, opt-in catalog updates and hardware-independent recommendations."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.request import urlopen

from llm_router import paths

CATALOG_TTL_S = 7 * 86400
CODE_TASKS = ("edit_with_test", "code_explanation")
RAW_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/ypollak2/llm-router/"
    "v{version}/src/llm_router/catalog/models.json"
)
_BUNDLED_DIR = Path(__file__).parent / "catalog"


@dataclass(frozen=True)
class CatalogResult:
    catalog: dict
    source: Literal["bundled", "cache", "fetched"]
    reason: str


@dataclass(frozen=True)
class Recommendation:
    name: str
    runtime: str
    size_gb: float
    reason: str


def _decode(data: bytes) -> dict:
    catalog = json.loads(data)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        raise ValueError("catalog must contain a models list")
    return catalog


def load_bundled() -> dict:
    return _decode((_BUNDLED_DIR / "models.json").read_bytes())


def load_pins() -> dict:
    pins = json.loads((_BUNDLED_DIR / "pins.json").read_bytes())
    if not isinstance(pins, dict):
        raise ValueError("pins must be a dictionary")
    return pins


def default_fetcher(url: str) -> bytes:
    """Network access is opt-in: pass this function to load_catalog."""
    with urlopen(url, timeout=5) as response:
        return response.read()


def _atomic_write(destination: Path, data: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_catalog(
    *,
    fetcher: Callable[[str], bytes] | None = None,
    now: float | None = None,
    cache_dir: str | Path | None = None,
    version: str | None = None,
    pins: dict | None = None,
) -> CatalogResult:
    """Return verified data; failed updates always fall back to bundled data."""
    try:
        bundled = load_bundled()
    except Exception:
        bundled = {"schema": 1, "models": []}

    def fallback(reason: str) -> CatalogResult:
        return CatalogResult(bundled, "bundled", reason)

    if fetcher is None:
        return fallback("network disabled")
    try:
        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp):
            return fallback("invalid time")
        directory = paths.state_path("catalog") if cache_dir is None else Path(cache_dir)
        expected = (load_pins() if pins is None else pins)["models.json"]
        if not isinstance(expected, str):
            return fallback("invalid pin")
    except Exception:
        return fallback("invalid configuration")

    try:
        cached = (directory / "models.json").read_bytes()
        fetched_at = json.loads((directory / "fetched_at.json").read_bytes())["fetched_at"]
        age = timestamp - float(fetched_at)
        if 0 <= age < CATALOG_TTL_S and sha256(cached).hexdigest() == expected:
            return CatalogResult(_decode(cached), "cache", "fresh pinned cache")
    except Exception:
        pass

    try:
        if version is None:
            from llm_router import __version__

            version = __version__
        data = fetcher(RAW_URL_TEMPLATE.format(version=version))
    except Exception:
        return fallback("offline")
    try:
        if sha256(data).hexdigest() != expected:
            return fallback("pin mismatch")
        catalog = _decode(data)
    except Exception:
        return fallback("invalid catalog")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(directory / "models.json", data)
        _atomic_write(
            directory / "fetched_at.json",
            json.dumps({"fetched_at": timestamp}).encode("utf-8"),
        )
    except Exception:
        return fallback("cache write failed")
    return CatalogResult(catalog, "fetched", "verified pinned catalog")


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _measured_score(quality: dict, task: str) -> float | None:
    entry = quality.get(task)
    if not isinstance(entry, dict):
        return None
    count = _number(entry.get("n"))
    score = _number(entry.get("score"))
    return score if count is not None and count > 0 else None


def _code_score(quality: dict) -> float | None:
    scores = [_measured_score(quality, task) for task in CODE_TASKS]
    if any(score is None for score in scores):
        return None
    return min(scores)


def recommend(
    hardware: dict,
    answers: dict | None = None,
    *,
    catalog: dict | None = None,
    pass_marks: dict | None = None,
    eligibility: dict | None = None,
    limit: int = 3,
) -> list[Recommendation]:
    """Rank fitting models using measured code score, priority and memory fit only.

    Measured code score affects ranking only when main_use=code and a mark exists.
    main_use otherwise and languages do not affect ranking; languages add reason text.
    The code gate binds only once P2.0 sets marks.

    eligibility is reserved for onboarding integration; no policy is inferred from it.
    """
    usable = _number(hardware.get("usable_gb")) if isinstance(hardware, dict) else None
    if usable is None or usable < 0:
        return []
    answers = answers if isinstance(answers, dict) else {}
    main_use = answers.get("main_use", "mixed")
    if main_use not in ("code", "chat", "writing", "mixed"):
        main_use = "mixed"
    priority = answers.get("priority", "balanced")
    if priority not in ("speed", "quality", "balanced"):
        priority = "balanced"
    languages = answers.get("languages", ["en"])
    if not isinstance(languages, list) or not all(isinstance(x, str) for x in languages):
        languages = ["en"]
    pass_marks = pass_marks if isinstance(pass_marks, dict) else {}
    has_mark = main_use == "code" and "code" in pass_marks
    mark = _number(pass_marks.get("code"))
    if has_mark and mark is None:
        return []
    if catalog is None:
        catalog = load_bundled()
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        return []
    ranked: list[tuple[tuple[bool, float, float], Recommendation]] = []
    for model in catalog["models"]:
        if not isinstance(model, dict):
            continue
        name, runtime = model.get("name"), model.get("runtime")
        size = _number(model.get("size_gb"))
        if not isinstance(name, str) or not isinstance(runtime, str) or size is None:
            continue
        if size < 0:
            continue
        memory = model.get("mem_gb_by_ctx", {})
        values = memory.values() if isinstance(memory, dict) else ()
        # The largest listed context: the router asks Ollama for a 32k window by
        # default, so the smallest listed estimate would recommend models that swap.
        estimates = [v for value in values if (v := _number(value)) is not None and v >= 0]
        required = max(estimates) if estimates else size
        if required > usable:
            continue
        quality = model.get("quality", {})
        quality = quality if isinstance(quality, dict) else {}
        score = _code_score(quality) if has_mark else None
        if has_mark and (score is None or score < mark):
            continue
        facts = [
            f"size {size:g} GB",
            f"fits {usable:g} GB usable (estimated memory {required:g} GB)",
            f"measured code score {score:g}" if score is not None else "no measured quality yet",
        ]
        if main_use == "code" and not has_mark:
            facts.append("unknown for code (no pass mark yet)")
        if "he" in languages and "hebrew_qa" not in quality:
            facts.append("Hebrew is unmeasured")
        size_order = size if priority == "speed" else -size
        key = (score is None, -score if score is not None else 0.0, size_order)
        ranked.append((key, Recommendation(name, runtime, size, "; ".join(facts))))
    ranked.sort(key=lambda item: item[0])
    return [item[1] for item in ranked[:max(1, min(3, limit))]]
