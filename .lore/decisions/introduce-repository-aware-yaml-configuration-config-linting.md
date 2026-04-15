# ADR: Introduce repository-aware YAML configuration, config linting, and yearly projection tokens

**Date**: 2026-04-10
**Status**: Accepted
**Affects**: src/llm_router/cli.py, src/llm_router/hooks/auto-route.py, src/llm_router/hooks/session-end.py, src/llm_router/repo_config.py, src/llm_router/router.py
**Risk**: medium
**Reversibility**: medium

## Decision

This commit introduces a new configuration system allowing for repository-specific YAML configurations (`.llm-router.yml`) in addition to user-level configurations. This provides flexibility for different projects to define their routing logic, blocked providers, and daily caps. The `config lint` command helps validate these configurations early. Yearly projection tokens likely enhance cost estimation or reporting. This allows for more granular and project-specific control over LLM routing and resource management, improving maintainability and reducing errors from misconfigurations.

## Consequences

_To be annotated as consequences become clear._
