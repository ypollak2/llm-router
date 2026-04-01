# LLM Router — Versioned TODO

> This file is the persistent cross-session work tracker.
> Each version has a branch (`feature/v<N>-<theme>`), a detailed plan in `docs/plans/`, and a status below.
> Update status here when tasks complete. Never delete rows — mark ✅ instead.

---

## Legend

| Symbol | Meaning |
|--------|---------|
| ✅ | Shipped to main |
| 🔄 | In progress (branch open) |
| ⬜ | Planned, not started |
| ❌ | Cancelled / de-scoped |

---

## v1.1 — Codex-First Routing + Prepaid Capacity ✅

**Branch**: `feature/codex-plugin-cc-integration` → merged to main
**Plan**: N/A (implemented directly)
**Status**: All shipped

| Task | Status |
|------|--------|
| Raise simple-task threshold 85% → 95% in auto-route.py | ✅ |
| Codex injection priority rewrite (CODE: after first Claude; subscription: at front) | ✅ |
| Provider filter moved before Codex/Ollama injection (subscription mode bug) | ✅ |
| `is_codex_plugin_available()` detection function | ✅ |
| Expanded `CODEX_PATHS` with npm + Homebrew locations | ✅ |
| 14-test suite for Codex routing (14/14 passing) | ✅ |
| Sync `marketplace.json` version to 1.1.0 | ✅ |

---

## v1.2 — Foundation Hardening 🔄

**Branch**: `feature/v1.2-foundation`
**Plan**: [docs/plans/v1.2-plan.md](docs/plans/v1.2-plan.md)
**Status**: Branch created, implementation pending
**Theme**: Break the 88KB monolith. Make the codebase extensible and the install frictionless.

| Task | Status | Notes |
|------|--------|-------|
| Decompose `server.py` into `tools/` modules | ⬜ | routing, text, media, pipeline, admin, subscription, codex, setup |
| `server.py` becomes thin entrypoint (<150 lines) | ⬜ | Only FastMCP init + module imports |
| `llm-router install` one-command CLI | ⬜ | Wraps existing `install_hooks.py` logic |
| MCP registry submission (`mcp-registry.json`) | ⬜ | `registry.modelcontextprotocol.io` gap |
| All 396 tests still pass after decomposition | ⬜ | Non-negotiable gate |
| Version bump to 1.2.0 + CHANGELOG entry | ⬜ | |

---

## v1.3 — Developer Experience ⬜

**Branch**: `feature/v1.3-devex` (not yet created)
**Plan**: [docs/plans/v1.3-plan.md](docs/plans/v1.3-plan.md) (not yet written)
**Theme**: Make spending visible. Make routing configurable without code changes.

| Task | Status | Notes |
|------|--------|-------|
| Web dashboard `localhost:7337` | ⬜ | Routing breakdown, cost/day, savings chart, model distribution |
| Anthropic prompt caching (`cache_control` breakpoints) | ⬜ | Up to 90% savings on repeated system prompts >2000 tokens |
| Semantic deduplication cache (Ollama embeddings, cosine 0.95) | ⬜ | Avoid re-running near-identical prompts |
| Config-as-code routing rules (`~/.llm-router/routing.yaml`) | ⬜ | Runtime override of static `profiles.py` table |
| Hard daily spend cap (`DAILY_SPEND_LIMIT_USD`) | ⬜ | Router refuses calls + returns clear error when exceeded |
| Cost forecasting ("weekly quota exhausted in Xh") | ⬜ | Based on hourly burn rate |
| Version bump to 1.3.0 + CHANGELOG entry | ⬜ | |

---

## v1.4 — Routing Intelligence ⬜

**Branch**: `feature/v1.4-routing-intelligence` (not yet created)
**Plan**: [docs/plans/v1.4-plan.md](docs/plans/v1.4-plan.md) (not yet written)
**Theme**: Right model for the right job, not just cheapest available tier.

| Task | Status | Notes |
|------|--------|-------|
| Task-aware model preferences (CODE→DeepSeek/Codex; math→Gemini; writing→Claude) | ⬜ | Extends `profiles.py` with task-type affinity |
| `deep_reasoning` complexity tier (→ o3 / Gemini 2.5 Pro thinking) | ⬜ | New complexity value in classifier |
| Context length routing (long convos → large-context models) | ⬜ | Compact before sending to small-context models |
| Codex MCP documentation + Codex marketplace submission | ⬜ | `docs/codex-mcp-setup.md` + `.claude-plugin/` polish |
| Claude Code marketplace submission | ⬜ | Submit to official registry |
| Learned routing (Qwen 0.5B fine-tune after 500+ samples) | ⬜ | Uses `llm_rate` feedback + `routing_decisions` |
| Version bump to 1.4.0 + CHANGELOG entry | ⬜ | |

---

## v1.5 — Agentic & Observability ⬜

**Branch**: `feature/v1.5-agentic` (not yet created)
**Plan**: [docs/plans/v1.5-plan.md](docs/plans/v1.5-plan.md) (not yet written)
**Theme**: Works as well for 10-agent pipelines as for single prompts.

| Task | Status | Notes |
|------|--------|-------|
| Agent-tree budget tracking (total spend across sub-agents) | ⬜ | |
| Tool-use routing (tool-heavy → GPT-4o/Sonnet; reasoning-only → Haiku) | ⬜ | |
| OTEL / Prometheus export (`--metrics-port`) | ⬜ | Counters: routed calls, cost, fallback rate per provider |
| Multi-user profiles (per-user quota pools, shared team budget) | ⬜ | |
| User-defined YAML pipelines (`~/.llm-router/pipelines/`) | ⬜ | |
| Version bump to 1.5.0 + CHANGELOG entry | ⬜ | |

---

## v2.0 — Learning Router ⬜

**Branch**: `feature/v2.0-learning-router` (not yet created)
**Plan**: [docs/plans/v2.0-plan.md](docs/plans/v2.0-plan.md) (not yet written)
**Theme**: The router gets smarter the more you use it.

| Task | Status | Notes |
|------|--------|-------|
| Export `routing_decisions` → JSONL training data | ⬜ | |
| `llm-router-train` CLI (fine-tunes Qwen 0.5B via Ollama) | ⬜ | |
| Hot-swap classifier (loads custom model on startup) | ⬜ | Falls back to heuristic chain |
| Continuous improvement loop (`llm_rate` → training queue) | ⬜ | |
| Model drift detection (alert when custom classifier disagrees >20%) | ⬜ | |
| Version bump to 2.0.0 + CHANGELOG entry | ⬜ | |

---

## How to use this file

1. **Starting a version**: Create branch `feature/v<N>-<theme>`, write `docs/plans/v<N>-plan.md`, mark version row as 🔄
2. **During work**: Check off tasks ✅ as they land on the branch
3. **Shipping**: Merge branch → main, mark version as ✅, update `pyproject.toml` version + CHANGELOG.md
4. **Next session**: Read this file first to orient, then read the active version's plan doc
