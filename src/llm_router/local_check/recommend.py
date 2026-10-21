"""local-check recommendations (LC-9): hardware-fit local models, from a data file only.

No model name appears in this module: every name, tag, size and licence is read from
``data/recommendations.json`` (guarded by tests/test_lc9_local_check_recommend.py). Nothing here installs, pulls or routes
anything; ``pull_command`` is text for the user to run.

Fit rule (documented, tunable only here): an entry fits when
``need_gb = max(min_ram_gb, download_size_gb) * HEADROOM + KV_ALLOWANCE_GB`` is at most the memory offered to the model:
the machine's CAPACITY ``total_gb - max(4 GiB, 20% of total)`` (the probe's own reserve, applied to total, not to free
memory, so a busy machine is not told its model is too big; on Apple unified memory vram == total, same rule), or
``vram_gb`` (total VRAM) when a discrete GPU is present with a known VRAM and the entry gives ``min_vram_gb``. No shipped
entry has ``min_vram_gb`` yet: the cited pages (library pages and model cards) state no VRAM figure, so GPU boxes fall
back to the RAM rule above rather than to an invented number. HEADROOM is 20%; KV_ALLOWANCE_GB is a flat allowance for
the KV cache at the default context. Unknown total memory is never read as zero: the entry is kept, marked
``fit == "unknown"``.

Order: measured passes (pooled rate desc, then smaller footprint), then untested (smaller footprint). A list older than
STALE_DAYS carries a "newer models may exist" note.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

DATA_FILE = Path(__file__).resolve().parent / "data" / "recommendations.json"
HEADROOM = 1.20
KV_ALLOWANCE_GB = 2.0
STALE_DAYS = 90
SCHEMA_VERSION = 1

# runtime -> (key in entry["ids"], pull command template)
_PULL = {
    "ollama": ("ollama", "ollama pull {id}"),
    "lmstudio": ("lmstudio", "lms get {id}"),
}


@dataclass(frozen=True)
class Recommendation:
    family: str
    model_id: str
    runtime: str
    pull_command: str | None
    status: str                    # measured | untested
    fit: str                       # ok | unknown
    need_gb: float
    download_size_gb: float
    licence: str
    bench: Mapping[str, Any] | None
    pooled_rate: float | None
    sources: Mapping[str, str] = field(default_factory=dict)
    note: str | None = None        # staleness note, same on every row
    untested_on: str | None = None


def load(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or DATA_FILE).read_text(encoding="utf-8"))


def stale_note(data: Mapping[str, Any], today: date | None = None) -> str | None:
    gen = date.fromisoformat(data["generated"])
    now = today or datetime.now(timezone.utc).date()
    if (now - gen).days > STALE_DAYS:
        return f"list from {gen.isoformat()}; newer models may exist"
    return None


def need_gb(entry: Mapping[str, Any]) -> float:
    base = max(float(entry["min_ram_gb"]), float(entry["download_size_gb"]))
    return round(base * HEADROOM + KV_ALLOWANCE_GB, 2)


def capacity_gb(hardware: Mapping[str, Any]) -> float | None:
    """Memory the machine can give a model: total minus max(4 GiB, 20% of total); None when total is unknown."""
    total = hardware.get("total_gb")
    if isinstance(total, bool) or not isinstance(total, (int, float)) or total <= 0:
        return None
    return round(max(0.0, total - max(4.0, 0.20 * total)), 2)


def _offered(hardware: Mapping[str, Any], entry: Mapping[str, Any]) -> float | None:
    gpu, vram = hardware.get("gpu"), hardware.get("vram_gb")
    if (gpu not in (None, "none", "apple_unified") and isinstance(vram, (int, float)) and not isinstance(vram, bool)
            and vram > 0 and entry.get("min_vram_gb") is not None):
        return float(vram)
    return capacity_gb(hardware)


def empty_reason(runtime: str | None) -> str:
    """Text for the CLI when ``recommend`` returns an empty list for ``runtime``."""
    return f"no verified recommendation for {runtime or 'this runtime'}"


def _pooled(entry: Mapping[str, Any]) -> float | None:
    p = (entry.get("bench") or {}).get("pooled")
    return p["k"] / p["n"] if p and p.get("n") else None


def _passed(current_result: Any) -> bool:
    v = current_result.get("verdict") if isinstance(current_result, Mapping) else getattr(current_result, "verdict", None)
    return v == "PASS"


def recommend(hardware: Mapping[str, Any] | None, runtime: str | None, current_model: str | None = None,
              current_result: Any = None, *, bank_version: Any = None, data: Mapping[str, Any] | None = None, today: date | None = None) -> list[Recommendation]:
    """Fit-filtered recommendations for ``runtime``; [] when the current model already passed."""
    if current_result is not None and _passed(current_result):
        return []
    d = data if data is not None else load()
    key_tmpl = _PULL.get(runtime or "")
    note = stale_note(d, today)
    out: list[Recommendation] = []
    for e in d["entries"]:
        ids = e.get("ids", {})
        model_id = ids.get(key_tmpl[0] if key_tmpl else (runtime or ""))
        if not model_id or model_id == current_model:
            continue
        need = need_gb(e)
        offered = _offered(hardware or {}, e)
        if offered is not None and need > offered:
            continue
        bench = e.get("bench")
        demoted = e["status"] == "measured" and not (
            bank_version is not None and bench is not None
            and str(bench.get("bank_version")).lower().lstrip("v") == str(bank_version).lower().lstrip("v"))
        out.append(Recommendation(
            family=e["family"], model_id=model_id, runtime=runtime or "", status="untested" if demoted else e["status"],
            pull_command=key_tmpl[1].format(id=model_id) if key_tmpl else None,
            fit="unknown" if offered is None else "ok", need_gb=need,
            download_size_gb=float(e["download_size_gb"]), licence=e["licence"], bench=None if demoted else bench,
            pooled_rate=None if demoted else _pooled(e), sources=e.get("sources", {}), note=note,
            untested_on=f"v{bank_version}" if demoted else None))
    out.sort(key=lambda r: (r.status != "measured", -(r.pooled_rate if r.pooled_rate is not None else 0.0), r.need_gb))
    return out
