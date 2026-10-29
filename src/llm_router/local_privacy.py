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
so LiteLLM does not look it up. That is guaranteed on every call, not once: the entry is written (failure-shaped) before
the fetch, concurrent callers wait for the fetch, and a missing entry is re-inserted. Residuals: an insert that
raises (failopen-counted) leaves LiteLLM its own lookup; a filename/config example label (``.env.example``) near a
value exempts nothing (SECDET: the text around a token is not the token's own placeholder look); earlier turns are not
scanned (counted: ``history_secret_shape``), and assistant ``tool_calls`` arguments are never read. A non-loopback Ollama base (a LAN GPU box, say) is left exactly as it was:
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
        fetch = direct_opener().open  # a network read, not a file open (the W0-4 raw-write scan reads `X.open(a)` as a file mode)
        return fetch(req, timeout=timeout)
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
    """A prompt with a secret, and no loopback local model to send it to: not sent anywhere.

    ``hits`` are the :class:`SecretHit` (category and place, never the value); :meth:`body` is the typed error a
    gateway returns with its 422."""

    def __init__(self, message: str, hits: Any = ()) -> None:
        super().__init__(message)
        self.hits = tuple(hits)

    @property
    def categories(self) -> list[str]:
        return sorted({h.label for h in self.hits})

    def body(self) -> dict[str, Any]:
        return {"error": {"type": "secret_local_only_refused", "code": "secret_local_only_refused",
                          "message": str(self), "categories": self.categories,
                          "places": sorted({h.where for h in self.hits}), "remedy": OVERRIDE_HINT}}


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
#: too weak to refuse on: JWTs, quoted literals, placeholders), ``history_secret_shape:<label>`` (an earlier turn holds a secret shape). Per process; ``llm-router`` log lines carry the same.
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


def note_history_secrets(messages: Any) -> None:
    """Count, by category only, the earlier turns (``messages``) that hold a high-precision secret shape. History is
    NOT scanned for the door (each turn was scanned when it was new; one old secret must not refuse every later
    turn), so a secret sent in turn N is replayed to a cloud model in turn N+1 when the caller sends history. This
    counter (``history_secret_shape:<label>``, once per request and category, never a value) makes that
    visible. Never raises, never changes routing. Assistant ``tool_calls`` arguments are not read (SECURITY.md)."""
    try:
        if not isinstance(messages, list):
            return
        labels: set[str] = set()
        for text in _texts(messages):
            for name, high in engine.secret_findings(text).items():
                if high:
                    labels.add(engine.SECRET_LABELS.get(name, name))
        for label in sorted(labels):
            count_secret_event("history_secret_shape", label)
    except Exception as exc:  # noqa: BLE001 - a counter must not break a request
        log.debug("history secret count failed (%s)", type(exc).__name__)


def refusal(hits: list[SecretHit], **kw: Any) -> SecretLocalOnlyRefused:
    """The refusal for a request with a high-precision secret and no loopback local model (``kw``: see
    :func:`refusal_message`): the message and the hits, so a door can answer with the typed body."""
    return SecretLocalOnlyRefused(refusal_message(hits, **kw), hits)


def guard_cloud_cli(prompt: str, *, what: str) -> str:
    """The secret door for a door whose only destination is a cloud CLI (``llm_codex`` / ``llm_gemini``): there is
    no local model to fall back to, so a secret-bearing prompt is refused, exactly as ``route_and_call`` does when
    its local-only chain is empty. Returns the prompt to send: unchanged, or without the ``cloud:`` prefix when the
    caller overrode (logged as a count, never the value). ``what`` names the door in the refusal."""
    prompt, override = split_override(prompt)
    hits = scan_secrets(prompt)
    if not hits:
        return prompt
    if override_requested(prefix=override):
        for h in hits:
            count_secret_event("overridden", h.label)
        return prompt
    for h in hits:
        count_secret_event("refused", h.label)
    raise refusal(hits, why=f"{what} sends it to a cloud service")


def contains_secret(*parts: Any) -> bool:
    """True when any text in ``parts`` (strings, or message lists) holds a high-precision secret."""
    return bool(scan_secrets(messages=[{"content": t} for part in parts for t in _texts(part)]))


#: The per-request override: a prompt that starts with ``cloud:`` is sent as ordinary cloud traffic even though it
#: holds a secret. Same family as the ``claude:`` / ``native:`` redo prefixes; stripped before anything reads the text.
#: Doors with no prompt prefix to type use the flag: ``allow_cloud=True`` (``route_and_call``, the SDK), the body field
#: ``allow_cloud`` (``/route``) or the header ``X-LLM-Router-Allow-Cloud: 1`` (the gateway).
_OVERRIDE_RE = re.compile(r"^\s*cloud\s*:\s*", re.IGNORECASE)
OVERRIDE_HEADER = "X-LLM-Router-Allow-Cloud"
OVERRIDE_HINT = ("start the prompt with `cloud:` (gateway: header `X-LLM-Router-Allow-Cloud: 1`; "
                 "SDK / route_and_call: `allow_cloud=True`)")

#: Set for one gateway request by the middleware that read :data:`OVERRIDE_HEADER`.
_request_allow_cloud: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "llm_router_request_allow_cloud", default=False)


def set_request_allow_cloud(on: bool) -> contextvars.Token:
    return _request_allow_cloud.set(bool(on))


def reset_request_allow_cloud(token: contextvars.Token) -> None:
    _request_allow_cloud.reset(token)


def header_allows_cloud(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def override_requested(*, prefix: bool = False, flag: bool = False) -> bool:
    """True when this request carries the override: the ``cloud:`` prefix, the ``allow_cloud`` flag, or the
    gateway header (read into the request context)."""
    return bool(prefix or flag or _request_allow_cloud.get())


def _secret_present(*parts: Any) -> bool:
    """True when any text in ``parts`` holds a high-precision secret. No counts (the door's scan counts). A scan that
    raises counts as found, so the override still reaches the door's own fail-closed hit."""
    try:
        return any(any(engine.secret_findings(t).values()) for part in parts for t in _texts(part))
    except Exception:  # noqa: BLE001
        return True


def split_override(prompt: str | None, *other_parts: Any) -> tuple[str | None, bool]:
    """``(prompt without the ``cloud:`` prefix, True)`` when the prompt starts with ``cloud:`` AND the request holds a
    secret (in the rest of the prompt or in ``other_parts``: system prompt, caller context); otherwise
    ``(prompt, False)`` with the prompt untouched, so a prompt that merely starts with "cloud:" is not rewritten."""
    if isinstance(prompt, str):
        m = _OVERRIDE_RE.match(prompt)
        if m and _secret_present(prompt[m.end():], *other_parts):
            return prompt[m.end():], True
    return prompt, False


def refusal_message(hits: list[SecretHit], *, model_override: str | None = None, why: str | None = None) -> str:
    """The refusal text: categories and places, never a value, and the three ways forward."""
    found = "; ".join(f"{h.label} ({h.where})" for h in hits) or "secret-shaped text"
    if why:
        pass
    elif model_override:
        why = f"you asked for model {model_override!r}, which is not a local model"
    elif not is_loopback_base(ollama_api_base()):
        why = "the configured Ollama endpoint is not loopback"
    else:
        why = "no local model is available for it"
    return (f"Refused: this request contains what looks like a secret: {found}. Nothing was sent to a cloud "
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
#: A fetch in flight for (daemon, bare model): concurrent callers wait on its event instead of returning early.
_info_inflight: dict[tuple[str, str], threading.Event] = {}
#: The last entry we computed for (daemon, bare model), re-inserted if the model map loses it.
_info_entries: dict[tuple[str, str], dict[str, Any]] = {}


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


def _install_model_info(litellm: Any, bare: str, entry: dict[str, Any]) -> None:
    """Put ``entry`` in LiteLLM's model map under ``ollama/<bare>`` and ``ollama_chat/<bare>`` (LiteLLM maps a model by
    its provider, so an ``ollama_chat/`` model needs its own key and provider) and refresh its lower-case index."""
    litellm.model_cost[f"ollama/{bare}"] = entry
    litellm.model_cost[f"ollama_chat/{bare}"] = {**entry, "litellm_provider": "ollama_chat"}
    _info_written.update((f"ollama/{bare}", f"ollama_chat/{bare}"))
    invalidate = getattr(litellm.utils, "_invalidate_model_cost_lowercase_map", None)
    if callable(invalidate):
        invalidate()


def ensure_ollama_model_info(model: str, api_base: str | None = None) -> None:
    """Give LiteLLM's model map the entry for a loopback Ollama ``model`` from our own ``/api/show``, and make it
    impossible for LiteLLM to run its own lookup.

    LiteLLM looks a non-catalogued Ollama model up with ``POST /api/show`` on its module-level httpx client, which
    honours proxy env and sends ``OLLAMA_API_KEY``; an entry in ``litellm.model_cost`` (its public model map) makes
    it skip that lookup. Ours goes by ``http.client`` to the loopback daemon only, with no proxy and no key.

    On every call, before it returns, the entry is in ``model_cost``: (1) a fetch in flight for the same daemon and
    model is waited for (at most the fetch timeout plus a second), (2) LiteLLM's failure-shaped entry is written
    BEFORE the fetch and overwritten by the real one on success, (3) the entry is re-inserted when it is gone
    (a cost-map reload) even inside the freshness window, (4) the failopen path leaves an entry too. A good answer is
    re-fetched after :data:`INFO_REFRESH_OK_S`, a failed one after :data:`INFO_RETRY_FAILED_S`. An entry LiteLLM's
    own catalogue already has is left alone. Never raises; if even the insert fails, failopen counts it and LiteLLM
    does its own lookup (the one residual, see SECURITY.md)."""
    if not direct_ollama(model, api_base):
        return
    mine: threading.Event | None = None
    key: tuple[str, str] | None = None
    try:
        import litellm
        base, bare = ollama_api_base(api_base), _bare(model)
        prefix = "ollama_chat/" if model.startswith("ollama_chat/") else "ollama/"
        names = {bare.lower(), f"{prefix}{bare}".lower()}  # the key LiteLLM maps THIS model by
        key = (base, bare)
        wait_for: threading.Event | None = None
        with _info_lock:
            now = time.monotonic()
            last = _info_attempted.get(key)
            present = any(k.lower() in names for k in list(litellm.model_cost))
            inflight = _info_inflight.get(key)
            if inflight is not None:
                wait_for = inflight
            elif last is None and present:
                return  # LiteLLM's own catalogue knows this model
            elif last is not None and now - last[0] < (INFO_REFRESH_OK_S if last[1] else INFO_RETRY_FAILED_S):
                if not present:  # fresh answer, entry gone (cost-map reload): put it back
                    _install_model_info(litellm, bare, _info_entries.get(key) or _model_info_entry(bare, None))
                return
            else:
                if not present:  # the safe entry first: LiteLLM must never find the model unknown
                    _install_model_info(litellm, bare, _info_entries.get(key) or _model_info_entry(bare, None))
                mine = _info_inflight[key] = threading.Event()
        if wait_for is not None:
            wait_for.wait(MODEL_INFO_TIMEOUT_S + 1.0)
            with _info_lock:
                if not any(k.lower() in names for k in list(litellm.model_cost)):
                    _install_model_info(litellm, bare, _info_entries.get(key) or _model_info_entry(bare, None))
            return
        from llm_router.local_check import runtimes
        ok, show = runtimes._fetch(base, "POST", "/api/show", {"name": bare}, timeout=MODEL_INFO_TIMEOUT_S)
        ok = bool(ok) and isinstance(show, dict)
        entry = _model_info_entry(bare, show if ok else None)
        with _info_lock:
            _info_attempted[key] = (time.monotonic(), ok)
            _info_entries[key] = entry
            _install_model_info(litellm, bare, entry)
    except Exception as exc:  # noqa: BLE001 - counted, never raised; an entry is left in place when we can
        from llm_router import failopen
        failopen.record("CHZ-FO-LOCAL-PRIVACY-MODEL-INFO", exc, detail=f"model={model}")
        try:
            import litellm
            bare = _bare(model)
            with _info_lock:
                if key is not None:
                    _info_attempted.setdefault(key, (time.monotonic(), False))
                if f"ollama/{bare}" not in litellm.model_cost or f"ollama_chat/{bare}" not in litellm.model_cost:
                    _install_model_info(litellm, bare, _info_entries.get(key) or _model_info_entry(bare, None))
        except Exception:  # noqa: BLE001
            pass
    finally:
        if mine is not None and key is not None:
            with _info_lock:
                if _info_inflight.get(key) is mine:
                    del _info_inflight[key]
            mine.set()
