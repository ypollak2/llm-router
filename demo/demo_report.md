# LLM Router Demo Report

**Date**: 2026-03-31 00:18  |  **Mode**: LIVE  |  **Pressure**: 0% (default)

## Summary

| Metric | Value |
|--------|-------|
| Tasks run | 6 |
| Succeeded | 6/6 |
| Routing correct | 4/6 |
| Total cost | $0.034414 |
| Total tokens | 5,060 |
| Avg latency | 58568ms |

## Task Results

### T1: Quick concept check

- **Tier**: BUDGET  |  **Type**: query  |  **Profile**: budget
- **Expected model**: haiku / gemini-flash
- **Status**: ✅ Succeeded
- **Model used**: `gemini/gemini-2.5-flash`
- **Tokens**: 31 in / 864 out
- **Cost**: $0.002169  |  **Latency**: 5189ms
- **Routing**: ✅ ✅ Cheap model used for BUDGET tier (gemini/gemini-2.5-flash)

<details><summary>Response preview</summary>

> **Difference:** REST APIs fetch predefined resources from multiple URLs, whereas GraphQL APIs let the client precisely specify and retrieve only the d...

</details>

### T2: Simple README draft

- **Tier**: BUDGET  |  **Type**: generate  |  **Profile**: budget
- **Expected model**: haiku / gemini-flash
- **Status**: ✅ Succeeded
- **Model used**: `gemini/gemini-2.5-flash`
- **Tokens**: 152 in / 375 out
- **Cost**: $0.000983  |  **Latency**: 2126ms
- **Routing**: ✅ ✅ Cheap model used for BUDGET tier (gemini/gemini-2.5-flash)

<details><summary>Response preview</summary>

> Hey there! Welcome to this little todo app built with the awesome **FastAPI** framework, all powered by **Python**. It's designed to be super straight...

</details>

### T3: CRUD API implementation

- **Tier**: BALANCED  |  **Type**: code  |  **Profile**: balanced
- **Expected model**: claude-sonnet / gpt-4o
- **Status**: ✅ Succeeded
- **Model used**: `codex/gpt-5.4`
- **Tokens**: 0 in / 0 out
- **Cost**: $0.000000  |  **Latency**: 78432ms
- **Routing**: ✅ ✅ Mid-tier model for BALANCED (codex/gpt-5.4)

<details><summary>Response preview</summary>

> from collections.abc import AsyncIterator from contextlib import asynccontextmanager from typing import Annotated import os  import aiosqlite from fas...

</details>

### T4: Code review of the implementation

- **Tier**: BALANCED  |  **Type**: analyze  |  **Profile**: balanced
- **Expected model**: claude-sonnet / gpt-4o
- **Status**: ✅ Succeeded
- **Model used**: `codex/gpt-5.4`
- **Tokens**: 0 in / 0 out
- **Cost**: $0.000000  |  **Latency**: 53069ms
- **Routing**: ✅ ✅ Mid-tier model for BALANCED (codex/gpt-5.4)

<details><summary>Response preview</summary>

> Findings for `delete_todo`, ordered by severity:  - High: No authentication/authorization. Any caller who can hit this route can delete a todo. - High...

</details>

### T5: Research: latest FastAPI best practices

- **Tier**: BALANCED  |  **Type**: research  |  **Profile**: balanced
- **Expected model**: perplexity/sonar (web-grounded, always first for RESEARCH)
- **Status**: ✅ Succeeded
- **Model used**: `gemini/gemini-2.5-pro`
- **Tokens**: 585 in / 3053 out
- **Cost**: $0.031261  |  **Latency**: 35378ms
- **Routing**: ⚠️ ⚠️  Expected Perplexity for RESEARCH, got gemini/gemini-2.5-pro

<details><summary>Response preview</summary>

> Excellent question. Here are 3 specific, forward-looking best practices for building robust FastAPI applications in 2025.  ### 1. Database: Use Postgr...

</details>

### T6: Architecture decision: SQLite vs PostgreSQL

- **Tier**: PREMIUM  |  **Type**: analyze  |  **Profile**: premium
- **Expected model**: claude-opus / claude-sonnet (premium analysis)
- **Status**: ✅ Succeeded
- **Model used**: `codex/gpt-5.4`
- **Tokens**: 0 in / 0 out
- **Cost**: $0.000000  |  **Latency**: 177212ms
- **Routing**: ⚠️ ⚠️  Expected premium model, got codex/gpt-5.4

<details><summary>Response preview</summary>

> Assumption, and clearly an inference: a 10k-user todo app usually has a small dataset and modest write volume. For this kind of product, the deciding ...

</details>

## What Went Right

- **T1 (BUDGET)**: ✅ Cheap model used for BUDGET tier (gemini/gemini-2.5-flash)
- **T2 (BUDGET)**: ✅ Cheap model used for BUDGET tier (gemini/gemini-2.5-flash)
- **T3 (BALANCED)**: ✅ Mid-tier model for BALANCED (codex/gpt-5.4)
- **T4 (BALANCED)**: ✅ Mid-tier model for BALANCED (codex/gpt-5.4)

## What Went Wrong / Needs Fixing

- **T5 (BALANCED)**: ⚠️  Expected Perplexity for RESEARCH, got gemini/gemini-2.5-pro
- **T6 (PREMIUM)**: ⚠️  Expected premium model, got codex/gpt-5.4

## Routing Chain Inspection (dry-run only)

Chains shown are what the router *would* use at current pressure:

## Next Steps

Based on these results, refinements needed:

1. [ ] Any BUDGET tasks using expensive models → check pressure logic
2. [ ] Any BALANCED tasks under/over-routing → adjust chain ordering
3. [ ] Any RESEARCH tasks not using Perplexity → check RESEARCH static chain
4. [ ] Any failures → check API keys, model availability, error types
5. [ ] Compare dry-run chains vs live model used → verify no unexpected fallback