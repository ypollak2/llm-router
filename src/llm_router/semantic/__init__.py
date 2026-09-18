"""Per-project semantic layer: one project scope, one derived index.

Currently exports the scope resolver. The structural index, the engineering
experience records and the retrieval pack land alongside it — see
docs/decisions/0002-semantic-layer.md for what this is and what it is not.
"""
from llm_router.semantic.scope import resolve_scope, scope_key

__all__ = ["resolve_scope", "scope_key"]
