# ADR: Agent-context chain reordering for LLM selection

**Date**: 2026-04-10
**Status**: Accepted
**Affects**: src/llm_router/router.py, src/llm_router/state.py, src/llm_router/tools/routing.py, .claude-plugin/, .codex-plugin/
**Risk**: medium
**Reversibility**: medium

## Decision

When specific agents (Claude Code or Codex) are identified, their subscription-covered models are prioritized in the LLM selection chain. This maximizes the utilization of already-paid capacity before falling back to pay-per-call APIs, optimizing cost efficiency for users of these agents. This is a behavioral change in how models are selected.

## Consequences

_To be annotated as consequences become clear._
