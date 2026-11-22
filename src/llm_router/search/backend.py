"""SearXNG adapter: ``search(query, n) -> list[Hit]``.

One GET to ``<base>/search?format=json``, 10 s timeout, one attempt (no retries:
a slow search should fail fast so the model can answer without it). Every failure
is raised as ``SearchError`` carrying a short, content-free ``kind`` such as
``timeout``, ``connect``, ``http_503`` or ``bad_json``; the query text and any
response body never appear in the error.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import httpx

DEFAULT_URL = "http://127.0.0.1:8888"
TIMEOUT_S = 10.0
MAX_RESULTS_CEILING = 10
MAX_BODY_BYTES = 1 << 20  # 1 MiB, checked before json parsing


class SearchError(Exception):
    """A search failure. ``kind`` is short and contains no query or response text."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class Hit:
    title: str
    url: str
    snippet: str


def base_url() -> str:
    return (os.environ.get("LLM_ROUTER_SEARXNG_URL") or DEFAULT_URL).strip().rstrip("/")


async def search(query: str, n: int = 5, *, client: httpx.AsyncClient | None = None) -> list[Hit]:
    """Return up to ``n`` hits (1..10). Raises ``SearchError``."""
    n = max(1, min(int(n), MAX_RESULTS_CEILING))
    own = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT_S)
    try:
        try:
            async with asyncio.timeout(TIMEOUT_S):  # total deadline; httpx's is per phase
                resp = await http.get(f"{base_url()}/search", params={"q": query, "format": "json"})
        except (httpx.TimeoutException, TimeoutError):
            raise SearchError("timeout") from None
        except httpx.ConnectError:
            raise SearchError("connect") from None
        except httpx.HTTPError:
            raise SearchError("transport") from None
    finally:
        if own:
            await http.aclose()
    if resp.status_code != 200:
        raise SearchError(f"http_{resp.status_code}")
    if len(resp.content) > MAX_BODY_BYTES:
        raise SearchError("too_large")
    try:
        results = resp.json()["results"]
        if not isinstance(results, list):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise SearchError("bad_json") from None
    hits: list[Hit] = []
    for r in results:
        if not isinstance(r, dict) or not r.get("url"):
            continue
        hits.append(Hit(str(r.get("title") or "").strip(),
                        str(r["url"]).strip(),
                        " ".join(str(r.get("content") or "").split())))
        if len(hits) >= n:
            break
    return hits
