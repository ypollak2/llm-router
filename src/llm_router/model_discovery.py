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
from llm_router.local_models import serves_generate

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
    if any(marker in low for marker in _EMBEDDING_MARKERS):
        return False
    # HOOK-DRAFT-1: a decision-only model (nimble:9b, HTTP 400 on generate) or a
    # classifier alias reached the hook draft chain and first_installed() from here.
    return serves_generate(low)


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
    ensure_grandfather([f"ollama/{m}" for m in models])   # before discovery.json exists on a fresh install
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


# ── Promotion gate (P0.15, DISC-PROMOTE-1) ───────────────────────────────────
#
# A model that shows up in the discovery cache or in a live /api/tags answer used
# to lead every cheap chain the moment it appeared, with nothing having measured
# it. The gate sorts every injected local model into two groups:
#
#   front  the user named it (env, config, routing.yaml pin) or it was already
#          installed when this version first ran (grandfathered). Placed as today.
#   tail   everything else. D-60c = A: appended after the LAST entry of a
#          non-critical chain (simple query/generate/analyze) and absent from
#          every other chain. Graded models (16.3) will join `front`; until then
#          "validated" means configured or grandfathered.
#
# Matching is by id only. `LLM_ROUTER_PROMOTE_GATE=off` restores the old behaviour.

GATE_ENV = "LLM_ROUTER_PROMOTE_GATE"
_GATE_OFF_VALUES = frozenset({"off", "0", "false", "no"})
_LOCAL_ID_PREFIXES = ("ollama/", "openai_compat/")
_PRIOR_STATE_FILES = ("discovery.json", "profile.yaml", "usage.db")
_gate_off_logged = False


def gate_enabled() -> bool:
    return os.environ.get("LLM_ROUTER_PROMOTE_GATE", "").strip().lower() not in _GATE_OFF_VALUES


def _id_forms(name: str) -> set[str]:
    """Every full id a configured name can mean. A bare name is ambiguous between
    the two local providers, so it covers both; a name with no tag also covers
    ``:latest`` because that is what Ollama lists it as."""
    name = name.strip()
    if not name:
        return set()
    bases = {name} if name.startswith(_LOCAL_ID_PREFIXES) else {f"ollama/{name}", f"openai_compat/{name}"}
    out = set(bases)
    for b in bases:
        if ":" not in b.rsplit("/", 1)[-1]:
            out.add(f"{b}:latest")
    return out


def _env_configured_ids() -> set[str]:
    """Models the operator named in the environment. Literal reads, as in ``_from_env``."""
    out: set[str] = set()
    out |= _id_forms(os.environ.get("LLM_ROUTER_OLLAMA_MODEL", ""))
    for raw in (os.environ.get("OLLAMA_BUDGET_MODELS", ""), os.environ.get("OLLAMA_MODELS", "")):
        for name in raw.split(","):
            out |= _id_forms(name)
    return out


def _routing_yaml_pin_ids() -> set[str]:
    """Models pinned in the user's routing.yaml (per-task ``model`` and ``agentic_model``)."""
    try:
        from llm_router.repo_config import load_user_config

        cfg = load_user_config()
    except Exception as exc:                                 # noqa: BLE001
        from llm_router import failopen

        failopen.record("CHZ-FO-PROMOTE-GATE-ROUTING-YAML", exc)
        return set()
    names = [o.model for o in cfg.routing.values() if o.model]
    if cfg.agentic_model:
        names.append(cfg.agentic_model)
    return set().union(*(_id_forms(str(n)) for n in names)) if names else set()


def _grandfather_path() -> Path:
    return _cache_path().with_name("grandfathered_models.json")


def _grandfather_marker_path() -> Path:
    return _cache_path().with_name("grandfathered_models.init")


def ensure_grandfather(current_ids: "list[str] | tuple[str, ...]" = ()) -> None:
    """Write ``grandfathered_models.json`` once, at the first run of this version (D-62 = A).

    Upgrade (prior llm-router state exists: discovery.json, profile.yaml or usage.db):
    the file lists the models already installed (the old discovery cache plus
    ``current_ids``). Fresh install: the file is written with an empty ``ids``.
    The file is never rewritten, and is not recreated if deleted: the
    ``grandfathered_models.init`` marker records that the one-time write happened.

    Call this BEFORE anything writes discovery.json, or a fresh install would see
    its own first probe as prior state.
    """
    path, marker = _grandfather_path(), _grandfather_marker_path()
    if marker.exists() or path.exists():
        return
    home = path.parent
    ids: list[str] = []
    if any((home / name).exists() for name in _PRIOR_STATE_FILES):
        cached, _age = _read_cache()
        found = {f"ollama/{m}" for m in cached}
        for m in current_ids:
            found |= {m} if m.startswith(_LOCAL_ID_PREFIXES) else {f"ollama/{m}"}
        ids = sorted(found)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        home.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"at": time.time(), "ids": ids}, indent=2))
        os.link(tmp, path)          # exclusive create: a racing first run loses cleanly
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        return
    except OSError as exc:
        from llm_router import failopen

        failopen.record("CHZ-FO-PROMOTE-GATE-GRANDFATHER-WRITE", exc)
    finally:
        tmp.unlink(missing_ok=True)


_grandfather_cache: tuple[str, int, frozenset[str]] | None = None


def grandfathered_ids() -> frozenset[str]:
    """Ids in ``grandfathered_models.json``; empty when the file is absent or unreadable."""
    global _grandfather_cache
    path = _grandfather_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return frozenset()
    if _grandfather_cache and _grandfather_cache[:2] == (str(path), mtime):
        return _grandfather_cache[2]
    try:
        raw = json.loads(path.read_text()).get("ids", [])
        ids = frozenset(i for name in raw if isinstance(name, str) for i in _id_forms(name))
    except (OSError, ValueError, AttributeError):
        return frozenset()
    _grandfather_cache = (str(path), mtime, ids)
    return ids


def grandfather_status() -> str:
    """For `llm-router doctor`: ``ok``, ``missing`` (deleted after the one-time write) or ``unwritten``."""
    if _grandfather_path().exists():
        return "ok"
    return "missing" if _grandfather_marker_path().exists() else "unwritten"


def order_for_chain(
    models: "list[str]",
    task_class: str,
    *,
    critical: bool,
    configured: "tuple[str, ...] | list[str] | set[str]" = (),
    read_routing_yaml: bool = True,
) -> "tuple[list[str], list[str]]":
    """Split injected local model ids into ``(front, tail)`` (P0.15, D-60c = A).

    ``front``  configured or grandfathered models, in the order given.
    ``tail``   every other model on a non-critical chain; ``[]`` on a critical one
               (the models are absent from it). The caller appends ``tail`` after
               the chain's last entry, after every reorder.

    ``configured`` carries names the caller knows about (``Config`` fields, the
    pins of an already-loaded ``RepoConfig``); the environment is always read.
    ``read_routing_yaml`` is False for callers that pass the loaded pins.
    ``task_class`` is only used for the debug log until 16.3 adds per-class grades.

    With ``LLM_ROUTER_PROMOTE_GATE=off`` this returns ``(models, [])``: today's chain.
    """
    global _gate_off_logged
    models = list(models)
    if not gate_enabled():
        if not _gate_off_logged:
            _gate_off_logged = True
            import logging

            logging.getLogger("llm_router.model_discovery").warning(
                "%s=off: unvalidated local models are injected at the front of chains again", GATE_ENV)
        return models, []
    if not models:
        return [], []
    ensure_grandfather([m for m in models if m.startswith(_LOCAL_ID_PREFIXES)])
    keep = set(grandfathered_ids()) | _env_configured_ids()
    for name in configured:
        keep |= _id_forms(str(name))
    if read_routing_yaml and any(m not in keep for m in models):
        keep |= _routing_yaml_pin_ids()
    front = [m for m in models if m in keep]
    unvalidated = [m for m in models if m not in keep]
    if unvalidated:
        import logging

        logging.getLogger("llm_router.model_discovery").debug(
            "promote gate (%s, %s chain): %s unvalidated %s",
            task_class, "critical" if critical else "non-critical",
            "dropped" if critical else "tail", unvalidated)
    return front, ([] if critical else unvalidated)


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
