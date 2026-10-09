"""Read a few ``RouterConfig`` fields without importing pydantic-settings.

``llm_router.config`` costs ~31-52 ms to import (pydantic, pydantic-settings;
``python -X importtime``), which a per-prompt hook pays only to learn that a setting
nobody has touched is at its default (PG4 / P2-G-3). ``RouterConfig`` has no env
prefix: it reads each field's bare, case-insensitive name from the process
environment, then ``<state>/.env``, then the working directory's ``.env``. When none
of those can name the field, the field is at its declared default, and this module
returns that default without the import.

Any doubt takes the real path: if ``llm_router.config`` is already imported (a
server, the CLI), or an env var or ``.env`` file exists that could carry the field,
the value comes from ``get_config()`` exactly as before. ``DEFAULTS`` is pinned to
``RouterConfig``'s declared defaults by ``tests/test_pg4_auto_route_latency.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: field -> the default declared on ``RouterConfig``.
DEFAULTS: dict[str, object] = {
    "llm_router_persist_raw": False,
    "llm_router_persist_redaction": True,
    "llm_router_persist_ttl_days": 30,
    "session_context_enabled": True,
    "session_context_share_external": True,
    "session_context_max_tokens_draft": 3000,
}


def could_be_set(field: str) -> bool:
    """True when RouterConfig might read ``field`` from somewhere other than its default."""
    try:
        lowered = field.lower()
        if any(k.lower() == lowered for k in os.environ):
            return True
        from llm_router.paths import state_path

        return state_path(".env").exists() or Path(".env").exists()
    except Exception:  # noqa: BLE001 -- cannot tell: take the real path
        return True


def config_value(field: str) -> object:
    """``RouterConfig.<field>``; the declared default when it provably cannot differ.

    Raises whatever ``get_config()`` raises on the real path, so a caller's existing
    ``except`` and fallback behave as before.
    """
    default = DEFAULTS[field]
    if "llm_router.config" not in sys.modules and not could_be_set(field):
        return default
    from llm_router.config import get_config

    return getattr(get_config(), field, default)
