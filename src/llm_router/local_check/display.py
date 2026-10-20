"""Display surfaces for ``local-check`` (LC-7): status / doctor lines, the statusline segment and the first-run nudge.

Everything here is a read of files the router already keeps (``local_eligibility.json``, the model-discovery
cache ``discovery.json``, the optional ``local_routing.json``). Nothing here makes an HTTP call, runs a model or
prompts: the digests come from the TTL inventory cache, never from a live probe. A missing or unreadable file
means "unknown", which is shown as no line at all, never as a failure.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from llm_router import paths
from llm_router.resolver import eligibility_store as ES
from llm_router.resolver.eligibility_store import LCLine

#: Digests older than this in the inventory cache are not trusted to say "unchanged".
DIGEST_MAX_AGE_S = 7 * 86400
#: At most this many not-measured models are listed (the rest are counted).
MAX_UNMEASURED = 3
SEGMENT_OK, SEGMENT_NP = "local ok", "local n/p"


def _discovery() -> tuple[dict, float]:
    """(models dict, cache age in seconds) from ``discovery.json``; ({}, inf) when unusable."""
    from llm_router import model_discovery

    try:
        data = json.loads(model_discovery._cache_path().read_text())
        models = data.get("models")
        return (models if isinstance(models, dict) else {}), time.time() - float(data.get("cached_at", 0))
    except Exception:  # noqa: BLE001 -- no cache is "unknown"
        return {}, float("inf")


def inventory() -> tuple[list[str], dict[str, str]]:
    """(installed local model names, name -> digest) from the discovery cache. No network.

    Digests are prefixed ``sha256:`` like the runtime adapters store them, and are returned only
    while the cache is younger than :data:`DIGEST_MAX_AGE_S`.
    """
    from llm_router import model_discovery

    models, age = _discovery()
    names: list[str] = []
    digests: dict[str, str] = {}
    for mid, meta in models.items():
        if not isinstance(mid, str) or not mid.startswith("ollama/") or not model_discovery._is_completion_model(mid):
            continue
        name = mid.removeprefix("ollama/")
        names.append(name)
        d = meta.get("digest") if isinstance(meta, dict) else None
        if isinstance(d, str) and d and age <= DIGEST_MAX_AGE_S:
            digests[name] = d if d.startswith("sha256:") else "sha256:" + d
    return sorted(names), digests


def routing_state() -> dict[tuple[str, str], bool]:
    """(model, job) -> enabled from ``local_routing.json`` (LC-8 owns the writer); {} when absent.

    Accepted shapes per job: a bool or ``{"enabled": bool}``. Anything else is ignored (shown ``n/a``).
    """
    try:
        data = json.loads(paths.state_path("local_routing.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[tuple[str, str], bool] = {}
    if isinstance(data, dict):
        for model, jobs in data.items():
            if not isinstance(jobs, dict):
                continue
            for job, v in jobs.items():
                v = v.get("enabled") if isinstance(v, dict) else v
                if isinstance(v, bool):
                    out[(model, job)] = v
    return out


def lines(*, path: Path | None = None, now: float | None = None) -> list[LCLine]:
    """The status/doctor lines: pre-registered text_job lines, then local-check lines (kind ``prereg`` for the former)."""
    names, digests = inventory()
    pre = [LCLine(t, "prereg") for t in ES.text_job_lines(path=path, current_digests=digests, now=now, include_local=False)]
    shown = ES.local_check_lines(path=path, current_digests=digests, now=now, routing=routing_state(), unmeasured=tuple(names))
    measured = [ln for ln in shown if ln.kind != "not_measured"]
    unmeasured = [ln for ln in shown if ln.kind == "not_measured"]
    if len(unmeasured) > MAX_UNMEASURED:
        extra = len(unmeasured) - MAX_UNMEASURED
        unmeasured = unmeasured[:MAX_UNMEASURED] + [LCLine(f"local-check: +{extra} more local models not measured "
                                                           f"({ES._NOT_MEASURED})", "not_measured")]
    return pre + measured + unmeasured


def statusline_segment(*, path: Path | None = None, now: float | None = None) -> str:
    """``local ok`` when a fresh lane passed, ``local n/p`` when fresh lanes exist but none passed, else ``""``.

    A stale lane and an unmeasured model produce nothing (nothing is measured about the model now installed).
    Called from the background refresher; the status line itself only reads the cached string.
    """
    _, digests = inventory()
    kinds = {ln.kind for ln in ES.local_check_lines(path=path, current_digests=digests, now=now)}
    if "passed" in kinds:
        return SEGMENT_OK
    return SEGMENT_NP if "not_passed" in kinds else ""


# ---- first-run nudge ---------------------------------------------------------------------------

def _nudge_path() -> Path:
    return paths.state_path("local_check", "nudge.json")


def _bank_version() -> str | None:
    """The shipped bank's version from its manifest (one small read; the bank itself is not loaded)."""
    try:
        manifest = Path(__file__).with_name("data") / "manifest.json"
        v = json.loads(manifest.read_text(encoding="utf-8")).get("bank_version")
        return str(v) if v is not None else None
    except (OSError, ValueError, AttributeError):
        return None


def _nudged() -> list[str]:
    try:
        v = json.loads(_nudge_path().read_text(encoding="utf-8")).get("bank_versions")
        return [str(x) for x in v] if isinstance(v, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def maybe_nudge(*, path: Path | None = None, now: float | None = None) -> str | None:
    """One non-blocking line, or None. Never prompts and never runs anything.

    Returned (and recorded as shown) only when a local model is installed, no local-check lane exists for any
    model, and this bank version has not been nudged before. The record is a small 0600 file under the router
    home; if it cannot be written the line is not shown (so a read-only home never repeats it every run).
    """
    version = _bank_version()
    names, digests = inventory()
    if not names or version is None or version in _nudged():
        return None
    if any(ln.kind != "not_measured" for ln in ES.local_check_lines(path=path, current_digests=digests, now=now)):
        return None
    target = _nudge_path()
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(tmp, "w", encoding="utf-8", opener=paths.private_opener) as fh:
            json.dump({"bank_versions": [*_nudged(), version]}, fh)
        os.replace(tmp, target)
    except (OSError, ValueError):
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return (f"tip: local model found ({names[0]}) but not measured. "
            f"`{ES._NOT_MEASURED}` checks which text jobs it can do (optional; nothing runs until you do).")

