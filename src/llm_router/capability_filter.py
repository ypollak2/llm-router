"""P2.4: the capability filter. Exclude, never reorder.

A chain entry is removed when it is KNOWN to lack something the request needs:
tools (the request body carries a real tools array: the proxy), vision (images are
present) or window (the tokens that cannot be packed away exceed the model's
window). The prompt-text tools regex never excludes (D-165): it trips on ordinary
questions ("What is the current version of Python?"), so its verdict rides in
``Needs.tools_shadow`` and is only traced (``capability.shadow``). An unknown capability never excludes: API-key rows in the resource map
list only ``generate`` and registry capability lists are optional, so
"exclude unless proven capable" would delete every API model.

Capability sources, per field, first known wins:

1. the fresh resource map (``map_reader.MapView``): a model row (``ollama/<name>``)
   first, then the provider's seat row (anthropic, codex, gemini_cli). API-key
   rows are skipped: their capability list says nothing about tools or vision.
2. ``PROVIDER_CAPS``, the seat-level facts, used when the map has no answer
   (``tests/test_p24_capability_hard.py`` pins it equal to what ``resource_map``
   builds, so the two cannot drift).
3. ``model_registry``: a non-empty ``capabilities`` tuple is a statement (missing
   ``function-calling`` is a known lack); an empty tuple is unknown. The window is
   the registry's ``context_window`` when positive.

The window compares IRREDUCIBLE tokens only (system prompt, the newest user turn,
tool schemas), never the history: context packs are built per target model
(D-101), so a long history is packed down to each model's window.

Kill switch: ``LLM_ROUTER_CAPABILITY_ROUTING=0`` (also off, false, no).

Stdlib only at import time. Every public function is fail-open: any exception
returns the input unchanged and counts a ``failopen`` record.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence, TypeVar

T = TypeVar("T")

#: Share of a window the irreducible tokens may use (10% margin for the chars/4
#: estimator error).
WINDOW_MARGIN = 0.9

#: (tools, vision) of each seat. Equal to what ``resource_map`` builds for the
#: claude, codex and gemini seats; pinned by a parity test.
PROVIDER_CAPS: dict[str, tuple[bool, bool]] = {
    "anthropic": (True, True),
    "codex": (True, False),
    "gemini_cli": (True, True),
}

_IMAGE_TYPES = frozenset({"image", "image_url", "input_image", "image_file"})
_BODY_MEDIA_TYPES = _IMAGE_TYPES | {"document"}
_IMAGE_MARKER = re.compile(r"\[Image #\d+\]")


@dataclass(frozen=True)
class Needs:
    #: HARD: a real tools array in the request body (or an explicit caller flag).
    tools: bool = False
    vision: bool = False
    tokens: int | None = None
    #: SHADOW: the prompt-text tools regex. Traced, never excludes (D-165).
    tools_shadow: bool = False


@dataclass(frozen=True)
class ModelCaps:
    """What is known of one model; ``None`` is unknown, never "lacks"."""
    tools: bool | None = None
    vision: bool | None = None
    window: int | None = None


def enabled() -> bool:
    from llm_router.capabilities import capability_routing_enabled

    return capability_routing_enabled()


def _failopen(code: str, exc: BaseException) -> None:
    try:
        from llm_router import failopen

        failopen.record(code, exc)
    except Exception:  # noqa: BLE001 - accounting must not raise
        pass


def _estimate(text: str) -> int:
    from llm_router.token_budget import estimate_tokens

    return estimate_tokens(text)


# ── needs ────────────────────────────────────────────────────────────────────

def _text_of(content: Any) -> str:
    """Text of a message content: a string, or the text parts and tool results of blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for part in content:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            if isinstance(part.get("text"), str):
                out.append(part["text"])
            elif part.get("type") == "tool_result":
                out.append(_text_of(part.get("content")))
    return "\n".join(out)


def _has_media(content: Any, types: frozenset[str]) -> bool:
    if not isinstance(content, list):
        return False
    return any(isinstance(p, dict) and p.get("type") in types for p in content)


def needs_from_prompt(prompt: str, task_type: str = "", system: str = "") -> Needs:
    """Needs of a prompt: vision from the host's literal ``[Image #N]`` marker,
    tokens from system + prompt. The tools need is never set here: the
    ``detect_capabilities`` regex verdict goes to ``tools_shadow`` only (D-165), so
    "explain how git rebase works" cannot empty an Ollama-only chain."""
    try:
        from llm_router.capabilities import detect_capabilities

        text = prompt or ""
        shadow = detect_capabilities(text, task_type or "").required.needs_tools
        return Needs(vision=bool(_IMAGE_MARKER.search(text)),
                     tokens=_estimate((system or "") + text), tools_shadow=shadow)
    except Exception as exc:  # noqa: BLE001
        _failopen("CHZ-FO-CAPABILITY-FILTER", exc)
        return Needs()


def needs_from_messages(messages: Sequence[Any] | None, prompt: str = "", system: str = "") -> Needs:
    """Needs a caller's history adds to the request: none. ``messages`` excludes the
    request itself and every door renders it through the context pack, where an image
    part becomes the text ``[image]`` (the model never sees it, so it needs no vision:
    D-165 (4), images only in history keep the flattened handling) and a long history
    is packed down to each model's window (D-101). Tokens are system + prompt only."""
    try:
        return Needs(tokens=_estimate((system or "") + (prompt or "")))
    except Exception as exc:  # noqa: BLE001
        _failopen("CHZ-FO-CAPABILITY-FILTER", exc)
        return Needs()


def merge(a: Needs, b: Needs) -> Needs:
    """OR for the flags, max for the tokens."""
    toks = [t for t in (a.tokens, b.tokens) if t is not None]
    return Needs(a.tools or b.tools, a.vision or b.vision, max(toks) if toks else None,
                 a.tools_shadow or b.tools_shadow)


def needs_from_anthropic_body(body: dict) -> Needs:
    """Needs of an Anthropic Messages request: tools when it carries any, vision when
    any turn holds an image or document block, tokens of system + newest user turn
    + tool schemas."""
    try:
        msgs = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
        newest = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
        tools = body.get("tools")
        vision = any(_has_media(m.get("content"), _BODY_MEDIA_TYPES) for m in msgs)
        system = body.get("system")
        sys_text = system if isinstance(system, str) else _text_of(system)
        tool_text = json.dumps(tools, default=str) if tools else ""
        total = _estimate(sys_text + _text_of(newest.get("content") if newest else "") + tool_text)
        return Needs(tools=bool(tools), vision=vision, tokens=total)
    except Exception as exc:  # noqa: BLE001
        _failopen("CHZ-FO-CAPABILITY-FILTER", exc)
        return Needs()


# ── what a model can do ──────────────────────────────────────────────────────

def _registry_meta(model_id: str):
    from llm_router import model_registry as mr

    provider, _, name = model_id.partition("/")
    if not name:
        return None
    models = mr._default_registry().models
    mid = f"{mr._WINDOW_PROVIDER_ALIASES.get(provider, provider)}/{name}"
    meta = models.get(mid)
    if meta is None:
        best = ""
        for k in models:
            if len(k) > len(best) and mid.startswith(k) and mid[len(k):][:1] in ("-", ":", "@"):
                best = k
        meta = models.get(best) if best else None
    return meta


def _first(*vals):
    return next((v for v in vals if v is not None), None)


def caps_of(model_id: str, view: Any = None) -> ModelCaps:
    """What is known about *model_id* ("provider/name"); fail-open to all unknown."""
    try:
        provider = model_id.split("/", 1)[0]
        if view is None:
            from llm_router import map_reader

            view = map_reader.load_view()
        m_tools = m_vision = m_window = None
        if view is not None:
            row = view.caps_by_id.get(model_id) or view.caps_by_provider.get(provider)
            if row is not None:
                m_tools, m_vision = row
            m_window = view.window_by_id.get(model_id)
        s_tools, s_vision = (None, None)
        if provider in PROVIDER_CAPS:
            s_tools, s_vision = PROVIDER_CAPS[provider]
        r_tools = r_vision = r_window = None
        try:
            meta = _registry_meta(model_id)
        except Exception as exc:  # noqa: BLE001 - a broken registry means unknown
            _failopen("CHZ-FO-CAPABILITY-FILTER", exc)
            meta = None
        if meta is not None:
            caps = tuple(getattr(meta, "capabilities", ()) or ())
            if caps:
                r_tools, r_vision = "function-calling" in caps, "vision" in caps
            win = getattr(meta, "context_window", 0)
            r_window = win if isinstance(win, int) and win > 0 else None
        return ModelCaps(_first(m_tools, s_tools, r_tools), _first(m_vision, s_vision, r_vision),
                         _first(m_window, r_window))
    except Exception as exc:  # noqa: BLE001
        _failopen("CHZ-FO-CAPABILITY-FILTER", exc)
        return ModelCaps()


# ── the filter ───────────────────────────────────────────────────────────────

def _identity(m: Any) -> str:
    return m


def filter_chain_report(
    chain: Sequence[T],
    needs: Needs | None,
    *,
    key: Callable[[T], str] = _identity,  # type: ignore[assignment]
    view: Any = None,
) -> tuple[list[T], list[tuple[T, str]]]:
    """``(kept, excluded)``: *chain* without the entries known to lack a needed
    capability, in the original order, and each exclusion with its reason."""
    out = list(chain)
    try:
        if needs is None or not enabled() or not out:
            return out, []
        if not (needs.tools or needs.vision or needs.tokens or needs.tools_shadow):
            return out, []
        if view is None:
            from llm_router import map_reader

            view = map_reader.load_view()
        kept: list[T] = []
        excluded: list[tuple[T, str]] = []
        shadow: list[str] = []
        unknown = 0
        for m in out:
            caps = caps_of(key(m), view)
            why = None
            if needs.tools and caps.tools is False:
                why = "lacks tools"
            elif needs.vision and caps.vision is False:
                why = "lacks vision"
            elif needs.tokens and caps.window is not None and needs.tokens > WINDOW_MARGIN * caps.window:
                why = f"window {caps.window} < {needs.tokens} irreducible tokens"
            if why is None:
                kept.append(m)
                if needs.tools_shadow and not needs.tools and caps.tools is False:
                    shadow.append(key(m))
                unknown += int((needs.tools and caps.tools is None)
                               or (needs.vision and caps.vision is None))
            else:
                excluded.append((m, why))
        if excluded:
            try:
                from llm_router import trace

                trace.emit("capability.filter",
                           excluded=[{"model": key(m), "why": w} for m, w in excluded],
                           unknown=unknown)
            except Exception:  # noqa: BLE001 - a trace emit must not break routing
                pass
        if shadow:
            try:
                from llm_router import trace

                trace.emit("capability.shadow", need="tools", source="prompt_regex",
                           would_exclude=shadow)
            except Exception:  # noqa: BLE001 - a trace emit must not break routing
                pass
        return kept, excluded
    except Exception as exc:  # noqa: BLE001
        _failopen("CHZ-FO-CAPABILITY-FILTER", exc)
        return list(chain), []


def filter_chain(
    chain: Sequence[T],
    needs: Needs | None,
    *,
    key: Callable[[T], str] = _identity,  # type: ignore[assignment]
    view: Any = None,
) -> list[T]:
    """:func:`filter_chain_report` without the reasons."""
    return filter_chain_report(chain, needs, key=key, view=view)[0]


def describe(excluded: Sequence[tuple[Any, str]], key: Callable[[Any], str] = _identity) -> str:
    """``"model: reason, model: reason"`` for an error message."""
    return ", ".join(f"{key(m)}: {why}" for m, why in excluded)
