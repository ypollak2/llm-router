"""Shared fixture for the O3 tests. Imports nothing from the O3 code, so the golden
files in ``tests/golden/`` could be (and were) generated on the commit before O3."""
from __future__ import annotations

import json
from pathlib import Path

NOW = 1_791_300_000.0  # fixed clock: the goldens must not depend on time.time()


def proxy_row(i: int, *, sid="s-org", tier="sonnet", model=None, reason="policy",
              step=None, decision="forwarded", ts=None, pv="v-old", kind=None, **extra) -> dict:
    served = model or {"haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-sonnet-5-5",
                       "opus": "claude-opus-5-5"}.get(tier)
    row = {"ts": ts if ts is not None else NOW - 3600 + i, "session_id": sid, "msg_id": f"msg_{sid}_{i}",
           "stream": True, "requested_model": "claude-sonnet-5-5", "step_class": step,
           "prev_tools": [], "decision": decision, "reason": "routing_off",
           "session_kind": kind, "tier_policy_version": pv, "tier_proposed": tier,
           "tier": tier, "tier_reason": reason, "served_model": served,
           "upstream_status": 200, "usage": {"input_tokens": 1, "output_tokens": 1},
           "served_by": "anthropic"}
    row.update(extra)
    return row


def baseline_rows(n: int = 120) -> list[dict]:
    """Plain Sonnet/Opus organic traffic: enough rows for D4/G1/G3 to be measurable."""
    return [proxy_row(i, tier="opus" if i % 5 == 0 else "sonnet",
                      step="continuation" if i % 2 else None, kind="organic") for i in range(n)]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
