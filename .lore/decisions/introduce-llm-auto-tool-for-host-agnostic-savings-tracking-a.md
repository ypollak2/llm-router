# ADR: Introduce llm_auto tool for host-agnostic savings tracking and wire savings log import into admin tools

**Date**: 2026-04-10
**Status**: Accepted
**Affects**: src/llm_router/tools/admin.py, src/llm_router/tools/routing.py
**Risk**: medium
**Reversibility**: medium

## Decision

To enable persistent savings tracking and cumulative value visibility across different hosts (e.g., Codex CLI, Claude Desktop, GitHub Copilot) that lack a UserPromptSubmit hook, a new tool `llm_auto` is introduced. This tool automatically flushes pending hook-written savings records (JSONL) into SQLite before routing and appends a compact savings envelope every 5 calls. Additionally, `import_savings_log` is explicitly called in `llm_usage` and `llm_savings` admin tools to ensure that any pending hook-written records are processed and included in the reported statistics before queries are made, providing more accurate and up-to-date information.

## Consequences

_To be annotated as consequences become clear._
