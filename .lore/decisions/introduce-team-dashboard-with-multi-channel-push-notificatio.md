# ADR: Introduce Team Dashboard with Multi-Channel Push Notifications and Team Identity

**Date**: 2026-04-10
**Status**: Accepted
**Affects**: src/llm_router/team.py, src/llm_router/cli.py, src/llm_router/config.py, src/llm_router/cost.py, src/llm_router/tools/admin.py, .llm-router.yml, CHANGELOG.md, src/llm_router/server.py, .claude-plugin/plugin.json, pyproject.toml, tests/test_router.py, tests/test_server.py, uv.lock
**Risk**: high
**Reversibility**: low

## Decision

To provide visibility into LLM usage and cost savings for development teams. This feature enables aggregation of usage data by user and project, and pushes reports to various communication channels (Slack, Discord, Telegram, generic webhooks) in their native formats. This addresses a common need for transparency and cost management in organizations using LLM-router.

## Consequences

_To be annotated as consequences become clear._
