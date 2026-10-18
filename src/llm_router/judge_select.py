"""Judge selection (P3.1-3): who may grade an answer, decided at drain time.

Owner rule D-205: no model, tier or key literal lives here. Every name comes from
what the user has installed (discovery metadata) or what the user configured
(``LLM_ROUTER_JUDGE_MODEL``, ``judge.haiku_model`` in ``routing.yaml``, the haiku
tier of their ``claude_tiers.yaml`` policy).

Rules (OD-4 = A):
  * cloud answer  -> strongest installed generation-capable local model, else
                     ``ungraded_no_judge``. Never a cloud judge for a cloud answer.
  * local answer  -> strongest OTHER local model, else the Haiku fallback, else
                     ``ungraded_no_judge``.
  * The Haiku fallback is behind ``LLM_ROUTER_JUDGE_HAIKU_FALLBACK`` (default OFF)
    until P3.1-4 lands the privacy and spend gates it must not run without.

"Strongest" = parameter count, then on-disk size (both from ``/api/tags``), then
the user's own local-model setting, then discovery order. Unknown metadata ranks
below known metadata; it is never read as 0.

Independence is judged on canonical identity, not on strings: ``foo`` and
``foo:latest``, a provider prefix, a shared digest, or a dated alias of the same
family are all "the same model". On doubt the answer is "same".
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from llm_router import failopen, paths
from llm_router.model_aliases import model_family

STATUS_SELECTED = "selected"
STATUS_NO_JUDGE = "ungraded_no_judge"
KIND_LOCAL = "local"
KIND_SUBSCRIPTION = "subscription"


# ── identity ────────────────────────────────────────────────────────────────

def canon(model: str | None) -> str:
    """Canonical name: lower-case, provider prefix stripped, default tag removed.

    Only the last path segment survives, so ``ollama/foo``, ``hf.co/org/foo`` and
    ``foo`` collide on purpose (a collision refuses a judge; it never admits one).
    """
    name = (model or "").strip().lower()
    name = name.rsplit("/", 1)[-1]
    if name.endswith(":latest"):
        name = name[: -len(":latest")]
    return name


def _base_tag(model: str | None) -> tuple[str, str]:
    name = canon(model)
    base, _, tag = name.partition(":")
    return base, tag


def same_model(a: str | None, b: str | None, digests: dict[str, str] | None = None) -> bool:
    """True when ``a`` and ``b`` may be the same model. Empty input is "same" (doubt)."""
    ca, cb = canon(a), canon(b)
    if not ca or not cb or ca == cb:
        return True
    if digests:
        da, db = digests.get(ca), digests.get(cb)
        if da and db and da == db:
            return True
    (ba, ta), (bb, tb) = _base_tag(a), _base_tag(b)
    return model_family(ba) == model_family(bb) and ta == tb


# ── inventory ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LocalModel:
    name: str                      # as discovery lists it, ``ollama/`` prefix kept
    params: float | None = None    # parameter count (absolute), None = unknown
    size: int | None = None        # bytes on disk, None = unknown
    digest: str | None = None


_PARAM_UNITS = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}


def parse_param_size(text: object) -> float | None:
    """``"7.6B"`` -> 7.6e9; anything unparseable -> None (never 0)."""
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([kmbt])\s*", str(text or "").lower())
    return float(m.group(1)) * _PARAM_UNITS[m.group(2)] if m else None


def tag_metadata(entry: dict) -> dict:
    """The ranking fields of one ``/api/tags`` model entry (None where absent)."""
    details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
    size = entry.get("size")
    return {
        "parameter_size": details.get("parameter_size") or None,
        "size": size if isinstance(size, int) and size > 0 else None,
        "digest": entry.get("digest") or None,
    }


def _metadata_by_name() -> dict[str, dict]:
    try:
        data = json.loads(paths.state_path("discovery.json").read_text())
        models = data.get("models")
        return models if isinstance(models, dict) else {}
    except (OSError, ValueError):
        return {}


def local_inventory() -> list[LocalModel]:
    """Installed generation-capable Ollama models with whatever metadata discovery kept."""
    try:
        from llm_router.discover import get_cached_ollama_models, is_ollama_available
        from llm_router.local_models import serves_generate
        if not is_ollama_available():
            return []
        names = [n for n in get_cached_ollama_models() if serves_generate(n)]
    except Exception as exc:  # noqa: BLE001 — no inventory means no local judge, recorded
        failopen.record("CHZ-FO-JUDGE-SELECT-INVENTORY", exc)
        return []
    meta = _metadata_by_name()
    out = []
    for name in names:
        m = meta.get(name) if isinstance(meta.get(name), dict) else {}
        size = m.get("size")
        out.append(LocalModel(
            name=name, params=parse_param_size(m.get("parameter_size")),
            size=size if isinstance(size, int) and size > 0 else None,
            digest=m.get("digest") or None,
        ))
    return out


def _digests(inventory: list[LocalModel]) -> dict[str, str]:
    return {canon(m.name): m.digest for m in inventory if m.digest}


def _rank(m: LocalModel, index: int, setting: str) -> tuple:
    """Larger is stronger. Unknown sorts below every known value."""
    return (
        m.params is not None, m.params if m.params is not None else 0.0,
        m.size is not None, m.size if m.size is not None else 0,
        bool(setting) and canon(m.name) == canon(setting),
        -index,
    )


def strongest(candidates: list[LocalModel], setting: str = "") -> LocalModel | None:
    if not candidates:
        return None
    return max(enumerate(candidates), key=lambda p: _rank(p[1], p[0], setting))[1]


# ── Haiku fallback (config only) ────────────────────────────────────────────

def haiku_fallback_enabled() -> bool:
    return os.environ.get("LLM_ROUTER_JUDGE_HAIKU_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")


def configured_haiku_model() -> str | None:
    """Env, then ``judge.haiku_model`` in routing.yaml, then the haiku tier of the policy."""
    env = os.environ.get("LLM_ROUTER_JUDGE_MODEL", "").strip()
    if env:
        return env
    try:
        from llm_router.repo_config import _parse_yaml
        judge_cfg = _parse_yaml(paths.state_path("routing.yaml")).get("judge")
        cfg = judge_cfg.get("haiku_model") if isinstance(judge_cfg, dict) else None
        if isinstance(cfg, str) and cfg.strip():
            return cfg.strip()
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-JUDGE-SELECT-CONFIG", exc)
    try:
        from llm_router.proxy.tiers import REWRITE_HAIKU, tier_model
        return tier_model(REWRITE_HAIKU)
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-JUDGE-SELECT-TIER", exc)
        return None


# ── selection ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Selection:
    judge_model: str | None
    judge_kind: str | None
    status: str


def _no_judge() -> Selection:
    return Selection(None, None, STATUS_NO_JUDGE)


def select_judge(
    answering_model: str,
    inventory: list[LocalModel],
    *,
    setting: str = "",
    haiku_model: str | None = None,
    haiku_enabled: bool = False,
) -> Selection:
    """Pure decision: no I/O, so the property test can drive it with any inventory."""
    if not canon(answering_model):
        return _no_judge()  # cannot prove independence from an unnamed answerer
    digests = _digests(inventory)
    own = [m for m in inventory if same_model(answering_model, m.name, digests)]
    is_local = answering_model.strip().lower().startswith("ollama/") or bool(own)
    # Every alias of the answerer is excluded, by name and by digest.
    banned = [answering_model] + [m.name for m in own]
    others = [m for m in inventory if not any(same_model(b, m.name, digests) for b in banned)]
    pick = strongest(others, setting)
    if pick is not None:
        return Selection(pick.name, KIND_LOCAL, STATUS_SELECTED)
    if is_local and haiku_enabled and haiku_model and not any(
        same_model(b, haiku_model, digests) for b in banned
    ):
        return Selection(haiku_model, KIND_SUBSCRIPTION, STATUS_SELECTED)
    return _no_judge()


def select_for(answering_model: str) -> Selection:
    """Decide now, from the live inventory and the user's configuration."""
    from llm_router.model_discovery import user_local_model_setting
    enabled = haiku_fallback_enabled()
    return select_judge(
        answering_model, local_inventory(), setting=user_local_model_setting(),
        haiku_model=configured_haiku_model() if enabled else None, haiku_enabled=enabled,
    )


def kind_of(judge_model: str | None) -> str | None:
    """local when the model is in the installed inventory, else subscription."""
    if not judge_model:
        return None
    inv = local_inventory()
    return KIND_LOCAL if any(same_model(judge_model, m.name) for m in inv) else KIND_SUBSCRIPTION


def independent(answering_model: str | None, judge_model: str | None) -> bool:
    """Guard for ``_grade_one``: refuses on doubt (empty names, shared digest, same family)."""
    if not canon(answering_model) or not canon(judge_model):
        return False
    return not same_model(answering_model, judge_model, _digests(local_inventory()))
