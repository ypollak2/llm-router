"""Local models doing agentic work (Phase 3 of the Cursor-parity plan).

Used only by the per-call proxy (``llm_router.proxy``):

``capability``  may this proxy step be served by a local model, and by which
                path: the raw tool-call loop, the validated edit protocol
                (``llm_router.edit``), or Claude? Every decision carries a
                reason that lands in the proxy ledger. One rule holds even with
                this package switched off: an Edit/Write/MultiEdit/NotebookEdit
                reply from the raw tool-call loop is never served (0/20 edits
                passed through that loop, 2026-09-28).
``compact``     tool retrieval + history compaction, so a local step sees a
                few relevant tools and <= ~5k prompt tokens instead of the
                full ~15-30k-token Claude Code request.
``proxy_step``  the glue the proxy server calls.

Capability gating and compaction are OFF unless ``LLM_ROUTER_LOCAL_AGENT=on``
(or ``llm-router proxy --local-agent``): local full-context serving measured
0/46 steps served on the 2026-09-28 realistic A/B, and stays off by default
until a paired A/B says otherwise.

Every environment variable of this package is read here, once, so
``env_registry`` has one file to point at.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ENV_ENABLED = "LLM_ROUTER_LOCAL_AGENT"

DEFAULT_TOP_K = 6
DEFAULT_MAX_PROMPT_TOKENS = 5000
DEFAULT_KEEP_RESULTS = 3
DEFAULT_TOOL_DESC_CHARS = 600
DEFAULT_EMBED_MODEL = "nomic-embed-text"
EDIT_MODES = ("protocol", "claude")


def _int(raw: str | None, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(raw if raw not in (None, "") else default))
    except (TypeError, ValueError):
        return default


def enabled_from_env() -> bool:
    return os.environ.get("LLM_ROUTER_LOCAL_AGENT", "").strip().lower() in ("1", "on", "true", "yes")


@dataclass(frozen=True)
class LocalAgentConfig:
    """Knobs for capability + compaction. ``from_env`` is the only reader."""

    top_k: int = DEFAULT_TOP_K
    max_prompt_tokens: int = DEFAULT_MAX_PROMPT_TOKENS
    keep_results: int = DEFAULT_KEEP_RESULTS
    tool_desc_chars: int = DEFAULT_TOOL_DESC_CHARS
    embed_model: str = DEFAULT_EMBED_MODEL
    # "protocol": an edit-shaped step goes through edit.py's validated
    # protocol (19/20 on fixtures); "claude": it always goes back to Claude.
    edit_mode: str = "protocol"

    @classmethod
    def from_env(cls) -> "LocalAgentConfig":
        mode = os.environ.get("LLM_ROUTER_LOCAL_AGENT_EDIT", "protocol").strip().lower()
        return cls(
            top_k=_int(os.environ.get("LLM_ROUTER_LOCAL_AGENT_TOP_K"), DEFAULT_TOP_K, 1),
            max_prompt_tokens=_int(os.environ.get("LLM_ROUTER_LOCAL_AGENT_PROMPT_BUDGET"),
                                   DEFAULT_MAX_PROMPT_TOKENS, 500),
            keep_results=_int(os.environ.get("LLM_ROUTER_LOCAL_AGENT_KEEP_RESULTS"), DEFAULT_KEEP_RESULTS, 1),
            tool_desc_chars=_int(os.environ.get("LLM_ROUTER_LOCAL_AGENT_TOOL_DESC_CHARS"),
                                 DEFAULT_TOOL_DESC_CHARS, 80),
            embed_model=os.environ.get("LLM_ROUTER_LOCAL_AGENT_EMBED_MODEL", "").strip() or DEFAULT_EMBED_MODEL,
            edit_mode=mode if mode in EDIT_MODES else "protocol",
        )
