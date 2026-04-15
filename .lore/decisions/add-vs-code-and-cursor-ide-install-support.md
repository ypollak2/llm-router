# ADR: Add VS Code and Cursor IDE install support

**Date**: 2026-04-10
**Status**: Accepted
**Affects**: src/llm_router/cli.py, src/llm_router/rules/, tests/test_vscode_cursor_install.py, .claude-plugin/, .factory-plugin/, CHANGELOG.md, README.md
**Risk**: medium
**Reversibility**: medium

## Decision

To expand compatibility and ease of use for the LLM Router, allowing users of VS Code and Cursor IDEs to integrate the router directly. This involves specific installation routines and configuration file formats for each IDE (e.g., 'servers' root key for VS Code, 'mcpServers' for Cursor).

## Consequences

_To be annotated as consequences become clear._
