# ADR: Introduce Policy Engine, Savings Digest, and Community Benchmarks

**Date**: 2026-04-10
**Status**: Accepted
**Affects**: src/llm_router/policy.py, src/llm_router/digest.py, src/llm_router/community.py, src/llm_router/config.py, src/llm_router/repo_config.py, src/llm_router/router.py, src/llm_router/tools/admin.py, tests/test_v32_v33_v34_features.py, README.md, CHANGELOG.md
**Risk**: high
**Reversibility**: low

## Decision

Introduces new core functionalities for model routing control (Policy Engine), cost management and reporting (Savings Digest), and performance feedback (Community Benchmarks). These features enhance the system's ability to manage LLM usage, enforce organizational rules, provide financial insights, and leverage community feedback for better routing decisions. The Policy Engine, in particular, establishes a hierarchical system for model access control and cost caps, which is fundamental for large-scale enterprise adoption.

## Consequences

_To be annotated as consequences become clear._
