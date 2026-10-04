"""Data model shared by the inventory, the capability profile and the resolver.

Two rules shape every field here.

* Unknown is its own value. ``None`` / ``"unknown"`` never stands in for 0, for
  "no pressure" or for "capable"; the resolver treats an unknown hard-eligibility
  fact as a reason to exclude, and says so.
* Tier is a MEASURED ceiling per model with a stated basis, never a global
  ranking. ``tier_basis`` records where the ceiling came from: ``measured``
  (``llm-router calibrate``), ``prior`` (the curated registry's coarse class,
  shown but weaker) or ``unmeasured``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1


class Tier(str, Enum):
    EASY = "EASY"
    MEDIUM = "MEDIUM"
    FRONTIER = "FRONTIER"

    @property
    def rank(self) -> int:
        return _RANK[self.value]

    @classmethod
    def parse(cls, value: str) -> "Tier":
        try:
            return cls(str(value).strip().upper())
        except ValueError:
            raise ValueError(
                f"unknown tier {value!r}; expected one of {[t.value for t in cls]}"
            ) from None


_RANK = {"EASY": 1, "MEDIUM": 2, "FRONTIER": 3}

ROUTE_SUBSCRIPTION = "subscription"
ROUTE_API = "api"
ROUTE_LOCAL = "local"

PRIVACY_LOCAL = "local"
PRIVACY_CLOUD = "cloud"

#: Capabilities the resolver can require. ``long_context`` is not here: it is a
#: number (``Needs.context_tokens``) checked against the context window.
CAPABILITIES = ("tools", "vision", "json", "thinking", "edit")

CAP_YES = "yes"
CAP_NO = "no"
CAP_UNKNOWN = "unknown"

SRC_DECLARED = "declared"    # the provider / Ollama /api/show / registry says so
SRC_MEASURED = "measured"    # llm-router calibrate observed it


@dataclass(frozen=True)
class Cap:
    state: str = CAP_UNKNOWN
    source: str = SRC_DECLARED
    detail: str = ""


@dataclass(frozen=True)
class Quota:
    """Quota state of the route a model runs on.

    ``state``: ``ok`` | ``benched`` (provider_reset says unavailable until T) |
    ``unknown`` (no trustworthy reading) | ``metered`` (pay-per-use, no window)
    | ``n/a`` (local). ``pressure`` is 0.0-1.0 or None; never 0.0 for unknown.
    """

    state: str = "unknown"
    pressure: float | None = None
    benched_until: float | None = None
    detail: str = ""


@dataclass(frozen=True)
class ModelEntry:
    id: str                      # "ollama/qwen3:30b", "claude_subscription/opus", "openai/gpt-5.5"
    provider: str                # ollama | anthropic | codex | gemini_cli | openai | ...
    route_kind: str              # subscription | api | local
    privacy: str                 # local | cloud
    exec_path: str = ""          # binary path or endpoint; never a credential
    path_verified: bool = False
    path_detail: str = ""
    authorized: bool = False
    auth_detail: str = ""
    quota: Quota = field(default_factory=Quota)
    capabilities: dict[str, Cap] = field(default_factory=dict)
    context_window: int | None = None
    context_source: str = ""
    loaded: bool | None = None
    size_bytes: int | None = None
    price_in_per_mtok: float | None = None
    price_out_per_mtok: float | None = None
    tier_ceiling: str | None = None      # EASY | MEDIUM | FRONTIER | None
    tier_basis: str = "unmeasured"       # measured | prior | unmeasured
    tier_detail: str = ""
    measured_recall_tokens: int | None = None
    present: bool = True                 # False: in the last snapshot, gone now
    notes: tuple[str, ...] = ()

    def cap(self, name: str) -> Cap:
        return self.capabilities.get(name, Cap(CAP_UNKNOWN, SRC_DECLARED, "not reported"))

    @property
    def effective_context(self) -> int | None:
        """Window the model is trusted with: the declared window, capped by the
        largest size a recall probe actually passed."""
        if self.context_window is None:
            return self.measured_recall_tokens
        if self.measured_recall_tokens is not None:
            return min(self.context_window, self.measured_recall_tokens)
        return self.context_window


@dataclass
class SourceStatus:
    ok: bool
    detail: str = ""


@dataclass
class Inventory:
    generated_at: float
    models: list[ModelEntry] = field(default_factory=list)
    sources: dict[str, SourceStatus] = field(default_factory=dict)
    api_key_names: list[str] = field(default_factory=list)   # NAMES only
    removed: list[str] = field(default_factory=list)         # ids gone since the snapshot
    schema_version: int = SCHEMA_VERSION

    def get(self, model_id: str) -> ModelEntry | None:
        for m in self.models:
            if m.id == model_id:
                return m
        return None


# ---------------------------------------------------------------- (de)serialise

def inventory_to_dict(inv: Inventory) -> dict[str, Any]:
    return asdict(inv)


def _entry_from_dict(d: dict[str, Any]) -> ModelEntry:
    d = dict(d)
    d["quota"] = Quota(**d.get("quota") or {})
    d["capabilities"] = {k: Cap(**v) for k, v in (d.get("capabilities") or {}).items()}
    d["notes"] = tuple(d.get("notes") or ())
    known = {f for f in ModelEntry.__dataclass_fields__}
    return ModelEntry(**{k: v for k, v in d.items() if k in known})


def inventory_from_dict(d: dict[str, Any]) -> Inventory:
    return Inventory(
        generated_at=float(d["generated_at"]),
        models=[_entry_from_dict(m) for m in d.get("models", [])],
        sources={k: SourceStatus(**v) for k, v in (d.get("sources") or {}).items()},
        api_key_names=list(d.get("api_key_names") or []),
        removed=list(d.get("removed") or []),
        schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
    )
