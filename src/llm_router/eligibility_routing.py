"""F106 / D-235: the router stops sending a local model the lanes it measurably failed.

``local_eligibility.json`` (shape ``{model: {task_type: {tier: lane}}}``, written by
``resolver.eligibility_store.record_competence``) holds measured lane verdicts. Before F106 only
``proxy/local_gate`` (off by default) and ``resource_map`` read it, so ``route_and_call`` kept
handing an Ollama model task types it had failed (P2.0 requal: qa 13/113 vs reference 96/113).

WHAT IS SKIPPED. An ``ollama/<name>`` chain entry whose lane for the call's (task_type, tier) has
``grade == "failed"`` AND ``verdict == "failed"``, and whose stored grade is what
``eligibility.grade`` computes from the lane's own fields. ``verdict == "failed"`` is the prereg's
POWERED failure (PREREG-P20-requal section 10). A ``failed`` grade with verdict ``inconclusive``
is power-limited, which section 10 calls "no decision", so it changes nothing here. ``unknown``,
``probe``, ``not_informative``, ``eligible``, a missing lane, model, file or key: no effect.
Unknown is never read as failed. ``eligible`` grants nothing either: authority is a separate,
owner-gated switch.

TASK-TYPE MAPPING (honest scope, PREREG-P20-requal section 4).

* ``qa`` lane: the qa set's population is items whose task type is in
  ``qa_policy.QA_TASK_TYPES``. The router task types in that set (query, research, generate,
  analyze, coordinate, introspect) map to it.
* ``code`` lane: the code set (hidden-test coding tasks) maps to router ``code``.
* ``tech_op`` lane: no router task type. Unmapped, so no effect.
* Tier: router complexity ``simple``/``moderate`` maps to the lane tier of the same name.
  ``complex``/``deep_reasoning`` have no lane (the sets drop complex items), so no effect. The
  router reads the call's own complexity (``complexity_hint``, else the classifier's
  ``classification_data["complexity"]``), also when the caller passes an explicit ``profile=``.

Only the ``ollama`` provider is matched: the file's keys are bare Ollama names
(``eligibility_store``), and the measured identity is an Ollama model. An untagged name matches
its ``:latest`` form, as Ollama resolves it. Model names come only from the file and the chain.

VISIBILITY. Each skip is a ``skip_causes`` entry (``failed_eligibility`` with lane, k, n, source),
which ``routing_decisions.skipped_json`` stores and ``llm-router why`` shows. No prompt text.

CACHE. The parsed file is cached keyed by (path, mtime_ns, size); a call does one ``stat``. A
corrupt or unreadable file records a fail-open event (exception class only) once per file
version, and a file that cannot be ``stat``-ed (other than missing) once per (path, error
class); either way the chain is unchanged. The ``source`` id is cut to ``SOURCE_MAX`` chars.

DIGEST. A lane is keyed by the Ollama name only, not its digest: after ``ollama pull`` of the
same tag the old verdict still applies until the model is re-qualified.

OPT-OUT. ``LLM_ROUTER_IGNORE_ELIGIBILITY=1`` turns the filter off (byte-identical pre-F106 chain).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from llm_router import eligibility, paths, skip_causes
from llm_router.qa_policy import QA_TASK_TYPES
from llm_router.types import TaskType

ELIGIBILITY_NAME = "local_eligibility.json"

#: Router task type -> eligibility lane. Derived, not hand-listed: every router TaskType whose
#: value is in the prereg's qa population, plus code. Anything absent here has no lane.
TASK_LANE: dict[str, str] = {
    **{t.value: "qa" for t in TaskType if t.value in QA_TASK_TYPES},
    TaskType.CODE.value: "code",
}
LANE_TIERS: frozenset[str] = frozenset({"simple", "moderate"})
#: The verdicts acted on. ``inconclusive`` (power-limited) is "no decision" in the prereg.
ACT_ON_VERDICTS: frozenset[str] = frozenset({"failed"})
MATCHED_PROVIDERS: frozenset[str] = frozenset({"ollama"})

_FO_UNREADABLE = "CHZ-FO-ELIGIBILITY-ROUTING-UNREADABLE"
#: Longest ``source`` id written to the skip record (ledger ``skipped_json``, ``why``).
SOURCE_MAX = 64

# (path, mtime_ns, size) -> normalised index (None = file present but unusable).
_cache: tuple[tuple[str, int, int], dict[str, dict] | None] | None = None
# (path, error class) pairs whose ``stat`` failure was already recorded.
_stat_failed: set[tuple[str, str]] = set()


def eligibility_path() -> Path:
    return paths.state_path(ELIGIBILITY_NAME)


def _opted_out() -> bool:
    raw = os.environ.get("LLM_ROUTER_IGNORE_ELIGIBILITY", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _norm(name: str) -> str:
    """Ollama resolves an untagged name to ``:latest``; compare in that form."""
    name = name.split("/", 1)[1] if name.startswith("ollama/") else name
    return name if ":" in name else f"{name}:latest"


def _parse(text: str) -> dict[str, dict] | None:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("eligibility file is not a JSON object")
    return {_norm(k): v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}


def load(path: Path | None = None) -> dict[str, dict] | None:
    """The normalised ``{model: entry}`` index, or None (missing / unreadable / corrupt)."""
    global _cache
    target = Path(path) if path else eligibility_path()
    try:
        st = target.stat()
    except FileNotFoundError:
        return None  # no measurements yet: the normal state, not a failure
    except OSError as exc:
        seen = (str(target), type(exc).__name__)
        if seen not in _stat_failed:  # once per (path, error class), not on every call
            _stat_failed.add(seen)
            from llm_router import failopen
            failopen.record(_FO_UNREADABLE, exc)
        return None
    key = (str(target), st.st_mtime_ns, st.st_size)
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    try:
        index = _parse(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        from llm_router import failopen
        failopen.record(_FO_UNREADABLE, exc)  # class only, once per file version
        index = None
    _cache = (key, index)
    return index


def reset_cache() -> None:
    global _cache
    _cache = None
    _stat_failed.clear()


def lane_key(task_type: Any, complexity: Any) -> tuple[str, str] | None:
    """(lane, tier) for a router call, or None when no measured lane covers it."""
    tt = getattr(task_type, "value", task_type)
    tier = getattr(complexity, "value", complexity)
    lane = TASK_LANE.get(tt) if isinstance(tt, str) else None
    if lane is None or tier not in LANE_TIERS:
        return None
    return lane, tier


def failed_lane(index: dict[str, dict], model: str, lane: str, tier: str) -> dict | None:
    """The lane record when ``model`` measurably failed (lane, tier), else None."""
    provider = model.split("/", 1)[0] if "/" in model else ""
    if provider not in MATCHED_PROVIDERS:
        return None
    task = index.get(_norm(model), {}).get(lane)
    rec = task.get(tier) if isinstance(task, dict) else None
    if not isinstance(rec, dict):
        return None
    if rec.get("grade") != "failed" or rec.get("verdict") not in ACT_ON_VERDICTS:
        return None
    if eligibility.grade(rec) != "failed":
        return None  # stored grade disagrees with its own counts: not trusted, no effect
    return rec


def _count_or_none(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def filter_chain(models: list[str], task_type: Any, complexity: Any) -> list[str]:
    """Drop local entries whose lane for this call measurably failed; record each drop."""
    if not models or _opted_out():
        return models
    key = lane_key(task_type, complexity)
    if key is None:
        return models
    index = load()
    if not index:
        return models
    lane, tier = key
    kept: list[str] = []
    for m in models:
        rec = failed_lane(index, m, lane, tier)
        if rec is None:
            kept.append(m)
            continue
        source = rec.get("source")
        skip_causes.note(
            m, skip_causes.FAILED_ELIGIBILITY, lane=f"{lane}/{tier}",
            k=_count_or_none(rec.get("k")), n=_count_or_none(rec.get("n")),
            source=source[:SOURCE_MAX] if isinstance(source, str) else None,
        )
    return kept


def note_bypass(models: list[str], task_type: Any, complexity: Any, reason: str) -> list[str]:
    """F112 / D-242: the chain unchanged; record each entry :func:`filter_chain` would have dropped.

    For callers whose output a mechanical acceptance check gates, with a fallback. ``reason`` is
    the caller's id (``source`` of the skip entry). No prompt text.
    """
    if not models or _opted_out():
        return models
    key = lane_key(task_type, complexity)
    index = load() if key is not None else None
    if key is None or not index:
        return models
    lane, tier = key
    for m in models:
        if failed_lane(index, m, lane, tier) is not None:
            skip_causes.note(m, skip_causes.ELIGIBILITY_BYPASSED, lane=f"{lane}/{tier}",
                             source=reason[:SOURCE_MAX])
    return models
