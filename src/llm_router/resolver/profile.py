"""Versioned capability profile: what ``llm-router calibrate`` measured per model.

Stored at ``state_path("capability_profile.json")``. The file is a record of
measurements on THIS user's setup, not a ranking: a tier ceiling here means "this
model passed the probes that qualify for that tier, on this date, at these
sizes". Short probes can establish EASY and MEDIUM. They cannot establish
FRONTIER, so a FRONTIER ceiling is only ever granted when a measured MEDIUM is
combined with a curated-registry premium class, and is labelled ``prior``.

Reference priors from the 2026-10-01 routing experiment (20 tasks each; see
docs/model-resolver.md): qwen3.6 10/20, Codex 15/20, Claude 15/20, qwen3-coder
4/20. They are shown in docs as context for why a local model must be measured
before it is trusted. They are deliberately NOT encoded here.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from llm_router import paths
from llm_router.resolver.types import (
    CAP_NO,
    CAP_YES,
    SRC_MEASURED,
    Cap,
    ModelEntry,
    Tier,
)

PROFILE_VERSION = 1
PROBE_SUITE_VERSION = "1"

#: Smallest recall size that counts toward MEDIUM.
MEDIUM_RECALL_TOKENS = 8000

PROBE_NAMES = ("json", "edit", "tool_call", "vision", "long_context")


@dataclass
class ProbeResult:
    ok: bool | None            # None: not run / not probeable on this route
    detail: str = ""
    latency_s: float | None = None
    size_tokens: int | None = None   # long_context: the size that was tested


@dataclass
class ModelProfile:
    model_id: str
    measured_at: float
    suite_version: str = PROBE_SUITE_VERSION
    reachable: bool = False
    probes: dict[str, ProbeResult] = field(default_factory=dict)
    tier_ceiling: str | None = None
    tier_detail: str = ""
    recall_tokens: int | None = None


def profile_path() -> Path:
    return paths.state_path("capability_profile.json")


def tier_from_probes(
    probes: dict[str, ProbeResult], *, claims_tools: bool = True,
) -> tuple[str | None, str]:
    """(ceiling, why). ``None`` means it failed what EASY requires.

    EASY needs valid JSON and a verified edit. MEDIUM adds a tool round-trip
    (when the model is meant to use tools) and recall at >= MEDIUM_RECALL_TOKENS.
    A probe that was not run is not a pass.
    """
    def ok(name: str) -> bool:
        p = probes.get(name)
        return bool(p and p.ok is True)

    if not ok("json"):
        return None, "failed the valid-JSON probe"
    if not ok("edit"):
        return None, "failed the verified-edit probe"
    missing: list[str] = []
    if claims_tools and not ok("tool_call"):
        missing.append("tool round-trip")
    lc = probes.get("long_context")
    recall_ok = bool(lc and lc.ok is True and lc.size_tokens is not None
                     and lc.size_tokens >= MEDIUM_RECALL_TOKENS)
    if not recall_ok:
        missing.append(f"recall at >= {MEDIUM_RECALL_TOKENS} tokens")
    if missing:
        return Tier.EASY.value, "EASY: json + edit passed; MEDIUM needs " + ", ".join(missing)
    return Tier.MEDIUM.value, (
        "MEDIUM: json, edit, tool round-trip and recall passed. Short probes cannot "
        "establish FRONTIER."
    )


def build_profile(model_id: str, probes: dict[str, ProbeResult], *, reachable: bool,
                  claims_tools: bool = True, now: float | None = None) -> ModelProfile:
    ceiling, detail = tier_from_probes(probes, claims_tools=claims_tools)
    lc = probes.get("long_context")
    recall = lc.size_tokens if (lc and lc.ok is True) else None
    return ModelProfile(
        model_id=model_id, measured_at=time.time() if now is None else now,
        reachable=reachable, probes=probes, tier_ceiling=ceiling,
        tier_detail=detail, recall_tokens=recall,
    )


# ------------------------------------------------------------------ persistence

def load_profiles(path: Path | None = None) -> dict[str, ModelProfile]:
    """All stored profiles. A missing, unreadable or wrong-version file is
    ``{}`` (nothing measured), never a guess."""
    p = path or profile_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != PROFILE_VERSION:
        return {}
    out: dict[str, ModelProfile] = {}
    for mid, d in (raw.get("models") or {}).items():
        try:
            probes = {k: ProbeResult(**v) for k, v in (d.get("probes") or {}).items()}
            out[mid] = ModelProfile(
                model_id=mid, measured_at=float(d["measured_at"]),
                suite_version=str(d.get("suite_version", "")),
                reachable=bool(d.get("reachable", False)), probes=probes,
                tier_ceiling=d.get("tier_ceiling"), tier_detail=d.get("tier_detail", ""),
                recall_tokens=d.get("recall_tokens"),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def save_profiles(updates: dict[str, ModelProfile], path: Path | None = None) -> Path:
    """Merge ``updates`` into the stored file (atomic replace)."""
    p = path or profile_path()
    merged = load_profiles(p)
    merged.update(updates)
    payload: dict[str, Any] = {
        "version": PROFILE_VERSION,
        "suite_version": PROBE_SUITE_VERSION,
        "generated_at": time.time(),
        "note": "Measured per-model capability on this setup. Not a global ranking.",
        "models": {mid: asdict(mp) for mid, mp in merged.items()},
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".profile-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


# ------------------------------------------------------------------ merge

def merge_tier(prior: str | None, measured: ModelProfile | None) -> tuple[str | None, str, str]:
    """(ceiling, basis, detail) from a registry prior and an optional measurement."""
    if measured is None:
        if prior:
            return prior, "prior", "registry class only; not measured here (run `llm-router calibrate`)"
        return None, "unmeasured", "not measured (run `llm-router calibrate`)"
    m = measured.tier_ceiling
    if m is None:
        return None, "measured", measured.tier_detail
    if m == Tier.MEDIUM.value and prior == Tier.FRONTIER.value:
        return (Tier.FRONTIER.value, "prior",
                "MEDIUM measured; FRONTIER rests on the registry's premium class (short probes cannot confirm it)")
    return m, "measured", measured.tier_detail


def apply_profile(entry: ModelEntry, mp: ModelProfile | None, *,
                  prior_tier: str | None) -> ModelEntry:
    """Overlay measured capabilities, verified reachability and the tier ceiling."""
    caps = dict(entry.capabilities)
    verified = entry.path_verified
    detail = entry.path_detail
    if mp is not None:
        for probe, cap in (("tool_call", "tools"), ("vision", "vision"),
                           ("json", "json"), ("edit", "edit")):
            p = mp.probes.get(probe)
            if p is not None and p.ok is not None:
                caps[cap] = Cap(CAP_YES if p.ok else CAP_NO, SRC_MEASURED, p.detail)
        if mp.reachable and entry.authorized and not verified:
            verified, detail = True, "a calibration round-trip succeeded"
    ceiling, basis, tdetail = merge_tier(prior_tier, mp)
    return replace(
        entry, capabilities=caps, path_verified=verified, path_detail=detail,
        tier_ceiling=ceiling, tier_basis=basis, tier_detail=tdetail,
        measured_recall_tokens=mp.recall_tokens if mp else None,
    )
