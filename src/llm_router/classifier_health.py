# SPDX-License-Identifier: MIT
"""Classifier agreement in status and the automatic conservative mode (PLAN v16 P3.6-b, R-CLS-7).

Source (D-255, under D-121): the two LIVE deciders, the rules and the L3 kNN. Wherever both
answer for one turn (``proxy/tiers.with_complexity_knn``, when the kNN runs) one row is
appended to ``classifier_agreement.jsonl``: ``{ts, rules_tier, knn_tier, source}``, no text.
``rules_tier`` is the rules' complexity; ``knn_tier`` is ``frontier`` or ``standard``.
**Agreement** is the share of rows where both sit on the same side of the frontier boundary
(rules ``complex`` / ``deep_reasoning`` = frontier). It is rules-vs-kNN agreement, NOT
ground-truth accuracy, and it says so everywhere it is shown. No rows is "not measured"
(n=0), never 0%; with the kNN off the line reads "not measured (kNN off, n=0)".

The killed LLM classifier's shadow log (``classifier_shadow.jsonl``) is read only as a
FALLBACK, and only when there are no kNN rows in the 35 days and that file exists; the
status line then says "LLM-shadow fallback".

Windows (``now`` is the check time): the last 7 days, and the 28 days BEFORE them (days 7 to 35)
as the baseline, so a recent drop is not diluted by itself.

* **Trigger:** n >= 100 compared rows in BOTH windows and agreement(7d) <= agreement(28d) - 5 pts
  switches conservative mode on. n < 100 in either window never triggers, and never switches
  an active mode off (thin data does not flip anything).
* **Effect:** ``bias_complexity`` moves the proxy tier policy's complexity up one level
  (simple -> moderate -> complex -> deep_reasoning, capped), so the tier is one step more
  capable. It changes what is SERVED only with ``authority=route`` (under ``advise`` it only
  moves the ledger's proposed tier), and only in the proxy; the hook and MCP paths get no
  bias yet. ``LLM_ROUTER_CLASSIFIER_CONSERVATIVE=off`` keeps the report and drops the bias.
* **Exit:** n >= 100 in both windows and the drop is under 5 pts.
* **Notice:** ``Update.notice`` is one line, printed by ``llm-router status`` and ``kpi`` while
  the mode is on. A state older than 7 days (nobody re-checked) is ignored.
* **PSI:** population stability index of the rules' tier mix, 7d against the baseline
  (< 0.10 stable, 0.10 to 0.25 moderate shift, > 0.25 large shift); needs 30 rows in each window.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

from llm_router import capped_log, paths

MODE_ENV = "LLM_ROUTER_CLASSIFIER_CONSERVATIVE"
STATE_FILE = "classifier_conservative.json"
AGREEMENT_FILE = "classifier_agreement.jsonl"
AGREEMENT_MAX_BYTES = 1024 * 1024  # two generations (capped_log): about 24k rows
SOURCE_KNN = "knn"
SOURCE_FALLBACK = "llm_shadow_fallback"
_FRONTIER_TIERS = ("complex", "deep_reasoning")
RECENT_DAYS = 7
BASELINE_DAYS = 28
MIN_N = 100
DROP_PTS = 5.0
PSI_MIN_N = 30
STATE_MAX_AGE_S = 7 * 86400
_LEVELS = ("simple", "moderate", "complex", "deep_reasoning")


@dataclass(frozen=True)
class Window:
    n: int  # compared rows (rules and shadow both proposed a tier)
    agree: int
    mix: dict  # rules tier -> rows, over every answered row

    @property
    def rate(self) -> float | None:
        return self.agree / self.n if self.n else None


@dataclass(frozen=True)
class Health:
    recent: Window
    baseline: Window
    drop_pts: float | None  # baseline minus recent, in points; None when either window is empty
    psi: float | None
    triggered: bool  # drop >= 5 pts with n >= 100 in both windows
    measurable: bool  # n >= 100 in both windows
    source: str = SOURCE_KNN  # SOURCE_KNN | SOURCE_FALLBACK


@dataclass(frozen=True)
class Update:
    health: Health
    active: bool
    changed: str | None  # "entered" | "exited" | None
    notice: str | None


def _tier(rec: dict, side: str) -> str | None:
    t = (rec.get(side) or {}).get("tier")
    return t if isinstance(t, str) else None


def agreement_path() -> Path:
    return paths.state_path(AGREEMENT_FILE)


def record_agreement(rules_complexity, knn_needs_frontier: bool, *, source: str = "proxy",
                     now: float | None = None, path: Path | None = None) -> bool:
    """Append one ``{ts, rules_tier, knn_tier, source}`` row (no text). Fail-open: False on error."""
    try:
        tier = getattr(rules_complexity, "value", rules_complexity)
        if not isinstance(tier, str) or not tier:
            return False
        row = {"ts": int(time.time() if now is None else now), "rules_tier": tier,
               "knn_tier": "frontier" if knn_needs_frontier else "standard", "source": source}
        capped_log.append(path or agreement_path(), (json.dumps(row, separators=(",", ":")) + "\n").encode(),
                          AGREEMENT_MAX_BYTES)
        return True
    except Exception:  # noqa: BLE001 -- evidence never stops routing
        return False


def read_agreement(path: Path | None = None, days: float | None = None) -> list[dict]:
    """Rows of the agreement log (newest generation last), optionally only the last ``days``."""
    target = path or agreement_path()
    cutoff = time.time() - days * 86400 if days else None
    out = []
    for r in capped_log.read_dicts(target):
        if isinstance(r.get("ts"), (int, float)) and (cutoff is None or r["ts"] >= cutoff):
            out.append(r)
    return out


def _compared(rec: dict) -> tuple[str, bool] | None:
    """``(rules tier, agrees)`` of one record, or None when it is not a comparison. Understands
    a kNN row and (fallback) a classifier-shadow row."""
    if rec.get("kind") == "classifier_shadow":
        if rec.get("source") not in ("llm", "cache"):
            return None
        rules, shadow = _tier(rec, "rules"), _tier(rec, "llm")
        if not rules:
            return None
        return rules, bool(shadow) and ("haiku" if shadow == "local" else shadow) == rules
    rules, knn = rec.get("rules_tier"), rec.get("knn_tier")
    if not isinstance(rules, str) or knn not in ("frontier", "standard"):
        return None
    return rules, (rules in _FRONTIER_TIERS) == (knn == "frontier")


def _window(recs: list[dict]) -> Window:
    n = agree = 0
    mix: dict[str, int] = {}
    for r in recs:
        c = _compared(r)
        if c is None:
            continue
        if r.get("kind") == "classifier_shadow" and not _tier(r, "llm"):
            mix[c[0]] = mix.get(c[0], 0) + 1  # answered row without a shadow tier: mix only
            continue
        mix[c[0]] = mix.get(c[0], 0) + 1
        n += 1
        agree += c[1]
    return Window(n, agree, mix)


def psi(recent: dict, baseline: dict) -> float | None:
    """Population stability index of two count mixes (zero bins floored at 0.5 / total)."""
    nr, nb = sum(recent.values()), sum(baseline.values())
    if nr < PSI_MIN_N or nb < PSI_MIN_N:
        return None
    total = 0.0
    for k in set(recent) | set(baseline):
        a = max(recent.get(k, 0), 0.5) / nr
        b = max(baseline.get(k, 0), 0.5) / nb
        total += (a - b) * math.log(a / b)
    return total


def _load_records(window_days: int) -> tuple[list[dict], str]:
    """kNN agreement rows; else, only if that file exists, the killed LLM shadow log as a fallback."""
    rows = read_agreement(days=window_days)
    if rows:
        return rows, SOURCE_KNN
    try:
        from llm_router.proxy import llm_shadow

        if llm_shadow.shadow_path().exists():
            return llm_shadow.read_records(days=window_days), SOURCE_FALLBACK
    except Exception:  # noqa: BLE001 -- the fallback is optional
        pass
    return [], SOURCE_KNN


def compute(records: list[dict] | None = None, *, now: float | None = None) -> Health:
    stamp = time.time() if now is None else now
    source = SOURCE_KNN
    if records is None:
        records, source = _load_records(RECENT_DAYS + BASELINE_DAYS)
    elif any(r.get("kind") == "classifier_shadow" for r in records):
        source = SOURCE_FALLBACK
    cut = stamp - RECENT_DAYS * 86400
    floor = cut - BASELINE_DAYS * 86400
    calls = [r for r in records if isinstance(r.get("ts"), (int, float)) and not isinstance(r.get("ts"), bool)
             and (r.get("kind") == "classifier_shadow" or "knn_tier" in r)]
    recent = _window([r for r in calls if cut <= r["ts"] <= stamp])
    baseline = _window([r for r in calls if floor <= r["ts"] < cut])
    drop = (baseline.rate - recent.rate) * 100.0 if recent.n and baseline.n else None
    measurable = recent.n >= MIN_N and baseline.n >= MIN_N
    triggered = measurable and drop is not None and drop >= DROP_PTS
    return Health(recent, baseline, drop, psi(recent.mix, baseline.mix), triggered, measurable, source)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def lines(h: Health) -> list[str]:
    """The status lines: agreement with n, then the PSI drift line."""
    r, b = h.recent, h.baseline
    label = "LLM-shadow fallback, vs rules" if h.source == SOURCE_FALLBACK else "rules vs kNN"
    if r.n == 0 and b.n == 0:
        from llm_router import complexity_knn

        why = "n=0 in the last 35 days" if complexity_knn.enabled() else "kNN off, n=0"
        out = [f"classifier agreement ({label}): not measured ({why})"]
    else:
        thin = "" if h.measurable else f"; too few to trigger (needs n >= {MIN_N} in both)"
        out = [f"classifier agreement ({label}; agreement, not accuracy): 7d {_pct(r.rate)} (n={r.n}), "
               f"prior 28d {_pct(b.rate)} (n={b.n}){thin}"]
    if h.psi is None:
        out.append(f"classifier drift (PSI, rules tier mix 7d vs prior 28d): not measured "
                   f"(needs {PSI_MIN_N} rows in each, have {sum(r.mix.values())} and {sum(b.mix.values())})")
    else:
        word = "stable" if h.psi < 0.10 else "moderate shift" if h.psi < 0.25 else "large shift"
        out.append(f"classifier drift (PSI, rules tier mix 7d vs prior 28d): {h.psi:.3f} ({word})")
    return out


def _notice(h: Health) -> str:
    return (f"classifier agreement ({'LLM-shadow fallback' if h.source == SOURCE_FALLBACK else 'rules vs kNN'}) fell {h.drop_pts:.1f} pts (7d {_pct(h.recent.rate)}, n={h.recent.n}, vs prior 28d "
            f"{_pct(h.baseline.rate)}, n={h.baseline.n}): conservative mode ON, routing biased one tier up")


def state_path() -> Path:
    return paths.state_path(STATE_FILE)


_STATE_CACHE: dict = {}


def _read_state(path: Path | None = None) -> dict | None:
    """The state file, parsed once per file version: cached by (path, mtime, size, inode) so the
    proxy's per-request ``is_active`` costs one ``stat``, not a read and a parse."""
    target = path or state_path()
    try:
        st = target.stat()
    except OSError:
        _STATE_CACHE.pop(str(target), None)
        return None
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    hit = _STATE_CACHE.get(str(target))
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    state = raw if isinstance(raw, dict) else None
    _STATE_CACHE[str(target)] = (key, state)
    return state


def _write_state(state: dict, path: Path | None = None) -> None:
    import tempfile

    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def mode_enabled() -> bool:
    return os.environ.get("LLM_ROUTER_CLASSIFIER_CONSERVATIVE", "auto").strip().lower() not in ("off", "0", "false", "no")


def is_active(*, now: float | None = None, path: Path | None = None) -> bool:
    """True while a recent check says conservative mode is on (and the env switch is not off)."""
    state = _read_state(path)
    if not state or not state.get("active") or not mode_enabled():
        return False
    checked = state.get("checked_at")
    stamp = time.time() if now is None else now
    return isinstance(checked, (int, float)) and stamp - checked <= STATE_MAX_AGE_S


def evaluate_and_update(records: list[dict] | None = None, *, now: float | None = None,
                        path: Path | None = None) -> Update:
    """Compute the health, move the mode on/off per the rules above, persist, return the notice."""
    stamp = time.time() if now is None else now
    h = compute(records, now=stamp)
    state = _read_state(path) or {}
    was = bool(state.get("active"))
    active, changed = was, None
    if h.measurable:
        active = h.triggered
        changed = "entered" if active and not was else "exited" if was and not active else None
    if h.measurable or was:
        new = {"active": active, "checked_at": stamp if h.measurable else state.get("checked_at", stamp),
               "since": (stamp if changed == "entered" else state.get("since")) if active else None,
               "drop_pts": h.drop_pts, "n_recent": h.recent.n, "n_baseline": h.baseline.n}
        try:
            _write_state(new, path)
        except OSError:
            pass  # a status read must not fail; the mode then simply does not change
    notice = _notice(h) if active and h.drop_pts is not None else (
        f"classifier agreement recovered ({_pct(h.recent.rate)}, n={h.recent.n}): conservative mode off"
        if changed == "exited" else None)
    return Update(h, active, changed, notice)


def bias_complexity(complexity):
    """One level up while conservative mode is active; other values pass through unchanged."""
    if not isinstance(complexity, str) or complexity not in _LEVELS or not is_active():
        return complexity
    return _LEVELS[min(_LEVELS.index(complexity) + 1, len(_LEVELS) - 1)]
