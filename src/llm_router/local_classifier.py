"""Local (Ollama) classifier: task_type + complexity + recommended tier.

``LLM_ROUTER_LOCAL_CLASSIFIER`` = ``off`` (default) | ``shadow`` | ``on``.
Any other value reads as ``off``, so a typo cannot switch behaviour.

* ``off``     nothing here runs; routing is byte-identical to before.
* ``shadow``  the local model is asked, its answer is logged NEXT TO the rules'
              answer, and the rules' answer is returned unchanged.
* ``on``      a valid local answer replaces the rules' task_type/complexity.
              Floors (``apply_complexity_floor``) and the proxy's tier policy
              still apply downstream. ``on`` is never a default.

Design constraints (all pinned in tests/test_local_classifier.py):

* stdlib only, so the stand-alone hook can import it cheaply;
* strict JSON: exactly the three schema keys, every value in its enum, else the
  answer is discarded and the caller keeps the rules;
* hard wall-clock budget (a worker thread is abandoned on expiry), then rules;
* after a failure the endpoint is skipped for ``_COOLDOWN_S`` so a down or cold
  Ollama costs one budget, not one per prompt;
* the prompt is truncated head+tail to ~2k chars before it leaves the process;
* the shadow log stores lengths and labels, never prompt text.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PROMPT_VERSION = "v2"
TASK_TYPES = ("query", "research", "generate", "analyze", "code")
COMPLEXITIES = ("simple", "moderate", "complex")
TIERS = ("local", "haiku", "sonnet", "opus")
# The tier is the decision; complexity is what the downstream policy tables key on.
TIER_TO_COMPLEXITY = {"local": "simple", "haiku": "simple", "sonnet": "moderate", "opus": "complex"}

DEFAULT_MODEL = "qwen3.5:latest"
DEFAULT_TIMEOUT_MS = 1500
KEEP_ALIVE = "600s"
MAX_PROMPT_CHARS = 2000
_COOLDOWN_S = 30.0

SCHEMA = {
    "type": "object",
    "properties": {
        "task_type": {"type": "string", "enum": list(TASK_TYPES)},
        "complexity": {"type": "string", "enum": list(COMPLEXITIES)},
        "tier": {"type": "string", "enum": list(TIERS)},
    },
    "required": ["task_type", "complexity", "tier"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "Route a software task to the cheapest model tier that can finish it. "
    'Reply with JSON only: {"task_type":..,"complexity":..,"tier":..}.\n'
    "tier: local = trivial lookup, rename, format or short summary; "
    "haiku = a scoped change or question in one or two files, clear spec; "
    "sonnet = multi-file work, subtle debugging, design trade-offs; "
    "opus = hard architecture, deep reasoning, security-critical changes.\n"
    "complexity: simple | moderate | complex. "
    "task_type: query | research | generate | analyze | code."
)


@dataclass(frozen=True)
class LocalVerdict:
    task_type: str
    complexity: str  # tier-implied, see TIER_TO_COMPLEXITY
    tier: str
    model: str
    latency_ms: float
    raw_complexity: str  # what the model said; logged, not routed on


def mode() -> str:
    raw = os.environ.get("LLM_ROUTER_LOCAL_CLASSIFIER", "").strip().lower()
    return raw if raw in ("shadow", "on") else "off"


def _model() -> str:
    return os.environ.get("LLM_ROUTER_LOCAL_CLASSIFIER_MODEL", "").strip() or DEFAULT_MODEL


def _timeout_s() -> float:
    try:
        ms = int(os.environ.get("LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS", ""))
    except ValueError:
        ms = DEFAULT_TIMEOUT_MS
    return max(0.05, min(ms, 10_000) / 1000.0)


def _base_url() -> str:
    return os.environ.get("LLM_ROUTER_OLLAMA_URL", "").strip() or "http://localhost:11434"


def truncate(text: str, limit: int = MAX_PROMPT_CHARS) -> str:
    """Head and tail, so both the ask and the closing constraints survive."""
    if len(text) <= limit:
        return text
    half = (limit - 5) // 2
    return text[:half] + "\n...\n" + text[-half:]


def parse_verdict(content: str) -> dict | None:
    """Strict: valid JSON, exactly the schema keys, every value in its enum."""
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"task_type", "complexity", "tier"}:
        return None
    if (
        data["task_type"] not in TASK_TYPES
        or data["complexity"] not in COMPLEXITIES
        or data["tier"] not in TIERS
    ):
        return None
    return data


_cool_until = 0.0


def _post(model: str, text: str, timeout: float) -> str:
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": truncate(text)},
        ],
        "format": SCHEMA,
        "stream": False,
        "think": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0, "num_predict": 48, "num_ctx": 2048},
    }).encode()
    req = urllib.request.Request(
        f"{_base_url()}/api/chat", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local URL
        return json.loads(resp.read()).get("message", {}).get("content", "")


def classify_local(text: str, *, model: str | None = None,
                   timeout_s: float | None = None) -> LocalVerdict | None:
    """One local answer, or ``None`` (unreachable, slow, malformed). Never raises."""
    global _cool_until
    if not text or not text.strip():
        return None
    now = time.monotonic()
    if now < _cool_until:
        return None
    model = model or _model()
    budget = timeout_s if timeout_s is not None else _timeout_s()
    box: list[str | BaseException] = []

    def work() -> None:
        try:
            box.append(_post(model, text, budget))
        except BaseException as exc:  # noqa: BLE001 - reported through the box
            box.append(exc)

    t0 = time.monotonic()
    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(budget)
    latency_ms = (time.monotonic() - t0) * 1000.0
    if not box or isinstance(box[0], BaseException):
        _cool_until = time.monotonic() + _COOLDOWN_S
        return None
    parsed = parse_verdict(box[0])  # type: ignore[arg-type]
    if parsed is None:
        return None
    return LocalVerdict(
        task_type=parsed["task_type"],
        complexity=TIER_TO_COMPLEXITY[parsed["tier"]],
        tier=parsed["tier"],
        model=model,
        latency_ms=round(latency_ms, 1),
        raw_complexity=parsed["complexity"],
    )


def _log_path() -> Path:
    from llm_router import paths

    return paths.state_path("local_classifier_shadow.jsonl")


def shadow_record(surface: str, rules_task: str, rules_complexity: str,
                  verdict: LocalVerdict | None, prompt_chars: int) -> None:
    """Append one line: rules answer beside local answer. No prompt text, ever."""
    try:
        row = {
            "ts": round(time.time(), 3),
            "surface": surface,
            "prompt_chars": prompt_chars,
            "rules": {"task_type": rules_task, "complexity": rules_complexity},
            "local": None if verdict is None else {
                "task_type": verdict.task_type,
                "complexity": verdict.raw_complexity,
                "tier": verdict.tier,
                "model": verdict.model,
                "latency_ms": verdict.latency_ms,
            },
        }
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as exc:  # noqa: BLE001 - logging must never break routing
        try:
            from llm_router import failopen
            failopen.record("CHZ-FO-LOCAL-CLASSIFIER-SHADOW-LOG", exc)
        except Exception:  # noqa: BLE001
            pass


def apply(text: str, task_type: str, complexity: str, surface: str) -> tuple[str, str]:
    """The single seam every caller uses. ``(task_type, complexity)`` in, out.

    ``off`` returns the inputs untouched without any other work. ``shadow``
    logs and returns the inputs untouched. ``on`` returns the local answer when
    there is a valid one, else the inputs.
    """
    m = mode()
    if m == "off":
        return task_type, complexity
    verdict = classify_local(text)
    if m == "shadow":
        shadow_record(surface, task_type, complexity, verdict, len(text))
        return task_type, complexity
    if verdict is None:
        return task_type, complexity
    return verdict.task_type, verdict.complexity
