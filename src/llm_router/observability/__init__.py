"""Observability surfaces for llm-router — the "router is working" signals.

Ported from Chuzom's observability layer. These modules are *consumers* of the
shared append-only event log (``~/.llm-router/savings_log.jsonl`` + ``usage.db``);
they never make routing decisions, so they stay decoupled from the router core.

  * :mod:`llm_router.observability.surface_status` — cross-surface status line /
    terminal title / OS notification for hosts without a native statusline.
"""

from __future__ import annotations

# Re-export the OpenTelemetry layer from .core.
#
# Upstream keeps this as a top-level `observability.py` MODULE. Here that name
# is already a PACKAGE (surface_status, summary), and a module cannot sit
# beside a package of the same name — the package wins the import and the
# module becomes unreachable code that still passes every syntax check. So the
# sync lands it at `observability/core.py`.
#
# Without these re-exports the relocation is only half done: every upstream
# caller and test writes `observability.is_enabled()` / `.reset_for_test()`
# against what it believes is a module, and gets AttributeError on a package
# that happens to share the name. Eight tests failed exactly that way.
#
# Re-exporting rather than moving the code keeps ONE definition. Two copies at
# two paths is the drift this whole sync exists to stop.
from llm_router.observability.core import (  # noqa: F401
    emit_budget_breach,
    emit_pii_catch,
    emit_routing_decision,
    install_in_memory_exporter_for_test,
    is_enabled,
    reset_for_test,
    routing_span,
    setup,
)

__all__ = [
    "surface_status",
    "emit_budget_breach",
    "emit_pii_catch",
    "emit_routing_decision",
    "install_in_memory_exporter_for_test",
    "is_enabled",
    "reset_for_test",
    "routing_span",
    "setup",
]
