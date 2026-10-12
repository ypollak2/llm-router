"""Task-type fitness tasks for local models (PLAN v16 P2.8 task 5).

Six synthetic tasks (no user text), each graded deterministically; model output is
never executed. The pass mark per task type is fixed by the P2.0 preregistration, which
does not exist yet, so ``PASS_MARKS`` is empty and frozen: with no mark the result is
``unknown``, never ``pass``. A transport failure is also ``unknown``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from llm_router.resolver import calibrate as cal

PASS_MARKS: Mapping[str, float] = MappingProxyType({})
MAX_TASKS = 10

_EDIT_SRC = "def is_even(n):\n    return n % 2 == 1\n"
_EDIT_OK = "def is_even(n):\n    return n % 2 == 0\n"
_SUMMARY_IN = ("The ferry to Haifa leaves at 08:15 from Pier 4. Tickets cost 32 shekels "
               "and bicycles ride free. The crossing takes forty minutes in calm weather.")
_CODE_IN = "def f(xs):\n    seen = set()\n    return [x for x in xs if not (x in seen or seen.add(x))]"


def _has(text: str, *words: str, need: int | None = None) -> float:
    t = (text or "").lower()
    hit = sum(1 for w in words if w.lower() in t)
    return 1.0 if hit >= (need if need is not None else len(words)) else 0.0


def _g_edit(text: str) -> float:
    obj = cal._extract_json(text)
    if not isinstance(obj, dict):
        return 0.0
    out = cal.apply_edit(_EDIT_SRC, str(obj.get("old_string", "")), str(obj.get("new_string", "")))
    return 1.0 if out is not None and cal._same_program(out, _EDIT_OK) else 0.0


def _g_json(text: str) -> float:
    o = cal._extract_json(text)
    return 1.0 if isinstance(o, dict) and o.get("city") == "Haifa" and o.get("count") == 3 else 0.0


@dataclass(frozen=True)
class FitnessTask:
    task_type: str
    prompt: str
    grade: Callable[[str], float]


TASKS: tuple[FitnessTask, ...] = (
    FitnessTask("tech_step", "Which single git command shows the commits not yet pushed to "
                "origin/main? Reply with the command only.",
                lambda t: _has(t, "git", "log", "origin/main")),
    FitnessTask("summary", "Summarise in one sentence under 25 words:\n" + _SUMMARY_IN,
                lambda t: _has(t, "08:15", "32", "forty", "40", need=2)
                if len((t or "").split()) < 30 else 0.0),
    FitnessTask("edit_with_test", "This function fails the test is_even(4) is True:\n\n" + _EDIT_SRC +
                '\nReply with only JSON: {"old_string": <exact text>, "new_string": <replacement>}.',
                _g_edit),
    FitnessTask("json", 'Reply with only a JSON object with key "city" set to "Haifa" and key '
                '"count" set to the integer 3.', _g_json),
    FitnessTask("hebrew_qa", "מהי בירת ישראל? ענה במילה אחת בעברית.", lambda t: _has(t, "ירושלים")),
    FitnessTask("code_explanation", "Explain in two sentences what this does:\n" + _CODE_IN,
                lambda t: _has(t, "duplicate", "order", "unique", "set", need=2)),
)


@dataclass(frozen=True)
class FitnessResult:
    task_type: str
    score: float | None
    n: int
    result: str          # "pass" | "fail" | "unknown"
    source: str = "fitness"


def run_fitness(c: cal.Completer, model: str, *, pass_marks: Mapping[str, float] | None = None,
                timeout: float = cal.DEFAULT_PROBE_TIMEOUT_S, trials: int = 1,
                tasks: tuple[FitnessTask, ...] | None = None) -> dict[str, FitnessResult]:
    marks = PASS_MARKS if pass_marks is None else pass_marks
    out: dict[str, FitnessResult] = {}
    for task in (tasks or TASKS)[:MAX_TASKS]:
        scores: list[float] = []
        for _ in range(max(1, trials)):
            try:
                r = c.chat(model, [{"role": "user", "content": task.prompt}], timeout=timeout)
            except Exception:  # noqa: BLE001
                continue
            if r.error:
                continue
            scores.append(float(task.grade(r.text)))
        if not scores:
            out[task.task_type] = FitnessResult(task.task_type, None, 0, "unknown")
            continue
        score = sum(scores) / len(scores)
        mark = marks.get(task.task_type)
        res = "unknown" if mark is None else ("pass" if score >= mark else "fail")
        out[task.task_type] = FitnessResult(task.task_type, score, len(scores), res)
    return out
