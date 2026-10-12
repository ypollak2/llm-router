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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000", "google"),
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


# ------------------------------------------------------------ per-account lists
# P1.8 task 13. The same authenticated GET also carries the account's model ids; the
# ids are model names (not secrets) and are cached beside the verdict.

LIST_TTL_S = 6 * 3600.0          # at most one list refresh per provider per 6 h
MAX_IDS = 2000                   # ids kept per provider
MAX_BODY_BYTES = 1_000_000       # bytes read per response
MAX_PAGES = 20
MAX_ID_LEN = 200
_OPENAI_STYLE = ("openai", "deepseek", "groq", "xai", "mistral", "together", "moonshot")


@dataclass(frozen=True)
class ListResult:
    """What one provider list call returned. ``ids`` is None when the provider has
    no list in this call (or the body could not be read); ``complete`` is False when
    a cap or a failed later page cut the list short, in which case an id that is
    absent proves nothing."""

    status: int | None
    ids: tuple[str, ...] | None = None
    complete: bool = False


@dataclass(frozen=True)
class ListOutcome:
    verdict: str
    status: int | None
    ids: tuple[str, ...] | None = None
    complete: bool = False
    from_cache: bool = False


def parse_page(provider: str, body: Any) -> tuple[list[str], dict[str, str]] | None:
    """(ids, query params for the next page) from one decoded page, or None when the
    provider has no list or the shape is unknown. Ids are bare (no ``models/``)."""
    raw: list[Any]
    nxt: dict[str, str] = {}
    if provider in _OPENAI_STYLE:
        raw = body if isinstance(body, list) else (body.get("data") if isinstance(body, dict) else None)
        key = "id"
    elif provider == "anthropic" and isinstance(body, dict):
        raw, key = body.get("data"), "id"
        if body.get("has_more") is True and isinstance(body.get("last_id"), str):
            nxt = {"after_id": body["last_id"]}
    elif provider == "gemini" and isinstance(body, dict):
        raw, key = body.get("models"), "name"
        if isinstance(body.get("nextPageToken"), str) and body["nextPageToken"]:
            nxt = {"pageToken": body["nextPageToken"]}
    elif provider == "cohere" and isinstance(body, dict):
        raw, key = body.get("models"), "name"
        if isinstance(body.get("next_page_token"), str) and body["next_page_token"]:
            nxt = {"page_token": body["next_page_token"]}
    else:
        return None
    if not isinstance(raw, list):
        return None
    ids = []
    for item in raw:
        v = item.get(key) if isinstance(item, dict) else None
        if isinstance(v, str) and v.strip():
            ids.append(v.strip().removeprefix("models/")[:MAX_ID_LEN])
    return ids, nxt


def _get_json(url: str, style: str, key: str) -> tuple[int | None, Any, bool]:
    """(status, decoded body or None, body_ok). Reads at most MAX_BODY_BYTES + 1."""
    req = urllib.request.Request(url, headers=_headers(style, key), method="GET")
    try:
        with _opener().open(req, timeout=PING_TIMEOUT_S) as resp:  # noqa: S310 - fixed https URL
            status = int(resp.status)
            data = resp.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), None, False
    except Exception:  # noqa: BLE001 - never echo the exception
        return None, None, False
    if len(data) > MAX_BODY_BYTES:
        return status, None, False
    try:
        return status, json.loads(data.decode("utf-8")), True
    except ValueError:
        return status, None, False


def http_list(provider: str, key: str) -> ListResult:
    """GET the provider's list-models endpoint, following its pagination, within
    the caps. The key goes only in a header to the host in :data:`ENDPOINTS`."""
    entry = ENDPOINTS.get(provider)
    if entry is None or not entry[0].startswith("https://"):
        return ListResult(None)
    base, style = entry
    ids: list[str] = []
    params: dict[str, str] = {}
    first_status: int | None = None
    complete = False
    for page in range(MAX_PAGES):
        url = base
        if params:
            sep = "&" if "?" in base else "?"
            url = base + sep + urllib.parse.urlencode(params)
        status, body, ok = _get_json(url, style, key)
        if page == 0:
            first_status = status
            if status != 200:
                return ListResult(status)
        if not ok:
            break
        parsed = parse_page(provider, body)
        if parsed is None:
            return ListResult(first_status) if page == 0 else ListResult(first_status, tuple(ids), False)
        got, params = parsed
        for i in got:
            if len(ids) >= MAX_IDS:
                return ListResult(first_status, tuple(ids), False)
            ids.append(i)
        if not params:
            complete = True
            break
    if page == 0 and not ids and not complete:
        return ListResult(first_status)
    return ListResult(first_status, tuple(dict.fromkeys(ids)), complete)


def list_provider(
    provider: str,
    key: str,
    *,
    fetch: Callable[[str, str], ListResult] | None,
    now: float,
    cache: Path | None = None,
    ttl_s: float = LIST_TTL_S,
) -> ListOutcome:
    """Verdict plus the account's model ids, at most one ``fetch`` per provider per
    ``ttl_s`` unless the key changed (fingerprint). ``fetch=None`` reads the cache only
    (no network). Only verified/rejected outcomes are cached; a transport error or an
    unexpected status returns no ids, so it can never read as "not listed"."""
    if provider not in ENDPOINTS:
        return ListOutcome(NO_CHECK, None)
    path = cache or cache_path()
    fp = _fingerprint(provider, key)
    data = _load(path)
    hit = data.get(provider + ":list")
    if (isinstance(hit, dict) and hit.get("fp") == fp and isinstance(hit.get("ts"), (int, float))
            and hit.get("verdict") in (VERIFIED, REJECTED)):
        fresh = 0 <= now - hit["ts"] < ttl_s
        if fresh or fetch is None:
            ids = hit.get("ids")
            ids = tuple(i for i in ids if isinstance(i, str)) if isinstance(ids, list) else None
            return ListOutcome(hit["verdict"], hit.get("status"), ids,
                               bool(hit.get("complete")), True)
    if fetch is None:
        return ListOutcome(UNREACHABLE, None)
    res = fetch(provider, key)
    if res.status is None:
        return ListOutcome(UNREACHABLE, None)
    if res.status == 200:
        verdict = VERIFIED
    elif res.status in (401, 403):
        verdict = REJECTED
    else:
        return ListOutcome(UNEXPECTED, res.status)
    ids = res.ids if verdict == VERIFIED else None
    complete = bool(res.complete) if ids is not None else False
    data[provider + ":list"] = {"fp": fp, "ts": now, "verdict": verdict, "status": res.status,
                                "ids": list(ids) if ids is not None else None, "complete": complete}
    _store(path, data)
    return ListOutcome(verdict, res.status, ids, complete)
