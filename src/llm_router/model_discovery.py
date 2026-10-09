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
from pathlib import Path

from llm_router.lazy_urllib import urllib  # urllib.request on first use, not at import

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

# ── Hook path: never wait on the network ─────────────────────────────────────
#
# ``auto-route.py`` runs ``available_ollama_models()`` while it is being imported,
# before any prompt is looked at. With a stale or missing cache that was a live HTTP
# probe on the host's critical path: up to ``_PROBE_TIMEOUT_S`` seconds when Ollama is
# busy, and a failed connect on EVERY prompt when it is down (the failed probe never
# refreshes the cache). ``available_ollama_models_nowait`` answers from env / cache
# only (stale included: the same "stale beats nothing" rule) and starts the probe in a
# detached child, throttled so a dead Ollama costs one child per window, not one per
# prompt. The next prompt after the child finishes sees the fresh list.

PROBE_RETRY_S = 300.0


def _attempt_stamp_path() -> Path:
    return _cache_path().with_name("discovery.probe_attempt")


def _claim_probe_attempt(now: float | None = None) -> bool:
    """True once per ``PROBE_RETRY_S``: stamps the attempt before the child starts, so
    two prompts in the same instant start one probe, and a failed probe is not retried
    on every prompt."""
    now = time.time() if now is None else now
    path = _attempt_stamp_path()
    try:
        try:
            last = float(path.read_text().strip() or 0)
        except (OSError, ValueError):
            last = 0.0
        if -1.0 <= now - last < PROBE_RETRY_S:  # -1: the stamp is written to 3 decimals
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{now:.3f}")
        return True
    except OSError:
        return False  # cannot record the attempt: do not spawn unthrottled


def _spawn_probe_child() -> bool:
    """``python -m llm_router.model_discovery`` detached; never raises, never blocks."""
    import subprocess
    import sys

    try:
        # Pin the child to the state directory the parent resolved, so a parent running
        # under an overridden home refreshes that cache and never another one.
        env = dict(os.environ, LLM_ROUTER_HOME=str(_cache_path().parent))
        subprocess.Popen(
            [sys.executable, "-m", "llm_router.model_discovery"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=env,
        )
        return True
    except Exception:                                        # noqa: BLE001
        return False


def available_ollama_models_nowait(*, refresh: bool = True) -> list[str]:
    """``available_ollama_models`` without the synchronous probe.

    env override -> fresh cache -> stale cache -> []. When the cache is stale or
    missing and ``refresh`` is true, a throttled detached child refreshes it for the
    next caller.
    """
    from_env = _from_env()
    if from_env:
        return from_env
    cached, age = _read_cache()
    if cached and age <= _ttl_hours():
        return cached
    if refresh and _claim_probe_attempt():
        _spawn_probe_child()
    return cached


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


if __name__ == "__main__":  # the detached refresher started by available_ollama_models_nowait
    available_ollama_models()
