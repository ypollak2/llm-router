"""One definition of the usage-row provider that is not a call to anyone.

A ``usage`` row with provider ``cache`` is a call the semantic cache answered (P0.8-e): it
exists so the call is attributable to a session, and it carries 0 tokens and $0. It is
neither a paid call, a free (local) call nor a subscription call, so the surfaces that
split calls into those buckets must leave it out of all three. Hooks cannot import the
package, so each keeps a ``_CACHE_PROVIDER`` copy; ``tests/test_p08e_cache_hit_row.py``
asserts every copy equals this constant.
"""
from __future__ import annotations

CACHE_PROVIDER = "cache"


def is_cache_provider(provider: object) -> bool:
    return provider == CACHE_PROVIDER


# LEDGER-ERR-1: a ``usage`` row whose reason starts ``error_`` is a call that reached
# dispatch and FAILED (success=0, 0 tokens, $0, latency = time wasted). It exists so the
# call is attributable to a session; it is not a served call, so call counts, the
# local/paid mix and latency percentiles must leave it out, exactly like a cache row.
ERROR_REASON_PREFIX = "error_"

#: N21: a call the quality breaker REFUSED ("Do this yourself") is an attributable call that was
#: never routed: one ``usage`` row (``reason`` ``breaker_open``) and one ``routing_decisions`` row
#: (``reason_code`` ``breaker_open``), $0, so the caller's session can account for it. It is not a
#: served call and not a routing decision, so every predicate below leaves it out, like an error row.
REASON_BREAKER_OPEN = "breaker_open"

#: SQL predicate (usage table, ``reason`` column) that keeps only rows that are not error rows
#: (nor breaker-refusal rows).
SQL_NOT_ERROR_ROW = (
    "COALESCE(reason, '') NOT LIKE 'error\\_%' ESCAPE '\\' "
    "AND COALESCE(reason, '') != 'breaker_open'"
)


#: LEDGER-ERR-1 / owner decision 2026-10-10: ``routing_decisions`` carries one ``provenance='runtime'``
#: row per llm() call, so a failed call (``reason_code`` ``error_*``) and a cache-served call
#: (``reason_code`` ``cache_hit``, ``final_provider`` ``cache``) have rows too. They are
#: attributable calls, not routing decisions: routing quality, accuracy, shares and judge/ground-truth
#: sampling must leave them out, exactly like the matching ``usage`` rows.
REASON_CACHE_HIT_CODE = "cache_hit"

#: SQL predicate (routing_decisions table): keep only rows that are real routing decisions.
SQL_REAL_DECISION = (
    "COALESCE(reason_code, '') NOT LIKE 'error\\_%' ESCAPE '\\' "
    "AND COALESCE(reason_code, '') != 'cache_hit' "
    "AND COALESCE(reason_code, '') != 'breaker_open' "
    "AND COALESCE(final_provider, '') != 'cache'"
)


def is_non_decision_reason(reason: object) -> bool:
    """True for a ``reason_code`` that marks a cache-served or failed call."""
    return reason in (REASON_CACHE_HIT_CODE, REASON_BREAKER_OPEN) or is_error_reason(reason)


def real_decision_sql(con, alias: str = "") -> str:
    """``SQL_REAL_DECISION`` for a sync sqlite3 connection, optionally table-aliased. Each term is
    kept only when its column exists (a database or fixture older than P0.8 has no such rows),
    and the result is ``1`` when neither does."""
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(routing_decisions)")}
    except Exception:  # noqa: BLE001
        return "1"
    a = (alias.rstrip(".") + ".") if alias else ""
    parts = []
    if "reason_code" in cols:
        parts.append(f"COALESCE({a}reason_code, '') NOT LIKE 'error\\_%' ESCAPE '\\'")
        parts.append(f"COALESCE({a}reason_code, '') != 'cache_hit'")
        parts.append(f"COALESCE({a}reason_code, '') != 'breaker_open'")
    if "final_provider" in cols:
        parts.append(f"COALESCE({a}final_provider, '') != 'cache'")
    return " AND ".join(parts) or "1"


async def real_decision_sql_async(db) -> str:
    """``real_decision_sql`` for an aiosqlite connection."""
    try:
        cur = await db.execute("PRAGMA table_info(routing_decisions)")
        cols = {r[1] for r in await cur.fetchall()}
    except Exception:  # noqa: BLE001
        return "1"
    parts = []
    if "reason_code" in cols:
        parts.append(SQL_REAL_DECISION.split(" AND ")[0])
        parts.append("COALESCE(reason_code, '') != 'cache_hit'")
        parts.append("COALESCE(reason_code, '') != 'breaker_open'")
    if "final_provider" in cols:
        parts.append("COALESCE(final_provider, '') != 'cache'")
    return " AND ".join(parts) or "1"


def not_error_row_sql(con) -> str:
    """``SQL_NOT_ERROR_ROW`` for a sync sqlite3 connection, or ``1`` when the table has no
    ``reason`` column (a database older than P0.8-d has no error rows to exclude)."""
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(usage)")}
    except Exception:  # noqa: BLE001
        return "1"
    return SQL_NOT_ERROR_ROW if "reason" in cols else "1"


def is_error_reason(reason: object) -> bool:
    return isinstance(reason, str) and reason.startswith(ERROR_REASON_PREFIX)
