"""Onboarding needs questions (PLAN v16 P2.8 task 3).

At most three questions, each with a default and a skip. The answers feed
``model_catalog.recommend(hardware, answers)`` through ``as_recommender_input()``,
a plain dict with ``main_use``, ``languages`` and ``priority``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

MAX_QUESTIONS = 3

_MAIN_USES = ("mixed", "code", "chat", "writing")
_LANGUAGES = ("en", "he", "other")
_PRIORITIES = ("balanced", "speed", "quality")

QUESTIONS = (
    {
        "field": "main_use",
        "prompt": "What will you mainly use local models for?",
        "choices": _MAIN_USES,
        "default": "mixed",
        "skip": "s",
    },
    {
        "field": "languages",
        "prompt": "Which languages do you work in? (comma-separated; Hebrew = he)",
        "choices": _LANGUAGES,
        "default": "en",
        "skip": "s",
    },
    {
        "field": "priority",
        "prompt": "Speed or quality?",
        "choices": _PRIORITIES,
        "default": "balanced",
        "skip": "s",
    },
)

_TASK_WEIGHTS = {
    "code": {"code": 1.0, "analyze": 0.3, "query": 0.2, "generate": 0.1, "research": 0.1},
    "chat": {"query": 1.0, "generate": 0.3, "research": 0.2, "analyze": 0.2, "code": 0.1},
    "writing": {"generate": 1.0, "query": 0.3, "analyze": 0.2, "research": 0.2, "code": 0.0},
    "mixed": {"query": 0.5, "code": 0.5, "generate": 0.5, "analyze": 0.5, "research": 0.5},
}
_QUALITY_WEIGHT = {"speed": 0.2, "balanced": 0.5, "quality": 0.8}


@dataclass(frozen=True)
class NeedsAnswers:
    main_use: str = "mixed"
    languages: tuple[str, ...] = ("en",)
    priority: str = "balanced"
    skipped: tuple[str, ...] = ()

    def task_weights(self) -> dict[str, float]:
        return dict(_TASK_WEIGHTS.get(self.main_use, _TASK_WEIGHTS["mixed"]))

    def requires_hebrew(self) -> bool:
        return "he" in self.languages

    def quality_weight(self) -> float:
        return _QUALITY_WEIGHT.get(self.priority, 0.5)

    def as_recommender_input(self) -> dict:
        """The dict ``model_catalog.recommend`` accepts as ``answers``."""
        return {
            "main_use": self.main_use,
            "languages": list(self.languages),
            "priority": self.priority,
        }


def _parse_languages(raw: str) -> tuple[str, ...] | None:
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    if not parts or any(p not in _LANGUAGES for p in parts):
        return None
    return tuple(dict.fromkeys(parts))


def ask_needs(
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[..., None] = print,
) -> NeedsAnswers:
    """Ask the three questions. Blank/invalid = default; 's' = default, recorded as skipped."""
    values: dict[str, object] = {q["field"]: q["default"] for q in QUESTIONS}
    values["languages"] = ("en",)
    skipped: list[str] = []
    interrupted = False
    for q in QUESTIONS:
        field = str(q["field"])
        if interrupted:
            skipped.append(field)
            continue
        output_fn(f"  {q['prompt']}")
        output_fn(f"    choices: {', '.join(q['choices'])}  [default {q['default']}, s = skip]")
        try:
            raw = input_fn("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            interrupted = True
            skipped.append(field)
            continue
        if raw in ("s", "skip"):
            skipped.append(field)
        elif field == "languages":
            parsed = _parse_languages(raw) if raw else None
            if parsed:
                values[field] = parsed
        elif raw in q["choices"]:
            values[field] = raw
    return NeedsAnswers(
        main_use=str(values["main_use"]),
        languages=values["languages"],  # type: ignore[arg-type]
        priority=str(values["priority"]),
        skipped=tuple(skipped),
    )
