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
import time
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


_require_passed_first: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "llm_router_text_job_require_passed_first", default=False)


class PassedModelNotInChain(RuntimeError):
    """LC-12b: ``llm_text_job`` runs on a local pass alone (flag unset) and the router's chain does not put a
    routable passed Ollama model first. Raised before any model is called, so no cloud model serves the job."""


class LiveCheckRefused(PassedModelNotInChain):
    """LC-12f: the pre-dispatch read of the model's live ``/api/tags`` row no longer matches its pass
    (digest changed, remote metadata, model gone, or the daemon did not answer); the text job is refused
    before any model call."""


class EligibilityStepFailed(PassedModelNotInChain):
    """LC-12e: the local-only eligibility step itself raised; the text job is refused (fail closed), not routed."""


#: LC-12e: fail-CLOSED refusals of a pass-only text job (counted apart from fail-open events).
FC_STORE_NAME = "text_job_fail_closed.jsonl"
FC_ELIGIBILITY_STEP = "CHZ-FC-ROUTER-TEXT-JOB-ELIGIBILITY"
FC_CUT_CHECK = "CHZ-FC-ROUTER-TEXT-JOB-CUT-CHECK"
FC_LIVE_CHECK = "CHZ-FC-ROUTER-TEXT-JOB-LIVE-CHECK"
LIVE_CHECK_TIMEOUT_S = 2.0
#: Cap per generation (``capped_log``, two generations); a row is ~80 bytes, so ~3,000 rows each.
FC_MAX_BYTES = 256 * 1024
#: The doctor counts refusals of the last 7 days: one old crash does not alarm forever.
FC_WINDOW_S = 7 * 86400


def record_fail_closed(code: str, exc: BaseException | None = None) -> None:
    """Persist one refusal; recording failures never prevent the refusal."""
    try:
        from llm_router import capped_log
        row = {"c": code, "ts": round(time.time(), 3)}
        if exc is not None:
            row["e"] = type(exc).__name__
        capped_log.append(paths.state_path(FC_STORE_NAME),
                          (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"), FC_MAX_BYTES)
    except Exception as store_exc:  # noqa: BLE001 -- recording must never prevent refusal
        try:
            from llm_router import failopen
            failopen.record("CHZ-FO-FAIL-CLOSED-UNRECORDED", store_exc, detail=code)
        except Exception:  # noqa: BLE001 -- even the fallback must never raise
            pass


def _tail_lines(path: Path, max_bytes: int) -> list[str] | None:
    """The complete lines of the last ``max_bytes`` of ``path``; [] if missing, None if unreadable."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            start = max(0, size - max_bytes)
            fh.seek(start)
            data = fh.read()
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001 -- unreadable counts are unknown
        return None
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if start > 0 else lines  # the first line of a tail read may be cut


def fail_closed_counts(now: float | None = None, window_s: float = FC_WINDOW_S) -> dict[str, int] | None:
    """Refusals per code with ``ts`` in the last ``window_s`` seconds, over both ``capped_log`` generations.

    Each generation is tail-read to its cap, so the read stays bounded even for a file written before the
    cap existed. A store that holds lines but no valid row is unknown (None), never zero."""
    try:
        cutoff = (time.time() if now is None else now) - window_s
        base = paths.state_path(FC_STORE_NAME)
        lines: list[str] = []
        for gen in (base.with_name(base.name + ".1"), base):
            got = _tail_lines(gen, FC_MAX_BYTES + 4096)
            if got is None:
                return None
            lines.extend(got)
        counts: dict[str, int] = {}
        nonempty = valid = False
        for line in lines:
            if not line.strip():
                continue
            nonempty = True
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(row, dict) or not isinstance(row.get("c"), str) or not row["c"]:
                continue
            ts = row.get("ts")
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                continue
            valid = True
            if ts >= cutoff:
                counts[row["c"]] = counts.get(row["c"], 0) + 1
        return None if nonempty and not valid else counts
    except Exception:  # noqa: BLE001 -- counting must never raise
        return None


def _pass_only_refusal(code: str, exc: BaseException) -> None:
    """LC-12e: on the pass-only text-job path, a crashed eligibility helper is a counted fail-closed refusal
    (:class:`EligibilityStepFailed`), not a fail-open event that would read as "not in the chain"."""
    if _require_passed_first.get():
        record_fail_closed(code, exc)
        raise EligibilityStepFailed(
            f"the eligibility step failed ({type(exc).__name__}); local-only routing refuses") from exc


def set_require_passed_first() -> contextvars.Token:
    """Flag unset: the router must lead the text-job chain with a passed local model, or raise
    :class:`PassedModelNotInChain`. Reset with the token."""
    return _require_passed_first.set(True)


def reset_require_passed_first(token: contextvars.Token) -> None:
    _require_passed_first.reset(token)


def passed_model_leads(models: list[str], job: str, input_len: int, now: float) -> bool:
    """True iff ``models[0]`` is an Ollama model with a routable passed lane for ``job`` (the ``prefer_passed``
    guards). False on any error (fail closed); on the pass-only text-job path the error raises
    :class:`EligibilityStepFailed` instead (LC-12e), otherwise it never raises."""
    try:
        if not models or _opted_out() or not isinstance(input_len, int) or input_len < 0:
            return False
        from llm_router.local_check import display
        digests = display.inventory()[1]
        return _routable(models[0], job, input_len, digests, None, None, None, None, now)
    except Exception as exc:  # noqa: BLE001 -- fail-closed: an unverifiable chain is not taken
        _pass_only_refusal(FC_ELIGIBILITY_STEP, exc)
        from llm_router import failopen
        failopen.record("CHZ-FO-ELIGIBILITY-PASSED-LEADS", exc)
        return False


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
    A model is preferred iff ALL hold:

    * ``local_routing.json`` enables (model, job);
    * the effective lane (the user's fresh local-check lane, else pre-registered, D-262) is ``passed``;
    * a local-check lane is VERIFIED against the model digest in the inventory cache (``local_check.display.inventory``,
      no HTTP): the cache must be fresh and hold a digest equal to the lane's. A missing or stale cache, or no
      digest, means no preference (the 30-day rule of ``lane_is_fresh`` is for display only);
    * an entry enabled from a ``local_check`` source never falls back to a pre-registered lane when its own lane
      is stale (only ``prereg``/``manual`` entries may use one);
    * settings parity (LC-8b), for a lane that recorded the ctx it was measured with: route-time ``think`` is off,
      the router's ``num_ctx`` is >= the measured one, and ``input_len`` <= min(``scope.max_prompt_chars``,
      ``max_prompt_chars(router ctx)``). Temperature is NOT part of the claim: the lane measured at 0 and the router
      calls with 0.2; the acceptance check, not the temperature, gates every answer. A lane recorded before ctx was
      stored keeps the old rule (its ``settings_hash`` must equal the hash of the route-time settings);
    * a pre-registered lane carries no scope or settings: ``input_len`` must fit the window the router will
      request and thinking must be off.

    When a passed lane is withheld for a reason of this list the router did not fail on, a ``not_preferred``
    skip cause names it (``why`` shows it), including ``input_over_limit``;
    ``local_routing.doctor_lines`` reports the settings ones. Never
    raises (except on the pass-only text-job path, where an error raises :class:`EligibilityStepFailed`, LC-12e);
    the chain is returned as given on any error. The acceptance check and the Claude fallback in
    ``llm_text_job`` are unchanged.
    """
    try:
        return _prefer_passed(models, job, input_len, now, digests, ctx_for, think, eligibility, routing)
    except Exception as exc:  # noqa: BLE001 -- fail-open: the chain stays as built
        _pass_only_refusal(FC_ELIGIBILITY_STEP, exc)
        from llm_router import failopen
        failopen.record("CHZ-FO-ELIGIBILITY-PREFER-PASSED", exc)
        return models


def _measured_ctx(lane: dict) -> int | None:
    v = lane.get("ctx")
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def lane_settings_blocker(lane: dict, source: str, ctx: int | None, use_think: bool) -> tuple[str | None, int | None]:
    """(cause, prompt-chars limit) for a passed lane at the route-time settings; cause None = settings allow it.

    Causes: ``think_on``, ``ctx_unknown``, ``lane_think`` (measured with thinking on), ``ctx_below_measured``,
    ``settings_mismatch`` (old lane without ctx), ``no_scope``.
    """
    from llm_router.local_check import runtimes as rt
    if use_think:
        return "think_on", None
    if not ctx:
        return "ctx_unknown", None
    if source != "local_check":
        return None, rt.max_prompt_chars(ctx)
    scope = lane.get("scope")
    scope_limit = scope.get("max_prompt_chars") if isinstance(scope, dict) else None
    if not isinstance(scope_limit, int) or isinstance(scope_limit, bool):
        return "no_scope", None
    measured = _measured_ctx(lane)
    if measured is not None and isinstance(lane.get("think"), bool):
        if lane["think"]:
            return "lane_think", None
        if ctx < measured:
            return "ctx_below_measured", None
        window = rt.max_prompt_chars(ctx)
        return None, (min(scope_limit, window) if window is not None else None)
    want = lane.get("settings_hash")  # a lane recorded before ctx/think were stored
    if not isinstance(want, str) or rt.settings_hash(
            temperature=rt.TEMPERATURE, ctx=ctx, max_tokens=rt.MAX_TOKENS, think=False) != want:
        return "settings_mismatch", None
    return None, scope_limit


def _passed_lane(m_bare: str, job: str, digests: dict[str, str], now: float, eligibility: Path | None,
                 entry_source: Any) -> tuple[dict | None, str, str | None]:
    """(lane, source, withheld-cause) for the effective passed lane of one enabled model."""
    from llm_router.local_check import routing as lr
    from llm_router.resolver import eligibility_store as ES
    lane, source, key = None, "unknown", lr.candidates(m_bare)[0]
    for k in lr.candidates(m_bare):
        found = ES.effective_text_job_lane(k, job, digests.get(k), now, path=eligibility)
        if found.lane is not None:
            lane, source, key = found.lane, found.source, k
            break
    if lane is None or lane.get("grade") != "passed":
        return None, source, None
    if source == "prereg" and entry_source == "local_check":
        return None, source, "local_lane_stale"
    if source == "local_check":
        stored, cur = lane.get("model_digest"), digests.get(key)
        if not (isinstance(stored, str) and stored and isinstance(cur, str) and cur and stored == cur):
            return None, source, "digest_unverified"
    return lane, source, None


def _live_tags() -> Any | None:
    """The user's Ollama daemon's ``/api/tags`` answer, or None. The daemon is the one LiteLLM sends the
    generation to (``OLLAMA_API_BASE``, else ``http://localhost:11434``); a non-loopback endpoint is refused."""
    from llm_router.local_check import runtimes as rt
    base = os.environ.get("OLLAMA_API_BASE", "").strip() or "http://localhost:11434"
    try:
        ok, data = rt._fetch(base, "GET", "/api/tags", timeout=LIVE_CHECK_TIMEOUT_S)
    except rt.RuntimeError_:  # a non-loopback daemon: the text would leave the machine
        return None
    return data if ok else None


def live_check_refusal(model: str, job: str, now: float) -> str | None:
    """LC-12f: pre-dispatch check of a pass-only text job, right before the passed model is called.

    One GET of ``/api/tags`` to the user's own (loopback) Ollama daemon, with a short timeout.
    Returns None when the live row still matches the pass, else "ollama did not answer" (error,
    timeout or non-loopback endpoint: fail closed), "model not listed", "remote metadata"
    (``ollama_remote_marker`` on the row), "digest changed" (a local-check lane's stored
    ``model_digest`` differs from the live row's digest, normalised to the adapters' ``sha256:``
    prefix), or "no passed lane". A pre-registered lane is keyed by name, not digest, so only
    the remote check applies to it. Unexpected errors raise; the cut site turns them into
    EligibilityStepFailed.
    """
    head, _, bare = model.partition("/")
    if head != "ollama":
        return "no passed lane"
    from llm_router.local_check import display, routing as lr

    e = lr.entry(bare, job)
    lane, source, _ = _passed_lane(bare, job, display.inventory()[1], now, None, (e or {}).get("source"))
    if lane is None:
        return "no passed lane"
    data = _live_tags()
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return "ollama did not answer"
    rows = [m for m in data["models"] if isinstance(m, dict)]
    names = set(lr.candidates(bare))
    row = next((m for m in rows if m.get("name") in names or m.get("model") in names), None)
    if row is None:
        return "model not listed"
    from llm_router.resolver.inventory import ollama_remote_marker

    if ollama_remote_marker(row, None):
        return "remote metadata"
    if source == "local_check":
        live = row.get("digest")
        if isinstance(live, str) and live and not live.startswith("sha256:"):
            live = "sha256:" + live
        if not isinstance(live, str) or not live or live != lane.get("model_digest"):
            return "digest changed"
    return None


def _routable(m: str, job: str, input_len: int, digests, ctx_for, think, eligibility, routing, now: float) -> bool:
    """True iff ``ollama/<bare>`` ``m`` has a passed lane for ``job`` that clears every guard of ``prefer_passed``.

    An Ollama cloud model (``-cloud`` / ``:cloud`` tag) is never routable: its prompts leave the machine (LC-12e).
    A model whose cached discovery entry says ``remote: true`` is never routable either (LC-12f).
    """
    from llm_router.local_check import routing as lr
    from llm_router.resolver.inventory import is_ollama_cloud_model
    head, sep, bare = m.partition("/")
    if is_ollama_cloud_model(m):
        return False
    if not sep or head != "ollama" or not lr.is_enabled(bare, job, path=routing):
        return False
    from llm_router.local_check import display
    if display.cached_remote(bare):  # LC-12f: discovery saw remote_host / remote_model for this name
        skip_causes.note(m, skip_causes.NOT_PREFERRED, lane=job, detail="remote_cached")
        return False
    e = lr.entry(bare, job, path=routing)
    lane, source, withheld = _passed_lane(bare, job, digests, now, eligibility, (e or {}).get("source"))
    if withheld:
        skip_causes.note(m, skip_causes.NOT_PREFERRED, lane=job, detail=withheld)
    if lane is None:
        return False
    use_think = _route_time_think() if think is None else think
    ctx = (ctx_for or _route_time_ctx)(m)
    cause, limit = lane_settings_blocker(lane, source, ctx, use_think)
    if cause:
        skip_causes.note(m, skip_causes.NOT_PREFERRED, lane=job, detail=cause, ctx=ctx, measured=_measured_ctx(lane))
        return False
    if limit is None or input_len > limit:
        if limit is not None:
            skip_causes.note(
                m, skip_causes.NOT_PREFERRED, lane=job, detail="input_over_limit",
                limit=limit, input_len=input_len,
            )
        return False
    return True


def _prefer_passed(models, job, input_len, now, digests, ctx_for, think, eligibility, routing):
    if not models or _opted_out() or not isinstance(input_len, int) or input_len < 0:
        return models
    if digests is None:
        from llm_router.local_check import display
        digests = display.inventory()[1]
    for idx, m in enumerate(models):
        if _routable(m, job, input_len, digests, ctx_for, think, eligibility, routing, now):
            return [m] + models[:idx] + models[idx + 1:]
    return models


def passed_lane_available(
    job: str, input_len: int, now: float, *,
    digests: dict[str, str] | None = None, ctx_for: Any = None, think: bool | None = None,
    eligibility: Path | None = None, routing: Path | None = None,
) -> bool:
    """D-272: True iff some model enabled in ``local_routing.json`` has a lane for ``job`` that
    ``prefer_passed`` would move to the front (same guards: enabled, passed, fresh, digest-verified,
    settings-matched, input fits). The opt-in gate of ``llm_text_job`` when the env flag is unset.
    Never raises; False on any error."""
    try:
        from llm_router.local_check import routing as lr
        if _opted_out() or not isinstance(input_len, int) or input_len < 0:
            return False
        if digests is None:
            from llm_router.local_check import display
            digests = display.inventory()[1]
        return any(
            _routable(f"ollama/{model}", job, input_len, digests, ctx_for, think, eligibility, routing, now)
            for model in sorted(lr._read(routing))
        )
    except Exception as exc:  # noqa: BLE001 -- fail-closed: no lane means the job stays with Claude
        from llm_router import failopen
        failopen.record("CHZ-FO-ELIGIBILITY-PASSED-AVAILABLE", exc)
        return False


def settings_notices(*, digests: dict[str, str] | None = None, ctx_for: Any = None, think: bool | None = None,
                     eligibility: Path | None = None, routing: Path | None = None, now: float | None = None) -> list[str]:
    """``doctor`` lines for enabled, passed lanes that route-time settings keep from being preferred. Never raises."""
    import time as _time
    out: list[str] = []
    try:
        from llm_router.local_check import routing as lr
        if digests is None:
            from llm_router.local_check import display
            digests = display.inventory()[1]
        clock = _time.time() if now is None else now
        use_think = _route_time_think() if think is None else think
        for model, jobs in sorted(lr._read(routing).items()):
            for job, e in sorted(jobs.items() if isinstance(jobs, dict) else []):
                if not (isinstance(e, dict) and e.get("enabled") is True):
                    continue
                lane, source, _ = _passed_lane(model, job, digests, clock, eligibility, e.get("source"))
                if lane is None:
                    continue
                ctx = (ctx_for or _route_time_ctx)(f"ollama/{model}")
                cause, _limit = lane_settings_blocker(lane, source, ctx, use_think)
                if cause:
                    out.append(f"local-check: {model} - {job} passed but not preferred for routing ({cause}; router ctx "
                               f"{ctx}, measured {_measured_ctx(lane)}): align LLM_ROUTER_OLLAMA_NUM_CTX / think with the check "
                               f"or re-run `llm-router local-check`")
    except Exception:  # noqa: BLE001 -- doctor output must never break
        return out
    return out
