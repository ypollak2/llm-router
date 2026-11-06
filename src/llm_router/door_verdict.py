"""Hook-side routing verdict: a local model gives one verdict for every user prompt and every Agent spawn.

W10 (owner 2026-11-04): "the classifier should be a local model that decides on the routing itself".
Before this module the local classifier (``local_classifier`` + ``decision_classifier``, nimble:9b on Ollama
``/v1/systemone``) only ran inside the proxy's shadow seam. No hook called it, nothing read its verdict, and a
session without ``ANTHROPIC_BASE_URL`` never reached the proxy (docs/bugs/CLS-HOOK-1.md). This module is the
hook's client.

``LLM_ROUTER_LOCAL_CLASSIFIER``: ``off`` (zero Ollama calls) | ``shadow`` (a verdict is computed and logged in the
background; no behaviour changes) | ``on`` (the verdict DECIDES, below). Unset means ``shadow`` when the decision
model is configured (``LLM_ROUTER_CLASSIFIER_BACKEND=systemone`` and ``LLM_ROUTER_DECISION_MODEL``), else ``off``.

Design, each point from a measured defect (docs/bugs/CLS-HOOK-1.md, CLS-COLD-1.md, CLS-COOLDOWN-1.md, CLS-TIE-1.md,
CLS-STALL-1.md):

* a verdict never stalls a prompt. ``shadow`` waits for nothing: a detached worker (``python -m
  llm_router.door_verdict --worker``) makes the call and writes the row. ``on`` decides from a verdict made in a thread
  beside the hook's own work, but a prompt that finds the model NOT resident does not wait for the load (2-10 s): the
  worker makes the verdict (it loads the model, so the next prompt is warm) and this prompt keeps today's behaviour.
  Agent spawns, whose hook already runs for seconds, wait under one budget (default 8,000 ms);
* a slow call does not poison the next one. The proxy path set a 30 s cooldown after any timeout, so 129 of 170
  7-day timeouts never touched the model. Here a breaker opens only after ``BREAKER_FAILS`` consecutive failures;
* the call goes through ``local_classifier.classify_local``, so the request and the strict parse are the pinned
  ones. No prompt text is stored: a row holds a 16-hex hash, lengths, numbers and codes.

What a verdict decides (``preferred_door``, the seam a sub-agent path calls). The decision model answers one
question, the cheapest adequate Claude tier with a probability each, used here as a difficulty signal:

=========  ===============================  ==========================================================================
kind       verdict (p_max >= its threshold)  door (``on``)
=========  ===============================  ==========================================================================
any        opus (0.6)                       ``claude``: prompt hint quieted / no Codex attempt for the spawn
any        haiku (0.7), a local lane exists ``local``: the hint names the lane (a passed text job, or ``local:``)
prompt     sonnet or haiku (0.5 / 0.7)      ``codex`` when Codex is usable (not benched): the hint names ``codex:``
agent      sonnet or haiku                  ``codex``: the existing NS3 gates decide, unchanged
(any)      no verdict, or none of the above ``mcp`` / "": today's behaviour, fail-open
=========  ===============================  ==========================================================================

A "local lane exists" only on evidence: a fresh passed quality batch for that door and task group
(``lane_quality``), or an enabled local text-job routing switch for a detected commit-message / PR-description
prompt, or the caller says so (``local_lane=``, how a sub-agent fallback plugs in). Today none exists for Q&A or code,
so ``local`` is rare. A verdict never opens a door the existing gates keep shut: Q&A answers from local models stay
gated by ``qa_policy``, Codex stays behind its bench, window and budget gates. Sonnet and Haiku are not a non-Claude
door and are never counted as one.

Nothing here raises into the caller: every public function catches and records (``failopen``).
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import sys
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
DEFAULT_MODE = "shadow"        # when the decision model is configured; see ``mode``
DEFAULT_PROMPT_BUDGET_MS = 2000
DEFAULT_BUDGET_MS = 8000       # cold load of nimble:9b: 2.0 s idle, 6.7 s and >5 s while another model is resident (load 5-7); warm 0.35-0.55 s
# p_max thresholds. Shares of the 7-day proxy verdicts (n=1,755, 2026-10-28..11-03) at each: opus >= 0.6 is 4.1%,
# haiku >= 0.7 is 6.8%, sonnet >= 0.5 is 53.4% (docs/bugs/CLS-HOOK-1.md). A weaker local model is the riskier door, so
# haiku asks for more confidence than opus does.
DEFAULT_OPUS_MIN_CONF = 0.6
DEFAULT_HAIKU_MIN_CONF = 0.7
DEFAULT_SONNET_MIN_CONF = 0.5
BREAKER_FAILS = 2
BREAKER_WINDOW_S = 120.0
BREAKER_SKIP_S = 60.0
# Doors in lane_quality.jsonl (``lane_quality.Lane``) whose local batches count for each hook kind.
_LANE_DOORS = {"prompt": ("mcp", "hook", "direct", "techop"), "agent": ("subagent",)}
_TEXT_JOBS = (
    ("commit_message", re.compile(r"\b(?:write|draft|generate|compose|give me|suggest)\b.{0,40}\bcommit message", re.I)),
    ("pr_description", re.compile(
        r"\b(?:write|draft|generate|compose|give me)\b.{0,40}\b(?:pr|pull request)\b.{0,12}"
        r"\b(?:description|body|summary)", re.I)),
)
NO_CONTEXT = {
    "prompt": "(no prior context: the hook sees only this prompt)",
    "agent": "(a sub-agent spawn: the hook sees only the brief the main agent wrote)",
}
_ACTING = {"prompt": ("claude", "local", "codex"), "agent": ("claude",)}
_OUTCOME = {("prompt", "claude"): "hint_quieted", ("prompt", "local"): "hint_prefers_local",
            ("prompt", "codex"): "hint_prefers_codex", ("agent", "claude"): "codex_skipped"}


@dataclass(frozen=True)
class DoorVerdict:
    """What the model said, and how the call went. ``ok`` only for a real verdict."""

    kind: str
    source: str                       # llm | cache | timeout | parse_error | breaker | off | error | abstain | deferred
    tier: str | None = None
    confidence: float | None = None
    ms: float = 0.0
    model: str = ""
    resident: bool | None = None      # was the model loaded when the call began (None: unknown)

    @property
    def ok(self) -> bool:
        return self.source in ("llm", "cache") and self.tier in TIERS


@dataclass(frozen=True)
class DoorChoice:
    """The door a verdict prefers and why. ``lane`` names the local lane when the door is ``local``."""

    door: str              # "" = no verdict, the caller keeps today's behaviour
    reason: str
    lane: str = ""


@dataclass(frozen=True)
class DoorDecision:
    door: str              # "" = no verdict, the caller keeps today's behaviour
    reason: str
    applied: bool          # True only in ``on`` mode and only for a door that changes what the hook does
    verdict: DoorVerdict
    lane: str = ""


def _decision_model_configured() -> bool:
    return (os.environ.get("LLM_ROUTER_CLASSIFIER_BACKEND", "").strip().lower() == "systemone"
            and bool(os.environ.get("LLM_ROUTER_DECISION_MODEL", "").strip()))


def mode() -> str:
    """``off`` | ``shadow`` | ``on``, read per call. ``LLM_ROUTER_LOCAL_CLASSIFIER`` set to ``shadow`` or ``on`` is taken
    as is; ``off`` (and anything else) is off; UNSET is ``DEFAULT_MODE`` when the decision model is configured, else
    ``off``, so a machine without it makes no call and a chat model is never picked up by guesswork."""
    raw = os.environ.get("LLM_ROUTER_LOCAL_CLASSIFIER", "").strip().lower()
    if raw in ("shadow", "on"):
        return raw
    if raw == "" and DEFAULT_MODE != "off" and _decision_model_configured():
        return DEFAULT_MODE
    return "off"


def _ms(raw: str, default: int) -> float:
    try:
        ms = int(raw)
    except ValueError:
        ms = default
    return max(0.2, min(ms, 30_000) / 1000.0)


def budget_s(kind: str | None = None) -> float:
    """Wall-clock one verdict may take. An Agent spawn (whose hook already runs for seconds) and a detached worker get
    ``LLM_ROUTER_DOOR_VERDICT_BUDGET_MS`` (8,000); a PROMPT decided in the hook's own process gets at most
    ``LLM_ROUTER_DOOR_VERDICT_PROMPT_BUDGET_MS`` (2,000: warm verdicts measured 0.35-0.6 s, p95 1.0 s in the proxy
    ledger), so a prompt is never held longer than that."""
    full = _ms(os.environ.get("LLM_ROUTER_DOOR_VERDICT_BUDGET_MS", ""), DEFAULT_BUDGET_MS)
    if kind == "prompt":
        return min(full, _ms(os.environ.get("LLM_ROUTER_DOOR_VERDICT_PROMPT_BUDGET_MS", ""), DEFAULT_PROMPT_BUDGET_MS))
    return full


def _conf(raw: str, default: float) -> float:
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if 0.0 <= v <= 1.0 else default


def opus_min_conf() -> float:
    return _conf(os.environ.get("LLM_ROUTER_DOOR_OPUS_MIN_CONF", ""), DEFAULT_OPUS_MIN_CONF)


def haiku_min_conf() -> float:
    return _conf(os.environ.get("LLM_ROUTER_DOOR_HAIKU_MIN_CONF", ""), DEFAULT_HAIKU_MIN_CONF)


def sonnet_min_conf() -> float:
    return _conf(os.environ.get("LLM_ROUTER_DOOR_SONNET_MIN_CONF", ""), DEFAULT_SONNET_MIN_CONF)


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


def _resident_models(base_url: str, timeout: float = 0.5) -> list[str] | None:
    """The models Ollama has loaded right now, from one short ``/api/ps``; None when it cannot say."""
    import urllib.request

    from llm_router import local_privacy

    try:
        req = urllib.request.Request(f"{base_url}/api/ps")
        with local_privacy.open_url(req, base_url=base_url, timeout=timeout) as resp:  # the one sanctioned local sink
            models = json.loads(resp.read()).get("models")
        if not isinstance(models, list):
            return None
        return [str(m.get("name") or m.get("model") or "") for m in models if isinstance(m, dict)]
    except Exception:  # noqa: BLE001 - an unknown residency is logged as None, never an error
        return None


def _is_resident(model: str, names: list[str] | None) -> bool | None:
    if names is None:
        return None
    norm = model if ":" in model else f"{model}:latest"
    return any(n == model or n == norm for n in names)


def _resident(model: str, base_url: str, timeout: float = 0.5) -> bool | None:
    """Is ``model`` loaded right now? None when Ollama cannot say (or the base is not one the classifier may use)."""
    from llm_router import local_privacy

    if not local_privacy.classifier_base_allowed(base_url):
        return None
    return _is_resident(model, _resident_models(base_url, timeout))


def classify(text: str, kind: str, context: str = "", timeout_s: float | None = None) -> DoorVerdict:
    """One synchronous verdict (a worker thread's body, and the checks'). ``timeout_s`` bounds the whole call, the
    residency probe included. Never raises.

    ``shadow`` never evicts: it classifies only when the model is already resident or nothing is (``/api/ps``), and
    otherwise answers ``skipped_slot_busy`` without a request, so the local text-job lane's model is not pushed out of
    memory by a log-only verdict (W10 review). ``on`` loads the model as before. A non-loopback Ollama is refused
    (``refused_remote``) unless ``LLM_ROUTER_CLASSIFIER_ALLOW_REMOTE`` is set."""
    t0 = time.perf_counter()
    total = timeout_s if timeout_s is not None else budget_s(kind)

    def took() -> float:
        return round((time.perf_counter() - t0) * 1000.0, 1)

    try:
        m = mode()
        if m == "off":
            return DoorVerdict(kind, "off")
        if breaker_open():
            return DoorVerdict(kind, "breaker")
        from llm_router import local_classifier as lc
        from llm_router import local_privacy

        model = lc._model()
        if not model:
            return DoorVerdict(kind, "off")
        base = lc._base_url()
        if not local_privacy.classifier_base_allowed(base):
            return DoorVerdict(kind, "refused_remote", ms=took(), model=model)
        names = _resident_models(base, min(0.5, max(0.1, total / 4)))
        resident = _is_resident(model, names)
        if m == "shadow" and resident is not True:
            if names is None:
                return DoorVerdict(kind, "skipped_slot_unknown", ms=took(), model=model, resident=None)
            if names:
                return DoorVerdict(kind, "skipped_slot_busy", ms=took(), model=model, resident=False)
        left = max(0.2, total - (time.perf_counter() - t0))
        v = lc.classify_local(lc.Assembled(context or NO_CONTEXT.get(kind, NO_CONTEXT["prompt"]), text),
                              model=model, timeout_s=left)
        out = DoorVerdict(kind, v.source, v.tier, v.confidence, took(), model, resident)
        if v.source in ("llm", "timeout"):  # a parse_error still means the model answered: it is up
            note_result(out.ok)
        return out
    except Exception as exc:  # noqa: BLE001 - the contract is "no verdict", not an exception
        from llm_router import failopen

        failopen.record("LR-FO-DOOR-VERDICT-CALL", exc)
        return DoorVerdict(kind, "error", ms=took())


def preload_enabled() -> bool:
    """Session start loads the decision model only in ``on``: in ``shadow`` the verdict is log-only and must never
    evict the model another lane (the local text jobs) is using, so the old edit / general warm-ups stay.
    ``LLM_ROUTER_CLASSIFIER_PRELOAD=off`` keeps them in ``on`` too."""
    return mode() == "on" and os.environ.get("LLM_ROUTER_CLASSIFIER_PRELOAD", "on").strip().lower() not in (
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


# ---- the doors ------------------------------------------------------------------------------------------------


def text_job_of(text: str | None) -> str | None:
    """``commit_message`` / ``pr_description`` when the prompt asks for one of the two text jobs a local model has
    passed; None otherwise. Deliberately narrow: a miss only means the prompt takes the default door."""
    if not text:
        return None
    head = text[:600]
    for job, pat in _TEXT_JOBS:
        if pat.search(head):
            return job
    return None


def local_lane_for(kind: str, task_type: str | None = None, text: str | None = None) -> str:
    """The local lane that can do this work now, or "". Evidence only, never a guess:

    * ``text_job:<job>``: the prompt asks for a commit message or PR description AND the local text-job routing
      switch is enabled for some model (``local_check.routing``; commit_message 32/35 and pr_description 28/35 passed);
    * ``qa`` / ``code``: a FRESH quality batch passed a local lane for this kind's doors and this task group
      (``lane_quality``). Local Q&A 13/113 and code 27/74 in the router's own eligibility, so none today;
    * for an Agent spawn only the ``subagent`` lanes count (a sub-agent fallback reports itself through the
      ``local_lane=`` argument of :func:`preferred_door` until a batch exists for it).
    """
    try:
        job = text_job_of(text) if kind == "prompt" else None
        if job:
            from llm_router.local_check import routing

            if any(routing.is_enabled(model, job) for model in routing._read()):
                return f"text_job:{job}"
        from llm_router import lane_quality
        from llm_router.qa_policy import QA_TASK_TYPES

        group = "qa" if (task_type or "") in QA_TASK_TYPES else "code"
        lanes = lane_quality.sampled_lanes(lane_quality.read_rows(), time.time())
        if any(served == "local" and grp == group and door in _LANE_DOORS.get(kind, ())
               for (door, served, grp) in lanes):
            return group
    except Exception as exc:  # noqa: BLE001
        from llm_router import failopen

        failopen.record("LR-FO-DOOR-VERDICT-LANE", exc)
    return ""


def codex_usable() -> bool:
    """Codex can take a prompt's ``codex:`` call now. The gates that call meets, read without spending anything:
    a binary exists; neither the account nor a configured Codex model is benched (``provider_reset``); the Plus meter
    reserve lets the router chain spend (``codex_meter.check``, D-373: it reserves Codex for sub-agent delegation, so
    this is false whenever the weekly meter is at the reserve or no reading and no counter allows it); and the 5 h
    delegation window still admits work (``codex_window.admit``, so a prompt hint never takes the last slots of
    the window that sub-agent delegation is protecting)."""
    try:
        from llm_router import codex_agent, codex_meter, codex_window, provider_reset

        if not codex_agent.is_codex_available():
            return False
        keys = ["codex"] + [f"codex:{m.split('/', 1)[-1]}" for m in codex_agent.CODEX_MODELS]
        if any(provider_reset.get_provider_reset_until(k) is not None for k in keys):
            return False
        if not codex_meter.check("router_chain").allowed:
            return False
        return codex_window.admit(complexity="moderate", deep_reasoning=False).allowed
    except Exception as exc:  # noqa: BLE001
        from llm_router import failopen

        failopen.record("LR-FO-DOOR-VERDICT-CODEX", exc)
        return False


def preferred_door(v: DoorVerdict | None, *, kind: str, task_type: str | None = None, text: str | None = None,
                   local_lane: bool | str | None = None) -> DoorChoice:
    """The door this verdict prefers: the seam a hook (or a sub-agent fallback) calls.

    ``local_lane``: None looks one up (:func:`local_lane_for`); False says there is none; True or a lane name says
    the caller has one (a sub-agent fallback that is installed and healthy). Pure apart from those lookups, and
    ``("", "no_verdict")`` when the model gave nothing usable."""
    if v is None or not v.ok:
        return DoorChoice("", "no_verdict")
    conf = v.confidence if v.confidence is not None else 0.0
    if v.tier == "opus":
        if conf >= opus_min_conf():
            return DoorChoice("claude", "opus_confident")
        return DoorChoice("codex" if kind == "agent" else "mcp", "opus_unsure")
    if v.tier == "haiku" and conf >= haiku_min_conf():
        if local_lane is None:
            lane = local_lane_for(kind, task_type, text)
        else:
            lane = local_lane if isinstance(local_lane, str) else ("caller" if local_lane else "")
        if lane:
            return DoorChoice("local", "haiku_local_lane", lane)
    reason = f"tier_{v.tier}"
    if kind == "agent":
        return DoorChoice("codex", reason)
    if conf >= (sonnet_min_conf() if v.tier == "sonnet" else haiku_min_conf()) and codex_usable():
        return DoorChoice("codex", reason)
    return DoorChoice("mcp", reason)


def hint_note(door: str, lane: str = "", tier: str | None = None) -> str:
    """The line a prompt hint gains when the verdict prefers ``local`` or ``codex`` (advice, like the rest of the
    hint: Claude composes the call, and the llm() door's own prefix handling and gates apply). "" for any other door."""
    if door == "local" and lane.startswith("text_job:"):
        job = lane.split(":", 1)[1]
        return (f"🎯 LOCAL VERDICT — a local model passed this text job ({job}). Call llm_text_job(job=\"{job}\", "
                "input_text=<the diff or text to work from>) first (if that tool is listed) and use the result only "
                "when its status is accepted; otherwise do the job yourself.\n")
    if door == "local":
        return (f"🎯 LOCAL VERDICT — the classifier rates this {tier or 'easy'}-level and a local lane passed its "
                "quality batch for this kind of work. Call llm(task=\"auto\", prompt=\"local: <user's request>\") "
                "first and use the answer after checking its key claim; if it is weak, answer it yourself.\n")
    if door == "codex":
        return (f"🎯 CODEX VERDICT — the classifier rates this {tier or 'moderate'}-level, work Codex can take. Call "
                "llm(task=\"auto\", prompt=\"codex: <user's request>\") first; if it is refused or weak, answer it "
                "yourself.\n")
    return ""


def pick_door(v: DoorVerdict | None, *, kind: str, task_type: str | None = None) -> tuple[str, str]:
    """``(door, reason)`` of :func:`preferred_door` with a lane looked up from the evidence."""
    c = preferred_door(v, kind=kind, task_type=task_type)
    return c.door, c.reason


# ---- the handles the hooks use ----------------------------------------------------------------------------


def _write_row(row: dict) -> None:
    try:
        from llm_router import capped_log

        capped_log.append(paths.state_path(LEDGER), (json.dumps(row) + "\n").encode(), MAX_LEDGER_BYTES)
    except Exception as exc:  # noqa: BLE001
        from llm_router import failopen

        failopen.record("LR-FO-DOOR-VERDICT-LOG", exc)


class _Handle:
    """A verdict in flight. ``decide`` picks the door and (unless ``defer_log``) writes the row; ``record`` writes it
    later with the outcome the hook actually produced. A row is written once; a hook that exits first still leaves it."""

    def __init__(self, text: str, kind: str, *, session_id: str | None, tool_use_id: str | None) -> None:
        self.kind = kind
        self.session_id = session_id or ""
        self.tool_use_id = tool_use_id or ""
        self.sha = text_sha(text)
        self.chars = len(text)
        self._text = text
        self._logged = False
        self._dec: DoorDecision | None = None
        self._task_type: str | None = None
        self._lock = threading.Lock()
        atexit.register(self._at_exit)

    def verdict(self, wait: bool = True) -> DoorVerdict:  # pragma: no cover - subclasses
        raise NotImplementedError

    def decide(self, *, task_type: str | None = None, outcome: str = "", defer_log: bool = False,
               wait: bool = True) -> DoorDecision:
        """Join the verdict, pick the door. ``applied`` only in ``on`` mode and only for a door that changes what the
        hook builds (``_ACTING``). ``wait=False`` takes only what has already arrived. Never raises."""
        self._task_type = task_type
        try:
            v = self.verdict(wait=wait)
            c = preferred_door(v, kind=self.kind, task_type=task_type, text=self._text if self.kind == "prompt" else None)
            dec = DoorDecision(c.door, c.reason, mode() == "on" and c.door in _ACTING[self.kind], v, c.lane)
        except Exception as exc:  # noqa: BLE001
            from llm_router import failopen

            failopen.record("LR-FO-DOOR-VERDICT-DECIDE", exc)
            dec = DoorDecision("", "error", False, DoorVerdict(self.kind, "error"))
        self._dec = dec
        if not defer_log:
            self.record(outcome)
        return dec

    def record(self, outcome: str = "") -> None:
        """Write the row (once) with ``outcome``, defaulting to what the verdict asked for."""
        dec = self._dec
        if dec is None:
            return
        with self._lock:
            if self._logged:
                return
            self._logged = True
        v = dec.verdict
        _write_row({
            "kind": "door_verdict", "ts": round(time.time(), 3), "hook_kind": self.kind, "mode": mode(),
            "session_id": self.session_id, "tool_use_id": self.tool_use_id, "text_sha": self.sha,
            "chars": self.chars, "task_type": self._task_type, "source": v.source, "model": v.model,
            "resident": v.resident, "ms": v.ms, "tier": v.tier, "confidence": v.confidence,
            "door": dec.door, "reason": dec.reason, "lane": dec.lane, "applied": dec.applied,
            "outcome": outcome or (_OUTCOME.get((self.kind, dec.door), "applied") if dec.applied else "no_change"),
        })

    def _at_exit(self) -> None:
        """A hook that exits before its decision point still leaves its row (coverage counts every prompt)."""
        if self._logged:
            return
        if self._dec is None:
            self.decide(outcome="exit_before_decision", wait=False)  # an early exit never waits for the model
        else:
            self.record("exit_before_record")


class Pending(_Handle):
    """One in-flight verdict made in a thread of this process, joined under the budget at ``decide``. The budget runs
    from ``t0`` (the moment ``start`` was entered), so the residency probe and the call are inside it."""

    def __init__(self, text: str, kind: str, *, session_id: str | None = None, context: str = "",
                 tool_use_id: str | None = None, t0: float | None = None) -> None:
        super().__init__(text, kind, session_id=session_id, tool_use_id=tool_use_id)
        self._t0 = time.monotonic() if t0 is None else t0
        self._budget = budget_s(kind)
        self._box: list[DoorVerdict] = []
        self._thread = threading.Thread(target=self._work, args=(text, context), daemon=True)
        self._thread.start()

    def _work(self, text: str, context: str) -> None:
        left = max(0.2, self._budget - (time.monotonic() - self._t0))
        self._box.append(classify(text, self.kind, context, timeout_s=left))

    def verdict(self, wait: bool = True) -> DoorVerdict:
        """Wait until the budget (measured from ``start``) is spent; a late call reads as a timeout. Without ``wait``,
        only an answer that is already here counts, and a missing one is ``abandoned``."""
        if wait:
            left = self._budget - (time.monotonic() - self._t0)
            self._thread.join(max(0.0, left))
        if self._box:
            return self._box[0]
        ms = round((time.monotonic() - self._t0) * 1000.0, 1)
        return DoorVerdict(self.kind, "timeout" if wait else "abandoned", ms=ms)


class Deferred(_Handle):
    """A verdict made by a detached worker. This process never waits for it: ``decide`` answers "no verdict, today's
    behaviour" at once and logs a ``deferred`` row; the worker writes the verdict row (``outcome=background_result``)."""

    def __init__(self, text: str, kind: str, why: str, *, session_id: str | None = None, context: str = "",
                 tool_use_id: str | None = None) -> None:
        super().__init__(text, kind, session_id=session_id, tool_use_id=tool_use_id)
        self.why = why
        self.spawned = _spawn_worker({
            "text": text[-2000:], "kind": kind, "context": context, "session_id": self.session_id,
            "tool_use_id": self.tool_use_id, "sha": self.sha, "chars": self.chars})

    def verdict(self, wait: bool = True) -> DoorVerdict:
        return DoorVerdict(self.kind, "deferred" if self.spawned else "error")

    def decide(self, *, task_type: str | None = None, outcome: str = "", defer_log: bool = False,
               wait: bool = True) -> DoorDecision:
        self._task_type = task_type
        self._dec = DoorDecision("", f"deferred_{self.why}", False, self.verdict())
        if not defer_log:
            self.record(outcome or f"background_{self.why}")
        return self._dec


class Withheld(_Handle):
    """A prompt that holds a secret (the secret door): it is classified by no model, loopback or not. The row says so."""

    def verdict(self, wait: bool = True) -> DoorVerdict:
        return DoorVerdict(self.kind, "withheld_secret")


def _holds_secret(text: str) -> bool:
    """The secret door (``local_privacy.scan_secrets``, the one the Agent hook's NS3 / DIRECT paths use). A door error
    holds: fail closed."""
    try:
        from llm_router import local_privacy

        return bool(local_privacy.scan_secrets(text))
    except Exception as exc:  # noqa: BLE001
        from llm_router import failopen

        failopen.record("LR-FO-DOOR-VERDICT-SECRET", exc)
        return True


def spawn_allowed() -> bool:
    """``LLM_ROUTER_NO_DETACHED_SPAWN`` (set by the test suite, F44) forbids any detached child."""
    flag = os.environ.get("LLM_ROUTER_NO_DETACHED_SPAWN", "")
    return not (flag and flag.lower() not in ("0", "off", "false", "no"))


def _spawn_worker(payload: dict) -> bool:
    """Start ``python -m llm_router.door_verdict --worker`` detached, the text on its stdin (never on argv). Never raises."""
    import subprocess

    try:
        proc = subprocess.Popen([sys.executable, "-m", "llm_router.door_verdict", "--worker"], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload).encode("utf-8"))
        proc.stdin.close()
        return True
    except Exception as exc:  # noqa: BLE001
        from llm_router import failopen

        failopen.record("LR-FO-DOOR-VERDICT-SPAWN", exc)
        return False


def _worker_main() -> int:
    """The detached child: make the verdict (loading the model if needed) and write its row. Fail-open, silent."""
    try:
        data = json.loads(sys.stdin.read())
        kind = data["kind"]
        v = classify(str(data["text"]), kind, str(data.get("context") or ""), timeout_s=budget_s())
        c = preferred_door(v, kind=kind)
        _write_row({
            "kind": "door_verdict", "ts": round(time.time(), 3), "hook_kind": kind, "mode": mode(),
            "session_id": data.get("session_id", ""), "tool_use_id": data.get("tool_use_id", ""),
            "text_sha": data.get("sha", ""), "chars": data.get("chars"), "task_type": None, "source": v.source,
            "model": v.model, "resident": v.resident, "ms": v.ms, "tier": v.tier, "confidence": v.confidence,
            "door": c.door, "reason": c.reason, "lane": c.lane, "applied": False, "outcome": "background_result",
        })
        return 0
    except Exception as exc:  # noqa: BLE001
        from llm_router import failopen

        failopen.record("LR-FO-DOOR-VERDICT-WORKER", exc)
        return 1


def _model_and_url() -> tuple[str, str] | None:
    """The decision model and Ollama URL from the environment alone (no heavy import in the hook's own thread),
    or None when it is not the decision-model backend."""
    if not _decision_model_configured():
        return None
    url = os.environ.get("LLM_ROUTER_OLLAMA_URL", "").strip() or "http://localhost:11434"
    return os.environ["LLM_ROUTER_DECISION_MODEL"].strip(), url


def start(text: str, kind: str, *, session_id: str | None = None, context: str = "",
          tool_use_id: str | None = None) -> Pending | Deferred | Withheld | None:
    """Begin a verdict, or None when the classifier is off or there is nothing to classify. Never raises.

    The secret door runs first: a prompt that holds a secret is classified by no model (``Withheld``). ``shadow``
    always defers to a worker (nothing depends on the answer, so nothing waits for it). ``on`` decides from a verdict
    made in this process; a PROMPT that finds the model not resident defers instead of waiting for the load. Without a
    detached child (``LLM_ROUTER_NO_DETACHED_SPAWN``) or with the breaker open, the verdict is made here. The budget of
    a verdict made here runs from this call, so no step of it (the probe included) can hold a prompt past it."""
    try:
        t0 = time.monotonic()
        m = mode()
        if m == "off" or kind not in KINDS or not (text or "").strip():
            return None
        kw = {"session_id": session_id, "tool_use_id": tool_use_id}
        if _holds_secret(text):
            return Withheld(text, kind, **kw)
        kw["context"] = context
        if spawn_allowed() and not breaker_open():
            if m == "shadow":
                return Deferred(text, kind, "shadow", **kw)
            if kind == "prompt":
                mu = _model_and_url()
                if mu is not None and _resident(*mu, timeout=min(0.25, budget_s(kind) / 4)) is False:
                    return Deferred(text, kind, "cold", **kw)
        return Pending(text, kind, t0=t0, **kw)
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


def _is_ok(r: dict) -> bool:
    return r.get("source") in ("llm", "cache") and r.get("tier") in TIERS


def summarize(rows: list[dict]) -> dict:
    """Counts over ``door_verdicts.jsonl`` rows (the caller windows them). Every share carries its n.

    A decision row is one per prompt or spawn. ``deferred`` decision rows have a verdict row from a worker
    (``outcome=background_result``); ``coverage`` is the share of decisions that got a verdict, inline or from a worker;
    ``inline_coverage`` is the share that had one at the moment of the decision."""
    rows = [r for r in rows if isinstance(r, dict) and r.get("kind") == "door_verdict"]
    bg = [r for r in rows if r.get("outcome") == "background_result"]
    dec = [r for r in rows if r.get("outcome") != "background_result"]
    n = len(dec)
    inline_ok = [r for r in dec if _is_ok(r)]
    deferred = [r for r in dec if r.get("source") == "deferred"]
    bg_ok = [r for r in bg if _is_ok(r)]
    covered = len(inline_ok) + min(len(bg_ok), len(deferred))
    by_source: dict[str, int] = {}
    for r in dec:
        by_source[str(r.get("source"))] = by_source.get(str(r.get("source")), 0) + 1
    ms_ok = [float(r["ms"]) for r in inline_ok + bg_ok if isinstance(r.get("ms"), (int, float))]
    known = [r for r in inline_ok + bg_ok if r.get("resident") is not None]
    doors: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    for r in dec:
        doors[str(r.get("door") or "none")] = doors.get(str(r.get("door") or "none"), 0) + 1
        outcomes[str(r.get("outcome"))] = outcomes.get(str(r.get("outcome")), 0) + 1
    cold = sum(1 for r in known if r["resident"] is False)
    return {
        "n": n, "verdicts": covered, "coverage": round(covered / n, 4) if n else None,
        "inline_verdicts": len(inline_ok), "inline_coverage": round(len(inline_ok) / n, 4) if n else None,
        "deferred": len(deferred), "background_verdicts": len(bg_ok),
        "background_by_source": {k: sum(1 for r in bg if str(r.get("source")) == k)
                                 for k in sorted({str(r.get("source")) for r in bg})},
        "by_source": by_source, "latency_ms_p50": _pct(ms_ok, 0.5), "latency_ms_p95": _pct(ms_ok, 0.95),
        "cold_n": cold, "residency_known_n": len(known),
        "cold_share": round(cold / len(known), 4) if known else None,
        "doors": doors, "outcomes": outcomes,
        "applied": sum(1 for r in dec if r.get("applied")),
        "by_kind": {k: sum(1 for r in dec if r.get("hook_kind") == k) for k in KINDS},
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


__all__ = ["DoorVerdict", "DoorChoice", "DoorDecision", "Pending", "Deferred", "Withheld", "classify", "mode", "pick_door",
           "preferred_door", "hint_note", "start", "summarize", "read_rows", "local_lane_for", "text_job_of", "codex_usable",
           "warm", "preload_enabled", "spawn_allowed"]


if __name__ == "__main__":
    sys.exit(_worker_main() if sys.argv[1:] == ["--worker"] else 2)
