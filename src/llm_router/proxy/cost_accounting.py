"""Real Anthropic spend per proxied call and per proxied session (PR 8).

WHY THIS EXISTS. On a proxied session Claude Code's own ``total_cost_usd`` is
phantom: it prices locally served tokens at list price. Measured 2026-10-03
(p1p2/REPORT.txt finding C4): $21.09 reported across 178 trials against a real
Anthropic spend of $0. The proxy ledger sees every call and what Anthropic
actually returned, so it is the source of truth; ``total_cost_usd`` is only
ever shown next to it, labelled :data:`UNRELIABLE`.

PER-CALL FIELDS, added to every ledger row by :func:`annotate` (the key is
always present; for rows written since #261 an unknown value is ``null``, never 0.
Rows written BEFORE #261 carry zeros for unknown usage and cannot be told apart
from a real zero, so "unknown is null" is not a ledger-wide invariant):

  served_by               "local" (a non-Claude backend answered) | "anthropic"
                          (forwarded, or a local attempt that fell back)
  anthropic_usage         the REAL Anthropic usage, flat, with the cache read
                          and write split (``ledger.normalize_usage``). All
                          zeros when served locally or when Anthropic answered
                          with an error status (nothing is billed). ``null``
                          when a 2xx call has no usage on record or its stream
                          was cut before the terminal message_delta (unknown).
  anthropic_cost_usd      that usage priced at the model it was sent to;
                          0.0 when served locally; ``null`` when unpriced.
  counterfactual_cost_usd what THIS step would have cost on the model the
                          client requested: a served step is priced as a cache
                          read of the session's last real prefix plus its own
                          reply as output; a forwarded one as
                          ``ledger.tier_counterfactual_cost``. A STEP-level
                          estimate: do not sum it (see below).

PER-SESSION SUMMARY, :func:`proxy_session_cost`: ``real_anthropic_usd`` (sum
of the per-call real cost), ``est_avoided_usd`` (an ESTIMATE: ``ledger``'s
once-per-served-run ``net_avoided`` upper bound plus the tier-rewrite saving;
the per-call counterfactuals are not summed because a run of served steps is
one deferred Anthropic call, not N), and ``claude_code_total_cost_usd`` with
its :data:`UNRELIABLE` label when a figure is supplied.

RECONCILED. A session is *reconciled* when every row carries a known real
usage and a price, and every locally served row carries exactly zero Anthropic
tokens. That is an internal-consistency check on the ledger, not a comparison
with Claude Code; the comparison is :func:`check_forwarded` /
:func:`check_local` and ``scripts/reconcile_proxy_cost.py``.
"""

from __future__ import annotations

from llm_router.proxy import ledger

SERVED_BY_LOCAL = "local"
SERVED_BY_ANTHROPIC = "anthropic"
UNRELIABLE = "unreliable"
# Stated tolerance for "ledger cost matches Claude Code's total_cost_usd" on a
# FULLY FORWARDED session: PROXY_REDESIGN_PLAN.md section 2, PR 8 (3%).
DEFAULT_TOLERANCE = 0.03
_ZERO_USAGE = {k: 0 for k in ledger._NORMALIZED_KEYS}  # noqa: SLF001
_MAX_SESSIONS = 4096


def served_by(row: dict) -> str:
    return SERVED_BY_LOCAL if row.get("decision") == ledger.DECISION_SERVED else SERVED_BY_ANTHROPIC


def real_usage(row: dict) -> dict | None:
    """The row's REAL Anthropic usage; zeros for a locally served row or an
    error reply (nothing is billed), ``None`` when unknown: a 2xx forwarded
    call that recorded no usage, or whose stream was cut (``stop_reason`` None:
    the usage seen is a placeholder and the output under-counted). Either
    signal alone makes the call unknown, so one bad signal cannot pass."""
    if served_by(row) == SERVED_BY_LOCAL:
        return dict(_ZERO_USAGE)
    status = row.get("upstream_status")
    if isinstance(status, int) and status >= 400:
        return dict(_ZERO_USAGE)  # an error response bills nothing
    if not isinstance(row.get("usage"), dict):
        return None
    if "stop_reason" in row and row["stop_reason"] is None:
        return None  # truncated: the terminal message_delta never arrived
    return ledger.normalize_usage(row["usage"])


def row_cost(row: dict) -> float | None:
    """Real Anthropic USD of one row: 0.0 local, ``None`` unknown/unpriced.
    Reads the recorded field when present, else recomputes (legacy rows)."""
    if served_by(row) == SERVED_BY_LOCAL:
        return 0.0
    if "anthropic_cost_usd" in row:
        v = row["anthropic_cost_usd"]
        return float(v) if isinstance(v, (int, float)) else None
    u = real_usage(row)
    if u is None:
        return None
    if not any(u.values()):
        return 0.0
    return ledger.anthropic_cost(dict(row, usage=u))


def prefix_context(usage: dict | None) -> int:
    u = ledger.normalize_usage(usage)
    return u["cache_read_input_tokens"] + u["cache_creation_input_tokens"]


def counterfactual(row: dict, prefix_ctx: int) -> float | None:
    if served_by(row) == SERVED_BY_LOCAL:
        model = row.get("requested_model")
        if not model:
            return None
        out = (row.get("backend_usage") or {}).get("output_tokens") or 0
        return ledger._price_tokens(model, cache_read_input_tokens=prefix_ctx,  # noqa: SLF001
                                    output_tokens=int(out))
    if not isinstance(row.get("usage"), dict):
        return None
    return ledger.tier_counterfactual_cost(row)


def annotate(row: dict, prefix_ctx: int = 0) -> dict:
    """Add the per-call cost fields to ``row`` in place and return it. All four
    values are computed before any is assigned: if one raises, the row gets
    none of them (and ``has_cost_fields`` is False), never a partial set."""
    usage = real_usage(row)
    by = served_by(row)
    if by == SERVED_BY_LOCAL:
        cost: float | None = 0.0
    elif usage is None:
        cost = None
    elif not any(usage.values()):
        cost = 0.0
    else:
        cost = ledger.anthropic_cost(dict(row, usage=usage))
    cf = counterfactual(row, prefix_ctx)
    row["served_by"] = by
    row["anthropic_usage"] = usage
    row["anthropic_cost_usd"] = None if cost is None else round(cost, 6)
    row["counterfactual_cost_usd"] = None if cf is None else round(cf, 6)
    return row


class PrefixTracker:
    """Last real Anthropic prefix size (cache read + write tokens) per session:
    what a served step would have read. Bounded; in memory."""

    def __init__(self) -> None:
        self._ctx: dict[str, int] = {}

    def get(self, session_id: str | None) -> int:
        return self._ctx.get(session_id or "", 0)

    def update(self, row: dict) -> None:
        if row.get("served_by") != SERVED_BY_ANTHROPIC or not isinstance(row.get("usage"), dict):
            return
        ctx = prefix_context(row["usage"])
        if not ctx:
            return
        key = row.get("session_id") or ""
        if len(self._ctx) >= _MAX_SESSIONS and key not in self._ctx:
            self._ctx.pop(next(iter(self._ctx)))
        self._ctx[key] = ctx


COST_FIELDS = ("served_by", "anthropic_usage", "anthropic_cost_usd", "counterfactual_cost_usd")


def has_cost_fields(row: dict) -> bool:
    """All four per-call fields are present (null is a value: it means unknown)."""
    return all(k in row for k in COST_FIELDS)


def row_complete(row: dict) -> bool:
    """Known real usage and a price, and zero Anthropic tokens when local."""
    u = real_usage(row)
    if u is None or row_cost(row) is None:
        return False
    return not (served_by(row) == SERVED_BY_LOCAL and stray_anthropic_tokens(row))


def proxy_session_cost(rows: list[dict], session_id: str | None = None,
                       claude_code_total_cost_usd: float | None = None) -> dict:
    """The per-session summary. ``rows`` may hold several sessions when
    ``session_id`` is given (they are filtered), else all are one session."""
    if session_id is not None:
        rows = [r for r in rows if r.get("session_id") == session_id]
    local = [r for r in rows if served_by(r) == SERVED_BY_LOCAL]
    remote = [r for r in rows if served_by(r) == SERVED_BY_ANTHROPIC]
    tokens = {k: 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                             "cache_creation_5m", "cache_creation_1h")}
    real = 0.0
    unknown = 0
    for r in remote:
        u, c = real_usage(r), row_cost(r)
        if u is None or c is None:
            unknown += 1
            continue
        real += c
        for k in tokens:
            tokens[k] += u[k]
    avoided, runs = ledger._net_avoided(rows)  # noqa: SLF001
    tiers = ledger.tier_stats(rows)
    tier_saving = tiers["est_saving_usd"] if tiers else 0.0
    out = {
        "session_id": session_id, "calls": len(rows), "calls_local": len(local),
        "calls_anthropic": len(remote), "unknown_calls": unknown,
        "reconciled": bool(rows) and all(row_complete(r) for r in rows),
        "real_anthropic_usd": round(real, 4), "anthropic_tokens": tokens,
        "est_avoided_usd": round(avoided + tier_saving, 4), "est_avoided_runs": runs,
        "est_avoided_label": "est.",
        "claude_code_total_cost_usd": None, "claude_code_total_cost_label": None,
    }
    if claude_code_total_cost_usd is not None:
        out["claude_code_total_cost_usd"] = round(float(claude_code_total_cost_usd), 4)
        out["claude_code_total_cost_label"] = UNRELIABLE
    return out


def label_claude_code_cost(value: float | None) -> str:
    """How any surface must print Claude Code's own ``total_cost_usd`` for a
    proxied session."""
    return "n/a" if value is None else f"${value:.4f} ({UNRELIABLE}: prices local tokens at list)"


def check_forwarded(rows: list[dict], reference_usd: float,
                    tolerance: float = DEFAULT_TOLERANCE) -> dict:
    """Fully forwarded session: ledger real cost vs a reference total
    (Claude Code's ``total_cost_usd`` or transcript usage priced the same way)
    within ``tolerance`` (relative). Not a forwarded session -> not applicable."""
    if not rows or any(served_by(r) == SERVED_BY_LOCAL for r in rows):
        return {"applicable": False, "ok": False, "reason": "session has locally served calls"}
    s = proxy_session_cost(rows)
    if s["unknown_calls"]:
        return {"applicable": True, "ok": False, "ledger_usd": s["real_anthropic_usd"],
                "reference_usd": reference_usd, "reason": f"{s['unknown_calls']} call(s) without known usage"}
    ledger_usd = s["real_anthropic_usd"]
    rel = abs(ledger_usd - reference_usd) / reference_usd if reference_usd else (0.0 if not ledger_usd else None)
    return {"applicable": True, "ok": rel is not None and rel <= tolerance, "ledger_usd": ledger_usd,
            "reference_usd": reference_usd, "rel_diff": None if rel is None else round(rel, 4),
            "tolerance": tolerance}


def stray_anthropic_tokens(row: dict) -> int:
    """Anthropic tokens a LOCALLY SERVED row carries in its raw fields
    (``usage`` or a recorded ``anthropic_usage``). ``real_usage`` forces zeros
    for a local row, so the exactly-zero check reads the raw fields instead of
    comparing the forced zeros with themselves."""
    total = 0
    for key in ("usage", "anthropic_usage"):
        if isinstance(row.get(key), dict):
            total += sum(ledger.normalize_usage(row[key]).values())
    return total


def check_local(rows: list[dict]) -> dict:
    """Fully local session: the ledger's Anthropic tokens must be exactly 0."""
    if not rows or any(served_by(r) != SERVED_BY_LOCAL for r in rows):
        return {"applicable": False, "ok": False, "reason": "session has Anthropic calls"}
    total = sum(stray_anthropic_tokens(r) for r in rows)
    return {"applicable": True, "ok": total == 0, "anthropic_tokens": total}
