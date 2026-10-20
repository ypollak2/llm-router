"""Anti-retry log for ``local-check`` (LC-5, design section 3).

Attempts are tracked per configuration = (model, model digest, bank version + sha256, settings_hash) in ``<runs_root>/attempts.jsonl``
(0600, one JSON line per attempt, no prompt or answer text). Rules:

* Draw 1 is used first; draw 2 only by a rerun with the same configuration; the verdict is then computed on the CUMULATIVE union
  (n=140). A third run with the same configuration is refused (the bank is exhausted) until the bank, the model digest or the settings change.
  ``force`` does not lift that refusal.
* A rerun within 24 h of the last scored attempt is refused unless ``force``.
* Only scored attempts (a judged run that reached a PASS / FAIL / NOT_PASSED / INCONCLUSIVE verdict) consume a draw. INVALID runs, ``--no-judge``
  runs and PROBE results are logged but consume nothing and do not enter the union.
* The cumulative verdict replaces a recorded PASS/FAIL lane (latest wins) except that an INCONCLUSIVE rerun writes nothing: an earlier recorded
  PASS is kept and the rerun is only logged (D-263 revised; the owner accepts the best-of-two risk).
* No reroll. A run that ended without a verdict AFTER a local item ran (INVALID, budget stop, interrupt, store refusal) leaves an ``open``
  entry: the run directory is a RESERVATION for (configuration, draw). The next run of that configuration must resume the same run id
  (cached answers and judgements are reused) unless it is discarded (``fresh``, a strike) or older than 7 days. INVALID outcomes and discards are
  strikes: the first ``FREE_STRIKES`` per configuration are free, the third and later also consume the draw (the draw is burnt, empty).
  An INVALID before the local arm (G3a, G3b) logs and consumes nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

COOLDOWN_S = 24 * 3600
MAX_DRAWS = 2
RESERVATION_TTL_S = 7 * 24 * 3600
FREE_STRIKES = 2
LOG_NAME = "attempts.jsonl"


def config_key(model: str, digest: str | None, bank_version: Any, bank_sha256: str, settings_hash: str) -> str:
    # The digest identifies the weights: `ollama cp m m2` is the same configuration. The name is only the fallback when no digest is known.
    ident = ["digest", digest] if digest else ["name", model]
    raw = json.dumps([ident, str(bank_version), bank_sha256, settings_hash], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


@dataclass
class Plan:
    draw: int | None                     # 1 or 2; None when refused
    refused: str | None = None           # reason when refused
    prior: list[dict[str, Any]] = field(default_factory=list)      # consumed attempts of this configuration, oldest first
    next_allowed_ts: float | None = None
    reservation: dict[str, Any] | None = None    # an open, unexpired run of this configuration and draw: it must be resumed
    strikes: int = 0                             # INVALID-after-local-arm outcomes and discards so far for this configuration

    @property
    def prior_rows(self) -> list[dict[str, Any]]:
        return [r for e in self.prior for r in e.get("rows", [])]

    @property
    def prior_assigned(self) -> list[str]:
        return [i for e in self.prior for i in e.get("assigned", [])]

    @property
    def prior_abandoned(self) -> list[str]:
        """Items assigned by an earlier attempt that stopped early and never ran them (they count as not passed)."""
        return [i for e in self.prior for i in e.get("unrun", [])]

    @property
    def prior_served(self) -> list[str]:
        return sorted({s for e in self.prior for s in e.get("judge_served", [])})



class AttemptLog:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.path = self.root / LOG_NAME

    def entries(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue              # torn write
            if isinstance(e, dict) and "config_key" in e:
                out.append(e)
        return out

    def plan(self, key: str, now: float, *, force: bool = False) -> Plan:
        prior = sorted((e for e in self.entries() if e["config_key"] == key and e.get("consumed")), key=lambda e: e.get("ts", 0.0))
        used = {e.get("draw") for e in prior}
        if len(used) >= MAX_DRAWS:
            return Plan(None, "bank exhausted for this model, bank version and settings (both 70-item draws used); change the model, the settings or update the bank", prior)
        draw = 1 if 1 not in used else 2
        last = prior[-1]["ts"] if prior else None
        nxt = None if last is None else last + COOLDOWN_S
        if nxt is not None and now < nxt and not force:
            return Plan(None, "cooldown: the previous scored run of this configuration was less than 24 h ago (use --force to override)", prior, nxt)
        return Plan(draw, None, prior, nxt, self.reservation(key, draw, now), self.strikes(key))

    def reservation(self, key: str, draw: int, now: float) -> dict[str, Any] | None:
        """The newest ``open`` run of this configuration and draw (the latest entry of its run id says ``open``), if younger than 7 days."""
        latest: dict[str, dict[str, Any]] = {}
        for e in self.entries():
            if e["config_key"] == key and e.get("run_id"):
                latest[e["run_id"]] = e
        live = [e for e in latest.values() if e.get("state") == "open" and e.get("draw") == draw
                and isinstance(e.get("ts"), (int, float)) and now - e["ts"] <= RESERVATION_TTL_S]
        return max(live, key=lambda e: e["ts"]) if live else None

    def strikes(self, key: str) -> int:
        return sum(1 for e in self.entries() if e["config_key"] == key and e.get("strike"))

    def append(self, entry: dict[str, Any]) -> None:
        """Append one attempt (fsynced, 0600). ``entry`` must carry ``config_key``; it must not carry answer or prompt text."""
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as f:
            f.write(json.dumps(entry, sort_keys=True).encode() + b"\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(self.path, 0o600)


def union_ids(prior: Iterable[str], current: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for i in (*prior, *current):
        seen.setdefault(i, None)
    return list(seen)
