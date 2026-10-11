"""Keep the local edit model resident, and fail fast when it is not (plan 3.7).

WHY THIS EXISTS
---------------
Zero-Claude local bounded edits (``zero_claude_edit.maybe_replace``) served 16/20
fixtures offline but only 3/5 live with a warm model and 0/5 cold or contended.
Every live failure hit the hook's ~37 s edit deadline with "model returned no
response". Three causes, each addressed here:

1. NO keep_alive ON THE EDIT CALL. ``direct_executor.call_ollama`` sent no
   ``keep_alive``, so Ollama's server default (5 minutes) unloaded the model
   between prompts. Measured on the qwen3.8 answer set
   (``p2b_work/answers_qwen38.jsonl``, n=82 calls): 36 of 82 paid a model reload,
   min 2.2 s, median 9.3 s, p90 18.6 s, max 36.2 s. A reload at the tail is the
   whole 37 s deadline. ``edit_keep_alive()`` is the one value every edit-path
   call sends (default -1: never unload).

2. THE SESSION-START WARM-UP WARMED A DIFFERENT CONFIGURATION. It loaded
   ``first_installed()`` with the server-default context and a 5 minute
   keep-alive. The edit call then asks for ``num_ctx`` = ``agent_loop._num_ctx``
   (32768 since N20; was 131072 for qwen3.5/3.8), and Ollama treats a different
   ``num_ctx`` as a different runner: it reloads. The warm-up bought nothing for
   the first edit. ``warmup_payload`` builds the request with the SAME model,
   ``num_ctx`` and ``keep_alive`` the edit call uses.

3. A COLD MODEL PLUS A TIGHT DEADLINE IS A PREDICTABLE FAILURE. 0/5 cold-or-contended
   live edits were served inside ~37 s. Burning the whole deadline and then blocking with a
   failure message is the worst outcome: the user waits, then Claude is told to
   do the work anyway. ``should_skip_cold`` asks ``/api/ps`` (one cheap local
   call) whether the model is resident; if it is not and the remaining deadline
   cannot cover a load plus a generation, the caller falls through to Claude
   immediately and the warm-up is fired so the NEXT edit finds the model warm.

What is NOT fixable from the client: two resident models fighting for one
Ollama slot ("two model instances corrupted each other before"). ``/api/ps``
already tells us how many models are resident, so ``dedicated_server_warning``
reports it once at session start.

Everything here is fail-open: if Ollama or ``/api/ps`` is unreachable or answers
in an unexpected shape, the answer is "unknown" and the edit path behaves exactly
as it did before this module existed.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request

#: Default for ``LLM_ROUTER_ZCE_COLD_BUDGET_S``: seconds a cold model needs to
#: load AND generate an edit. Chosen from two measurements, not a model of the
#: server: reload p90 = 18.6 s (n=36 reloads, see module docstring) plus an edit
#: generation that missed a 37 s deadline in 2 of 5 WARM live runs; 0 of 5
#: cold-or-contended live runs were served. Under the standard ~37 s hook budget a cold model therefore fails
#: fast; a caller that grants a larger deadline (or an operator who lowers this)
#: gets a real attempt. Operator-tunable because it is a measured guess.
DEFAULT_COLD_BUDGET_S = 45.0

#: ``/api/ps`` is a local, in-memory listing; it must never be what slows the hook.
_PS_TIMEOUT_S = 0.5


#: Bounded fallback when LLM_ROUTER_LOCAL_KEEP_ALIVE is unset (N20).
DEFAULT_KEEP_ALIVE_S = 600


def edit_keep_alive() -> int | str:
    """The ``keep_alive`` every edit-path Ollama call sends. Default 600 s (bounded).

    ``LLM_ROUTER_LOCAL_KEEP_ALIVE`` overrides it with an integer (seconds, -1 =
    forever, honoured only when set explicitly) or an Ollama duration string such as
    ``"30m"``. An empty or missing value falls back to ``DEFAULT_KEEP_ALIVE_S`` (N20: a
    forever-pinned model contributed to an Ollama hang/eviction on 2026-10-10).
    """
    raw = os.environ.get("LLM_ROUTER_LOCAL_KEEP_ALIVE", "").strip()
    if not raw:
        return DEFAULT_KEEP_ALIVE_S
    try:
        return int(raw)
    except ValueError:
        return raw


def cold_budget_s() -> float:
    """Seconds of deadline a cold model needs; ``LLM_ROUTER_ZCE_COLD_BUDGET_S`` overrides."""
    raw = os.environ.get("LLM_ROUTER_ZCE_COLD_BUDGET_S", "").strip()
    try:
        value = float(raw)
        return value if value >= 0 else DEFAULT_COLD_BUDGET_S
    except ValueError:
        return DEFAULT_COLD_BUDGET_S


def _base_url() -> str:
    from llm_router.hooks.direct_executor import _get_ollama_url
    return _get_ollama_url().rstrip("/")


def ollama_ps(timeout: float = _PS_TIMEOUT_S) -> list[str] | None:
    """Names of the models Ollama has resident right now, or None when unknown.

    None (not []) on any failure or on a response without a ``models`` list, so
    "Ollama is empty" and "I could not find out" are never conflated: an empty
    list means cold, None means do not interfere.
    """
    try:
        req = urllib.request.Request(f"{_base_url()}/api/ps")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return None
        return [str(m.get("name") or m.get("model") or "") for m in models if isinstance(m, dict)]
    except Exception:                                            # noqa: BLE001
        return None


def model_matches(wanted: str, resident: str) -> bool:
    """``qwen3.5`` and ``qwen3.5:latest`` are the same model; ``qwen3.5:7b`` is not."""
    if wanted == resident:
        return True
    if ":" not in wanted:
        return resident == f"{wanted}:latest"
    if ":" not in resident:
        return wanted == f"{resident}:latest"
    return False


def is_model_loaded(model: str, resident: list[str]) -> bool:
    return any(model_matches(model, r) for r in resident)


def should_skip_cold(model: str, deadline_s: float, *, now: float | None = None) -> str | None:
    """A reason string when the edit should fall through to Claude, else None.

    Skips only when ALL hold: ``/api/ps`` answered, the model is not resident,
    and the time left before ``deadline_s`` (an absolute ``time.monotonic()``
    instant) is below ``cold_budget_s()``. Unknown state, a resident model, or a
    generous deadline all return None, i.e. proceed exactly as before.
    """
    resident = ollama_ps()
    if resident is None or is_model_loaded(model, resident):
        return None
    remaining = deadline_s - (time.monotonic() if now is None else now)
    need = cold_budget_s()
    if remaining >= need:
        return None
    return (
        f"model {model!r} is not loaded and {remaining:.0f}s of hook budget cannot cover "
        f"a cold load plus an edit (~{need:.0f}s) — warming it for the next edit"
    )


def dedicated_server_warning(resident: list[str] | None = None) -> str | None:
    """A one-line warning when more than one model is resident, else None.

    Two resident models share one Ollama slot and have corrupted each other
    before. This does not prove a tuned dedicated server is absent; it reports the
    observable symptom, which is the thing that actually breaks the edit path.
    """
    if resident is None:
        resident = ollama_ps()
    if not resident or len(resident) < 2:
        return None
    return (
        f"Ollama has {len(resident)} models resident ({', '.join(resident)}); they contend for one "
        "slot and can evict each other, which makes local edits miss their deadline. "
        "Run a dedicated tuned server (OLLAMA_NUM_PARALLEL / OLLAMA_MAX_LOADED_MODELS=1, "
        "see hooks/start-ollama.sh) or stop the extra model with `ollama stop <model>`."
    )


def warmup_payload(model: str) -> dict:
    """The /api/generate body that loads *model* exactly as the edit call will use it.

    Same ``num_ctx`` (``direct_executor._local_num_ctx`` — the one local window)
    and same ``keep_alive`` (``edit_keep_alive``). ``num_predict: 1`` keeps the
    warm-up itself to a single token: it is a load, not a generation.
    """
    from llm_router.hooks.direct_executor import _local_num_ctx
    options: dict = {"num_predict": 1}
    num_ctx = _local_num_ctx(model)
    if num_ctx:
        options["num_ctx"] = num_ctx
    return {
        "model": model,
        "prompt": " ",
        "stream": False,
        "think": False,
        "keep_alive": edit_keep_alive(),
        "options": options,
    }


def warm_edit_model_bg(model: str | None = None) -> str | None:
    """Fire-and-forget load of the zero-Claude edit model. Returns the model, or None.

    Detached curl (the same pattern as session-start's ``_warm_ollama_bg``): the
    caller never waits and a failure to spawn never propagates. Returns the model
    name when a warm-up was started so a caller can avoid warming the same model
    a second time with different options. Gated on
    ``LLM_ROUTER_ZERO_CLAUDE_SCOPE=edit`` (nothing to warm for otherwise) and
    ``LLM_ROUTER_ZCE_WARMUP`` not off.
    """
    if os.environ.get("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "").strip().lower() != "edit":
        return None
    if os.environ.get("LLM_ROUTER_ZCE_WARMUP", "on").strip().lower() in ("0", "off", "false", "no"):
        return None
    try:
        if model is None:
            from llm_router.zero_claude_edit import edit_model
            model = edit_model()
        payload = json.dumps(warmup_payload(model))
        subprocess.Popen(
            [
                "curl", "-sm", "60", "-o", "/dev/null",
                "-X", "POST", f"{_base_url()}/api/generate",
                "-H", "Content-Type: application/json",
                "-d", payload,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),  # hardcoded argv, no credential-leak risk (R4 scope)
        )
        return model
    except Exception:                                            # noqa: BLE001
        # Best-effort: a spawn failure must not break session start or an edit
        # decision. Nothing is persisted here, so there is nothing to record.
        return None
