"""Ollama adapter (native tool API) and the helpers `hooks/agent_loop.py` shares.

The helpers here (URL validation, context window, temperature, and the
text/XML tool-call repair shim) were extracted from `hooks/agent_loop.py` so
there is one copy; that module re-exports them under its old private names.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field

from llm_router import local_privacy
from llm_router.local_context_guard import (
    ContextOverflow,
    check_overflow,
    estimate_payload_tokens,
)

# ── shared helpers (verbatim from hooks/agent_loop.py) ───────────────────────


def constrained_decoding_enabled() -> bool:
    """Default ON for the hook loop. `off` falls back to `tools=` plus the repair shim."""
    return os.environ.get("LLM_ROUTER_CONSTRAINED_TOOLS", "").strip().lower() not in (
        "0", "off", "false", "no",
    )


def default_num_ctx(model: str | None) -> int:
    """Per-model window from the single table in ``llm_router.local_models`` (M3.4)."""
    from llm_router.local_models import num_ctx as _table_num_ctx
    return _table_num_ctx(model)


_SHARED_KA_DURATION = re.compile(r"(?:\d+(?:\.\d+)?(?:ns|us|\u00b5s|ms|s|m|h))+")
_SHARED_KA_WARNED: set[str] = set()


def shared_keep_alive() -> int | str:
    """``keep_alive`` the shared runner is sent: -1 (forever) unless ``LLM_ROUTER_SHARED_KEEP_ALIVE`` is set.

    Set, it is an Ollama duration (``"2h"``, ``"90m"``) or an integer in seconds. 0 and negatives
    other than -1 are rejected (0 would unload the model after every call). An invalid value is
    NOT raised (this runs on every hot path): it logs one warning and falls back to -1, today's
    behaviour. Every shared-runner call site gets this through :func:`shared_runner_opts`, so one
    path left at -1 cannot keep the model alive (each request resets the timer)."""
    raw = os.environ.get("LLM_ROUTER_SHARED_KEEP_ALIVE", "").strip()
    if not raw:
        return -1
    value: int | str | None = None
    try:
        n = int(raw)
        value = n if (n > 0 or n == -1) else None
    except ValueError:
        if _SHARED_KA_DURATION.fullmatch(raw) and re.search(r"[1-9]", raw):
            value = raw
    if value is None:
        if raw not in _SHARED_KA_WARNED:
            _SHARED_KA_WARNED.add(raw)
            logging.getLogger(__name__).warning(
                "LLM_ROUTER_SHARED_KEEP_ALIVE=%r is invalid (want an Ollama duration like '2h', "
                "seconds > 0, or -1); using -1 (never expire)", raw)
        return -1
    return value


def shared_runner_opts(model: str | None) -> tuple[int, int | str] | None:
    """``(num_ctx, keep_alive)`` every caller must send for *model*, or None.

    Set only when ``LLM_ROUTER_LOCAL_MODEL`` is set and equals *model* (``ollama/`` prefix
    optional). Ollama keys a runner on num_ctx and, with one slot, a call at another context
    evicts the proxy's runner while a short keep_alive expires it (docs/bugs/CLSCTX-1.md). So
    under the one-model policy the proxy's window wins over every other override
    (the ``local_models`` table value, which every process derives alike; the env is not
    consulted) and keep_alive is :func:`shared_keep_alive` (-1 unless
    ``LLM_ROUTER_SHARED_KEEP_ALIVE`` sets a bound). num_ctx does not depend on keep_alive, so a
    reload after expiry asks for the same window as the pinned runner."""
    one = get_local_model()
    name = (model or "").strip()
    name = name.split("/", 1)[1] if name.startswith("ollama/") else name
    if not one or not name or not _same_name(name, one):
        return None
    return default_num_ctx(name), shared_keep_alive()


def num_ctx(model: str | None = None) -> int | None:
    """Context window to request, or None to accept the server's default."""
    shared = shared_runner_opts(model)
    if shared:
        return shared[0]
    raw = (os.environ.get("LLM_ROUTER_AGENT_NUM_CTX", "").strip()
           or os.environ.get("LLM_ROUTER_LOCAL_NUM_CTX", "").strip())
    if not raw:
        return default_num_ctx(model)
    try:
        value = int(raw)
        return value if value > 0 else None
    except ValueError:
        return default_num_ctx(model)


_env_num_ctx = num_ctx  # OllamaAdapter.__init__ has a parameter named num_ctx that shadows the function


def agent_temperature() -> float:
    raw = os.environ.get("LLM_ROUTER_AGENT_TEMPERATURE", "").strip()
    try:
        value = float(raw)
        return value if 0.0 <= value <= 2.0 else 0.1
    except ValueError:
        return 0.1


_log = logging.getLogger(__name__)

OLLAMA_URL_DEFAULT = "http://localhost:11434"


def validated_ollama_url(raw: str) -> str:
    """CHZ-SEC-06 scheme/host validation, failing CLOSED to localhost."""
    if not raw:
        return OLLAMA_URL_DEFAULT
    try:
        from llm_router.config import validate_ollama_url
    except Exception:  # noqa: BLE001
        return raw if raw == OLLAMA_URL_DEFAULT else OLLAMA_URL_DEFAULT
    return validate_ollama_url(raw) or OLLAMA_URL_DEFAULT


def get_ollama_url() -> str:
    return validated_ollama_url(
        os.environ.get("LLM_ROUTER_OLLAMA_URL")
        or os.environ.get("OLLAMA_BASE_URL")
        or OLLAMA_URL_DEFAULT
    )


def get_local_model_url() -> str | None:
    """``LLM_ROUTER_LOCAL_MODEL_URL`` validated, or None when unset/invalid.

    The server that holds the ONE big local model (a separate Ollama with
    OLLAMA_MAX_LOADED_MODELS=1). Unset = every local call uses
    :func:`get_ollama_url`, exactly as before.
    """
    raw = os.environ.get("LLM_ROUTER_LOCAL_MODEL_URL", "").strip()
    if not raw:
        return None
    try:
        from llm_router.config import validate_ollama_url
    except Exception:  # noqa: BLE001
        return None
    return validate_ollama_url(raw) or None


def main_url() -> str:
    """Server of THE local model: LLM_ROUTER_LOCAL_MODEL_URL, else the default Ollama URL (one server, never two)."""
    return get_local_model_url() or get_ollama_url()


def url_for_model(model: str | None = None) -> str:
    """The Ollama server a chat/generate call for ``model`` goes to.

    The main local model goes to ``LLM_ROUTER_LOCAL_MODEL_URL`` when set. A
    small model (classifier / decision model, by role or configured name) must
    NOT move: sending it there would evict the big model (max 1 loaded).
    """
    main = get_local_model_url()
    if main is None:
        return get_ollama_url()
    if model:
        try:
            from llm_router import local_models
            bare = model.removeprefix("ollama_chat/").removeprefix("ollama/")
            if not local_models.serves_generate(bare):
                return get_ollama_url()
        except Exception:  # noqa: BLE001
            pass
    return main


def get_local_model() -> str | None:
    """``LLM_ROUTER_LOCAL_MODEL``: the single big local model allowed, or None when unset."""
    return os.environ.get("LLM_ROUTER_LOCAL_MODEL", "").strip() or None


def _is_small_model(model: str | None) -> bool:
    if get_local_model():      # strictly ONE model: nothing is exempt when LLM_ROUTER_LOCAL_MODEL is set
        return False
    if not model:
        return False
    try:
        from llm_router import local_models
        bare = model.removeprefix("ollama_chat/").removeprefix("ollama/")
        return not local_models.serves_generate(bare)
    except Exception:  # noqa: BLE001
        return False


def resolve_main_model(model: str | None, site: str = "", *, quiet: bool = False) -> str | None:
    """The model a main-local-model path must use.

    With ``LLM_ROUTER_LOCAL_MODEL`` set, every caller-supplied model is replaced by
    it (the override is logged); a small classifier/decision model is left alone.
    Unset: ``model`` unchanged. ``quiet`` suppresses the override log (pure lookups).
    """
    allowed = get_local_model()
    if not allowed or _is_small_model(model):
        return model
    if model:
        had_prefix = next((p for p in ("ollama_chat/", "ollama/") if model.startswith(p)), "")
        if model.removeprefix("ollama_chat/").removeprefix("ollama/") != allowed:
            if not quiet:
                _log.warning("LLM_ROUTER_LOCAL_MODEL override at %s: %s -> %s", site or "?", model, allowed)
            return had_prefix + allowed
        return model
    return allowed


class LocalModelConflict(RuntimeError):
    """Another big model is resident on a local Ollama; sending would evict or OOM (typed, fail closed)."""

    status = "backend_error"
    kind = "local_model_conflict"

    def __init__(self, resident: str, size_gb: float, url: str, allowed: str, limit_gb: float):
        self.resident, self.size_gb, self.url = resident, size_gb, url
        self.allowed, self.limit_gb = allowed, limit_gb
        super().__init__(
            f"local_model_conflict: {resident} is resident on {url} using {size_gb:.1f} GB of VRAM "
            f"(limit for any model other than {allowed}: {limit_gb:g} GB). Not sending the request. "
            f"With eviction on (the default when LLM_ROUTER_LOCAL_MODEL is set; LLM_ROUTER_MEM_ON_PRESSURE) the "
            f"router unloads it itself and re-checks; otherwise stop it with `ollama stop {resident}`.")


class MemoryBudgetExceeded(LocalModelConflict):
    """Loading ``model`` would put resident local models past what this machine's memory allows (typed, fail closed).

    A :class:`LocalModelConflict`, so every caller that already falls through to the cloud on a conflict does
    the same here. ``rule`` is ``"sum"`` (resident + target above total - reserve) or ``"free"`` (target above
    what the OS reports free minus a small reserve).
    """

    kind = "memory_budget_exceeded"

    def __init__(self, rule: str, model: str, url: str, need_gb: float, resident_gb: float, limit_gb: float,
                 resident: str = "", allowed: str = ""):
        self.rule, self.model, self.need_gb, self.resident_gb, self.limit_gb = rule, model, need_gb, resident_gb, limit_gb
        self.resident, self.size_gb, self.url, self.allowed = resident, resident_gb, url, allowed
        what = ("resident models + this one exceed total memory minus the reserve" if rule == "sum"
                else "this model exceeds the memory the OS reports free minus the reserve")
        RuntimeError.__init__(
            self,
            f"memory_budget_exceeded: loading {model} on {url} needs ~{need_gb:.1f} GB with {resident_gb:.1f} GB "
            f"already resident (limit {limit_gb:.1f} GB; {what}). Not sending the request. "
            f"Resident: {resident or 'none'}. Unload one with `ollama stop <model>`, or set LLM_ROUTER_MEM_GUARD=off.")


_LAST = __import__("threading").local()


def record_refusal(exc: "LocalModelConflict | None") -> None:
    """Remember (per thread) why the last main-local call was refused, for callers that get only None."""
    _LAST.refusal = exc


def last_refusal() -> "LocalModelConflict | None":
    return getattr(_LAST, "refusal", None)


def _max_other_vram_gb() -> float:
    raw = os.environ.get("LLM_ROUTER_LOCAL_MAX_OTHER_VRAM_GB", "").strip()
    try:
        value = float(raw)
        return value if value >= 0 else 2.0
    except ValueError:
        return 2.0


def _same_name(a: str, b: str) -> bool:
    def norm(x: str) -> str:
        return x if ":" in x else x + ":latest"
    return norm(a) == norm(b)


_GIB = 1024 ** 3
_SMALL_RESERVE_GIB = 2.0      # rule (b): kept free on top of the target, for the OS and other apps
_PS_TTL_S = 5.0
# (model, base_url, settings...) -> (monotonic time, refusal or None). One entry per target, so a decision for
# one model/server is never served for another (the old single-slot cache was keyed on the allowed model only).
_PS_CACHE: dict = {}
# (model, base_url) -> (expires monotonic, estimated bytes incl. headroom): loads approved but not yet in /api/ps.
# Without it two different models checked inside the cache window both pass against the same /api/ps snapshot.
_RESERVED: dict = {}
_RESERVE_TTL_S = 120.0
_EVICT_POLLS = 6
_LOCK = __import__("threading").RLock()


def _is_loopback(url: str) -> bool:
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1")


def _prune(now: float) -> None:
    with _LOCK:
        for k in [k for k, v in _PS_CACHE.items() if now - v[0] >= _PS_TTL_S]:
            del _PS_CACHE[k]
        for k in [k for k, v in _RESERVED.items() if v[0] <= now]:
            del _RESERVED[k]


def _clone(exc: LocalModelConflict) -> LocalModelConflict:
    """A fresh exception equal to a cached one (never re-raise one object: its traceback grows on each raise)."""
    if isinstance(exc, MemoryBudgetExceeded):
        return MemoryBudgetExceeded(exc.rule, exc.model, exc.url, exc.need_gb, exc.resident_gb, exc.limit_gb,
                                    exc.resident, exc.allowed)
    return LocalModelConflict(exc.resident, exc.size_gb, exc.url, exc.allowed, exc.limit_gb)


def mem_guard_enabled() -> bool:
    """``LLM_ROUTER_MEM_GUARD=off`` (also 0/false/no) turns the whole memory guard off."""
    return os.environ.get("LLM_ROUTER_MEM_GUARD", "").strip().lower() not in ("off", "0", "false", "no")


def _headroom() -> float:
    try:
        value = float(os.environ.get("LLM_ROUTER_MEM_HEADROOM", "").strip())
        return value if value >= 1.0 else 1.25
    except ValueError:
        return 1.25


def _reserve_gib(total_gib: float) -> float:
    try:
        value = float(os.environ.get("LLM_ROUTER_MEM_RESERVE_GB", "").strip())
        if value >= 0:
            return value
    except ValueError:
        pass
    return max(6.0, 0.20 * total_gib)


def _evict_mode() -> bool:
    """``LLM_ROUTER_MEM_ON_PRESSURE``: ``evict`` unloads a resident model to make room, ``refuse`` never does.
    Unset: evict only when LLM_ROUTER_LOCAL_MODEL is set (there is then one model that must win), else refuse."""
    raw = os.environ.get("LLM_ROUTER_MEM_ON_PRESSURE", "").strip().lower()
    if raw in ("evict", "refuse"):
        return raw == "evict"
    return bool(get_local_model())


def _bare(model: str) -> str:
    return model.removeprefix("ollama_chat/").removeprefix("ollama/")


def _fail_open(code: str, detail: str, exc: BaseException | None = None) -> None:
    _log.info("memory guard: %s; not blocking", detail)
    try:
        from llm_router import failopen
        failopen.record(code, exc, detail=detail)
    except Exception:  # noqa: BLE001 - recording must never block a request
        pass


def _read_ps(url: str) -> list[dict]:
    """``/api/ps`` rows of one server (raises when unreachable)."""
    with urllib.request.urlopen(urllib.request.Request(f"{url}/api/ps"), timeout=1.5) as resp:  # nosec B310 — validated URL
        rows = json.loads(resp.read()).get("models") or []
    return [r for r in rows if isinstance(r, dict)]


def _tags_size(url: str, name: str) -> int | None:
    """On-disk size in bytes of ``name`` from ``/api/tags``, or None."""
    with urllib.request.urlopen(urllib.request.Request(f"{url}/api/tags"), timeout=1.5) as resp:  # nosec B310 — validated URL
        rows = json.loads(resp.read()).get("models") or []
    for row in rows:
        if isinstance(row, dict) and _same_name(str(row.get("name") or row.get("model") or ""), name):
            size = row.get("size")
            if isinstance(size, (int, float)) and size > 0:
                return int(size)
    return None


# (url, bare name) -> /api/show payload. Static metadata of an installed model, so asked once, not per check.
_SHOW_CACHE: dict = {}


def _show_payload(url: str, name: str) -> dict:
    """``/api/show`` of ``name``, cached per (url, name); raises when unreachable (failures are not cached)."""
    key = (url, _bare(name))
    hit = _SHOW_CACHE.get(key)
    if hit is not None:
        return hit
    req = urllib.request.Request(f"{url}/api/show", data=json.dumps({"model": name}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=1.5) as resp:  # nosec B310 — validated URL
        payload = json.loads(resp.read())
    payload = payload if isinstance(payload, dict) else {}
    _SHOW_CACHE[key] = payload
    return payload


def _show_size(url: str, name: str) -> int | None:
    """``size`` of ``name`` from ``/api/show`` when this Ollama reports one, else None."""
    size = _show_payload(url, name).get("size")
    return int(size) if isinstance(size, (int, float)) and size > 0 else None


# -- context-window (KV) memory ---------------------------------------------------------------------------------
# The weights on disk are not what a load holds: the KV cache grows with num_ctx, a hybrid model adds a recurrent
# state, and the runtime adds a compute buffer. Sources: engine log of qwen3.6:35b-a3b-coding (qwen35moe, 10
# full-attention layers): KV f16 640 MiB at 32768 cells = 20 MiB per 1k tokens, MTP draft KV +2 MiB per 1k,
# recurrent state 188 MiB, compute buffer ~0.5 GiB (RESEARCH-256K-R3-ENGINE-20261110.md section 5).
_MIB = 1024 ** 2
_COMPUTE_BYTES = 512 * _MIB
# Speculative (MTP) draft context KV is +2 MiB per 1k on top of 20 (engine log): x1.1 on both the registry and GGUF paths.
_KV_DRAFT_MARGIN = 1.1
_SMALL_MODEL_BYTES = 4 * _GIB
# bare model name -> (KV bytes per 1k tokens, constant state bytes). Used when /api/show has no usable metadata.
_KV_REGISTRY: dict[str, tuple[int, int]] = {
    "qwen3.6:35b-a3b-coding": (20 * _MIB, 188 * _MIB),
}
# Unknown model with no metadata: 128 MiB per 1k tokens = 32 layers x 8 KV heads x 128 dim x f16 (an 8B-class GQA
# dense model). It is deliberately on the high side: over-counting refuses one load, under-counting can OOM the Mac.
_KV_DEFAULT_PER_1K = 128 * _MIB
_METAL_FRACTION = 0.78        # default Metal working-set cap / RAM: measured 40,200.90 MB of 51,539.6 MB (Ollama log)
_WIRED_MB: list = []          # cached iogpu.wired_limit_mb (static per boot); [None] = unreadable


def _show_model_info(url: str, name: str) -> dict:
    """``model_info`` from ``/api/show`` ({} when absent)."""
    info = _show_payload(url, name).get("model_info")
    return info if isinstance(info, dict) else {}


def _trained_ctx(url: str, name: str) -> int | None:
    """The model's own trained context length (``<arch>.context_length``); Ollama clamps a request to it."""
    try:
        info = _show_model_info(url, name)
    except Exception:  # noqa: BLE001
        return None
    arch = info.get("general.architecture")
    v = info.get(f"{arch}.context_length") if isinstance(arch, str) else None
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def _kv_from_info(info: dict) -> tuple[int, int] | None:
    """(KV bytes per token f16, constant state bytes) from GGUF metadata, or None when it lacks the fields."""
    arch = info.get("general.architecture")
    if not isinstance(arch, str) or not arch:
        return None

    def num(key: str):
        v = info.get(f"{arch}.{key}")
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None

    blocks = num("block_count")
    heads_kv = info.get(f"{arch}.attention.head_count_kv")
    heads = num("attention.head_count")
    if not blocks:
        return None
    klen = num("attention.key_length")
    vlen = num("attention.value_length") or klen
    if not klen:
        emb = num("embedding_length")
        if not (emb and heads):
            return None
        klen = vlen = emb / heads
    interval = num("full_attention_interval")      # hybrid models: only every Nth block keeps a KV cache
    layers = int(blocks // interval) if interval else int(blocks)
    if isinstance(heads_kv, list):
        kv_heads = sum(h for h in heads_kv if isinstance(h, (int, float)) and h > 0)    # per-layer list
        if not kv_heads:
            return None
        per_token = kv_heads * (klen + vlen) * 2
    else:
        h = heads_kv if isinstance(heads_kv, (int, float)) and heads_kv > 0 else heads
        if not h:
            return None
        per_token = layers * h * (klen + vlen) * 2
    state = 192 * _MIB if interval else 0       # hybrid recurrent layers hold a constant state (188 MiB measured)
    return int(per_token), state


def _kv_terms(url: str, name: str, ctx: int, size: float = 0.0) -> tuple[float, str]:
    """(bytes the context window adds on top of the weights, source label) for ``ctx`` tokens:
    KV(ctx) x draft margin + recurrent state + compute buffer."""
    try:
        derived = _kv_from_info(_show_model_info(url, name))
    except Exception as exc:  # noqa: BLE001
        _log.info("memory guard: /api/show model_info of %s on %s failed (%s)", name, url, exc)
        derived = None
    if derived is not None:
        per_token, state = derived
        return per_token * ctx * _KV_DRAFT_MARGIN + state + _COMPUTE_BYTES, "gguf"
    for key, (per_1k, state) in _KV_REGISTRY.items():
        if _same_name(key, name):
            return per_1k * ctx / 1000 * _KV_DRAFT_MARGIN + state + _COMPUTE_BYTES, "registry"
    if size < _SMALL_MODEL_BYTES:
        # No metadata (often /api/show timing out while Ollama is busy) and a small model: the flat default would
        # over-count a 1 GB model by 4 GiB and refuse it. Keep today's weights x headroom and record it.
        _fail_open("CHZ-FO-MEM-GUARD-KV", f"no KV metadata for small model {name} on {url}; weights x headroom only")
        return 0.0, "failopen"
    return _KV_DEFAULT_PER_1K * ctx / 1000 + _COMPUTE_BYTES, "default"


def _metal_cap_gib(total_gib: float) -> float | None:
    """Metal working-set cap in GiB on Apple Silicon: ``iogpu.wired_limit_mb`` when set (readable without sudo,
    read once per process), else 78% of RAM. None off macOS."""
    import sys
    if sys.platform != "darwin":
        return None
    if not _WIRED_MB:
        wired = None
        try:
            import subprocess
            out = subprocess.run(["sysctl", "-n", "iogpu.wired_limit_mb"], capture_output=True, text=True,
                                 timeout=1.5, check=False).stdout.strip()
            wired = int(out) if out.isdigit() and int(out) > 0 else None
        except Exception:  # noqa: BLE001
            wired = None
        _WIRED_MB.append(wired)
    return _WIRED_MB[0] / 1024 if _WIRED_MB[0] else total_gib * _METAL_FRACTION


def _target_size(url: str, name: str) -> int | None:
    for fetch in (_tags_size, _show_size):
        try:
            size = fetch(url, name)
        except Exception as exc:  # noqa: BLE001
            _log.info("memory guard: %s on %s failed (%s)", fetch.__name__, url, exc)
            continue
        if size:
            return size
    return None


def _unload(url: str, name: str) -> bool:
    """Ask the server at ``url`` to drop ``name`` now (``keep_alive`` 0). True when the request was accepted."""
    body = json.dumps({"model": name, "keep_alive": 0, "prompt": ""}).encode()
    req = urllib.request.Request(f"{url}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:  # nosec B310 — validated URL, no prompt text
            resp.read()
        _log.warning("memory guard: unloaded %s from %s to make room", name, url)
        return True
    except Exception as exc:  # noqa: BLE001
        _log.info("memory guard: could not unload %s from %s (%s)", name, url, exc)
        return False


def _row_bytes(row: dict) -> float:
    """Memory a loaded model holds. ``size`` includes the KV cache; ``size_vram`` is the fallback."""
    for key in ("size", "size_vram"):
        raw = row.get(key)
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw)
    return 0.0


def _machine_memory() -> tuple[float | None, float | None]:
    """(total, free) in GiB from the host (hardware.probe: sysctl + vm_stat on macOS), None where unreadable."""
    from llm_router import hardware
    info = hardware.probe_cached(_PS_TTL_S)
    return info.get("total_gb"), info.get("free_gb")


def _servers(base_url: str) -> list[str]:
    urls: list[str] = []
    for u in (base_url, get_local_model_url(), get_ollama_url()):
        if u and u.rstrip("/") not in urls:
            urls.append(u.rstrip("/"))
    return urls


def _verdict(name: str, base_url: str, allowed: str | None, purpose: str, budget: bool = True):
    """One evaluation of both rules. Returns None (fits / fail open) or ``(refusal, victim)`` where victim is
    ``(url, model)`` of the resident model eviction would drop, or None."""
    by_url: dict[str, list[dict]] = {}
    for url in _servers(base_url):
        try:
            by_url[url] = _read_ps(url)
        except Exception as exc:  # noqa: BLE001
            _fail_open("CHZ-FO-MEM-GUARD-PS", f"/api/ps on {url} unreachable ({purpose})", exc)
    if not by_url:
        return None
    rows = [(url, r, str(r.get("name") or r.get("model") or "")) for url, rs in by_url.items() for r in rs]
    rows = [t for t in rows if t[2]]
    # (0) the one-model rule (#864): a model other than the allowed one above the VRAM limit.
    if allowed:
        limit = _max_other_vram_gb()
        for url, row, rname in rows:
            if _same_name(rname, allowed) or row.get("size_vram") is None:
                continue
            size_gb = float(row["size_vram"]) / 1e9
            if size_gb > limit:
                return LocalModelConflict(rname, size_gb, url, allowed, limit), (url, rname)
    if not budget:      # LLM_ROUTER_MEM_GUARD=off: only the one-model rule above
        return None
    from llm_router import local_models
    want_ctx = local_models.num_ctx(name)
    trained = _trained_ctx(base_url, name)
    if trained:
        want_ctx = min(want_ctx, trained)      # Ollama clamps the request to the model's own window
    # (1) already resident on that server: no new load, unless it was loaded with a smaller window than the one
    # requested (a reload). Same-or-larger window, or an unreported one: nothing to add, never double counted.
    same = [(u, r, n) for u, r, n in rows if u == base_url and _same_name(n, name)]
    if same:
        have = same[0][1].get("context_length")
        if not isinstance(have, (int, float)) or have >= want_ctx:
            return None
        rows = [t for t in rows if t not in same]      # the reload replaces the resident copy: count it once
    if not _is_loopback(base_url):
        _log.debug("memory guard: %s is not a loopback server; its memory is not this machine's, budget skipped", base_url)
        return None
    rows = [t for t in rows if _is_loopback(t[0])]     # only this machine's servers count against this machine's RAM
    size = _target_size(base_url, name)
    if size is None:
        _fail_open("CHZ-FO-MEM-GUARD-SIZE", f"size of {name} on {base_url} unknown ({purpose})")
        return None
    extra, kv_src = _kv_terms(base_url, name, want_ctx, size)
    # weights + window (KV + state + compute); the old weights x headroom stays as the floor, never lowered.
    need_bytes = max(size * _headroom(), size + extra)
    need = need_bytes / _GIB
    _log.info("memory guard: %s needs %.1f GiB (weights %.1f + window %.1f GiB at num_ctx %d, %s)",
              name, need, size / _GIB, extra / _GIB, want_ctx, kv_src)
    now = time.monotonic()
    _prune(now)
    with _LOCK:
        for (rname, rurl) in list(_RESERVED):      # a reserved load that /api/ps now shows has become real
            if any(u == rurl and _same_name(n, rname) for u, _r, n in rows):
                del _RESERVED[(rname, rurl)]
        reserved = sum(v[1] for k, v in _RESERVED.items() if k != (name, base_url))
    resident = (sum(_row_bytes(r) for _u, r, _n in rows) + reserved) / _GIB
    listing = ", ".join(f"{n} {_row_bytes(r) / _GIB:.1f} GB" for _u, r, n in rows)
    if reserved:
        listing += f", loading {reserved / _GIB:.1f} GB"
    try:
        total, free = _machine_memory()
    except Exception as exc:  # noqa: BLE001
        total, free = None, None
        _fail_open("CHZ-FO-MEM-GUARD-HOST", f"host memory unreadable ({purpose})", exc)
    refusal = None
    if total:
        cap = total - _reserve_gib(total)
        if resident + need > cap:      # (a) model-sum cap: independent of vm_stat noise
            refusal = MemoryBudgetExceeded("sum", name, base_url, need, resident, cap, listing, allowed or "")
        metal = _metal_cap_gib(total)
        if refusal is None and metal is not None and resident + need > metal:     # (c) Metal working-set cap
            refusal = MemoryBudgetExceeded("sum", name, base_url, need, resident, metal, listing, allowed or "")
    if refusal is None and free is not None:
        room = free - _SMALL_RESERVE_GIB
        if need > room:                # (b) catches other apps (docker, arenas) holding the RAM
            refusal = MemoryBudgetExceeded("free", name, base_url, need, resident, max(room, 0.0), listing, allowed or "")
    elif refusal is None and free is None and total is not None:
        _fail_open("CHZ-FO-MEM-GUARD-HOST", f"free memory unreadable, only the sum cap applied ({purpose})")
    if refusal is None:
        with _LOCK:
            _RESERVED[(name, base_url)] = (now + _RESERVE_TTL_S, need_bytes)
        return None
    victims = [(url, rname, _row_bytes(r)) for url, r, rname in rows
               if not _same_name(rname, name) and not (allowed and _same_name(rname, allowed))]
    victim = max(victims, key=lambda v: v[2]) if victims else None
    return refusal, (victim[0], victim[1]) if victim else None


def ensure_fits(model: str | None, base_url: str | None = None, purpose: str = "", *,
                deadline_s: float | None = None) -> None:
    """THE choke point before any local model is loaded: raise :class:`LocalModelConflict` /
    :class:`MemoryBudgetExceeded` when loading ``model`` on ``base_url`` would not fit.

    Live state only: ``/api/ps`` on every known server (default URL, LLM_ROUTER_LOCAL_MODEL_URL, ``base_url``),
    ``size`` summed (it includes the KV cache); target size from ``/api/tags`` (or ``/api/show``) times
    LLM_ROUTER_MEM_HEADROOM; machine memory from sysctl/vm_stat via ``hardware``. Both must pass:
    (a) resident + target <= total - reserve, (b) target <= free - 2 GiB. A target already resident on that
    server at that window is always allowed. The target is weights + context window (KV(num_ctx) + state +
    compute, from /api/show model_info, a per-model registry, or a conservative default), never less than
    weights x headroom, and must also fit the Metal working-set cap. Unreadable state fails open with a ``failopen`` row. Pressure with
    ``LLM_ROUTER_MEM_ON_PRESSURE=evict`` unloads the largest other resident model (never the allowed model) and
    re-checks (polling up to ~3 s, since Ollama defers an unload until in-flight requests finish). With
    LLM_ROUTER_LOCAL_MODEL set the default is evict: any non-allowed model on ANY known server may be unloaded.
    An approved load is reserved for 120 s (or until /api/ps shows it) so that two different models checked
    back to back are summed rather than each judged against the same snapshot. The memory budget is skipped
    for a non-loopback server (its RAM is not this machine's). Cached 5 s per (model, base_url). ``deadline_s`` is accepted for call-site compatibility and
    never skips the check. ``LLM_ROUTER_MEM_GUARD=off`` disables the memory budget and eviction (the one-model rule
    of LLM_ROUTER_LOCAL_MODEL stays).
    """
    allowed = get_local_model()
    model = model or allowed
    on = mem_guard_enabled()
    if not model or (not on and not allowed):     # off keeps only the one-model rule (#864) of LLM_ROUTER_LOCAL_MODEL
        return
    name = _bare(model)
    url = (base_url or url_for_model(model)).rstrip("/")
    key = (name, url, allowed, on, _max_other_vram_gb(), _headroom(), _reserve_gib(0.0), _evict_mode(),
           tuple(_servers(url)))
    now = time.monotonic()
    _prune(now)
    hit = _PS_CACHE.get(key)
    if hit is not None:
        if hit[1] is not None:
            raise _clone(hit[1])
        return
    result = _verdict(name, url, allowed, purpose, budget=on)
    evicted = False
    if result is not None and on and _evict_mode() and result[1] is not None and _unload(*result[1]):
        evicted = True
        for _ in range(_EVICT_POLLS):      # Ollama defers an unload until in-flight requests finish: poll briefly
            time.sleep(0.5)
            try:
                from llm_router import hardware
                hardware.clear_cache()
            except Exception:  # noqa: BLE001
                pass
            result = _verdict(name, url, allowed, purpose, budget=on)
            if result is None:
                break
    refusal = result[0] if result is not None else None
    if refusal is None or not evicted:          # a refusal that follows our own unload may just be the unload lagging
        with _LOCK:
            _PS_CACHE[key] = (now, refusal)
    if refusal is not None:
        raise refusal


def check_resident(model: str | None = None, *, deadline_s: float | None = None) -> None:
    """Back-compat entry point for the call sites that only name a model: :func:`ensure_fits` on the server
    that model is sent to."""
    ensure_fits(model, None, "check_resident", deadline_s=deadline_s)


_XML_FUNC_RE = re.compile(
    r"<function[=\s]+([A-Za-z_][A-Za-z0-9_]*)\s*>(.*?)(?=</function>|<function[=\s]|\Z)", re.DOTALL)
_XML_PARAM_RE = re.compile(
    r"<parameter[=\s]+([A-Za-z_][A-Za-z0-9_]*)\s*>(.*?)(?=</parameter>|<parameter[=\s]|\Z)", re.DOTALL)


def repair_xml_toolcalls(content: str, known: set[str]) -> list[dict]:
    """Recover tool calls emitted in Qwen's ``<function=name>`` XML dialect.

    Values stay stripped strings (never coerced: a path like ``2.0.0`` must not
    become a number). Unknown tool names are dropped.
    """
    if not content or "<function" not in content:
        return []
    calls: list[dict] = []
    for m in _XML_FUNC_RE.finditer(content):
        name = m.group(1)
        if name not in known:
            continue
        args = {k: v.strip() for k, v in _XML_PARAM_RE.findall(m.group(2))}
        if args:
            calls.append({"function": {"name": name, "arguments": args}})
    return calls


def repair_toolcalls(content: str, known: set[str]) -> list[dict]:
    """Recover tool calls a model emitted as TEXT (JSON object or XML) instead of
    the structured ``tool_calls`` field."""
    if not content:
        return []
    names = "|".join(sorted(known))
    text_re = re.compile(r'\{\s*"name"\s*:\s*"(?:' + names + r')"', re.IGNORECASE)
    if not text_re.search(content):
        return repair_xml_toolcalls(content, known)
    calls: list[dict] = []
    for m in text_re.finditer(content):
        start = content.rfind("{", 0, m.start() + 1)
        depth, i, in_str, esc = 0, start, False, False
        while i < len(content):
            c = content[i]
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = not in_str
            elif not in_str and c == "{":
                depth += 1
            elif not in_str and c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(content[start:i + 1])
                        args = obj.get("arguments") or obj.get("parameters") or {}
                        if isinstance(args, str):
                            args = json.loads(args)
                        calls.append({"function": {"name": obj["name"], "arguments": args}})
                    except (ValueError, KeyError):
                        pass
                    break
            i += 1
    return calls


def tool_call_schema(names: list[str]) -> dict:
    """JSON Schema for one tool call (Ollama `format`), derived from the live tools."""
    return {"type": "object",
            "properties": {"tool": {"type": "string", "enum": list(names)}, "arguments": {"type": "object"}},
            "required": ["tool", "arguments"]}


def parse_constrained_call(content: str, known: set[str]) -> list[dict]:
    """Parse a grammar-constrained reply `{"tool": ..., "arguments": {...}}`."""
    try:
        obj = json.loads(content)
    except (ValueError, TypeError):
        return []
    if not isinstance(obj, dict):
        return []
    name = obj.get("tool")
    args = obj.get("arguments") if isinstance(obj.get("arguments"), dict) else {}
    if not isinstance(name, str) or name not in known:
        return []
    return [{"function": {"name": name, "arguments": args}}]


# ── the router-owned loop's adapter ──────────────────────────────────────────

class AdapterError(Exception):
    """The model call failed. `retryable` marks a transient server-side condition (an empty
    reply with done:false, which a busy or restarting Ollama returns), not a model decision."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class AdapterReply:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    message: dict = field(default_factory=dict)
    tokens_in: int | None = None
    tokens_out: int | None = None
    ms: int = 0
    repaired: bool = False


class OllamaAdapter:
    """Chat against Ollama's native tool API, with the text/XML repair shim."""

    name = "ollama"

    def __init__(self, model: str, *, base_url: str | None = None, num_ctx: int | None = None,
                 temperature: float | None = None, constrained: bool = False, think: bool = False):
        self.model = model
        self.base_url = validated_ollama_url(base_url) if base_url else get_ollama_url()
        # M3.4: one window for every local call: the env-aware num_ctx(model) (override, else the
        # table); an explicit value still wins. None = accept the server's default.
        self.num_ctx = _env_num_ctx(model) if num_ctx is None else num_ctx
        self.temperature = agent_temperature() if temperature is None else temperature
        self.constrained = constrained
        self.think = think

    def chat(self, messages: list[dict], tools: list[dict], *, timeout_s: float) -> AdapterReply:
        names = {t["function"]["name"] for t in tools}
        payload = {
            "model": self.model, "messages": messages, "tools": tools, "stream": False,
            "think": self.think,
            "options": {"temperature": self.temperature},
        }
        if self.num_ctx is not None:
            payload["options"]["num_ctx"] = self.num_ctx
        if self.constrained:
            payload["format"] = tool_call_schema(sorted(names))
        try:
            from llm_router.pack_overflow import PayloadPack
            pack = PayloadPack(payload, num_ctx=self.num_ctx, base_url=self.base_url,
                               model=self.model, site="toolkit.adapters.ollama")
            payload = pack.trim_to()
            check_overflow(payload, num_ctx=pack.num_ctx, base_url=self.base_url, model=pack.model,
                           site="toolkit.adapters.ollama")
        except ContextOverflow as exc:
            raise AdapterError(f"context_overflow: {exc}") from exc
        body = json.dumps(payload).encode()
        req = urllib.request.Request(f"{self.base_url}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        t0 = time.monotonic()
        try:
            with local_privacy.open_url(req, base_url=self.base_url, timeout=max(1.0, timeout_s)) as resp:
                result = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"llm_unreachable: {type(exc).__name__}: {exc}") from exc
        if result.get("error"):
            raise AdapterError(f"ollama_error: {str(result['error'])[:200]}", retryable=True)
        msg = result.get("message", {}) or {}
        content = msg.get("content", "") or ""
        calls = list(msg.get("tool_calls") or [])
        if not content and not calls and not msg.get("thinking") and result.get("done") is not True:
            raise AdapterError("empty_reply: ollama returned no message and done=false", retryable=True)
        repaired = False
        if not calls and content:
            calls = parse_constrained_call(content, names) if self.constrained else []
            if not calls:
                calls = repair_toolcalls(content, names)
            repaired = bool(calls)
        tin, tout = result.get("prompt_eval_count"), result.get("eval_count")
        return AdapterReply(content=content, tool_calls=calls, message=msg,
                            tokens_in=tin if isinstance(tin, int) else None,
                            tokens_out=tout if isinstance(tout, int) else None,
                            ms=int((time.monotonic() - t0) * 1000), repaired=repaired)


__all__ = [
    "AdapterError", "AdapterReply", "OllamaAdapter", "agent_temperature",
    "constrained_decoding_enabled", "default_num_ctx", "estimate_payload_tokens", "get_local_model_url", "get_ollama_url", "url_for_model", "get_local_model",
    "resolve_main_model", "check_resident", "main_url", "record_refusal", "last_refusal", "LocalModelConflict",
    "num_ctx", "parse_constrained_call", "repair_toolcalls", "repair_xml_toolcalls",
    "tool_call_schema", "validated_ollama_url",
]
