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

#: SQL predicate (usage table, ``reason`` column) that keeps only rows that are not error rows.
SQL_NOT_ERROR_ROW = "COALESCE(reason, '') NOT LIKE 'error\\_%' ESCAPE '\\'"


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
