"""Hook-side routing verdict: a local model gives one verdict for every user prompt and every Agent spawn.

W10 (owner 2026-11-04): "the classifier should be a local model that decides on the routing itself".
Before this module the local classifier (``local_classifier`` + ``decision_classifier``, nimble:9b on Ollama
``/v1/systemone``) only ran inside the proxy's shadow seam. No hook called it, nothing read its verdict, and a
session without ``ANTHROPIC_BASE_URL`` never reached the proxy (docs/bugs/CLS-HOOK-1.md). This module is the
hook's client.

``LLM_ROUTER_LOCAL_CLASSIFIER`` keeps its meaning: ``off`` (default, zero Ollama calls) | ``shadow`` (a verdict is
computed and logged; no behaviour changes) | ``on`` (the verdict DECIDES, where it is safe, below).

Design, each point from a measured defect (docs/bugs/CLS-HOOK-1.md, CLS-COLD-1.md, CLS-TIE-1.md):

* the hook WAITS through a cold model load (``start`` runs the call in a thread beside the hook's own work and
  ``Pending.decide`` joins it under one wall-clock budget, default 8,000 ms). The proxy path returned ``cold``
  (no verdict) instead of loading: 399 of 2,417 rows in 7 days;
* a slow call does not poison the next one. The proxy path set a 30 s cooldown after any timeout, so 129 of 170
  7-day timeouts never touched the model. Here a breaker opens only after ``BREAKER_FAILS`` consecutive failures;
* the call goes through ``local_classifier.classify_local``, so the request and the strict parse are the pinned
  ones. No prompt text is stored: a row holds a 16-hex hash, lengths, numbers and codes.

What a verdict decides (``pick_door``). The decision model answers one question, the cheapest adequate Claude
tier (haiku/sonnet/opus) with a probability for each, so it can say "this is hard, keep it on Claude" and nothing
more. That is the only thing ``on`` acts on:

=========  =========================  ============================================================
kind       verdict                    door (``on``)
=========  =========================  ============================================================
prompt     opus, p_max >= 0.6         ``claude``: the routing hint is quieted (no directive, no hold)
prompt     anything else              ``mcp`` (``local`` when a local lane passed): the hint as today
agent      opus, p_max >= 0.6         ``claude``: no Codex attempt for this spawn
agent      anything else              ``codex``: the existing NS3 gates decide, unchanged
(any)      no verdict                 "" : today's behaviour, fail-open
=========  =========================  ============================================================

A verdict never opens a door the existing gates keep shut: Q&A answers from local models stay gated by
``qa_policy`` and the lane tests, Codex stays behind its bench, window and budget gates. Sonnet and Haiku are not
a non-Claude door and are never counted as one.

Nothing here raises into the caller: every public function catches and records (``failopen``).
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass

from llm_router import paths

KINDS = ("prompt", "agent")
DOORS = ("claude", "mcp", "local", "codex")
TIERS = ("haiku", "sonnet", "opus")
LEDGER = "door_verdicts.jsonl"
STATE = "door_verdict_state.json"
MAX_LEDGER_BYTES = 5_000_000
DEFAULT_BUDGET_MS = 8000       # cold load of nimble:9b: 2.0 s idle, 6.7 s and >5 s while another model is resident (load 5-7); warm 0.35-0.55 s
DEFAULT_OPUS_MIN_CONF = 0.6    # 72 of 231 opus verdicts in 7 days (31%) reach it; 0.5 is the floor of the 3-way model
BREAKER_FAILS = 2
BREAKER_WINDOW_S = 120.0
BREAKER_SKIP_S = 60.0
_APPLIED = {"prompt": "hint_quieted", "agent": "codex_skipped"}
NO_CONTEXT = {
    "prompt": "(no prior context: the hook sees only this prompt)",
    "agent": "(a sub-agent spawn: the hook sees only the brief the main agent wrote)",
}


@dataclass(frozen=True)
class DoorVerdict:
    """What the model said, and how the call went. ``ok`` only for a real verdict."""

    kind: str
    source: str                       # llm | cache | timeout | parse_error | breaker | off | error | abstain
    tier: str | None = None
    confidence: float | None = None
    ms: float = 0.0
    model: str = ""
    resident: bool | None = None      # was the model loaded when the call began (None: unknown)

    @property
    def ok(self) -> bool:
        return self.source in ("llm", "cache") and self.tier in TIERS


@dataclass(frozen=True)
class DoorDecision:
    door: str              # "" = no verdict, the caller keeps today's behaviour
    reason: str
    applied: bool          # True only in ``on`` mode and only for a door that changes what the hook does
    verdict: DoorVerdict


def mode() -> str:
    """``off`` | ``shadow`` | ``on``: the existing ``LLM_ROUTER_LOCAL_CLASSIFIER`` vocabulary, read per call."""
    raw = os.environ.get("LLM_ROUTER_LOCAL_CLASSIFIER", "").strip().lower()
    return raw if raw in ("shadow", "on") else "off"


def budget_s() -> float:
    try:
        ms = int(os.environ.get("LLM_ROUTER_DOOR_VERDICT_BUDGET_MS", ""))
    except ValueError:
        ms = DEFAULT_BUDGET_MS
    return max(0.2, min(ms, 30_000) / 1000.0)


def opus_min_conf() -> float:
    try:
        v = float(os.environ.get("LLM_ROUTER_DOOR_OPUS_MIN_CONF", ""))
    except ValueError:
        return DEFAULT_OPUS_MIN_CONF
    return v if 0.0 <= v <= 1.0 else DEFAULT_OPUS_MIN_CONF


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


# ---- the breaker: consecutive failures, never a single slow call ---------------------------------------------


def _state_path():
    return paths.state_path(STATE)


def _read_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(data: dict) -> None:
    p = _state_path()
    try:
        from llm_router import state_files

        state_files.private_dir(p.parent)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        with state_files.open_private(tmp, "w") as fh:
            fh.write(json.dumps(data))
        os.replace(tmp, p)
    except OSError as exc:
        from llm_router import failopen

        failopen.record("LR-FO-DOOR-VERDICT-STATE", exc)


def _num(d: dict, key: str) -> float | None:
    """A stored number, or None: an absent or malformed value is unknown, never 0 (docs: unknown-as-number lint)."""
    v = d.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def breaker_open(now: float | None = None) -> bool:
    now = time.time() if now is None else now
    until = _num(_read_state(), "open_until")
    return until is not None and until > now


def note_result(ok: bool, now: float | None = None) -> None:
    """A verdict resets the count; a failure counts if it follows another within ``BREAKER_WINDOW_S``."""
    now = time.time() if now is None else now
    st = _read_state()
    if ok:
        if st.get("fails") or st.get("open_until"):
            _write_state({"fails": 0, "last_fail": 0.0, "open_until": 0.0})
        return
    last, prior = _num(st, "last_fail"), _num(st, "fails")
    consecutive = last is not None and prior is not None and now - last <= BREAKER_WINDOW_S
    fails = int(prior) + 1 if consecutive and prior is not None else 1
    _write_state({"fails": fails, "last_fail": now,
                  "open_until": now + BREAKER_SKIP_S if fails >= BREAKER_FAILS else 0.0})


# ---- the call -------------------------------------------------------------------------------------------------


def _resident(model: str, base_url: str) -> bool | None:
    """Is ``model`` loaded right now? One short ``/api/ps``; None when Ollama cannot say."""
    import urllib.request

    from llm_router import local_privacy

    try:
        req = urllib.request.Request(f"{base_url}/api/ps")
        with local_privacy.open_url(req, base_url=base_url, timeout=0.5) as resp:  # the one sanctioned local sink
            models = json.loads(resp.read()).get("models")
        if not isinstance(models, list):
            return None
        names = {str(m.get("name") or m.get("model") or "") for m in models if isinstance(m, dict)}
        norm = model if ":" in model else f"{model}:latest"
        return any(n == model or n == norm for n in names)
    except Exception:  # noqa: BLE001 - an unknown residency is logged as None, never an error
        return None


def classify(text: str, kind: str, context: str = "", timeout_s: float | None = None) -> DoorVerdict:
    """One synchronous verdict (the worker thread's body, and the 30-call check's). Never raises."""
    t0 = time.perf_counter()

    def took() -> float:
        return round((time.perf_counter() - t0) * 1000.0, 1)

    try:
        if mode() == "off":
            return DoorVerdict(kind, "off")
        if breaker_open():
            return DoorVerdict(kind, "breaker")
        from llm_router import local_classifier as lc

        model = lc._model()
        if not model:
            return DoorVerdict(kind, "off")
        resident = _resident(model, lc._base_url())
        v = lc.classify_local(lc.Assembled(context or NO_CONTEXT.get(kind, NO_CONTEXT["prompt"]), text),
                              model=model, timeout_s=timeout_s if timeout_s is not None else budget_s())
        out = DoorVerdict(kind, v.source, v.tier, v.confidence, took(), model, resident)
        if v.source in ("llm", "timeout"):  # a parse_error still means the model answered: it is up
            note_result(out.ok)
        return out
    except Exception as exc:  # noqa: BLE001 - the contract is "no verdict", not an exception
        from llm_router import failopen

        failopen.record("LR-FO-DOOR-VERDICT-CALL", exc)
        return DoorVerdict(kind, "error", ms=took())


def preload_enabled() -> bool:
    """``LLM_ROUTER_CLASSIFIER_PRELOAD=off`` keeps the old session start (edit model first, no decision model)."""
    return mode() != "off" and os.environ.get("LLM_ROUTER_CLASSIFIER_PRELOAD", "on").strip().lower() not in (
        "0", "off", "false", "no")


def warm() -> str | None:
    """Load the decision model with the keep_alive the verdict calls use (session start, in the detached child),
    so the first prompt finds it resident. Returns the model, or None when off, unconfigured or it did not load.
    Never raises. Session start used to warm ``model_discovery.local_model()`` (the user's general model, 22 GB here)
    instead, which also evicts the decision model when memory is short (docs/bugs/CLS-COLD-1.md)."""
    try:
        if not preload_enabled():
            return None
        from llm_router import local_classifier as lc

        model = lc._model()
        if not model:
            return None
        v = lc.classify_local(lc.Assembled(NO_CONTEXT["prompt"], "warm-up"), model=model, timeout_s=60.0)
        if v.source in ("llm", "parse_error"):
            return model
        from llm_router import failopen

        failopen.record("CHZ-FO-LOCAL-CLASSIFIER-WARMUP", None, detail=f"door_verdict.warm {v.source}")
        return None
    except Exception as exc:  # noqa: BLE001
        from llm_router import failopen

        failopen.record("LR-FO-DOOR-VERDICT-WARM", exc)
        return None


def local_lane_passed(task_type: str | None) -> bool:
    """A local lane exists for this task when a FRESH quality batch passed it (``lane_quality``). Today none does
    for Q&A or code (local Q&A 13/113, code 27/74 in the router's own eligibility), so this is False and the
    prompt door stays ``mcp``; it turns True only on a passed batch, never on a guess."""
    try:
        from llm_router import lane_quality
        from llm_router.qa_policy import QA_TASK_TYPES

        group = "qa" if (task_type or "") in QA_TASK_TYPES else "code"
        lanes = lane_quality.sampled_lanes(lane_quality.read_rows(), time.time())
        return any(served == "local" and grp == group for (_door, served, grp) in lanes)
    except Exception as exc:  # noqa: BLE001
        from llm_router import failopen

        failopen.record("LR-FO-DOOR-VERDICT-LANE", exc)
        return False


def pick_door(v: DoorVerdict | None, *, kind: str, task_type: str | None = None) -> tuple[str, str]:
    """``(door, reason)``. ``("", "no_verdict")`` when the model gave nothing usable."""
    if v is None or not v.ok:
        return "", "no_verdict"
    conf = v.confidence if v.confidence is not None else 0.0
    if v.tier == "opus" and conf >= opus_min_conf():
        return "claude", "opus_confident"
    reason = "opus_unsure" if v.tier == "opus" else f"tier_{v.tier}"
    if kind == "agent":
        return "codex", reason
    return ("local" if local_lane_passed(task_type) else "mcp"), reason


# ---- the handle the hooks use -----------------------------------------------------------------------------


class Pending:
    """One in-flight verdict. ``decide`` joins it (at most once) and writes the row."""

    def __init__(self, text: str, kind: str, *, session_id: str | None = None, context: str = "",
                 tool_use_id: str | None = None) -> None:
        self.kind = kind
        self.session_id = session_id or ""
        self.tool_use_id = tool_use_id or ""
        self.sha = text_sha(text)
        self.chars = len(text)
        self._t0 = time.monotonic()
        self._budget = budget_s()
        self._box: list[DoorVerdict] = []
        self._logged = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._work, args=(text, context), daemon=True)
        self._thread.start()
        atexit.register(self._at_exit)

    def _work(self, text: str, context: str) -> None:
        self._box.append(classify(text, self.kind, context, timeout_s=self._budget))

    def verdict(self) -> DoorVerdict:
        """Wait until the budget (measured from ``start``) is spent; a late call reads as a timeout."""
        left = self._budget - (time.monotonic() - self._t0)
        self._thread.join(max(0.0, left))
        if self._box:
            return self._box[0]
        return DoorVerdict(self.kind, "timeout", ms=round((time.monotonic() - self._t0) * 1000.0, 1))

    def decide(self, *, task_type: str | None = None, outcome: str = "") -> DoorDecision:
        """Join the verdict, pick the door, log one row. ``applied`` only in ``on`` mode and only for the
        ``claude`` door, the one door that changes what the hook does. Never raises."""
        try:
            v = self.verdict()
            door, reason = pick_door(v, kind=self.kind, task_type=task_type)
            dec = DoorDecision(door, reason, mode() == "on" and door == "claude", v)
        except Exception as exc:  # noqa: BLE001
            from llm_router import failopen

            failopen.record("LR-FO-DOOR-VERDICT-DECIDE", exc)
            dec = DoorDecision("", "error", False, DoorVerdict(self.kind, "error"))
        self._log(dec, outcome or (_APPLIED[self.kind] if dec.applied else "no_change"), task_type)
        return dec

    def _log(self, dec: DoorDecision, outcome: str, task_type: str | None) -> None:
        with self._lock:
            if self._logged:
                return
            self._logged = True
        v = dec.verdict
        row = {
            "kind": "door_verdict", "ts": round(time.time(), 3), "hook_kind": self.kind, "mode": mode(),
            "session_id": self.session_id, "tool_use_id": self.tool_use_id, "text_sha": self.sha,
            "chars": self.chars, "task_type": task_type, "source": v.source, "model": v.model,
            "resident": v.resident, "ms": v.ms, "tier": v.tier, "confidence": v.confidence,
            "door": dec.door, "reason": dec.reason, "applied": dec.applied, "outcome": outcome,
        }
        try:
            from llm_router import capped_log

            capped_log.append(paths.state_path(LEDGER), (json.dumps(row) + "\n").encode(), MAX_LEDGER_BYTES)
        except Exception as exc:  # noqa: BLE001
            from llm_router import failopen

            failopen.record("LR-FO-DOOR-VERDICT-LOG", exc)

    def _at_exit(self) -> None:
        """A hook that exits before its decision point still leaves its row (coverage counts every prompt)."""
        if not self._logged:
            self.decide(outcome="exit_before_decision")


def start(text: str, kind: str, *, session_id: str | None = None, context: str = "",
          tool_use_id: str | None = None) -> Pending | None:
    """Begin a verdict in the background, or None when the classifier is off or there is nothing to classify.
    Never raises."""
    try:
        if mode() == "off" or kind not in KINDS or not (text or "").strip():
            return None
        return Pending(text, kind, session_id=session_id, context=context, tool_use_id=tool_use_id)
    except Exception as exc:  # noqa: BLE001
        from llm_router import failopen

        failopen.record("LR-FO-DOOR-VERDICT-START", exc)
        return None


# ---- the report (coverage, latency, cold share, door mix, verdict vs outcome) -----------------------------------


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def summarize(rows: list[dict]) -> dict:
    """Counts over ``door_verdicts.jsonl`` rows (the caller windows them). Every share carries its n."""
    rows = [r for r in rows if isinstance(r, dict) and r.get("kind") == "door_verdict"]
    n = len(rows)
    ok = [r for r in rows if r.get("source") in ("llm", "cache") and r.get("tier") in TIERS]
    by_source: dict[str, int] = {}
    for r in rows:
        by_source[str(r.get("source"))] = by_source.get(str(r.get("source")), 0) + 1
    ms_ok = [float(r["ms"]) for r in ok if isinstance(r.get("ms"), (int, float))]
    known = [r for r in rows if r.get("resident") is not None]
    doors: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    for r in rows:
        doors[str(r.get("door") or "none")] = doors.get(str(r.get("door") or "none"), 0) + 1
        outcomes[str(r.get("outcome"))] = outcomes.get(str(r.get("outcome")), 0) + 1
    return {
        "n": n, "verdicts": len(ok), "coverage": round(len(ok) / n, 4) if n else None,
        "by_source": by_source, "latency_ms_p50": _pct(ms_ok, 0.5), "latency_ms_p95": _pct(ms_ok, 0.95),
        "cold_n": sum(1 for r in known if r["resident"] is False), "residency_known_n": len(known),
        "cold_share": round(sum(1 for r in known if r["resident"] is False) / len(known), 4) if known else None,
        "doors": doors, "outcomes": outcomes,
        "applied": sum(1 for r in rows if r.get("applied")),
        "by_kind": {k: sum(1 for r in rows if r.get("hook_kind") == k) for k in KINDS},
    }


def read_rows(days: float | None = None) -> list[dict]:
    p = paths.state_path(LEDGER)
    cutoff = time.time() - days * 86400 if days else None
    out: list[dict] = []
    try:
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                ts = _num(r, "ts")
                if cutoff is None or (ts is not None and ts >= cutoff):
                    out.append(r)
    except OSError:
        return []
    return out


__all__ = ["DoorVerdict", "DoorDecision", "Pending", "classify", "mode", "pick_door", "start", "summarize",
           "read_rows", "local_lane_passed", "warm", "preload_enabled"]
