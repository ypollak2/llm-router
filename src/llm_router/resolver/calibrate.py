"""Short capability probes, per model, local-first and zero-cost by default.

Five probes, each deterministic to grade and safe to grade:

* ``json``         reply with a JSON object; exact values checked
* ``edit``         return an old/new pair for a one-token bug; it is APPLIED to a
                   fixture and the result must equal the expected source (no model
                   code is ever executed)
* ``tool_call``    a two-turn tool round-trip: the model must call ``lookup`` with
                   the right argument, then use the tool result in its answer
* ``vision``       read random digits from an image (only where vision is claimed;
                   same three-trial exactness bar as ``vision_registry``)
* ``long_context`` recall a needle placed mid-prompt at a STATED size

Cost rule: only models on the ``local`` route are probed unless ``allow_paid`` is
set, and with it the estimate is shown before anything is sent. Subscription CLIs
cannot take tool schemas or images, so those two probes record ``ok=None`` there.
A probe that errors records ``ok=False`` with the error; one that was not run
records ``ok=None``. Neither is a pass.
"""

from __future__ import annotations

import ast
import base64
import fnmatch
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from llm_router.resolver import profile as profile_mod
from llm_router.resolver.profile import ModelProfile, ProbeResult
from llm_router.resolver.types import CAP_NO, CAP_YES, ROUTE_LOCAL, Inventory, ModelEntry

DEFAULT_BUDGET_S = 300.0
DEFAULT_PROBE_TIMEOUT_S = 90.0
DEFAULT_RECALL_TOKENS = 8000
VISION_TRIALS = 3

_EDIT_SOURCE = "def add(a, b):\n    return a - b\n"
_EDIT_EXPECTED = "def add(a, b):\n    return a + b\n"
_TOOLS = [{"type": "function", "function": {
    "name": "lookup", "description": "Look up the value stored under a key.",
    "parameters": {"type": "object", "properties": {"key": {"type": "string"}},
                   "required": ["key"]}}}]


#: The ONLY failure texts that are ever stored in a profile. Model output and
#: exception messages can carry secrets (a key echoed back, a URL with a token), so
#: nothing a model or a provider said is persisted; a probe stores ``ok`` plus one of
#: these fixed reasons.
ERR_NO_RESPONSE = "no response"
ERR_PROVIDER = "provider error"
TRANSPORT_DETAILS = (ERR_NO_RESPONSE, ERR_PROVIDER)


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)   # [{"name":..., "arguments": {...}}]
    error: str | None = None


def _safe_err(r: "Reply") -> str:
    """The fixed category of a failed reply; anything else collapses to ERR_PROVIDER."""
    return r.error if r.error in TRANSPORT_DETAILS else ERR_PROVIDER


class Completer(Protocol):
    supports_tools: bool
    supports_images: bool

    def chat(self, model: str, messages: list[dict], *, tools: list[dict] | None = None,
             num_ctx: int | None = None, timeout: float = DEFAULT_PROBE_TIMEOUT_S) -> Reply: ...


# --------------------------------------------------------------------- Ollama

class OllamaCompleter:
    """POST /api/chat on the local Ollama. Local, so zero cost."""

    supports_tools = True
    supports_images = True

    def __init__(self, base: str, post: Callable[[str, dict, float], Any]):
        self._base = base.rstrip("/")
        self._post = post

    def chat(self, model, messages, *, tools=None, num_ctx=None, timeout=DEFAULT_PROBE_TIMEOUT_S):
        body: dict[str, Any] = {"model": model.removeprefix("ollama/"), "messages": messages,
                                "stream": False, "think": False,
                                "options": {"temperature": 0.0}}
        if num_ctx:
            body["options"]["num_ctx"] = int(num_ctx)
        if tools:
            body["tools"] = tools
        data = self._post(f"{self._base}/api/chat", body, timeout)
        if not isinstance(data, dict) or "message" not in data:
            if isinstance(data, dict) and data.get("error"):
                return Reply(error=ERR_PROVIDER)
            return Reply(error=ERR_NO_RESPONSE)
        msg = data["message"] or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append({"name": fn.get("name"), "arguments": args if isinstance(args, dict) else {}})
        return Reply(text=msg.get("content") or "", tool_calls=calls)


# ---------------------------------------------------------------------- cloud

class CloudCompleter:
    """Paid/quota-spending route. Only constructed when ``allow_paid`` is set."""

    def __init__(self, entry: ModelEntry):
        self._entry = entry
        self.supports_tools = entry.route_kind == "api"
        self.supports_images = entry.route_kind == "api"

    def chat(self, model, messages, *, tools=None, num_ctx=None, timeout=DEFAULT_PROBE_TIMEOUT_S):
        try:
            if self._entry.route_kind == "api":
                return self._litellm(model, messages, tools, timeout)
            return self._cli(model, messages, timeout)
        except Exception:  # noqa: BLE001 - the message may carry a key; keep only the category
            return Reply(error=ERR_PROVIDER)

    def _litellm(self, model, messages, tools, timeout) -> Reply:
        from llm_router import providers

        resp = providers.litellm.completion(model=model, messages=messages, tools=tools,
                                            timeout=timeout, temperature=0)
        msg = resp.choices[0].message
        calls = []
        for tc in getattr(msg, "tool_calls", None) or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except ValueError:
                args = {}
            calls.append({"name": tc.function.name, "arguments": args})
        return Reply(text=msg.content or "", tool_calls=calls)

    def _cli(self, model, messages, timeout) -> Reply:
        import asyncio
        import tempfile

        prompt = "\n\n".join(str(m.get("content", "")) for m in messages if m.get("role") != "tool")
        name = model.split("/", 1)[-1]
        with tempfile.TemporaryDirectory() as tmp:
            kw = {"working_dir": tmp, "timeout": int(timeout), "context_root": tmp}
            if model.startswith("codex/"):
                from llm_router.codex_agent import run_codex
                res = asyncio.run(run_codex(prompt, model=name, **kw))
            elif model.startswith("claude_subscription/"):
                from llm_router.claude_agent import run_claude
                res = asyncio.run(run_claude(prompt, model=name, **kw))
            else:
                from llm_router.gemini_cli_agent import run_gemini_cli
                res = asyncio.run(run_gemini_cli(prompt, model=name, **kw))
        if not res.success:
            return Reply(error=ERR_PROVIDER)
        return Reply(text=res.content)


def default_completer(entry: ModelEntry, post: Callable[[str, dict, float], Any],
                      ollama_base: str) -> Completer:
    if entry.route_kind == ROUTE_LOCAL or entry.provider == "ollama":
        # Ollama cloud models are reached through the local daemon too; they stay
        # on a non-local route, so calibrate still refuses them without --allow-paid.
        return OllamaCompleter(ollama_base, post)
    return CloudCompleter(entry)


# --------------------------------------------------------------------- probes

def _extract_json(text: str) -> Any | None:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def probe_json(c: Completer, model: str, timeout: float) -> ProbeResult:
    t0 = time.monotonic()
    r = c.chat(model, [{"role": "user", "content":
               'Reply with only a JSON object with key "a" set to the integer 17 and key "b" set to the string "x".'}],
               timeout=timeout)
    lat = time.monotonic() - t0
    if r.error:
        return ProbeResult(False, _safe_err(r), lat)
    obj = _extract_json(r.text)
    ok = isinstance(obj, dict) and obj.get("a") == 17 and obj.get("b") == "x"
    return ProbeResult(ok, "valid object with the expected values" if ok else "wrong value", lat)


def apply_edit(source: str, old: str, new: str) -> str | None:
    """Apply one exact-unique replacement, or None when ``old`` is not found
    exactly once."""
    if not old or source.count(old) != 1:
        return None
    return source.replace(old, new, 1)


def _same_program(a: str, b: str) -> bool:
    try:
        return ast.dump(ast.parse(a)) == ast.dump(ast.parse(b))
    except SyntaxError:
        return False


def probe_edit(c: Completer, model: str, timeout: float) -> ProbeResult:
    t0 = time.monotonic()
    r = c.chat(model, [{"role": "user", "content":
               "This function should add its arguments but has a bug:\n\n" + _EDIT_SOURCE +
               '\nReply with only JSON: {"old_string": <exact text to replace>, '
               '"new_string": <replacement>}. Change as little as possible.'}], timeout=timeout)
    lat = time.monotonic() - t0
    if r.error:
        return ProbeResult(False, _safe_err(r), lat)
    obj = _extract_json(r.text)
    if not isinstance(obj, dict):
        return ProbeResult(False, "no JSON edit in the reply", lat)
    edited = apply_edit(_EDIT_SOURCE, str(obj.get("old_string", "")), str(obj.get("new_string", "")))
    if edited is None:
        return ProbeResult(False, "old_string did not match the fixture exactly once", lat)
    ok = _same_program(edited, _EDIT_EXPECTED)
    return ProbeResult(ok, "edit applied; result equals the expected program" if ok
                       else "edit applied but the result is not the expected program", lat)


def probe_tool_call(c: Completer, model: str, timeout: float) -> ProbeResult:
    if not getattr(c, "supports_tools", False):
        return ProbeResult(None, "this route cannot take tool schemas")
    t0 = time.monotonic()
    secret = f"ZX-{random.randint(1000, 9999)}"
    user = {"role": "user", "content": "What value is stored under the key 'alpha'? Use the lookup tool."}
    r1 = c.chat(model, [user], tools=_TOOLS, timeout=timeout)
    if r1.error:
        return ProbeResult(False, _safe_err(r1), time.monotonic() - t0)
    call = next((tc for tc in r1.tool_calls if tc.get("name") == "lookup"), None)
    if call is None:
        return ProbeResult(False, "no lookup tool call", time.monotonic() - t0)
    if str((call.get("arguments") or {}).get("key", "")).lower() != "alpha":
        return ProbeResult(False, "wrong arguments", time.monotonic() - t0)
    msgs = [user,
            {"role": "assistant", "content": r1.text or "",
             "tool_calls": [{"function": {"name": "lookup", "arguments": {"key": "alpha"}}}]},
            {"role": "tool", "name": "lookup", "content": secret}]
    r2 = c.chat(model, msgs, tools=_TOOLS, timeout=timeout)
    lat = time.monotonic() - t0
    if r2.error:
        return ProbeResult(False, _safe_err(r2), lat)
    ok = secret in (r2.text or "")
    return ProbeResult(ok, "called the tool and used its result" if ok
                       else "called the tool but did not use its result", lat)


def probe_vision(c: Completer, model: str, timeout: float) -> ProbeResult:
    if not getattr(c, "supports_images", False):
        return ProbeResult(None, "this route cannot take images")
    from llm_router.vision_registry import _probe_image

    t0 = time.monotonic()
    for _ in range(VISION_TRIALS):
        code = str(random.randint(1000, 9999))
        png = base64.b64encode(_probe_image(code)).decode()
        r = c.chat(model, [{"role": "user", "images": [png], "content":
                   "What number is written in this image? Reply with the digits only."}],
                   timeout=timeout)
        if r.error:
            return ProbeResult(False, _safe_err(r), time.monotonic() - t0)
        if code not in (r.text or ""):
            return ProbeResult(False, "misread the code", time.monotonic() - t0)
    return ProbeResult(True, f"read {VISION_TRIALS}/{VISION_TRIALS} random codes exactly",
                       time.monotonic() - t0)


_FILLER = ("The committee reviewed the quarterly logistics report and noted that shipment "
           "schedules were unchanged across all regional depots. ")


def probe_long_context(c: Completer, model: str, timeout: float, *, tokens: int,
                       window: int | None) -> ProbeResult:
    if window is not None and window < tokens + 512:
        return ProbeResult(None, f"declared window {window} is smaller than the {tokens}-token probe",
                           size_tokens=tokens)
    code = f"ZEBRA-{random.randint(1000, 9999)}"
    chars = tokens * 4
    block = (_FILLER * (chars // len(_FILLER) + 1))[:chars]
    half = len(block) // 2
    prompt = (block[:half] + f"\nThe access code is {code}.\n" + block[half:] +
              "\n\nWhat is the access code? Reply with the code only.")
    t0 = time.monotonic()
    r = c.chat(model, [{"role": "user", "content": prompt}], num_ctx=tokens + 1024, timeout=timeout)
    lat = time.monotonic() - t0
    if r.error:
        return ProbeResult(False, _safe_err(r), lat, tokens)
    ok = code in (r.text or "")
    return ProbeResult(ok, f"recalled the needle at ~{tokens} tokens" if ok
                       else f"missed the needle at ~{tokens} tokens", lat, tokens)


# --------------------------------------------------------------- orchestration

@dataclass
class Estimate:
    model_id: str
    input_tokens: int
    output_tokens: int
    usd: float | None
    note: str


def estimate_cost(entry: ModelEntry, tokens: int) -> Estimate:
    """Rough cost of the suite: ~2.5k input tokens of short probes + the recall
    prompt, ~600 output tokens. An estimate, not a quote."""
    tin, tout = 2500 + tokens, 600
    if entry.route_kind == ROUTE_LOCAL:
        return Estimate(entry.id, tin, tout, 0.0, "local, no cost")
    if entry.route_kind == "subscription":
        return Estimate(entry.id, tin, tout, 0.0, "no marginal cost; spends subscription quota (~6 requests)")
    if entry.price_in_per_mtok is None or entry.price_out_per_mtok is None:
        return Estimate(entry.id, tin, tout, None, "price unknown")
    usd = (tin * entry.price_in_per_mtok + tout * entry.price_out_per_mtok) / 1_000_000
    return Estimate(entry.id, tin, tout, usd, "metered API")


@dataclass
class CalibrationReport:
    profiles: dict[str, ModelProfile] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    estimates: list[Estimate] = field(default_factory=list)
    out_of_budget: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)   # inconclusive: not recorded


def select_models(inv: Inventory, patterns: list[str] | None, *, allow_paid: bool
                  ) -> tuple[list[ModelEntry], list[tuple[str, str]]]:
    chosen: list[ModelEntry] = []
    skipped: list[tuple[str, str]] = []
    for m in inv.models:
        if patterns and not any(fnmatch.fnmatch(m.id, p) or p in m.id for p in patterns):
            continue
        if not m.present:
            skipped.append((m.id, "removed since the last inventory"))
        elif not m.authorized:
            skipped.append((m.id, f"not authorized ({m.auth_detail})"))
        elif m.route_kind != ROUTE_LOCAL and not allow_paid:
            skipped.append((m.id, "cloud route; pass --allow-paid to spend on it"))
        else:
            chosen.append(m)
    return chosen, skipped


def _is_transport(p: ProbeResult) -> bool:
    return p.ok is False and p.detail in TRANSPORT_DETAILS


def calibrate_model(entry: ModelEntry, c: Completer, *, tokens: int, probe_timeout: float,
                    deadline: float, clock: Callable[[], float]) -> ModelProfile | None:
    """Run the suite for one model. ``None`` when the budget ran out first.

    A profile with ``reachable=False`` means the run was INCONCLUSIVE: every probe hit
    a transport error, or one of the two probes that qualify EASY (json, edit) did.
    ``run_calibration`` does not record such a profile, so a flaky server can never
    overwrite a good earlier measurement with "failed". A transport error on a later
    probe is recorded as not run (``ok=None``), never as a failure of the model.
    """
    probes: dict[str, ProbeResult] = {}
    plan: list[tuple[str, Callable[[float], ProbeResult]]] = [
        ("json", lambda t: probe_json(c, entry.id, t)),
        ("edit", lambda t: probe_edit(c, entry.id, t)),
    ]
    if entry.cap("tools").state != CAP_NO:
        plan.append(("tool_call", lambda t: probe_tool_call(c, entry.id, t)))
    if entry.cap("vision").state == CAP_YES:
        plan.append(("vision", lambda t: probe_vision(c, entry.id, t)))
    plan.append(("long_context", lambda t: probe_long_context(
        c, entry.id, t, tokens=tokens, window=entry.context_window)))
    for name, fn in plan:
        left = deadline - clock()
        if left <= 1.0:
            return None
        try:
            probes[name] = fn(min(probe_timeout, left))
        except Exception:  # noqa: BLE001 - the message may carry a secret; keep only the category
            probes[name] = ProbeResult(False, ERR_PROVIDER)
    inconclusive = (all(_is_transport(p) for p in probes.values())
                    or any(_is_transport(probes[n]) for n in ("json", "edit") if n in probes))
    if not inconclusive:
        for name, p in list(probes.items()):
            if _is_transport(p):
                probes[name] = ProbeResult(None, p.detail, p.latency_s, p.size_tokens)
    return profile_mod.build_profile(
        entry.id, probes, reachable=not inconclusive,
        claims_tools=entry.cap("tools").state != CAP_NO, now=time.time())


def run_calibration(
    inv: Inventory,
    *,
    patterns: list[str] | None = None,
    allow_paid: bool = False,
    budget_s: float = DEFAULT_BUDGET_S,
    tokens: int = DEFAULT_RECALL_TOKENS,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT_S,
    completer_for: Callable[[ModelEntry], Completer],
    clock: Callable[[], float] = time.monotonic,
    on_start: Callable[[ModelEntry], None] | None = None,
) -> CalibrationReport:
    chosen, skipped = select_models(inv, patterns, allow_paid=allow_paid)
    report = CalibrationReport(skipped=skipped,
                               estimates=[estimate_cost(m, tokens) for m in chosen])
    deadline = clock() + budget_s
    for m in chosen:
        if on_start:
            on_start(m)
        prof = calibrate_model(m, completer_for(m), tokens=tokens, probe_timeout=probe_timeout,
                               deadline=deadline, clock=clock)
        if prof is None:
            report.out_of_budget.append(m.id)
        elif not prof.reachable:
            report.unreachable.append(m.id)
        else:
            report.profiles[m.id] = prof
    return report
