"""What Ollama actually has, right now — one implementation, not three.

This module exists because there were two answers to "which local models are
available" and the hot path used the worse one.

`auto-route.py` had a careful resolver (env override, then OLLAMA_BUDGET_MODELS,
then OLLAMA_MODELS, then ~/.llm-router/discovery.json) evaluated ONCE at import.
`chain_builder._ollama_models()` — the function that actually builds the draft
chain — had its own version that stopped at the env vars and fell back to a
hardcoded "qwen3.5:latest".

The consequence was silent and lasted weeks: an operator who set
LLM_ROUTER_ENSEMBLE_PRIMARY=ollama/qwen3.8:latest got qwen3.5 on every one of a
day's 69 draft calls, because that variable is read by `ensemble.py` and no
chain builder has ever consulted it.

Staleness was the second half. `discovery.json` is written once by
`llm-router discover` and never refreshed, so a model pulled today is invisible
until something rewrites the file. On the machine this was found on, the cache
was 14 hours old and already missing an installed model.

So: one function, a TTL, and a live query when the cache is stale.

Fallback is deliberately EMPTY rather than a guessed model name. A hardcoded
default that is not installed produces a chain that cannot run and an error that
blames the model; an empty list produces "no free-tier model available", which
is both true and already handled.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

DEFAULT_TTL_HOURS = 12.0
_PROBE_TIMEOUT_S = 2.0

# Ollama serves embedding models from the same /api/tags list as chat models,
# and they cannot answer a completion. Reading the list live — which is the
# point of this module — therefore surfaces models that would fail the moment
# they were chosen, in a way that looks like a model failure rather than a
# selection bug. `nomic-embed-text` was installed on the machine this was
# written for, and went straight into the draft chain until this filter existed.
_EMBEDDING_MARKERS = ("embed", "bge-", "gte-", "e5-", "all-minilm")


def _is_completion_model(name: str) -> bool:
    low = name.lower()
    return not any(marker in low for marker in _EMBEDDING_MARKERS)


def _cache_path() -> Path:
    base = os.environ.get("LLM_ROUTER_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".llm-router"
    return root / "discovery.json"


def _ollama_url() -> str:
    return os.environ.get("OLLAMA_HOST", "").strip() or "http://127.0.0.1:11434"


def _ttl_hours() -> float:
    raw = os.environ.get("LLM_ROUTER_DISCOVERY_TTL_HOURS", "").strip()
    try:
        value = float(raw)
        return value if value > 0 else DEFAULT_TTL_HOURS
    except ValueError:
        return DEFAULT_TTL_HOURS


def _from_env() -> list[str]:
    """Explicit operator intent always wins, in the documented order.

    Each variable is read as a LITERAL rather than through a loop over a tuple
    of names. The repo's env-registry test finds reads by AST and only sees
    literal arguments, so a loop makes a variable that IS honoured look like a
    registry phantom — `tool_intercept._env_override` carries the same note for
    the same reason.
    """
    explicit = os.environ.get("LLM_ROUTER_OLLAMA_MODEL", "").strip()
    if explicit:
        return [explicit]

    budget = os.environ.get("OLLAMA_BUDGET_MODELS", "").strip()
    if budget:
        models = [m.strip() for m in budget.split(",") if m.strip()]
        if models:
            return models

    listed = os.environ.get("OLLAMA_MODELS", "").strip()
    if listed:
        models = [m.strip() for m in listed.split(",") if m.strip()]
        if models:
            return models

    return []


def _read_cache() -> tuple[list[str], float]:
    """(models, age_hours). Age is +inf when there is no usable cache."""
    try:
        data = json.loads(_cache_path().read_text())
        models = [mid.removeprefix("ollama/") for mid in data.get("models", {})
                  if mid.startswith("ollama/") and _is_completion_model(mid)]
        age = (time.time() - float(data.get("cached_at", 0))) / 3600.0
        return models, age
    except Exception:                                        # noqa: BLE001
        return [], float("inf")


def probe_ollama() -> list[str]:
    """Ask Ollama what it has. Empty list on any failure — never raises."""
    try:
        req = urllib.request.Request(f"{_ollama_url().rstrip('/')}/api/tags")
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
        return [m["name"] for m in data.get("models", [])
                if m.get("name") and _is_completion_model(m["name"])]
    except Exception:                                        # noqa: BLE001
        return []


def _write_cache(models: list[str]) -> None:
    """Refresh the cache, preserving any richer per-model metadata it holds."""
    try:
        path = _cache_path()
        try:
            data = json.loads(path.read_text())
        except Exception:                                    # noqa: BLE001
            data = {}
        existing = data.get("models") or {}
        merged = {}
        for name in models:
            key = f"ollama/{name}"
            merged[key] = existing.get(key, {
                "model_id": key, "provider": "ollama", "provider_tier": "local",
            })
        data["models"] = merged
        data["cached_at"] = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except Exception:                                        # noqa: BLE001
        pass


def available_ollama_models(*, allow_probe: bool = True) -> list[str]:
    """Ollama models usable right now, cheapest source first.

    env override → fresh cache → live probe (refreshing the cache) → stale cache
    → empty.

    A stale cache beats nothing: if Ollama is momentarily unreachable, yesterday's
    model list is a better guess than an empty chain. But it is only reached
    AFTER a probe has been tried, so a model pulled today is picked up today.
    """
    from_env = _from_env()
    if from_env:
        return from_env

    cached, age = _read_cache()
    if cached and age <= _ttl_hours():
        return cached

    if allow_probe:
        live = probe_ollama()
        if live:
            _write_cache(live)
            return live

    return cached          # stale, or [] when there is no cache at all

def first_installed(prefer: tuple[str, ...] = ()) -> str | None:
    """The best local model that is ACTUALLY installed, or None.

    Several call sites used to carry a hardcoded default — `qwen3.5:latest` for
    the session warm-up, `qwen2.5:7b` for the Playwright compressor and the ReAct
    adapter, `qwen2.5-coder:7b` for the librarian. None of those was installed on
    the machine running them, so each call 404'd and degraded silently: the
    warm-up warmed nothing, every session.

    Substituting a different hardcoded name would repeat the mistake the first
    time the inventory changes. `prefer` expresses a preference among models that
    exist; it never invents one.
    """
    try:
        installed = available_ollama_models()
    except Exception:                                        # noqa: BLE001
        return None
    if not installed:
        return None
    for want in prefer:
        for have in installed:
            if have == want or have.split(":")[0] == want.split(":")[0]:
                return have
    return installed[0]
