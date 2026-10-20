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

Only local providers are matched (F109: ``qa_policy.QA_STRIP_PROVIDERS``, the router's own
local-provider detection, not a name list here): the file's keys are bare model names
(``eligibility_store``) and the provider prefix is dropped before lookup. An untagged name matches
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

import contextvars
import json
import os
from pathlib import Path
from typing import Any

from llm_router import eligibility, paths, skip_causes
from llm_router.qa_policy import QA_STRIP_PROVIDERS, QA_TASK_TYPES
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
#: F109: the router's own local-provider set (``qa_policy.QA_STRIP_PROVIDERS``, the D-116 test of
#: "runs on the user's machine"); no provider literal here.
MATCHED_PROVIDERS: frozenset[str] = QA_STRIP_PROVIDERS

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
    """Drop a local provider prefix; Ollama resolves an untagged name to ``:latest``, compare so."""
    head, sep, rest = name.partition("/")
    name = rest if sep and head in MATCHED_PROVIDERS else name
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


# ---- LC-8 / D-260: a passed local-check lane routes local-first, verified text-job path only ----------

_text_job_ctx: contextvars.ContextVar[tuple[str, int] | None] = contextvars.ContextVar(
    "llm_router_text_job_ctx", default=None)
#: The ``eligibility_exempt_reason`` ``llm_text_job`` already passes (F112); only it may be preferred.
TEXT_JOB_REASON = "verified_text_job"


def set_text_job_context(job: str, input_len: int) -> contextvars.Token:
    """``llm_text_job`` names its job and input size for the router's chain step; reset with the token."""
    return _text_job_ctx.set((job, int(input_len)))


def reset_text_job_context(token: contextvars.Token) -> None:
    _text_job_ctx.reset(token)


def _route_time_ctx(model: str) -> int | None:
    from llm_router import providers
    return providers._ollama_num_ctx(model)


def _route_time_think() -> bool:
    from llm_router import providers
    return providers._ollama_think_enabled()


def prefer_passed(
    models: list[str], job: str, input_len: int, now: float, *,
    digests: dict[str, str] | None = None, ctx_for: Any = None, think: bool | None = None,
    eligibility: Path | None = None, routing: Path | None = None,
) -> list[str]:
    """Move the first local model with a routable passed lane to the front; else the chain unchanged.

    Only models ALREADY in ``models`` are considered (D-116 / F106 / F112 have run; nothing is added).
    A model is preferred iff ALL hold: ``local_routing.json`` enables (model, job); the effective lane
    (user's fresh local-check lane, else pre-registered, D-262) is ``passed``; the lane is fresh against
    the inventory-cache digest (``local_check.display.inventory``; no HTTP); ``input_len`` fits the lane's
    ``scope.max_prompt_chars``; and the route-time Ollama settings (``think``, ``num_ctx``) hash to the
    lane's ``settings_hash``. Any unknown input means no preference. A pre-registered lane carries no
    scope or settings hash, so it is checked against the window the router will request instead
    (``input_len`` must fit it and thinking must be off). Never raises; the chain is returned as given
    on any error. The acceptance check and the Claude fallback in ``llm_text_job`` are unchanged.
    """
    try:
        return _prefer_passed(models, job, input_len, now, digests, ctx_for, think, eligibility, routing)
    except Exception as exc:  # noqa: BLE001 -- fail-open: the chain stays as built
        from llm_router import failopen
        failopen.record("CHZ-FO-ELIGIBILITY-PREFER-PASSED", exc)
        return models


def _prefer_passed(models, job, input_len, now, digests, ctx_for, think, eligibility, routing):
    from llm_router.local_check import routing as lr
    from llm_router.local_check import runtimes as rt
    from llm_router.resolver import eligibility_store as ES

    if not models or _opted_out() or not isinstance(input_len, int) or input_len < 0:
        return models
    if digests is None:
        from llm_router.local_check import display
        digests = display.inventory()[1]
    for idx, m in enumerate(models):
        head, sep, bare = m.partition("/")
        if not sep or head != "ollama" or not lr.is_enabled(bare, job, path=routing):
            continue
        lane, source = None, "unknown"
        for key in lr.candidates(bare):
            found = ES.effective_text_job_lane(key, job, digests.get(key), now, path=eligibility)
            if found.lane is not None:
                lane, source = found.lane, found.source
                break
        if lane is None or lane.get("grade") != "passed":
            continue
        use_think = _route_time_think() if think is None else think
        ctx = (ctx_for or _route_time_ctx)(m)
        if use_think or not ctx:
            continue
        if source == "local_check":
            scope = lane.get("scope")
            limit = scope.get("max_prompt_chars") if isinstance(scope, dict) else None
            want = lane.get("settings_hash")
            if not isinstance(limit, int) or isinstance(limit, bool) or not isinstance(want, str):
                continue
            if rt.settings_hash(temperature=rt.TEMPERATURE, ctx=ctx, max_tokens=rt.MAX_TOKENS, think=False) != want:
                continue
        else:
            limit = rt.max_prompt_chars(ctx)
        if limit is None or input_len > limit:
            continue
        return [m] + models[:idx] + models[idx + 1:]
    return models
