"""Overflow policy for local calls: trim, then a bigger window, then escalate.

PLAN v16 P1.3 (R-CTX-4, R-LOC-9). `local_context_guard.check_overflow` refuses a
payload that would not fit the local window; before this module the only
remedy was to escalate to the next model. The order is now:

1. trim the trimmable layers, in this order only: project knowledge, then
   summary detail (Goal and Current state are kept), then the instructions
   digest. The request and the recent messages are never trimmed;
2. still too big: the next eligible local model whose window
   (`local_models.num_ctx`, the only source of windows) is larger;
3. only then let `check_overflow` raise `ContextOverflow`.

OPT-IN, DEFAULT OFF (owner decision D-47: nothing new on a live path). Set
``LLM_ROUTER_PACK_OVERFLOW=1`` to enable. Off, `PayloadPack.trim_to` returns the
very payload object it was given and changes no field, so every call site
behaves exactly as before.

Layers are recognised by tag in the text of a system-role message (or of the
first message): ``<project-knowledge>``, ``<conversation-summary>`` and
``<project-instructions>``. Untagged text is never touched. The pure
`trim_pack` does the same ordering on a `ContextPack`-shaped dataclass (fields
``project``, ``summary``, ``instructions``) without importing `context_pack`.
"""
from __future__ import annotations

import copy
import dataclasses
import os
import re
from typing import Any

#: Trim order. Index 0 goes first.
LAYERS = ("project", "summary", "instructions")
TAGS = {
    "project": "project-knowledge",
    "summary": "conversation-summary",
    "instructions": "project-instructions",
}
#: Summary sections that survive summary trimming.
KEEP_SUMMARY_SECTIONS = ("goal", "current state")


def enabled() -> bool:
    return os.environ.get("LLM_ROUTER_PACK_OVERFLOW", "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Layer shrinking (pure text functions)
# ---------------------------------------------------------------------------

def _shrink_summary(text: str) -> str:
    """Keep only the Goal and Current state sections of a summary."""
    out: list[str] = []
    keep = False
    for line in text.splitlines():
        head = re.match(r"^\s{0,3}(?:#{1,6}\s*)?([A-Za-z][A-Za-z ]*?)\s*:?\s*$", line)
        if head and (line.lstrip().startswith("#") or line.rstrip().endswith(":")):
            keep = head.group(1).strip().lower() in KEEP_SUMMARY_SECTIONS
        if keep:
            out.append(line)
    return "\n".join(out).strip()


def _shrink_instructions(text: str) -> str:
    """Headings only: the digest's rule lines go, its structure stays."""
    return "\n".join(ln for ln in text.splitlines() if ln.lstrip().startswith("#")).strip()


def _shrink(layer: str, text: str) -> str:
    if layer == "project":
        return ""
    if layer == "summary":
        return _shrink_summary(text)
    return _shrink_instructions(text)


def _tokens(text: str) -> int:
    from llm_router.token_budget import estimate_tokens

    return estimate_tokens(text) if text else 0


# ---------------------------------------------------------------------------
# ContextPack-shaped objects
# ---------------------------------------------------------------------------

def trim_pack(pack: Any, budget_tokens: int) -> Any:
    """Return `pack` trimmed, in `LAYERS` order, until it fits `budget_tokens`.

    `pack` is a dataclass with ``request``, ``recent``, ``project``,
    ``summary`` and ``instructions`` (a `context_pack.ContextPack`). ``request``
    and ``recent`` are never read for trimming and never changed. A layer is
    shrunk only while the pack is still over budget, so a pack that fits comes
    back untouched (the same object).
    """
    def size(p: Any) -> int:
        recent = sum(_tokens(str(m.get("content", ""))) for m in (p.recent or []))
        return (_tokens(p.request) + recent + _tokens(p.project or "")
                + _tokens(p.summary or "") + _tokens(p.instructions or ""))

    cur = pack
    for layer in LAYERS:
        if size(cur) <= budget_tokens:
            return cur
        old = getattr(cur, layer)
        if not old:
            continue
        new = _shrink(layer, old)
        cur = dataclasses.replace(cur, **{layer: new or (None if layer == "summary" else "")})
    return cur


# ---------------------------------------------------------------------------
# Bigger window
# ---------------------------------------------------------------------------

def pick_larger_model(model: str | None, needed_tokens: int, candidates: list[str] | None = None) -> str | None:
    """The eligible candidate with the smallest window that is larger than
    `model`'s and holds `needed_tokens`; None when there is none.

    Windows come from `local_models.num_ctx` only. Without `candidates` the
    names listed in `local_models.NUM_CTX` are used (the P1.8 map is not built
    yet; pass its eligible models here when it lands).
    """
    from llm_router import local_models

    current = local_models.num_ctx(model)
    pool = candidates if candidates is not None else list(local_models.NUM_CTX)
    best: tuple[int, str] | None = None
    for name in pool:
        win = local_models.num_ctx(name)
        if win <= current or win < needed_tokens:
            continue
        if best is None or win < best[0]:
            best = (win, name)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Payload form: what the six check_overflow sites hold
# ---------------------------------------------------------------------------

def _tagged(layer: str) -> re.Pattern[str]:
    tag = TAGS[layer]
    return re.compile(rf"(<{tag}>)(.*?)(</{tag}>)", re.S)


def _message_text_slots(payload: dict) -> list[tuple[dict, str]]:
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return []
    slots = []
    for i, m in enumerate(msgs):
        if isinstance(m, dict) and isinstance(m.get("content"), str) and (
                m.get("role") == "system" or i == 0):
            slots.append((m, "content"))
    return slots


class PayloadPack:
    """A call site's payload with `trim_to` and an optional window upgrade.

    ``num_ctx`` and ``model`` start as the caller's values; after `trim_to`
    they are the values the site must pass to `check_overflow` (they change
    only when a bigger-window model was picked).
    """

    def __init__(self, payload: dict, *, num_ctx: int | None = None, base_url: str | None = None,
                 model: str | None = None, site: str = "unknown", allow_model_swap: bool = True,
                 reserved_output: int | None = None, candidates: list[str] | None = None):
        self.payload = payload
        self.num_ctx = num_ctx
        self.base_url = base_url
        self.model = model
        self.site = site
        self.allow_model_swap = allow_model_swap
        self.candidates = candidates
        self.reserved_output = reserved_output
        self.action = "off"  # off | fit | trimmed | bigger_window | overflow

    def trim_to(self, window: int | None = None) -> dict:
        """The payload, trimmed (and possibly re-pointed) to fit the window.

        `window` defaults to `local_context_guard.effective_window` for this
        call. Never raises; returns the original object when disabled or when
        the payload already fits.
        """
        if not enabled():
            return self.payload
        try:
            return self._trim(window)
        except Exception as exc:  # noqa: BLE001 — fail open: leave it to check_overflow
            from llm_router import failopen

            failopen.record("CHZ-FO-PACK-OVERFLOW", exc, detail=self.site)
            return self.payload

    def _trim(self, window: int | None) -> dict:
        from llm_router import local_context_guard as g

        reserved = g.DEFAULT_RESERVED_OUTPUT_TOKENS if self.reserved_output is None else self.reserved_output
        if window is None:
            window, _src = g.effective_window(num_ctx=self.num_ctx, base_url=self.base_url, model=self.model)
        budget = window - reserved
        before = g.estimate_payload_tokens(self.payload)
        if before <= budget:
            self.action = "fit"
            return self.payload

        payload = copy.deepcopy(self.payload)
        slots = _message_text_slots(payload)
        for layer in LAYERS:
            if g.estimate_payload_tokens(payload) <= budget:
                break
            pat = _tagged(layer)
            for msg, key in slots:
                def repl(m: re.Match[str], layer: str = layer) -> str:
                    new = _shrink(layer, m.group(2))
                    return f"{m.group(1)}{new}{m.group(3)}" if new else ""
                msg[key] = pat.sub(repl, msg[key])
        after = g.estimate_payload_tokens(payload)
        self.action = "trimmed" if after <= budget else "overflow"

        if after > budget and self.allow_model_swap and self.model:
            bigger = pick_larger_model(self.model, after + reserved, self.candidates)
            if bigger:
                from llm_router import local_models

                self.model = bigger
                self.num_ctx = local_models.num_ctx(bigger)
                payload["model"] = bigger
                payload.setdefault("options", {})["num_ctx"] = self.num_ctx
                self.action = "bigger_window"

        from llm_router import failopen

        failopen.record("CHZ-FO-PACK-OVERFLOW-TRIM",
                        detail=f"site={self.site} action={self.action} before={before} after={after} "
                               f"window={window} model={self.model}")
        return payload
