"""``web_search`` MCP tool: SearXNG results as plain text for the local model.

Off by default. ``LLM_ROUTER_WEB_SEARCH=on`` registers it, with
``_meta["anthropic/alwaysLoad"] = true`` so Claude Code puts the schema in every
request instead of deferring it behind ToolSearch (a deferred tool the model never
loaded fails with "No such tool available"). It is registered outside the slim-tier
gate: the default tier ("consolidated") would otherwise hide it.

Logging: query LENGTH only, never the text.
"""

from __future__ import annotations

import os

from llm_router.logging import get_logger
from llm_router.search import backend
from llm_router.token_budget import estimate_tokens

log = get_logger("llm_router.web_search")

MAX_EST_TOKENS = 2000
_MAX_CHARS = MAX_EST_TOKENS * 4  # estimate_tokens is chars // 4
_SNIPPET_CHARS = 400
_TITLE_CHARS = 200
_URL_CHARS = 500
ALWAYS_LOAD_META = {"anthropic/alwaysLoad": True}


def enabled() -> bool:
    return os.environ.get("LLM_ROUTER_WEB_SEARCH", "").strip().lower() in {"on", "1", "true", "yes"}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def format_hits(hits: list[backend.Hit]) -> str:
    """Numbered title / URL / snippet blocks, cut so the text stays <= MAX_EST_TOKENS.

    Title, URL and snippet are each clipped, so the first hit always fits.
    """
    if not hits:
        return "0 results"
    out: list[str] = []
    used = 0
    for i, h in enumerate(hits, 1):
        block = (f"{i}. {_clip(h.title, _TITLE_CHARS)}\n   {_clip(h.url, _URL_CHARS)}\n"
                 f"   {_clip(h.snippet, _SNIPPET_CHARS)}").rstrip()
        if out and used + len(block) + 2 > _MAX_CHARS:
            out.append(f"[truncated: {len(hits) - i + 1} more]")
            break
        out.append(block)
        used += len(block) + 2
    return "\n\n".join(out)


async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web (SearXNG). Returns title, URL and snippet per hit as plain text.

    Use for current facts or documentation the model does not know.

    Args:
        query: What to search for.
        max_results: Number of hits to return (1-10, default 5).
    """
    if not isinstance(query, str) or not query.strip():
        return "web_search failed: try=1 kind=empty_query"
    log.info("web_search", query_len=len(query), max_results=max_results)
    try:
        hits = await backend.search(query.strip(), max_results)
    except backend.SearchError as exc:
        log.info("web_search_failed", kind=exc.kind)
        return f"web_search failed: try=1 kind={exc.kind}"
    text = format_hits(hits)
    log.info("web_search_done", hits=len(hits), est_tokens=estimate_tokens(text))
    return text


def register(mcp) -> None:
    """Register ``web_search`` when ``LLM_ROUTER_WEB_SEARCH`` is on; otherwise nothing."""
    if enabled():
        mcp.tool(meta=ALWAYS_LOAD_META)(web_search)
