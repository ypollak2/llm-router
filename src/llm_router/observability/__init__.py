"""Observability surfaces for llm-router — the "router is working" signals.

Ported from Chuzom's observability layer. These modules are *consumers* of the
shared append-only event log (``~/.llm-router/savings_log.jsonl`` + ``usage.db``);
they never make routing decisions, so they stay decoupled from the router core.

  * :mod:`llm_router.observability.surface_status` — cross-surface status line /
    terminal title / OS notification for hosts without a native statusline.
"""

from __future__ import annotations

__all__ = ["surface_status"]
