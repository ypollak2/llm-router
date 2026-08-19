#!/usr/bin/env python3
"""Validate the claim-evidence registry (correctness-reset Phase 6, INV-CLAIM-001..004).

Enforces, per registered public claim:
  INV-CLAIM-001  a numeric/absolute claim must be backed by evidence, OR be marked
                 status="unsupported" AND carry no numeric magnitude in its text.
  INV-CLAIM-002  no permanent grandfathering: a supported claim's evidence expires
                 (expires_after_days) and must be re-verified (last_verified_at fresh).
  INV-CLAIM-003  a supported claim must name a benchmark/evidence and a metric drawn
                 from the allowed set.
  INV-CLAIM-004  a dollar claim backed only by subscription quota, or a "proven" claim
                 backed only by a simulated counterfactual, is rejected.

Dependency-free (json + stdlib). CLI: exit 0 = all valid, exit 1 = violations.
Importable: validate_registry(data, today) -> list[str] of violation messages.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

_REGISTRY = Path(__file__).resolve().parent / "claim_evidence.json"
_NUMERIC_RE = re.compile(r"\d+\s*(%|x\b|percent|dollars?|\$)", re.IGNORECASE)
_MAGNITUDE_RE = re.compile(r"\b\d+\s*[-–]\s*\d+\s*%|\b\d+\s*%|\b\d+\s*x\b", re.IGNORECASE)


def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def validate_registry(data: dict, today: date | None = None) -> list[str]:
    today = today or date.today()
    allowed_metrics = set(data.get("allowed_metrics", []))
    out: list[str] = []
    seen_ids: set[str] = set()

    for c in data.get("claims", []):
        cid = c.get("claim_id", "<no-id>")
        if cid in seen_ids:
            out.append(f"{cid}: duplicate claim_id")
        seen_ids.add(cid)
        text = c.get("claim_text", "")
        status = c.get("status")

        if status not in ("supported", "unsupported"):
            out.append(f"{cid}: status must be 'supported' or 'unsupported' (got {status!r})")
            continue

        if status == "unsupported":
            # INV-CLAIM-001/004: an unsupported claim must not assert a magnitude.
            if _MAGNITUDE_RE.search(text):
                out.append(
                    f"{cid}: status=unsupported but claim_text asserts a numeric magnitude "
                    f"— remove the number or provide evidence ({text!r})"
                )
            continue

        # status == "supported" → INV-CLAIM-002/003/004
        bench = c.get("benchmark_id")
        tests = c.get("evidence_tests") or []
        if not bench and not tests:
            out.append(f"{cid}: supported claim has no benchmark_id or evidence_tests (INV-CLAIM-003)")
        metric = c.get("metric")
        if metric not in allowed_metrics:
            out.append(f"{cid}: metric {metric!r} not in allowed_metrics (INV-CLAIM-003)")

        # INV-CLAIM-002: evidence must not be expired.
        exp = c.get("expires_after_days")
        lv = c.get("last_verified_at")
        if exp is not None:
            lvd = _parse_date(lv) if lv else None
            if lvd is None:
                out.append(f"{cid}: supported claim missing/!invalid last_verified_at (INV-CLAIM-002)")
            elif (today - lvd).days > int(exp):
                out.append(
                    f"{cid}: evidence expired — last_verified_at {lv} older than "
                    f"{exp} days (INV-CLAIM-002); re-verify"
                )

        # INV-CLAIM-004: honesty of the evidence *kind* vs the wording.
        is_dollar = bool(re.search(r"\$|\bdollar", text, re.IGNORECASE))
        if is_dollar and c.get("host_mode") == "subscription":
            out.append(
                f"{cid}: dollar claim backed only by subscription-mode (quota, not cash) "
                f"— not a real-dollars claim (INV-CLAIM-004)"
            )
        if "proven" in text.lower() and str(bench).lower().startswith("sim"):
            out.append(
                f"{cid}: 'proven' claim backed only by a simulated counterfactual "
                f"(INV-CLAIM-004) — needs a real control-group benchmark"
            )
    return out


def main() -> int:
    try:
        data = json.loads(_REGISTRY.read_text())
    except Exception as e:  # noqa: BLE001
        print(f"claim-evidence: cannot read registry {_REGISTRY}: {e}")
        return 1
    violations = validate_registry(data)
    if not violations:
        n = len(data.get("claims", []))
        print(f"claim-evidence: OK — {n} claim(s) valid.")
        return 0
    print("claim-evidence: FAILED — registry violations:")
    for v in violations:
        print(f"  - {v}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
