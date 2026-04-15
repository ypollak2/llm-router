# ADR: Add multi-host adapter support for Codex CLI, Claude Desktop, and Copilot

**Date**: 2026-04-10
**Status**: Accepted
**Affects**: docs/multi-host-research.md, src/llm_router/cli.py, src/llm_router/hooks/stop-enforce.py, src/llm_router/rules/codex-rules.md, src/llm_router/rules/copilot-rules.md, src/llm_router/rules/desktop-rules.md
**Risk**: medium
**Reversibility**: medium

## Decision

To enable the llm-router to function as a backend for multiple LLM hosts (Codex CLI, Claude Desktop, Copilot), specific installation instructions and configuration rules are needed for each. This involves creating host-specific rule files and modifying the CLI to generate appropriate copy-paste configurations. Also includes documenting research around persistent savings and hook/classifier overlap, confirming existing functionality for cross-session savings, and clarifying FastMCP string return behavior.

## Consequences

_To be annotated as consequences become clear._
