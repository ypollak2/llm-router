# ADR: Introduce multi-agent CLI compatibility and new plugin for 'Factory' agent

**Date**: 2026-04-10
**Status**: Accepted
**Affects**: .claude-plugin/, .codex-plugin/, .factory-plugin/, src/llm_router/cli.py, src/llm_router/hooks/, src/llm_router/rules/, tests/test_multi_host_install.py
**Risk**: high
**Reversibility**: low

## Decision

Expand LLM Router's compatibility to more CLI environments, specifically introducing support for a new 'Factory' agent. This involves adding new plugin definitions, updating existing ones (Claude, Codex), enhancing the core CLI routing logic to accommodate multi-agent configurations, and adding new rules and hooks for specific agents (Gemini CLI, Opecode, Copilot CLI, etc.). This decision aims to broaden the user base and utility of the LLM router by integrating with more development workflows.

## Consequences

_To be annotated as consequences become clear._
