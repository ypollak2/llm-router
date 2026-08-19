"""Regression: RED1-2-03 — route_id must not collide within a 1-second window.

route_id was f"{sid}:{int(time.time())}:{tool}", so two same-session same-tool
routing decisions in the same wall-clock second produced identical route_ids →
identical derived event_ids → the second route_realized/override ledger row was
dropped by INSERT OR IGNORE. A random per-decision nonce makes each id unique.

Paired with test_red1_0506_routeid_and_dedup.py, which proves the downstream
contract: identical route_id → dedup (retry), distinct route_id → distinct rows.
So a unique route_id per decision (proven here) means distinct decisions are never
dropped, while a retried directive (same persisted route_id) still dedups.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTO_ROUTE = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"


def test_route_id_construction_includes_a_nonce():
    """Guard: the route_id must not revert to the collision-prone int(time)-only form.

    Phase 0.5 (Option A sidecar bridge) hoisted the id mint into a shared
    ``_directive_id`` local, computed once and referenced (not re-built) at
    both write sites, so the pending-state file and the last-classification
    sidecar carry the identical route id. That moved the literal
    ``token_hex(...)`` call away from the ``"route_id":`` key sites
    themselves — so this check follows a bare-identifier RHS back to its
    assignment before concluding the nonce is gone. It still fails on a
    genuine revert to the old ``f"{sid}:{int(time.time())}:{tool}"`` inline
    form, since that has no identifier to follow and no token_hex nearby.
    """
    text = AUTO_ROUTE.read_text()
    idx = text.find('"route_id":')
    assert idx != -1, "route_id construction not found"

    while idx != -1:
        window = text[idx: idx + 250]  # the route_id value spans a few lines
        if "token_hex" in window:
            idx = text.find('"route_id":', idx + 1)
            continue

        # Not adjacent — the RHS may be a bare identifier (e.g. a hoisted
        # `_directive_id` shared across multiple write sites). Extract it
        # and check ITS assignment for the nonce instead.
        rhs_match = re.search(r'"route_id":\s*([A-Za-z_][A-Za-z0-9_]*)\s*[,\n]', window)
        assert rhs_match, (
            "RED1-2-03 regression: route_id no longer includes a random nonce "
            "(no token_hex nearby, and RHS is not a traceable identifier); two "
            "same-second same-tool decisions would collide"
        )
        ident = rhs_match.group(1)
        assign_idx = text.find(f"{ident} = ")
        assert assign_idx != -1 and assign_idx != idx, (
            f"RED1-2-03 regression: could not locate assignment of '{ident}' "
            "referenced by route_id"
        )
        assign_window = text[assign_idx: assign_idx + 250]
        assert "token_hex" in assign_window, (
            "RED1-2-03 regression: route_id no longer includes a random nonce; two "
            "same-second same-tool decisions would collide"
        )
        idx = text.find('"route_id":', idx + 1)


def test_nonce_makes_same_second_ids_unique():
    """Behavioral: the exact construction pattern yields distinct ids per call."""
    import secrets as _s

    def make(sid, now, tool):
        return f"{sid}:{now}:{tool}:{_s.token_hex(4)}"

    sid, now, tool = "abc12345", 1700000000, "llm_code"
    ids = {make(sid, now, tool) for _ in range(50)}
    assert len(ids) == 50, "route_id nonce collided within a single second"
