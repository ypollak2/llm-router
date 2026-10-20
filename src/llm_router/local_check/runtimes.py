"""Local-model runtimes for ``llm-router local-check`` (LC-3, LOCAL-CHECK-DESIGN §2).

Discovery, adapters, context-fit and digest for the runtimes a user may have: Ollama
(native /api/chat) and OpenAI-compatible servers (LM Studio, vLLM, llama.cpp).

Rules this module holds to:

* No model name is hard-coded anywhere (D-205: every user has a different model). Names
  come from what the runtime reports, or from the caller.
* Loopback only. Every request goes through ``http.client`` straight to the host (no
  proxy env), and a non-loopback host is refused unless the caller passes
  ``allow_remote=True``. Nothing here reaches the internet.
* Unknown stays ``None``: a context window or digest the runtime *answered* without (or
  with a body of the wrong shape) is ``None``, never 0 or a guess. ``digest is None`` means
  "digest_unverified".
* A probe that *failed in transit* (unreachable, timed out, unreadable body, HTTP 5xx/429/408)
  is NOT ``None``: ``ctx``/``settings_hash``/``max_prompt_chars``/``digest`` raise
  ``ProbeFailed`` after one retry and cache nothing. Callers (LC-6) must catch it; ``Runner()``
  turns it into ``RunStartError``. 404 and other 4xx are real answers (``None``, cached).
* Probes never raise on a 200 JSON body of the wrong shape (an unrelated service on 8000/8080):
  that reads as "not this runtime" (identify None, list_models [], value None).
* Generation is streamed so one wall-clock deadline covers the whole call, and closing
  the connection on a timeout lets the runtime stop generating.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlparse

TEMPERATURE = 0
MAX_TOKENS = 1024
PROBE_TIMEOUT_S = 1.5
CHARS_PER_TOKEN = 3.0          # conservative for diffs; prompt budget, not a tokenizer
PROMPT_RESERVE_TOKENS = 256    # system prompt + chat template overhead
OLLAMA_CTX_CAP = 32768         # when the model's trained window is larger than we should allocate
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# Conventional ports; the runtime is identified by its API, never by the port alone.
OPENAI_PROBE_PORTS = ((1234, "lmstudio"), (8000, "vllm"), (8080, "llamacpp"))
_EMBEDDING_MARKERS = ("embed", "bge-", "gte-", "e5-", "all-minilm")

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class RuntimeError_(Exception):
    """Raised for a refused endpoint (non-loopback) or an unreachable required service."""


class ProbeFailed(RuntimeError_):
    """A runtime probe (context window, digest) failed in transit: unreachable, timed out, an
    unreadable body, or a transient HTTP status (5xx, 429, 408). Distinct from "the runtime answered and has no such field" (that is
    ``None``). Never cached, so a transient failure cannot freeze a wrong value. Raised by
    ``context_length``/``digest``/``ctx``/``settings_hash``/``max_prompt_chars``; ``Runner()``
    converts it to ``RunStartError``."""


# --------------------------------------------------------------------------- helpers

def normalize_base(url: str) -> str:
    """``host:port`` -> ``http://host:port``; strip a trailing slash and a ``/v1`` suffix.
    Only http/https are accepted (ValueError otherwise)."""
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    scheme = url.split("://", 1)[0].lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme {scheme!r}: only http and https")
    url = url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def is_loopback(base_url: str) -> bool:
    try:
        host = urlparse(normalize_base(base_url)).hostname or ""
    except ValueError:
        return False
    return host in LOOPBACK or f"[{host}]" in LOOPBACK


def _conn(base_url: str, timeout: float, allow_remote: bool = False) -> http.client.HTTPConnection:
    """Connection to ``base_url``. RuntimeError_ for a refused (non-loopback) host; ValueError
    for a malformed URL (bad scheme, non-numeric or out-of-range port, no host)."""
    base = normalize_base(base_url)
    p = urlparse(base)
    port = p.port                                  # ValueError on "abc" / 99999
    if not p.hostname or (port is not None and port == 0):
        raise ValueError(f"bad endpoint {base!r}")
    if not allow_remote and not is_loopback(base):
        raise RuntimeError_(f"refusing non-loopback endpoint {base!r}")
    cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
    return cls(p.hostname, port or (443 if p.scheme == "https" else 80), timeout=timeout)


def _fetch(base_url: str, method: str, path: str, body: dict | None = None,
           timeout: float = PROBE_TIMEOUT_S, allow_remote: bool = False) -> tuple[bool, Any | None]:
    """``(True, data)`` for a 200 JSON body, ``(True, None)`` when the server answered with a
    definitive non-200 (404 and other 4xx), ``(False, None)`` when the request failed in transit:
    connection error, malformed endpoint, unreadable body, or a transient status
    (``TRANSIENT_STATUS``: 5xx, 429, 408). Raises only RuntimeError_ for a refused host."""
    conn = None
    try:
        conn = _conn(base_url, timeout, allow_remote)
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload,
                     headers={"Content-Type": "application/json"} if payload else {})
        resp = conn.getresponse()
        data = resp.read()
        if resp.status in TRANSIENT_STATUS or resp.status >= 500:
            return False, None
        if resp.status != 200:
            return True, None
        return True, json.loads(data)
    except RuntimeError_:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        return False, None
    finally:
        if conn is not None:
            conn.close()


def _json_request(base_url: str, method: str, path: str, body: dict | None = None,
                  timeout: float = PROBE_TIMEOUT_S, allow_remote: bool = False) -> Any | None:
    """JSON response body, or None on any failure (never raises except for a refused host)."""
    return _fetch(base_url, method, path, body, timeout, allow_remote)[1]


PROBE_RETRIES = 1
TRANSIENT_STATUS = frozenset({408, 429})   # plus every 5xx: "try again", never a real answer
PROBE_RETRY_DELAY_S = 0.2


def _strict_json(base_url: str, method: str, path: str, body: dict | None = None,
                 allow_remote: bool = False) -> Any | None:
    """Like ``_json_request`` but a transit failure raises ProbeFailed (after one retry) instead
    of looking like "the runtime has no such field". A 404/other 4xx answer is a real answer (None);
    5xx/429/408 count as transit failures."""
    for attempt in range(PROBE_RETRIES + 1):
        ok, data = _fetch(base_url, method, path, body, PROBE_TIMEOUT_S, allow_remote)
        if ok:
            return data
        if attempt < PROBE_RETRIES:
            time.sleep(PROBE_RETRY_DELAY_S)
    raise ProbeFailed(f"{method} {path} on {base_url} failed (unreachable, timed out or unreadable)")


def _entries(data: Any, key: str) -> list[dict]:
    """The dict entries of ``data[key]``; anything of the wrong shape (non-dict body, non-list
    value, non-dict entries) yields ``[]``. Probes call this, never ``.get`` on raw JSON."""
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        return []
    return [m for m in data[key] if isinstance(m, dict)]


def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def strip_think(text: str) -> str:
    """Remove <think>...</think> blocks. An unclosed <think> (output cut mid-thought) drops
    everything from it on; a bare </think> (template injected the opener) drops what precedes."""
    out = _THINK_BLOCK.sub("", text)
    low = out.lower()
    i = low.find("<think>")
    if i != -1:
        out = out[:i]
    j = out.lower().rfind("</think>")
    if j != -1:
        out = out[j + len("</think>"):]
    return out.strip()


def settings_hash(*, temperature: float, ctx: int | None, max_tokens: int, think: bool | None) -> str:
    """Stable id of the generation settings (design §2). Same settings -> same hash.
    ``ctx`` must be a confirmed value; an adapter's ``settings_hash`` raises ``ProbeFailed``
    rather than hash an unconfirmed ``None``."""
    blob = json.dumps({"temperature": temperature, "ctx": ctx, "max_tokens": max_tokens,
                       "think": think}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def max_prompt_chars(ctx: int | None, max_tokens: int = MAX_TOKENS) -> int | None:
    """Largest prompt (chars) that fits the runtime window with room for the answer.
    ``None`` when the window is unknown (the lane scope then records ctx_unverified).
    The adapter method of the same name raises ``ProbeFailed`` when the probe failed in transit
    (distinct from the runtime answering without a window, which is ``None``)."""
    if not ctx or ctx <= 0:
        return None
    room = ctx - max_tokens - PROMPT_RESERVE_TOKENS
    return max(0, int(room * CHARS_PER_TOKEN))


def _hash_fields(*parts: Any) -> str:
    return "meta:sha256:" + hashlib.sha256(
        json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:32]


# --------------------------------------------------------------------------- results

@dataclass
class GenResult:
    """One generation. ``status``: ok | truncated | empty | timeout | error."""
    status: str
    text: str = ""
    raw_chars: int = 0
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    first_token_s: float | None = None
    elapsed_s: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Endpoint:
    runtime: str                      # ollama | lmstudio | vllm | llamacpp | openai
    base_url: str
    models: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- adapters

class _Adapter:
    runtime = "openai"
    think: bool | None = None

    def __init__(self, base_url: str, *, allow_remote: bool = False, max_tokens: int = MAX_TOKENS,
                 temperature: float = TEMPERATURE):
        try:
            self.base_url = normalize_base(base_url)
            urlparse(self.base_url).port
        except ValueError as exc:
            raise RuntimeError_(f"bad endpoint {base_url!r}: {exc}") from exc
        self.allow_remote = allow_remote
        self.max_tokens = max_tokens
        self.temperature = temperature
        if not allow_remote and not is_loopback(self.base_url):
            raise RuntimeError_(f"refusing non-loopback endpoint {self.base_url!r}")

    # subclasses
    def _request(self, model: str, prompt: str, system: str | None) -> tuple[str, dict]:
        raise NotImplementedError

    def _parse_stream(self, resp: http.client.HTTPResponse) -> Iterator[dict]:
        raise NotImplementedError

    def context_length(self, model: str) -> int | None:
        raise NotImplementedError

    def digest(self, model: str) -> str | None:
        """Model digest, ``None`` if the runtime reports none ("digest_unverified"). Raises
        ``ProbeFailed`` when the probe failed in transit (LC-6 must catch it)."""
        raise NotImplementedError

    def ctx(self, model: str) -> int | None:
        """Context window, ``None`` if the runtime answered without one. Raises ``ProbeFailed``
        when the probe failed in transit (never cached; LC-6 must catch it)."""
        return self.context_length(model)

    def settings_hash(self, model: str) -> str:
        """Hash of the settings incl. the confirmed ctx. Raises ``ProbeFailed`` (never a hash
        over an unconfirmed ctx) when the context probe failed in transit."""
        return settings_hash(temperature=self.temperature, ctx=self.ctx(model),
                             max_tokens=self.max_tokens, think=self.think)

    def max_prompt_chars(self, model: str) -> int | None:
        """Prompt budget in chars, ``None`` if the window is unknown. Raises ``ProbeFailed`` when
        the context probe failed in transit (LC-6 scope partition must catch it)."""
        return max_prompt_chars(self.ctx(model), self.max_tokens)

    def generate(self, model: str, prompt: str, *, system: str | None = None,
                 timeout_s: float = 240.0) -> GenResult:
        """One serial, no-retry generation under a single wall-clock deadline."""
        t0 = time.monotonic()
        deadline = t0 + timeout_s
        conn = None
        parts: list[str] = []
        res = GenResult(status="ok")
        try:
            path, body = self._request(model, prompt, system)   # may probe ctx over the network
            conn = _conn(self.base_url, timeout_s, self.allow_remote)
            conn.request("POST", path, body=json.dumps(body).encode(),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status != 200:
                detail = resp.read(300).decode("utf-8", "replace")
                res.status, res.error = "error", f"http {resp.status}: {detail}"
            else:
                for ev in self._parse_stream(resp):
                    now = time.monotonic()
                    if now > deadline:
                        raise TimeoutError
                    if conn.sock is not None:
                        conn.sock.settimeout(max(0.05, deadline - now))
                    if ev.get("text"):
                        parts.append(ev["text"])
                        if res.first_token_s is None:
                            res.first_token_s = now - t0
                    for k in ("finish_reason", "prompt_tokens", "completion_tokens"):
                        if ev.get(k) is not None:
                            setattr(res, k, ev[k])
                    if ev.get("error"):
                        res.status, res.error = "error", str(ev["error"])[:300]
                if time.monotonic() > deadline:
                    raise TimeoutError
        except (TimeoutError, OSError) as exc:      # socket.timeout is an OSError/TimeoutError
            timed_out = isinstance(exc, TimeoutError) or "timed out" in str(exc)
            res.status = "timeout" if timed_out else "error"
            res.error = None if timed_out else f"{type(exc).__name__}: {exc}"[:300]
        except Exception as exc:    # noqa: BLE001  one item must never crash the run
            res.status, res.error = "error", f"{type(exc).__name__}: {exc}"[:300]
        finally:
            if conn is not None:
                conn.close()
        res.elapsed_s = time.monotonic() - t0
        raw = "".join(parts)
        res.raw_chars = len(raw)
        res.text = strip_think(raw)
        if res.status == "ok":
            if not res.text:
                res.status = "empty"             # incl. thinking that ate the whole budget
            elif res.finish_reason == "length":
                res.status = "truncated"
        return res


class OllamaAdapter(_Adapter):
    """Ollama native /api/chat: think=false, temperature 0, num_ctx from the runtime."""
    runtime = "ollama"
    think = False

    def __init__(self, base_url: str, **kw):
        super().__init__(base_url, **kw)
        self._show: dict[str, dict | None] = {}

    def _show_info(self, model: str) -> dict | None:
        if model not in self._show:     # only a real answer is cached; ProbeFailed is not
            self._show[model] = _strict_json(self.base_url, "POST", "/api/show",
                                             {"model": model}, allow_remote=self.allow_remote)
        return self._show[model]

    def context_length(self, model: str) -> int | None:
        info = _dict(self._show_info(model))
        params = info.get("parameters")
        for line in (params if isinstance(params, str) else "").splitlines():
            m = re.match(r"\s*num_ctx\s+(\d+)", line)
            if m:
                return int(m.group(1))
        for k, v in _dict(info.get("model_info")).items():
            if k.endswith(".context_length") and isinstance(v, int) and v > 0:
                return min(v, OLLAMA_CTX_CAP)
        return None

    def digest(self, model: str) -> str | None:
        data = _strict_json(self.base_url, "GET", "/api/tags", allow_remote=self.allow_remote)
        for m in _entries(data, "models"):
            if model in (m.get("name"), m.get("model")) and m.get("digest"):
                d = str(m["digest"])
                return d if d.startswith("sha256:") else "sha256:" + d
        return None

    def _request(self, model, prompt, system):
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
        opts: dict[str, Any] = {"temperature": self.temperature, "num_predict": self.max_tokens}
        ctx = self.ctx(model)
        if ctx:
            opts["num_ctx"] = ctx
        return "/api/chat", {"model": model, "messages": msgs, "stream": True,
                             "think": False, "options": opts}

    def _parse_stream(self, resp):
        for raw in resp:
            line = raw.strip()
            if not line:
                continue
            d = json.loads(line)
            if not isinstance(d, dict):
                continue
            ev: dict[str, Any] = {}
            if d.get("error"):
                ev["error"] = d["error"]
            msg = d.get("message")
            ev["text"] = (msg.get("content") if isinstance(msg, dict) else None) or ""
            if d.get("done"):
                ev["finish_reason"] = d.get("done_reason")
                ev["prompt_tokens"] = d.get("prompt_eval_count")
                ev["completion_tokens"] = d.get("eval_count")
            yield ev


class OpenAICompatAdapter(_Adapter):
    """Generic OpenAI-compatible chat (LM Studio, vLLM, llama.cpp): max_tokens 1024,
    temperature 0, <think> blocks stripped. ``runtime`` selects the context/digest source."""

    def __init__(self, base_url: str, runtime: str = "openai", **kw):
        super().__init__(base_url, **kw)
        self.runtime = runtime
        self._cache: dict[str, Any] = {}

    def _get(self, path: str) -> Any | None:
        if path not in self._cache:     # only a real answer is cached; ProbeFailed is not
            self._cache[path] = _strict_json(self.base_url, "GET", path,
                                             allow_remote=self.allow_remote)
        return self._cache[path]

    def _v1_entry(self, model: str) -> dict:
        for m in _entries(self._get("/v1/models"), "data"):
            if m.get("id") == model:
                return m
        return {}

    def context_length(self, model: str) -> int | None:
        def pos(v):
            return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None
        if self.runtime == "lmstudio":
            for m in _entries(self._get("/api/v0/models"), "data"):
                if m.get("id") == model:
                    return pos(m.get("loaded_context_length")) or pos(m.get("max_context_length"))
        if self.runtime == "llamacpp":
            props = _dict(self._get("/props"))
            return pos(_dict(props.get("default_generation_settings")).get("n_ctx"))
        return pos(self._v1_entry(model).get("max_model_len"))   # vLLM

    def digest(self, model: str) -> str | None:
        """id plus whatever size/mtime/path the runtime reports; None when nothing beyond the
        id is available (an id alone does not detect a swapped file)."""
        entry = self._v1_entry(model)
        extra: list[Any] = []
        if self.runtime == "lmstudio":
            for m in _entries(self._get("/api/v0/models"), "data"):
                if m.get("id") == model:
                    entry = {**entry, **m}
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
        for k in ("size", "n_params", "mtime", "created", "modified_at", "quantization",
                  "max_model_len"):
            v = entry.get(k) if entry.get(k) is not None else meta.get(k)
            if v is not None:
                extra.append((k, v))
        if self.runtime == "llamacpp":
            props = _dict(self._get("/props"))
            for k in ("model_path", "build_info"):
                if props.get(k):
                    extra.append((k, props[k]))
        # `created` alone is often a server start time; keep it only with a size-like field.
        if not [e for e in extra if e[0] != "created"]:
            return None
        return _hash_fields(model, extra)

    def _request(self, model, prompt, system):
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
        return "/v1/chat/completions", {
            "model": model, "messages": msgs, "stream": True, "temperature": self.temperature,
            "max_tokens": self.max_tokens, "stream_options": {"include_usage": True}}

    def _parse_stream(self, resp):
        for raw in resp:
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                return
            d = json.loads(payload)
            if not isinstance(d, dict):
                continue
            ev: dict[str, Any] = {}
            if d.get("error"):
                ev["error"] = d["error"]
            for ch in d.get("choices") or []:
                if not isinstance(ch, dict):
                    continue
                delta = ch.get("delta")
                ev["text"] = (delta.get("content") if isinstance(delta, dict) else None) or ""
                if ch.get("finish_reason"):
                    ev["finish_reason"] = ch["finish_reason"]
            u = d.get("usage")
            if isinstance(u, dict) and u:
                ev["prompt_tokens"] = u.get("prompt_tokens")
                ev["completion_tokens"] = u.get("completion_tokens")
            yield ev


# --------------------------------------------------------------------------- discovery

def identify(base_url: str, *, allow_remote: bool = False) -> str | None:
    """Which runtime answers at ``base_url``, judged by its API. None if nothing answers."""
    t = PROBE_TIMEOUT_S
    kw = {"timeout": t, "allow_remote": allow_remote}
    tags = _json_request(base_url, "GET", "/api/tags", **kw)
    if isinstance(tags, dict) and isinstance(tags.get("models"), list):
        return "ollama"
    lm = _json_request(base_url, "GET", "/api/v0/models", **kw)
    if isinstance(lm, dict) and isinstance(lm.get("data"), list):
        return "lmstudio"
    v1 = _json_request(base_url, "GET", "/v1/models", **kw)
    if not (isinstance(v1, dict) and isinstance(v1.get("data"), list)):
        return None                   # not an OpenAI-compatible model list: not a runtime
    if isinstance(_json_request(base_url, "GET", "/props", **kw), dict):
        return "llamacpp"
    if any("max_model_len" in m for m in _entries(v1, "data")):
        return "vllm"
    return "openai"


def drop_embeddings(names: list[str]) -> list[str]:
    """Model names minus embedding models (not chat models); one filter for every source."""
    return [n for n in names if isinstance(n, str)
            and not any(x in n.lower() for x in _EMBEDDING_MARKERS)]


def list_models(base_url: str, runtime: str, *, allow_remote: bool = False) -> list[str]:
    if runtime == "ollama":
        data = _json_request(base_url, "GET", "/api/tags", allow_remote=allow_remote)
        names = [m.get("name") for m in _entries(data, "models") if m.get("name")]
    else:
        data = _json_request(base_url, "GET", "/v1/models", allow_remote=allow_remote)
        names = [m.get("id") for m in _entries(data, "data") if m.get("id")]
    return drop_embeddings(names)


def make_adapter(endpoint: Endpoint, **kw) -> _Adapter:
    if endpoint.runtime == "ollama":
        return OllamaAdapter(endpoint.base_url, **kw)
    return OpenAICompatAdapter(endpoint.base_url, runtime=endpoint.runtime, **kw)


def discover(endpoint: str | None = None, *, ports: tuple = OPENAI_PROBE_PORTS,
             include_ollama: bool = True, allow_remote: bool = False) -> list[Endpoint]:
    """Endpoints that answer right now, each with the models it reports.

    ``endpoint`` (an http/https URL) is the override: only it is probed. It must be loopback
    unless the caller passes ``allow_remote=True`` (an explicit opt-in; a non-loopback endpoint
    without it raises RuntimeError_, a malformed one yields ``[]``). Otherwise Ollama
    comes from ``model_discovery`` (one implementation of "what does Ollama have") and the
    OpenAI-compatible ports are probed on 127.0.0.1 with short timeouts.
    """
    if endpoint:
        try:
            base = normalize_base(endpoint)
            rt = identify(base, allow_remote=allow_remote)
            if rt is None:
                return []
            return [Endpoint(rt, base, list_models(base, rt, allow_remote=allow_remote))]
        except ValueError:
            return []
    found: list[Endpoint] = []
    seen: set[str] = set()
    if include_ollama:
        from llm_router import model_discovery
        base = normalize_base(model_discovery._ollama_url())
        if is_loopback(base):
            names = drop_embeddings(list(model_discovery.probe_ollama() or []))
            if names:
                found.append(Endpoint("ollama", base, list(names)))
                seen.add(base)
    for port, _hint in ports:
        base = f"http://127.0.0.1:{port}"
        if base in seen:
            continue
        rt = identify(base)
        if rt is None or rt == "ollama":
            continue
        found.append(Endpoint(rt, base, list_models(base, rt)))
    return found
