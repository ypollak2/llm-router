"""Ollama adapter (native tool API) and the helpers `hooks/agent_loop.py` shares.

The helpers here (URL validation, context window, temperature, and the
text/XML tool-call repair shim) were extracted from `hooks/agent_loop.py` so
there is one copy; that module re-exports them under its old private names.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field

from llm_router import local_privacy
from llm_router.local_context_guard import (
    ContextOverflow,
    check_overflow,
    estimate_payload_tokens,
)

# ── shared helpers (verbatim from hooks/agent_loop.py) ───────────────────────


def constrained_decoding_enabled() -> bool:
    """Default ON for the hook loop. `off` falls back to `tools=` plus the repair shim."""
    return os.environ.get("LLM_ROUTER_CONSTRAINED_TOOLS", "").strip().lower() not in (
        "0", "off", "false", "no",
    )


def default_num_ctx(model: str | None) -> int:
    """Per-model window from the single table in ``llm_router.local_models`` (M3.4)."""
    from llm_router.local_models import num_ctx as _table_num_ctx
    return _table_num_ctx(model)


def num_ctx(model: str | None = None) -> int | None:
    """Context window to request, or None to accept the server's default."""
    raw = (os.environ.get("LLM_ROUTER_AGENT_NUM_CTX", "").strip()
           or os.environ.get("LLM_ROUTER_LOCAL_NUM_CTX", "").strip())
    if not raw:
        return default_num_ctx(model)
    try:
        value = int(raw)
        return value if value > 0 else None
    except ValueError:
        return default_num_ctx(model)


_env_num_ctx = num_ctx  # OllamaAdapter.__init__ has a parameter named num_ctx that shadows the function


def agent_temperature() -> float:
    raw = os.environ.get("LLM_ROUTER_AGENT_TEMPERATURE", "").strip()
    try:
        value = float(raw)
        return value if 0.0 <= value <= 2.0 else 0.1
    except ValueError:
        return 0.1


_log = logging.getLogger(__name__)

OLLAMA_URL_DEFAULT = "http://localhost:11434"


def validated_ollama_url(raw: str) -> str:
    """CHZ-SEC-06 scheme/host validation, failing CLOSED to localhost."""
    if not raw:
        return OLLAMA_URL_DEFAULT
    try:
        from llm_router.config import validate_ollama_url
    except Exception:  # noqa: BLE001
        return raw if raw == OLLAMA_URL_DEFAULT else OLLAMA_URL_DEFAULT
    return validate_ollama_url(raw) or OLLAMA_URL_DEFAULT


def get_ollama_url() -> str:
    return validated_ollama_url(
        os.environ.get("LLM_ROUTER_OLLAMA_URL")
        or os.environ.get("OLLAMA_BASE_URL")
        or OLLAMA_URL_DEFAULT
    )


def get_local_model_url() -> str | None:
    """``LLM_ROUTER_LOCAL_MODEL_URL`` validated, or None when unset/invalid.

    The server that holds the ONE big local model (a separate Ollama with
    OLLAMA_MAX_LOADED_MODELS=1). Unset = every local call uses
    :func:`get_ollama_url`, exactly as before.
    """
    raw = os.environ.get("LLM_ROUTER_LOCAL_MODEL_URL", "").strip()
    if not raw:
        return None
    try:
        from llm_router.config import validate_ollama_url
    except Exception:  # noqa: BLE001
        return None
    return validate_ollama_url(raw) or None


def main_url() -> str:
    """Server of THE local model: LLM_ROUTER_LOCAL_MODEL_URL, else the default Ollama URL (one server, never two)."""
    return get_local_model_url() or get_ollama_url()


def url_for_model(model: str | None = None) -> str:
    """The Ollama server a chat/generate call for ``model`` goes to.

    The main local model goes to ``LLM_ROUTER_LOCAL_MODEL_URL`` when set. A
    small model (classifier / decision model, by role or configured name) must
    NOT move: sending it there would evict the big model (max 1 loaded).
    """
    main = get_local_model_url()
    if main is None:
        return get_ollama_url()
    if model:
        try:
            from llm_router import local_models
            bare = model.removeprefix("ollama_chat/").removeprefix("ollama/")
            if not local_models.serves_generate(bare):
                return get_ollama_url()
        except Exception:  # noqa: BLE001
            pass
    return main


def get_local_model() -> str | None:
    """``LLM_ROUTER_LOCAL_MODEL``: the single big local model allowed, or None when unset."""
    return os.environ.get("LLM_ROUTER_LOCAL_MODEL", "").strip() or None


def _is_small_model(model: str | None) -> bool:
    if get_local_model():      # strictly ONE model: nothing is exempt when LLM_ROUTER_LOCAL_MODEL is set
        return False
    if not model:
        return False
    try:
        from llm_router import local_models
        bare = model.removeprefix("ollama_chat/").removeprefix("ollama/")
        return not local_models.serves_generate(bare)
    except Exception:  # noqa: BLE001
        return False


def resolve_main_model(model: str | None, site: str = "") -> str | None:
    """The model a main-local-model path must use.

    With ``LLM_ROUTER_LOCAL_MODEL`` set, every caller-supplied model is replaced by
    it (the override is logged); a small classifier/decision model is left alone.
    Unset: ``model`` unchanged.
    """
    allowed = get_local_model()
    if not allowed or _is_small_model(model):
        return model
    if model:
        had_prefix = next((p for p in ("ollama_chat/", "ollama/") if model.startswith(p)), "")
        if model.removeprefix("ollama_chat/").removeprefix("ollama/") != allowed:
            _log.warning("LLM_ROUTER_LOCAL_MODEL override at %s: %s -> %s", site or "?", model, allowed)
            return had_prefix + allowed
        return model
    return allowed


class LocalModelConflict(RuntimeError):
    """Another big model is resident on a local Ollama; sending would evict or OOM (typed, fail closed)."""

    status = "backend_error"
    kind = "local_model_conflict"

    def __init__(self, resident: str, size_gb: float, url: str, allowed: str, limit_gb: float):
        self.resident, self.size_gb, self.url = resident, size_gb, url
        super().__init__(
            f"local_model_conflict: {resident} is resident on {url} using {size_gb:.1f} GB of VRAM "
            f"(limit for any model other than {allowed}: {limit_gb:g} GB). Not sending the request; "
            f"this router never unloads another server's model. Stop it with `ollama stop {resident}`.")


_LAST = __import__("threading").local()


def record_refusal(exc: "LocalModelConflict | None") -> None:
    """Remember (per thread) why the last main-local call was refused, for callers that get only None."""
    _LAST.refusal = exc


def last_refusal() -> "LocalModelConflict | None":
    return getattr(_LAST, "refusal", None)


def _max_other_vram_gb() -> float:
    raw = os.environ.get("LLM_ROUTER_LOCAL_MAX_OTHER_VRAM_GB", "").strip()
    try:
        value = float(raw)
        return value if value >= 0 else 2.0
    except ValueError:
        return 2.0


def _same_name(a: str, b: str) -> bool:
    def norm(x: str) -> str:
        return x if ":" in x else x + ":latest"
    return norm(a) == norm(b)


_PS_CACHE: dict = {"at": 0.0, "key": None, "err": None}
_PS_TTL_S = 5.0


def check_resident(model: str | None = None, *, deadline_s: float | None = None) -> None:
    """Cached (~5 s) wrapper over :func:`_check_resident`; ``deadline_s`` is the caller's remaining budget in
    seconds (the /api/ps reads are skipped when less than 1 s is left, so a hook never spends its budget here)."""
    allowed = get_local_model()
    if not allowed or _is_small_model(model):
        return
    key = (allowed, get_local_model_url(), get_ollama_url(), _max_other_vram_gb())
    now = time.monotonic()
    if _PS_CACHE["key"] == key and now - _PS_CACHE["at"] < _PS_TTL_S:
        if _PS_CACHE["err"] is not None:
            raise _PS_CACHE["err"]
        return
    if deadline_s is not None and deadline_s < 1.0:
        _log.info("resident guard skipped: %.2fs of budget left", deadline_s)
        return
    try:
        _check_resident(model)
        _PS_CACHE.update(at=now, key=key, err=None)
    except LocalModelConflict as exc:
        _PS_CACHE.update(at=now, key=key, err=exc)
        raise


def _check_resident(model: str | None = None) -> None:
    """Raise :class:`LocalModelConflict` if a model other than the allowed one is resident above the VRAM limit.

    No-op when ``LLM_ROUTER_LOCAL_MODEL`` is unset, or when ``model`` is a small model (its path is exempt).
    Reads ``/api/ps`` on the local-model URL and on the default Ollama URL. An unreachable server never
    blocks (logged). Never unloads anything.
    """
    allowed = get_local_model()
    if not allowed or _is_small_model(model):
        return
    limit = _max_other_vram_gb()
    urls = []
    for u in (get_local_model_url(), get_ollama_url()):
        if u and u.rstrip("/") not in urls:
            urls.append(u.rstrip("/"))
    import json
    import urllib.request
    for url in urls:
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{url}/api/ps"), timeout=1.5) as resp:  # nosec B310 — validated URL
                rows = json.loads(resp.read()).get("models") or []
        except Exception as exc:  # noqa: BLE001
            _log.info("resident guard: /api/ps on %s unreachable (%s); not blocking", url, exc)
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("model") or "")
            if not name or _same_name(name, allowed):
                continue
            raw = row.get("size_vram")
            if raw is None:  # unknown size: not read as 0 GB; logged and not blocking, like an unreachable server
                _log.info("resident guard: %s on %s reports no size_vram; not blocking", name, url)
                continue
            size_gb = float(raw) / 1e9
            if size_gb > limit:
                raise LocalModelConflict(name, size_gb, url, allowed, limit)


_XML_FUNC_RE = re.compile(
    r"<function[=\s]+([A-Za-z_][A-Za-z0-9_]*)\s*>(.*?)(?=</function>|<function[=\s]|\Z)", re.DOTALL)
_XML_PARAM_RE = re.compile(
    r"<parameter[=\s]+([A-Za-z_][A-Za-z0-9_]*)\s*>(.*?)(?=</parameter>|<parameter[=\s]|\Z)", re.DOTALL)


def repair_xml_toolcalls(content: str, known: set[str]) -> list[dict]:
    """Recover tool calls emitted in Qwen's ``<function=name>`` XML dialect.

    Values stay stripped strings (never coerced: a path like ``2.0.0`` must not
    become a number). Unknown tool names are dropped.
    """
    if not content or "<function" not in content:
        return []
    calls: list[dict] = []
    for m in _XML_FUNC_RE.finditer(content):
        name = m.group(1)
        if name not in known:
            continue
        args = {k: v.strip() for k, v in _XML_PARAM_RE.findall(m.group(2))}
        if args:
            calls.append({"function": {"name": name, "arguments": args}})
    return calls


def repair_toolcalls(content: str, known: set[str]) -> list[dict]:
    """Recover tool calls a model emitted as TEXT (JSON object or XML) instead of
    the structured ``tool_calls`` field."""
    if not content:
        return []
    names = "|".join(sorted(known))
    text_re = re.compile(r'\{\s*"name"\s*:\s*"(?:' + names + r')"', re.IGNORECASE)
    if not text_re.search(content):
        return repair_xml_toolcalls(content, known)
    calls: list[dict] = []
    for m in text_re.finditer(content):
        start = content.rfind("{", 0, m.start() + 1)
        depth, i, in_str, esc = 0, start, False, False
        while i < len(content):
            c = content[i]
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = not in_str
            elif not in_str and c == "{":
                depth += 1
            elif not in_str and c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(content[start:i + 1])
                        args = obj.get("arguments") or obj.get("parameters") or {}
                        if isinstance(args, str):
                            args = json.loads(args)
                        calls.append({"function": {"name": obj["name"], "arguments": args}})
                    except (ValueError, KeyError):
                        pass
                    break
            i += 1
    return calls


def tool_call_schema(names: list[str]) -> dict:
    """JSON Schema for one tool call (Ollama `format`), derived from the live tools."""
    return {"type": "object",
            "properties": {"tool": {"type": "string", "enum": list(names)}, "arguments": {"type": "object"}},
            "required": ["tool", "arguments"]}


def parse_constrained_call(content: str, known: set[str]) -> list[dict]:
    """Parse a grammar-constrained reply `{"tool": ..., "arguments": {...}}`."""
    try:
        obj = json.loads(content)
    except (ValueError, TypeError):
        return []
    if not isinstance(obj, dict):
        return []
    name = obj.get("tool")
    args = obj.get("arguments") if isinstance(obj.get("arguments"), dict) else {}
    if not isinstance(name, str) or name not in known:
        return []
    return [{"function": {"name": name, "arguments": args}}]


# ── the router-owned loop's adapter ──────────────────────────────────────────

class AdapterError(Exception):
    """The model call failed. `retryable` marks a transient server-side condition (an empty
    reply with done:false, which a busy or restarting Ollama returns), not a model decision."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class AdapterReply:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    message: dict = field(default_factory=dict)
    tokens_in: int | None = None
    tokens_out: int | None = None
    ms: int = 0
    repaired: bool = False


class OllamaAdapter:
    """Chat against Ollama's native tool API, with the text/XML repair shim."""

    name = "ollama"

    def __init__(self, model: str, *, base_url: str | None = None, num_ctx: int | None = None,
                 temperature: float | None = None, constrained: bool = False, think: bool = False):
        self.model = model
        self.base_url = validated_ollama_url(base_url) if base_url else get_ollama_url()
        # M3.4: one window for every local call: the env-aware num_ctx(model) (override, else the
        # table); an explicit value still wins. None = accept the server's default.
        self.num_ctx = _env_num_ctx(model) if num_ctx is None else num_ctx
        self.temperature = agent_temperature() if temperature is None else temperature
        self.constrained = constrained
        self.think = think

    def chat(self, messages: list[dict], tools: list[dict], *, timeout_s: float) -> AdapterReply:
        names = {t["function"]["name"] for t in tools}
        payload = {
            "model": self.model, "messages": messages, "tools": tools, "stream": False,
            "think": self.think,
            "options": {"temperature": self.temperature},
        }
        if self.num_ctx is not None:
            payload["options"]["num_ctx"] = self.num_ctx
        if self.constrained:
            payload["format"] = tool_call_schema(sorted(names))
        try:
            from llm_router.pack_overflow import PayloadPack
            pack = PayloadPack(payload, num_ctx=self.num_ctx, base_url=self.base_url,
                               model=self.model, site="toolkit.adapters.ollama")
            payload = pack.trim_to()
            check_overflow(payload, num_ctx=pack.num_ctx, base_url=self.base_url, model=pack.model,
                           site="toolkit.adapters.ollama")
        except ContextOverflow as exc:
            raise AdapterError(f"context_overflow: {exc}") from exc
        body = json.dumps(payload).encode()
        req = urllib.request.Request(f"{self.base_url}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        t0 = time.monotonic()
        try:
            with local_privacy.open_url(req, base_url=self.base_url, timeout=max(1.0, timeout_s)) as resp:
                result = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"llm_unreachable: {type(exc).__name__}: {exc}") from exc
        if result.get("error"):
            raise AdapterError(f"ollama_error: {str(result['error'])[:200]}", retryable=True)
        msg = result.get("message", {}) or {}
        content = msg.get("content", "") or ""
        calls = list(msg.get("tool_calls") or [])
        if not content and not calls and not msg.get("thinking") and result.get("done") is not True:
            raise AdapterError("empty_reply: ollama returned no message and done=false", retryable=True)
        repaired = False
        if not calls and content:
            calls = parse_constrained_call(content, names) if self.constrained else []
            if not calls:
                calls = repair_toolcalls(content, names)
            repaired = bool(calls)
        tin, tout = result.get("prompt_eval_count"), result.get("eval_count")
        return AdapterReply(content=content, tool_calls=calls, message=msg,
                            tokens_in=tin if isinstance(tin, int) else None,
                            tokens_out=tout if isinstance(tout, int) else None,
                            ms=int((time.monotonic() - t0) * 1000), repaired=repaired)


__all__ = [
    "AdapterError", "AdapterReply", "OllamaAdapter", "agent_temperature",
    "constrained_decoding_enabled", "default_num_ctx", "estimate_payload_tokens", "get_local_model_url", "get_ollama_url", "url_for_model", "get_local_model",
    "resolve_main_model", "check_resident", "main_url", "record_refusal", "last_refusal", "LocalModelConflict",
    "num_ctx", "parse_constrained_call", "repair_toolcalls", "repair_xml_toolcalls",
    "tool_call_schema", "validated_ollama_url",
]
