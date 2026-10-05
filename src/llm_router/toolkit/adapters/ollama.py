"""Ollama adapter (native tool API) and the helpers `hooks/agent_loop.py` shares.

The helpers here (URL validation, context window, temperature, and the
text/XML tool-call repair shim) were extracted from `hooks/agent_loop.py` so
there is one copy; that module re-exports them under its old private names.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field

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


_LARGE_WINDOW_FAMILIES = ("qwen3.5", "qwen3.8")


def default_num_ctx(model: str | None) -> int:
    name = (model or "").lower()
    return 131072 if any(f in name for f in _LARGE_WINDOW_FAMILIES) else 32768


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


def agent_temperature() -> float:
    raw = os.environ.get("LLM_ROUTER_AGENT_TEMPERATURE", "").strip()
    try:
        value = float(raw)
        return value if 0.0 <= value <= 2.0 else 0.1
    except ValueError:
        return 0.1


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

TOOLKIT_NUM_CTX = 25000          # PLAN section 2.3: 25k-token cap for the local model


class AdapterError(Exception):
    pass


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

    def __init__(self, model: str, *, base_url: str | None = None, num_ctx: int = TOOLKIT_NUM_CTX,
                 temperature: float | None = None, constrained: bool = False, think: bool = False):
        self.model = model
        self.base_url = validated_ollama_url(base_url) if base_url else get_ollama_url()
        self.num_ctx = num_ctx
        self.temperature = agent_temperature() if temperature is None else temperature
        self.constrained = constrained
        self.think = think

    def chat(self, messages: list[dict], tools: list[dict], *, timeout_s: float) -> AdapterReply:
        names = {t["function"]["name"] for t in tools}
        payload = {
            "model": self.model, "messages": messages, "tools": tools, "stream": False,
            "think": self.think,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        if self.constrained:
            payload["format"] = tool_call_schema(sorted(names))
        try:
            check_overflow(payload, num_ctx=self.num_ctx, base_url=self.base_url, model=self.model,
                           site="toolkit.adapters.ollama")
        except ContextOverflow as exc:
            raise AdapterError(f"context_overflow: {exc}") from exc
        body = json.dumps(payload).encode()
        req = urllib.request.Request(f"{self.base_url}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=max(1.0, timeout_s)) as resp:
                result = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"llm_unreachable: {type(exc).__name__}: {exc}") from exc
        msg = result.get("message", {}) or {}
        content = msg.get("content", "") or ""
        calls = list(msg.get("tool_calls") or [])
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
    "AdapterError", "AdapterReply", "OllamaAdapter", "TOOLKIT_NUM_CTX", "agent_temperature",
    "constrained_decoding_enabled", "default_num_ctx", "estimate_payload_tokens", "get_ollama_url",
    "num_ctx", "parse_constrained_call", "repair_toolcalls", "repair_xml_toolcalls",
    "tool_call_schema", "validated_ollama_url",
]
