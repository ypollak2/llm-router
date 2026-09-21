"""Capture prompt text at the routing choke point, scrubbed before it lands.

Why this module exists
----------------------
Audited 2026-09-20: `routing_quality.jsonl` (22,356 records) carries the
routing decision but no prompt text, and `auto-route-debug.log` records
`prompt_len=` and never the prompt. Prompt text survives only in conversation
transcripts, which carry no `route_id`, no `ts` and no `task_type`. There is
no join key between the two halves, so no amount of offline work can
reconstruct "this prompt got this routing decision" for historical traffic.

This writes both halves in one record, at the moment the decision is made.
Everything downstream — the ground-truth dataset, the regret curve, any
counterfactual — depends on it, and none of it can be backfilled.

Privacy posture: scrub-at-write-time. `scrub()` runs before the record is
serialised, so unscrubbed prompt text never reaches disk. There is no "raw"
mode and adding one should be treated as a change of policy, not a flag.

Off by default. `LLM_ROUTER_GROUND_TRUTH=1` turns the whole path on.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

# The scrubber lives with the ground-truth tooling; import defensively so a
# packaging layout that omits `scripts/` disables capture instead of breaking
# the router. Capture is never important enough to fail a request.
_SCRUB_ERR: str | None = None
try:  # pragma: no cover - exercised by the import-failure test
    # src/llm_router/prompt_capture.py -> parents[2] is the repo root.
    _scripts = Path(__file__).resolve().parents[2] / "scripts"
    if str(_scripts) not in sys.path:
        sys.path.insert(0, str(_scripts))
    from groundtruth.scrub import residual_risk, scrub  # type: ignore
except Exception as exc:  # noqa: BLE001 - any import failure disables capture
    scrub = None  # type: ignore[assignment]
    residual_risk = None  # type: ignore[assignment]
    _SCRUB_ERR = f"{type(exc).__name__}: {exc}"

# The single hash convention, shared with RouteLedgerRecord.prompt_sha256.
try:  # pragma: no cover - exercised by the import-failure test
    from llm_router.trace_id import hash_prompt as _hash_prompt
except Exception:  # noqa: BLE001
    _hash_prompt = None  # type: ignore[assignment]

def _scripts_on_path() -> None:
    """Make scripts/ importable. Same resolution the scrubber import uses."""
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))


# ONE flag controls the whole Ground Truth path: capture, eligibility gate,
# replay envelope, candidate pool. Two flags meant two ways to be half-on, and
# "capture without accumulating" is not a state anyone wants — the capture file
# alone is what produced 121 rows and zero labels.
#
# Opt-in rather than default-on, because this is the one part of the system
# that writes user prompt text to disk. Scrubbing runs first and fails closed,
# but "we scrub it" is a reason to allow the choice, not to make it for people.
ENV_FLAG = "LLM_ROUTER_GROUND_TRUTH"
# Retired name, still honoured so an existing export keeps working. Not a
# second control: it enables exactly the same thing.
ENV_FLAG_LEGACY = "LLM_ROUTER_CAPTURE_PROMPTS"
ENV_PATH = "LLM_ROUTER_CAPTURE_PATH"
ENV_MAX_CHARS = "LLM_ROUTER_CAPTURE_MAX_CHARS"
ENV_DENYLIST = "LLM_ROUTER_CAPTURE_DENYLIST"
# Ground Truth accumulation. Separate from capture: capture records what was
# asked, accumulation decides whether it could ever be replayed and verified.
# Off unless capture is on, since it has nothing to assess otherwise.
# Escape hatch for the narrow case of wanting the capture file without the
# pool. Defaults to ON whenever the Ground Truth flag is on, so it is not a
# second switch to remember — only a way to turn one half off.
ENV_NO_ACCUMULATE = "LLM_ROUTER_GT_NO_ACCUMULATE"
ENV_OUTCOME_LOG = "LLM_ROUTER_GT_OUTCOME_LOG"

# A prompt longer than this is truncated. Pasted logs and file dumps run to
# hundreds of KB and are not tasks; the truncation is recorded so a reader can
# tell a long prompt from a clipped one.
DEFAULT_MAX_CHARS = 20_000

_lock = threading.Lock()
_denylist_cache: tuple[float, list[str]] | None = None


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    return _truthy(ENV_FLAG) or _truthy(ENV_FLAG_LEGACY)


def accumulation_enabled() -> bool:
    """On whenever Ground Truth is on, unless explicitly suppressed."""
    return enabled() and not _truthy(ENV_NO_ACCUMULATE)


# ── Accumulation outcomes ────────────────────────────────────────────────────
# Four distinguishable results, because "nothing appeared in the pool" has four
# very different causes and only one of them is a bug. An `except: pass` here
# made all four look identical, which is the failure mode where accumulation
# silently stops and nobody notices for weeks.
OUTCOME_PERSISTED = "persisted"          # a candidate entered the pool
OUTCOME_REJECTED = "rejected"            # the eligibility gate said no, with a reason
OUTCOME_DEDUPED = "deduplicated"         # already in the pool
OUTCOME_ERROR = "error"                  # accumulation itself failed
OUTCOME_SKIPPED = "skipped"              # nothing to assess

_DEDUP_REASONS = frozenset({"duplicate-exact", "duplicate-near"})

_counters: dict[str, int] = {}


def outcome_log_path() -> Path:
    explicit = os.environ.get(ENV_OUTCOME_LOG)
    if explicit:
        return Path(explicit)
    home = Path(os.environ.get("LLM_ROUTER_HOME", Path.home() / ".llm-router"))
    return home / "gt_accumulation.jsonl"


def _record_outcome(outcome: str, reason: str, *, route_id: str | None = None,
                    task_type: str | None = None, detail: str = "") -> None:
    """Append one accumulation outcome. Last-resort failure is swallowed, but
    only here — everything above this line reports."""
    _counters[outcome] = _counters.get(outcome, 0) + 1
    try:
        path = outcome_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.time(), "outcome": outcome, "reason": reason,
                "route_id": route_id, "task_type": task_type,
                "detail": detail[:300],
            }, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 — the observability write is the last thing
        pass


def counters() -> dict[str, int]:
    """In-process outcome tallies since import. The log is the durable record."""
    return dict(_counters)


def capture_path() -> Path:
    explicit = os.environ.get(ENV_PATH)
    if explicit:
        return Path(explicit)
    home = Path(os.environ.get("LLM_ROUTER_HOME", Path.home() / ".llm-router"))
    return home / "prompt_capture.jsonl"


def _denylist() -> list[str]:
    """Literal terms to redact, reloaded when the file changes.

    Regexes cannot know a customer's name. This file is the only mechanism
    that can, so it is read on every write rather than cached for the process
    lifetime — a term added mid-session must take effect immediately.
    """
    global _denylist_cache
    path = os.environ.get(ENV_DENYLIST)
    if not path:
        return []
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return []
    if _denylist_cache and _denylist_cache[0] == mtime:
        return _denylist_cache[1]
    try:
        terms = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
        return []
    _denylist_cache = (mtime, terms)
    return terms


def capture(
    prompt: str,
    *,
    route_id: str | None = None,
    session_id: str | None = None,
    task_type: str | None = None,
    complexity: str | None = None,
    chosen_tier: int | None = None,
    chosen_model: str | None = None,
    classification_method: str | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Append one scrubbed capture record. Returns True if a record was written.

    Never raises. A failure to capture telemetry must not fail a user's
    request, so every error path returns False and the router continues.
    """
    if not enabled() or not prompt or scrub is None:
        return False
    try:
        max_chars = int(os.environ.get(ENV_MAX_CHARS, DEFAULT_MAX_CHARS))
    except ValueError:
        max_chars = DEFAULT_MAX_CHARS

    try:
        text = prompt
        truncated = False
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars]
            truncated = True

        clean, report = scrub(text, denylist=_denylist())
        flags = residual_risk(clean) if residual_risk else []

        # THE JOIN KEY, and the one subtle decision in this module: the hash is
        # taken over the ORIGINAL prompt, not the scrubbed one.
        #
        # It has to be, or it would not match `RouteLedgerRecord.prompt_sha256`,
        # which is computed at the routing choke point before any scrubbing
        # exists. Hashing the scrubbed text instead would produce a key that
        # joins to nothing — the failure this whole schema change exists to fix.
        #
        # This leaks nothing new: the same hash is already in the ledger by
        # design, it is not reversible, and the scrubbed text stored beside it
        # is strictly less revealing than the hash's preimage.
        prompt_sha = _hash_prompt(prompt) if _hash_prompt else None

        record = {
            "ts": time.time(),
            "prompt": clean,
            "prompt_chars": len(prompt),
            "truncated": truncated,
            # Identity / join
            "route_id": route_id,
            "session_id": session_id,
            "prompt_sha256": prompt_sha,
            "capture_ref": f"capture:{prompt_sha}" if prompt_sha else None,
            # Decision metadata, mirroring the ledger's field names exactly so
            # a joined row has no renaming to do.
            "task_type": task_type,
            "complexity": complexity,
            "chosen_tier": chosen_tier,
            "chosen_model": chosen_model,
            "classification_method": classification_method,
            # Provenance of the scrub itself
            "scrub_counts": dict(report.counts),
            "residual_flags": flags,
            "capture_schema": 2,
        }
        if extra:
            record["extra"] = extra

        path = capture_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        # One lock and one append per record. Appends under the POSIX
        # atomic-write size are not interleaved by other processes, and the
        # lock covers threads within this one.
        with _lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)

        # Assess Ground Truth eligibility while the state still exists. This is
        # the whole reason it happens here rather than offline: a replay
        # envelope captured a week later describes a different repo.
        if accumulation_enabled():
            try:
                # A test or benchmark run never contributes candidates. Checked
                # here rather than filtered later: the pool is what a future
                # Ground Truth v1 is sampled from, so a fixture must not enter
                # it in the first place.
                from llm_router.routing_quality import detect_synthetic
                if detect_synthetic():
                    _record_outcome(OUTCOME_SKIPPED, "synthetic-run",
                                    route_id=route_id, task_type=task_type)
                    return True
                _scripts_on_path()
                from groundtruth.accumulate import accumulate as _accumulate
                admitted, reason, _elig = _accumulate(
                    prompt,
                    route_id=route_id, session_id=session_id,
                    prompt_sha256=prompt_sha, task_type=task_type,
                    complexity=complexity,
                    model_config={"chosen_model": chosen_model},
                )
                if admitted:
                    out = OUTCOME_PERSISTED
                elif reason in _DEDUP_REASONS:
                    out = OUTCOME_DEDUPED
                elif reason.startswith("accumulation-error"):
                    out = OUTCOME_ERROR
                else:
                    out = OUTCOME_REJECTED
                _record_outcome(out, reason, route_id=route_id, task_type=task_type)
            except Exception as exc:  # noqa: BLE001 — never breaks capture, but IS recorded
                _record_outcome(OUTCOME_ERROR, "exception", route_id=route_id,
                                task_type=task_type,
                                detail=f"{type(exc).__name__}: {exc}")
        return True
    except Exception:  # noqa: BLE001 - telemetry must never break routing
        return False


def status() -> dict[str, Any]:
    """Enough to answer 'why is nothing being captured?' without guesswork."""
    path = capture_path()
    try:
        n = sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
    except OSError:
        n = 0
    return {
        "enabled": enabled(),
        "scrubber_available": scrub is not None,
        "scrubber_error": _SCRUB_ERR,
        "path": str(path),
        "exists": path.exists(),
        "records": n,
        "accumulation_enabled": accumulation_enabled(),
        "outcome_log": str(outcome_log_path()),
        "outcome_counters": counters(),
        "denylist_terms": len(_denylist()),
        "max_chars": os.environ.get(ENV_MAX_CHARS, str(DEFAULT_MAX_CHARS)),
    }
