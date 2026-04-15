# ADR: Introduce Latency-Aware and Personalized Routing with 'smart' enforcement mode and user acceptance feedback loop

**Date**: 2026-04-10
**Status**: Accepted
**Affects**: src/llm_router/benchmarks.py, src/llm_router/cli.py, src/llm_router/cost.py, src/llm_router/hooks/enforce-route.py, src/llm_router/hooks/session-start.py, src/llm_router/profiles.py, src/llm_router/router.py, src/llm_router/tools/admin.py, README.md, CHANGELOG.md
**Risk**: high
**Reversibility**: low

## Decision

The system previously used a 'hard' enforcement mode which could block developer workflow too aggressively. This decision introduces a 'smart' enforcement mode as the new default, aiming for a better balance between cost savings (routing compliance) and developer experience (workflow unblocking). It also incorporates user acceptance scores and latency stats into the routing algorithm to improve model selection and overall routing quality. The goal is to provide more intelligent, personalized, and performant model routing.

## Consequences

_To be annotated as consequences become clear._
