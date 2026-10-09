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
