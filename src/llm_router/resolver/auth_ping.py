"""Zero-cost credential check for API-key providers (``inventory --verify``).

One authenticated GET to the provider's own list-models (or key-info) endpoint:
it sends no completion and uses no tokens. HTTP 200 means the key is accepted,
401/403 means it is rejected, anything else (or no response) means "could not
verify", which is NOT a failure of the key.

Safety rules, each pinned by a test:

* The key is sent only to the endpoint for ITS provider in :data:`ENDPOINTS`,
  over https. The table is the whole list of hosts a key can reach, which is why
  NO redirect is ever followed (urllib would forward the Authorization header to
  the new host, even a different one) and why proxies are bypassed (HTTP(S)_PROXY
  would otherwise see the connection).
* The key lives in a header, never in the URL, and is never logged, returned,
  cached or placed in an exception message. The ping returns an int status only.
* The cache stores a provider, a 12-hex-char fingerprint of provider+key (to notice
  a changed key, useless for recovering one), a timestamp and a verdict.
* Providers without a zero-cost endpoint (perplexity) are not pinged.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from llm_router import failopen, paths

PING_TIMEOUT_S = 5.0
DEFAULT_TTL_S = 600.0

#: provider -> (https URL, header style). Official endpoints only.
#:
#: Verified 2026-10-04 by an UNAUTHENTICATED GET to each URL (no key sent): every
#: one answers 401 (openai, anthropic, openrouter, deepseek, groq, xai, mistral,
#: together, moonshot, cohere) or 403 (gemini) with no redirect, i.e. the endpoint
#: exists and demands a credential. That proves the URL is live, not that a valid key
#: returns 200; the 200 path was exercised only against fakes. Host/path choices that
#: the re-review of #259 corrected against the providers' current docs:
#: together = api.together.ai (api.together.xyz also answers), openrouter =
#: /api/v1/key (the older /auth/key also answers). Moonshot: api.moonshot.ai is the
#: international platform; a key issued on the China platform (.cn) is rejected here
#: and reads as unverified. tests/resolver pins this table.
ENDPOINTS: dict[str, tuple[str, str]] = {
    "openai": ("https://api.openai.com/v1/models", "bearer"),
    "anthropic": ("https://api.anthropic.com/v1/models", "anthropic"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1", "google"),
    "openrouter": ("https://openrouter.ai/api/v1/key", "bearer"),
    "deepseek": ("https://api.deepseek.com/models", "bearer"),
    "groq": ("https://api.groq.com/openai/v1/models", "bearer"),
    "xai": ("https://api.x.ai/v1/models", "bearer"),
    "mistral": ("https://api.mistral.ai/v1/models", "bearer"),
    "together": ("https://api.together.ai/v1/models", "bearer"),
    "moonshot": ("https://api.moonshot.ai/v1/models", "bearer"),
    "cohere": ("https://api.cohere.com/v1/models", "bearer"),
}

VERIFIED = "verified"
REJECTED = "rejected"
UNREACHABLE = "unreachable"
UNEXPECTED = "unexpected"
NO_CHECK = "no_check"


def _headers(style: str, key: str) -> dict[str, str]:
    if style == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    if style == "google":
        return {"x-goog-api-key": key}
    return {"Authorization": f"Bearer {key}"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the default handler re-sends every header, the key
    included, to wherever the Location points. Returning None makes the 3xx surface
    as an HTTPError, which :func:`http_ping` reports as its status code."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _opener() -> urllib.request.OpenerDirector:
    # ProxyHandler({}) ignores HTTP(S)_PROXY, so no proxy sees even connection metadata.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _ping_url(url: str, style: str, key: str) -> int | None:
    """One GET with the key in a header; the HTTP status, or None for no response."""
    req = urllib.request.Request(url, headers=_headers(style, key), method="GET")
    try:
        with _opener().open(req, timeout=PING_TIMEOUT_S) as resp:  # noqa: S310 - caller passes a fixed https URL
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:  # noqa: BLE001 - network failure is "unreachable"; never echo the exception
        return None


def http_ping(provider: str, key: str) -> int | None:
    """HTTP status of the provider's list-models call, or None when there was no
    response. Returns nothing else, so the key cannot leak through the result."""
    entry = ENDPOINTS.get(provider)
    if entry is None:
        return None
    url, style = entry
    if not url.startswith("https://"):
        return None
    return _ping_url(url, style, key)


def _fingerprint(provider: str, key: str) -> str:
    return hashlib.sha256(f"{provider}\0{key}".encode()).hexdigest()[:12]


def cache_path() -> Path:
    return paths.state_path("auth_ping_cache.json")


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _store(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)       # so the 0600 creation mode below always applies
        with open(tmp, "w", encoding="utf-8", opener=paths.private_opener) as fh:
            fh.write(json.dumps(data))
        tmp.replace(path)
    except OSError as exc:
        failopen.record("CHZ-FO-AUTH-PING-CACHE", exc)


def verify_provider(
    provider: str,
    key: str,
    *,
    ping: Callable[[str, str], int | None],
    now: float,
    cache: Path | None = None,
    ttl_s: float = DEFAULT_TTL_S,
) -> tuple[str, int | None]:
    """(verdict, http_status). Only verified/rejected verdicts are cached."""
    if provider not in ENDPOINTS:
        return NO_CHECK, None
    path = cache or cache_path()
    fp = _fingerprint(provider, key)
    data = _load(path)
    hit = data.get(provider)
    if (isinstance(hit, dict) and hit.get("fp") == fp
            and isinstance(hit.get("ts"), (int, float)) and 0 <= now - hit["ts"] < ttl_s
            and hit.get("verdict") in (VERIFIED, REJECTED)):
        return hit["verdict"], hit.get("status")
    status = ping(provider, key)
    if status is None:
        return UNREACHABLE, None
    if status == 200:
        verdict = VERIFIED
    elif status in (401, 403):
        verdict = REJECTED
    else:
        return UNEXPECTED, status
    data[provider] = {"fp": fp, "ts": now, "verdict": verdict, "status": status}
    _store(path, data)
    return verdict, status
