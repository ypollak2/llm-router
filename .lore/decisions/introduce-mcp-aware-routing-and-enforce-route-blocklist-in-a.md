# ADR: Introduce MCP-aware routing and enforce-route blocklist in auto-route hook

**Date**: 2026-04-10
**Status**: Accepted
**Affects**: src/llm_router/hooks/auto-route.py, src/llm_router/hooks/enforce-route.py
**Risk**: high
**Reversibility**: low

## Decision

The system needs to intelligently route user prompts not just to LLMs, but also to specific 'Multi-Capability Platform' (MCP) servers when the user's intent matches the capabilities of those servers. This prevents unnecessary LLM calls and allows for direct interaction with specialized tools (e.g., Obsidian, GitHub, Calendar). The `enforce-route` hook is updated to allow for dynamic blocking of specific routes, giving more control over routing decisions and preventing misroutes. The previous `AUTO_ROUTE_ENABLED` toggle was too blunt, this allows for more granular control over blocking specific routes or tools rather than disabling the entire auto-routing system.

## Rejected Alternatives

The previous routing mechanism was too rigid, primarily focused on LLM routing directives and using a simple `AUTO_ROUTE_ENABLED` flag. This was implicitly rejected in favor of a more sophisticated, intent-based routing system that considers external MCP servers and provides granular control over route blocking.
## Consequences

_To be annotated as consequences become clear._
