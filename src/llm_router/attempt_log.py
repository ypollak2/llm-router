"""Per-attempt outcomes for the local chain — the record nothing kept.

`RoutingDecision` (model_tracking) is written BEFORE the model is called and
carries no latency and no outcome, and the only latency trace fires for the
candidate that won. So a model that times out on every call leaves no trace that
anything can read, and nothing downstream can adapt to it.

Measured 2026-09-14 on 200 real prompts: the first model in the chain timed out on
72 of 166 attempts and 50 of the run's 99 minutes produced nothing. That number
had to come from an ad-hoc benchmark harness, because production telemetry cannot
see its own failures.

This is the smallest store that fixes that: one line per attempt, append-only,
bounded by rotation, read back as a per-model summary cheap enough to consult
while building a chain.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path

OK = "ok"
TIMEOUT = "timeout"
EMPTY = "empty"
REJECTED = "rejected"
SKIPPED = "skipped"

_MAX_LINES = 5000          # rotate well before the file is expensive to read
_KEEP_LINES = 2500


def _path() -> Path:
    base = os.environ.get("LLM_ROUTER_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".llm-router"
    name = ("attempts.test.jsonl" if os.environ.get("PYTEST_CURRENT_TEST")
            else "attempts.jsonl")
    return root / name


def _scrub(text: str) -> str:
    """Redact credentials BEFORE they are written. R1.

    `reason` is built from provider exception text — `direct_executor` passes
    `f"call raised: {exc}"` — and auth failures routinely echo the credential
    that failed. Measured 2026-09-22 against this file with no scrubbing:
    a GitHub PAT, an AWS key id, an AWS secret, a Slack token and a bearer
    token all landed INTACT, in a file created at 0644 (world-readable).

    Truncation is not redaction. `reason[:80]` shortened a 100-char Anthropic
    key enough to defeat a naive exact-match search while leaving 80 characters
    of it on disk — which is how a first probe of this defect reported "no
    leak". Scrub first, truncate second.

    Fail-CLOSED: if the scrubber cannot be reached, withhold the text rather
    than write it. A reason field is diagnostic; a leaked credential is not
    recoverable.
    """
    if not text:
        return text
    try:
        from llm_router.secret_scrubber import scrub_text
        return scrub_text(text)
    except Exception:  # noqa: BLE001
        return "[SCRUB-UNAVAILABLE: reason withheld]"


def record(model: str, outcome: str, latency_ms: int, *, reason: str = "") -> None:
    """Append one attempt. Fail-open: telemetry must never break routing."""
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # R1: 0600 at CREATION. `open` creates with 0666 & ~umask -- 0644 on a
        # default umask -- so the file was world-readable for the whole write
        # and a handle opened in that window stays readable afterwards.
        from llm_router.paths import private_opener
        with open(p, "a", opener=private_opener) as fh:
            fh.write(json.dumps({
                "ts": time.time(), "model": model, "outcome": outcome,
                "latency_ms": int(latency_ms), "reason": _scrub(reason)[:80],
            }) + "\n")
        _repair_legacy_mode(p)
        _rotate(p)
    except Exception:                                        # noqa: BLE001
        pass


def _repair_legacy_mode(p: Path) -> None:
    """An opener only sets the mode when it CREATES the file.

    A file an older version already wrote at 0644 keeps that mode forever
    otherwise, so the fix would protect new installs and leave existing ones
    exposed.
    """
    try:
        import stat as _stat
        if _stat.S_IMODE(p.stat().st_mode) != 0o600:
            p.chmod(0o600)
    except OSError:
        pass


def _rotate(p: Path) -> None:
    try:
        if p.stat().st_size < 400_000:
            return
        lines = p.read_text(errors="replace").splitlines()
        if len(lines) > _MAX_LINES:
            p.write_text("\n".join(lines[-_KEEP_LINES:]) + "\n")
    except Exception:                                        # noqa: BLE001
        pass


def summary(window: int = 200) -> dict[str, dict]:
    """Per-model outcome counts over the last *window* attempts.

    Returns {model: {"attempts": n, "ok": n, "timeout": n, "timeout_rate": f,
    "p50_ms": int}}. Empty when there is nothing recorded — callers must treat an
    absent model as "no evidence", never as "bad".
    """
    rows: list[dict] = []
    try:
        for line in _path().read_text(errors="replace").splitlines()[-window:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except Exception:                                        # noqa: BLE001
        return {}

    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("model"):
            by[r["model"]].append(r)

    out: dict[str, dict] = {}
    for model, rs in by.items():
        oks = [r for r in rs if r.get("outcome") == OK]
        lat = sorted(r.get("latency_ms", 0) for r in oks)
        out[model] = {
            "attempts": len(rs),
            "ok": len(oks),
            "timeout": sum(1 for r in rs if r.get("outcome") == TIMEOUT),
            "timeout_rate": (sum(1 for r in rs if r.get("outcome") == TIMEOUT)
                             / len(rs)) if rs else 0.0,
            "p50_ms": lat[len(lat) // 2] if lat else 0,
        }
    return out
