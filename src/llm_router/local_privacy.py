"""LC-12h: local Ollama calls go straight to the loopback daemon, and a prompt with a secret stays local.

Two facts measured offline (tests/test_lc12g.py, tests/test_lc12h.py):

* LiteLLM 1.103.0 sends a loopback Ollama request through ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY``
  when ``NO_PROXY`` is unset, on both its aiohttp and httpx transports; ``urllib.request.urlopen`` (the
  semantic cache's embeddings, the ``/api/tags`` probe) does the same. A proxy is the user's environment, but
  "localhost" is not a request the user meant to send anywhere else.
* LiteLLM's model-info lookup POSTs ``/api/show`` (model name, URL, ``Authorization: Bearer $OLLAMA_API_KEY``
  when set) with its module-level httpx client, so through the proxy too, several times per call.

So, for an ``ollama/`` or ``ollama_chat/`` model whose daemon (``OLLAMA_API_BASE``, else localhost:11434) is
loopback: the call gets its own client that ignores proxy env and never follows a redirect
(:func:`direct_handler`), our urllib requests use an opener with no proxy handler (:func:`open_url`), and
LiteLLM's model map gets the model's entry from our own direct ``/api/show`` (:func:`ensure_ollama_model_info`),
so LiteLLM does not look it up. A non-loopback Ollama base (a LAN GPU box, say) is left exactly as it was:
proxy policy there is the user's environment, NO_PROXY included.

The secret door (D-28 A): a prompt in which ``engine.secret_names`` finds a secret goes only to loopback, non-cloud
local models (:func:`local_only_chain`), or the call is refused (:class:`SecretLocalOnlyRefused`). For that
dispatch the router sets :func:`set_secret_dispatch`; :func:`private_dispatch_active` (that marker, or the
pass-only text-job marker) is what the semantic cache, the judge queue, the embedding backends and the providers
read, so the private paths share one predicate.
"""
from __future__ import annotations

import contextvars
import logging
import collections
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

from llm_router import engine

log = logging.getLogger("llm_router.local_privacy")

OLLAMA_PREFIXES = ("ollama/", "ollama_chat/")
DEFAULT_OLLAMA_BASE = "http://localhost:11434"
MODEL_INFO_TIMEOUT_S = 2.0


def is_ollama_model(model: object) -> bool:
    return isinstance(model, str) and model.startswith(OLLAMA_PREFIXES)


def ollama_api_base(api_base: str | None = None) -> str:
    """The daemon LiteLLM sends an Ollama call to: ``api_base``, else ``OLLAMA_API_BASE``, else localhost:11434."""
    raw = (api_base or "").strip() or os.environ.get("OLLAMA_API_BASE", "").strip() or DEFAULT_OLLAMA_BASE
    return _strip_endpoint(raw)


_ENDPOINT_SUFFIX = re.compile(r"/api(?:/(?:generate|chat|show|tags|embed|embeddings|pull|ps))?/*$|/+$")


def _strip_endpoint(url: str) -> str:
    """The daemon's base: LiteLLM's ``get_server_api_base`` drops a trailing ``/api/generate``-style endpoint (and
    slashes), so an ``OLLAMA_API_BASE`` of ``http://127.0.0.1:11434/api/generate`` is the daemon at 127.0.0.1:11434."""
    return _ENDPOINT_SUFFIX.sub("", url)


def is_loopback_base(url: str | None) -> bool:
    if not url:
        return False
    try:
        from llm_router.local_check import runtimes
        return runtimes.is_loopback(url)
    except Exception:  # noqa: BLE001 - a URL we cannot read is not loopback
        return False


def direct_ollama(model: object, api_base: str | None = None) -> bool:
    """True when ``model`` is an Ollama model served by a loopback daemon: its call goes direct."""
    return is_ollama_model(model) and is_loopback_base(ollama_api_base(api_base))


async def direct_handler(timeout):
    """A LiteLLM handler for ONE call whose httpx client ignores proxy env and never follows a redirect.

    LiteLLM's shared client honours HTTP(S)_PROXY / ALL_PROXY for ``localhost`` too when NO_PROXY is unset
    (both its aiohttp and httpx transports), so the text could reach a proxy. Returns (handler, client to close).
    """
    import httpx
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
    handler = AsyncHTTPHandler(timeout=timeout)
    await handler.close()  # the default client it built (env proxies honoured) is never used
    handler.client = direct = httpx.AsyncClient(trust_env=False, timeout=timeout, follow_redirects=False)
    return handler, direct


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib's signature
        return None  # a loopback daemon that redirects is not followed elsewhere


def direct_opener() -> urllib.request.OpenerDirector:
    """A urllib opener with no proxy handler and no redirects."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def open_url(req: Any, *, base_url: str, timeout: float):
    """``urlopen`` for a request to ``base_url``: direct (no proxy, no redirect) when it is loopback; a
    non-loopback base keeps ``urllib.request.urlopen`` and so the user's proxy env and NO_PROXY."""
    if is_loopback_base(base_url):
        return direct_opener().open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


# ── the private-dispatch marker ──────────────────────────────────────────────

#: Set by the router for the dispatch of a call the secret door made local-only. Sibling of
#: ``eligibility_routing._pass_only_dispatch`` (the pass-only text job), read through :func:`private_dispatch_active`.
_secret_dispatch: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "llm_router_secret_dispatch", default=False)


def set_secret_dispatch(on: bool) -> contextvars.Token:
    return _secret_dispatch.set(bool(on))


def reset_secret_dispatch(token: contextvars.Token) -> None:
    _secret_dispatch.reset(token)


def secret_dispatch_active() -> bool:
    return _secret_dispatch.get()


def private_dispatch_active() -> bool:
    """True for a dispatch whose text must stay on this machine: the secret door or a pass-only text job."""
    from llm_router import eligibility_routing
    return _secret_dispatch.get() or eligibility_routing.pass_only_dispatch_active()


# ── the secret door ──────────────────────────────────────────────────────────

class SecretLocalOnlyRefused(engine.SecretLocalOnlyError):
    """A prompt with a secret, and no loopback local model to send it to: not sent anywhere."""


class PrivateEndpointRefused(RuntimeError):
    """A private (local-only) call reached a non-loopback Ollama endpoint; refused before any request."""


def _texts(part: Any):
    if isinstance(part, str):
        yield part
    elif isinstance(part, list):
        for msg in part:
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str):
                yield content
            elif isinstance(content, list):
                for piece in content:
                    text = piece.get("text") if isinstance(piece, dict) else None
                    if isinstance(text, str):
                        yield text


@dataclass(frozen=True)
class SecretHit:
    """One high-precision secret shape found in a request: its category and where (never the value)."""
    label: str
    where: str


#: Counts by category only, never a value: ``refused:<label>``, ``overridden:<label>``, ``logged:<label>`` (a shape
#: too weak to refuse on: JWTs, quoted literals, placeholders). Per process; ``llm-router`` log lines carry the same.
secret_counts: collections.Counter = collections.Counter()
_counts_lock = threading.Lock()


def count_secret_event(kind: str, label: str) -> None:
    with _counts_lock:
        secret_counts[f"{kind}:{label}"] += 1
    log.info("secret door: %s %s", kind, label)


def _scan_text(text: str, where: str, hits: list[SecretHit], seen: set[tuple[str, str]]) -> None:
    for name, high in engine.secret_findings(text).items():
        label = engine.SECRET_LABELS.get(name, name)
        if high:
            if (label, where) not in seen:
                seen.add((label, where))
                hits.append(SecretHit(label, where))
        else:
            count_secret_event("logged", label)


def scan_secrets(prompt: Any = None, system_prompt: Any = None, caller_context: Any = None,
                 messages: Any = None) -> list[SecretHit]:
    """The high-precision secrets in a request, each with the category and where it was found: ``current prompt``,
    ``system prompt``, ``caller context`` or ``earlier turn N`` (N counts ``messages`` from 1). Placeholders and the
    log-only shapes are counted (:data:`secret_counts`) and not returned. Fails closed: a scan that raises is one
    ``unscannable text`` hit, so the call stays local or is refused."""
    hits: list[SecretHit] = []
    seen: set[tuple[str, str]] = set()
    try:
        for part, where in ((prompt, "current prompt"), (system_prompt, "system prompt"),
                            (caller_context, "caller context")):
            for text in _texts(part):
                _scan_text(text, where, hits, seen)
        if isinstance(messages, list):
            for n, msg in enumerate(messages, 1):
                for text in _texts([msg]):
                    _scan_text(text, f"earlier turn {n}", hits, seen)
    except Exception as exc:  # noqa: BLE001 - fail closed
        log.warning("secret scan failed (%s); treating the request as holding a secret", type(exc).__name__)
        return hits + [SecretHit("unscannable text", "the request")]
    return hits


def contains_secret(*parts: Any) -> bool:
    """True when any text in ``parts`` (strings, or message lists) holds a high-precision secret."""
    return bool(scan_secrets(messages=[{"content": t} for part in parts for t in _texts(part)]))


#: The per-request override: a prompt that starts with ``cloud:`` is sent as ordinary cloud traffic even though it
#: holds a secret. Same family as the ``claude:`` / ``native:`` redo prefixes; stripped before anything reads the text.
_OVERRIDE_RE = re.compile(r"^\s*cloud\s*:\s*", re.IGNORECASE)
OVERRIDE_HINT = "start the prompt with `cloud:`"


def split_override(prompt: str | None) -> tuple[str | None, bool]:
    """``(prompt without the ``cloud:`` prefix, True)`` when the prompt carries the override, else ``(prompt, False)``."""
    if isinstance(prompt, str):
        m = _OVERRIDE_RE.match(prompt)
        if m:
            return prompt[m.end():], True
    return prompt, False


def refusal_message(hits: list[SecretHit], *, model_override: str | None = None, why: str | None = None) -> str:
    """The refusal text: categories and places, never a value, and the three ways forward."""
    found = "; ".join(f"{h.label} ({h.where})" for h in hits) or "secret-shaped text"
    history_only = bool(hits) and all(h.where.startswith("earlier turn") for h in hits)
    cause = (" The secret is only in the conversation history, not in your current prompt "
             f"({', '.join(sorted({h.where for h in hits}))})." if history_only else "")
    if why:
        pass
    elif model_override:
        why = f"you asked for model {model_override!r}, which is not a local model"
    elif not is_loopback_base(ollama_api_base()):
        why = "the configured Ollama endpoint is not loopback"
    else:
        why = "no local model is available for it"
    return (f"Refused: this request contains what looks like a secret: {found}.{cause} Nothing was sent to a cloud "
            f"model ({why}). To proceed: install or start a local model (`llm-router local-check`), remove the "
            f"secret, or override for this request ({OVERRIDE_HINT}; the override is logged as a count only).")


def stays_local(model: object, api_base: str | None = None) -> bool:
    """An Ollama model on a loopback daemon whose name is not an Ollama cloud model (``-cloud`` / ``:cloud``: the
    daemon forwards those to Ollama's cloud)."""
    from llm_router.resolver.inventory import is_ollama_cloud_model
    return direct_ollama(model, api_base) and not is_ollama_cloud_model(model)


def local_only_chain(models: list[str]) -> list[str]:
    """The entries of ``models`` that keep the text on this machine (:func:`stays_local`). A non-loopback
    ``OLLAMA_API_BASE`` gives an empty chain."""
    return [m for m in models if stays_local(m)]


# ── LiteLLM's model-info lookup ──────────────────────────────────────────────

#: (daemon, bare model) -> monotonic time of our last ``/api/show`` fetch, and whether it answered.
_info_attempted: dict[tuple[str, str], tuple[float, bool]] = {}
#: A failed, 404 or slow answer is retried after this long (LiteLLM retried every call); a good answer is
#: re-fetched after the longer one, so an ``ollama cp`` / re-pull is seen without a restart.
INFO_RETRY_FAILED_S = 10.0
INFO_REFRESH_OK_S = 300.0
#: The ``litellm.model_cost`` keys this module wrote (tests remove them, conftest ``_reset_local_privacy``).
_info_written: set[str] = set()
_info_lock = threading.Lock()


def _bare(model: str) -> str:
    for prefix in OLLAMA_PREFIXES:
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def _model_info_entry(bare: str, show: Any) -> dict[str, Any]:
    """The dict LiteLLM's ``OllamaModelInfo.get_runtime_model_info`` builds from an ``/api/show`` answer (its
    failure dict when there is none)."""
    entry: dict[str, Any] = {"key": bare, "litellm_provider": "ollama", "mode": "chat",
                             "input_cost_per_token": 0.0, "output_cost_per_token": 0.0}
    if not isinstance(show, dict):
        return {**entry, "max_tokens": None, "max_input_tokens": None, "max_output_tokens": None}
    info = show.get("model_info") if isinstance(show.get("model_info"), dict) else {}
    window = next((v for k, v in info.items() if "context_length" in k), None)
    return {**entry, "supports_function_calling": "tools" in str(show.get("template", "") or "").lower(),
            "max_tokens": window, "max_input_tokens": window, "max_output_tokens": window}


def ensure_ollama_model_info(model: str, api_base: str | None = None) -> None:
    """Give LiteLLM's model map the entry for a loopback Ollama ``model`` from our own ``/api/show``.

    LiteLLM looks a non-catalogued Ollama model up with ``POST /api/show`` on its module-level httpx client, which
    honours proxy env and sends ``OLLAMA_API_KEY``; an entry in ``litellm.model_cost`` (its public model map) makes
    it skip that lookup. Ours goes by ``http.client`` to the loopback daemon only, with no proxy and no key. Once
    per daemon and model while the last answer is fresh (:data:`INFO_REFRESH_OK_S`); a failed answer writes
    LiteLLM's own failure entry, so its lookup does not run through the proxy instead, and is retried after
    :data:`INFO_RETRY_FAILED_S`. An entry LiteLLM's catalogue already has is left alone. Never raises."""
    if not direct_ollama(model, api_base):
        return
    try:
        import litellm
        base, bare = ollama_api_base(api_base), _bare(model)
        names = {bare.lower(), f"ollama/{bare}".lower(), f"ollama_chat/{bare}".lower()}
        key, now = (base, bare), time.monotonic()
        with _info_lock:
            last = _info_attempted.get(key)
            if last is not None and now - last[0] < (INFO_REFRESH_OK_S if last[1] else INFO_RETRY_FAILED_S):
                return
            if last is None and any(k.lower() in names for k in list(litellm.model_cost)):
                return  # LiteLLM's own catalogue knows this model
            _info_attempted[key] = (now, False)  # claimed: concurrent callers skip while this one fetches
        from llm_router.local_check import runtimes
        ok, show = runtimes._fetch(base, "POST", "/api/show", {"name": bare}, timeout=MODEL_INFO_TIMEOUT_S)
        ok = bool(ok) and isinstance(show, dict)
        with _info_lock:
            _info_attempted[key] = (time.monotonic(), ok)
        litellm.model_cost[f"ollama/{bare}"] = _model_info_entry(bare, show if ok else None)
        _info_written.add(f"ollama/{bare}")
        invalidate = getattr(litellm.utils, "_invalidate_model_cost_lowercase_map", None)
        if callable(invalidate):
            invalidate()
    except Exception as exc:  # noqa: BLE001 - LiteLLM then does its own lookup; counted, never raised
        from llm_router import failopen
        failopen.record("CHZ-FO-LOCAL-PRIVACY-MODEL-INFO", exc, detail=f"model={model}")
