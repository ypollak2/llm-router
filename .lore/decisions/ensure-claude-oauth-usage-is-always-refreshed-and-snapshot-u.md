# ADR: Ensure Claude OAuth usage is always refreshed and snapshot updated

**Date**: 2026-04-10
**Status**: Accepted
**Affects**: src/llm_router/hooks/session-end.py, src/llm_router/hooks/session-start.py
**Risk**: medium
**Reversibility**: medium

## Decision

Previously, the session-start hook might fail to refresh Claude usage if the `.env` file was not available to the hook (e.g., when hooks run outside the MCP server process). This led to stale usage data and incorrect delta calculations. The fix ensures that the `.env` is loaded by the hooks, and the `session-end` hook now writes a fresh snapshot, acting as a fallback if `session-start` fails. Additionally, CC mode detection is made implicit based on OAuth token presence, rather than relying solely on an environment variable, making it more robust.

## Consequences

_To be annotated as consequences become clear._
