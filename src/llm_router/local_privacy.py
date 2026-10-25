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
import os
import threading
import urllib.request
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
    return (api_base or "").strip() or os.environ.get("OLLAMA_API_BASE", "").strip() or DEFAULT_OLLAMA_BASE


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


def contains_secret(*parts: Any) -> bool:
    """True when any text in ``parts`` (strings, or message lists) holds a secret shape (``engine.secret_names``).

    Fails closed: a scan that raises reads as "secret", so the call stays local or is refused."""
    try:
        return any(engine.secret_names(text) for part in parts for text in _texts(part))
    except Exception as exc:  # noqa: BLE001 - fail closed
        log.warning("secret scan failed (%s); treating the prompt as holding a secret", type(exc).__name__)
        return True


def stays_local(model: object, api_base: str | None = None) -> bool:
    """An Ollama model on a loopback daemon whose name is not an Ollama cloud model (``-cloud`` / ``:cloud``: the
    daemon forwards those to ollama.com)."""
    from llm_router.resolver.inventory import is_ollama_cloud_model
    return direct_ollama(model, api_base) and not is_ollama_cloud_model(model)


def local_only_chain(models: list[str]) -> list[str]:
    """The entries of ``models`` that keep the text on this machine (:func:`stays_local`). A non-loopback
    ``OLLAMA_API_BASE`` gives an empty chain."""
    return [m for m in models if stays_local(m)]


def secret_refusal_reason() -> str:
    why = ("the Ollama endpoint is not loopback" if not is_loopback_base(ollama_api_base())
           else "no local model in the chain")
    return f"{engine.SECRET_REFUSAL} ({why})"


# ── LiteLLM's model-info lookup ──────────────────────────────────────────────

_info_attempted: set[tuple[str, str]] = set()
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
    per daemon and model per process (LiteLLM caches its own lookups the same way); a failed answer writes
    LiteLLM's own failure entry, so its lookup does not run through the proxy instead. Never raises."""
    if not direct_ollama(model, api_base):
        return
    try:
        import litellm
        base, bare = ollama_api_base(api_base), _bare(model)
        names = {bare.lower(), f"ollama/{bare}".lower(), f"ollama_chat/{bare}".lower()}
        with _info_lock:
            if (base, bare) in _info_attempted or any(k.lower() in names for k in litellm.model_cost):
                return
            _info_attempted.add((base, bare))
        from llm_router.local_check import runtimes
        ok, show = runtimes._fetch(base, "POST", "/api/show", {"name": bare}, timeout=MODEL_INFO_TIMEOUT_S)
        litellm.model_cost[f"ollama/{bare}"] = _model_info_entry(bare, show if ok else None)
        _info_written.add(f"ollama/{bare}")
        invalidate = getattr(litellm.utils, "_invalidate_model_cost_lowercase_map", None)
        if callable(invalidate):
            invalidate()
    except Exception as exc:  # noqa: BLE001 - LiteLLM then does its own lookup; counted, never raised
        from llm_router import failopen
        failopen.record("CHZ-FO-LOCAL-PRIVACY-MODEL-INFO", exc, detail=f"model={model}")
