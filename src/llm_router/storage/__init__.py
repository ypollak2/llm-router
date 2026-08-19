"""Storage abstraction layer for llm_router file operations.

Single entry point for all I/O: budgets, audit logs, configuration.
Routes decision logic (validation, classification) to cheap models (llm_query).
Keeps actual file I/O local and synchronous.
"""

from __future__ import annotations

from .service import storage_service

__all__ = ["storage_service"]
