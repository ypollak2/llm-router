# ADR: Introduce an interactive onboarding wizard to configure LLM Router

**Date**: 2026-04-10
**Status**: Accepted
**Affects**: src/llm_router/cli.py, src/llm_router/hooks/auto-route.py, src/llm_router/hooks/enforce-route.py, src/llm_router/hooks/session-end.py, src/llm_router/hooks/session-start.py
**Risk**: medium
**Reversibility**: medium

## Decision

To simplify the initial setup and adoption of LLM Router for new users. The wizard detects available LLM backends (Ollama, Codex, API keys) and recommends an enforcement profile (budget, balanced) and mode (shadow, suggest, enforce), then writes the configuration. This makes the tool more user-friendly and reduces friction.

## Consequences

_To be annotated as consequences become clear._
