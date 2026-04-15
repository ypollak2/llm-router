# ADR: Enhance savings projection accuracy and persistence, add host attribution

**Date**: 2026-04-10
**Status**: Accepted
**Affects**: src/llm_router/cost.py, src/llm_router/hooks/session-end.py, src/llm_router/hooks/usage-refresh.py
**Risk**: medium
**Reversibility**: medium

## Decision

Improved cross-session persistence for savings_log data by importing it into SQLite on every refresh to prevent data loss. Added a 'host' column to savings_stats for better attribution. Enhanced projection accuracy in session-end summaries by prioritizing a 30-day average over a 7-day average for yearly savings projections, providing a more stable and representative forecast.

## Consequences

_To be annotated as consequences become clear._
