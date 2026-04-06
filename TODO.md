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

| Task | Status |
|------|--------|
| Raise simple-task threshold 85% → 95% in auto-route.py | ✅ |
| Codex injection priority rewrite | ✅ |
| Provider filter moved before Codex/Ollama injection | ✅ |
| `is_codex_plugin_available()` detection function | ✅ |
| Expanded `CODEX_PATHS` with npm + Homebrew locations | ✅ |
| 14-test suite for Codex routing | ✅ |
| Sync `marketplace.json` version to 1.1.0 | ✅ |

---

## v1.2 — Foundation Hardening ✅

| Task | Status | Notes |
|------|--------|-------|
| Decompose `server.py` into `tools/` modules | ✅ | routing, text, media, pipeline, admin, subscription, codex, setup |
| `server.py` becomes thin entrypoint (<150 lines) | ✅ | 110 lines |
| `llm-router install` one-command CLI | ✅ | install/--check/--force/uninstall |
| MCP registry submission (`mcp-registry.json`) | ✅ | |
| All tests pass after decomposition | ✅ | |

---

## v1.3 — Observability ✅

| Task | Status | Notes |
|------|--------|-------|
| Web dashboard `localhost:7337` | ✅ | Routing breakdown, cost/day, savings chart, model distribution |
| Anthropic prompt caching (`cache_control` breakpoints) | ✅ | Up to 90% savings on repeated system prompts |
| Semantic deduplication cache (Ollama embeddings, cosine 0.95) | ✅ | |
| Hard daily spend cap (`DAILY_SPEND_LIMIT_USD`) | ✅ | |
| `llm-router doctor` health check command | ✅ | |
| `llm-router setup` interactive wizard | ✅ | |
| `llm-router demo` command | ✅ | |
| `deep_reasoning` complexity tier | ✅ | Routes to extended thinking |
| Visible routing indicator in hook output | ✅ | |
| Shareable savings line in session-end | ✅ | |
| Smithery marketplace listing (`smithery.yaml`) | ✅ | |
| Cross-IDE docs (Cursor, Windsurf, Zed) | ✅ | |
| Friendly auth error messages | ✅ | |

---

## v1.4 — Developer Ergonomics ✅

| Task | Status | Notes |
|------|--------|-------|
| `llm-router status` real cumulative savings | ✅ | Today/7d/30d/all-time with bar charts |
| `llm-router update` command | ✅ | Re-installs hooks, checks PyPI version |
| Linux/Windows compatibility (`sys.executable`, chmod skip) | ✅ | |
| CI hang fix (`pytest-timeout`, `timeout-minutes: 10`) | ✅ | |
| `llm-router demo` real routing history from DB | ✅ | Falls back to examples when DB empty |
| `llm-router uninstall --purge` | ✅ | Deletes `~/.llm-router/` after confirmation |
| Animated SVG demo in README | ✅ | `docs/images/demo.svg` via svg-term |
| Dashboard savings gauge — real data from `usage` table | ✅ | Was reading empty `savings_stats` table |
| Dashboard recent traffic — real data from `usage` table | ✅ | Was reading empty `routing_decisions` table |
| Dashboard version — dynamic from `importlib.metadata` | ✅ | Was hardcoded `v1.3` |
| Railway SSE deployment support | ✅ | Reads `$PORT`/`$HOST` from env |

---

## v1.5 — Configuration & Transparency 🔄

**Theme**: Power users should be able to customize and understand routing without touching source code.

| Task | Status | Notes |
|------|--------|-------|
| `llm_fs_find` / `llm_fs_rename` / `llm_fs_edit_many` MCP tools | ✅ | Routes filesystem reasoning to Haiku/Ollama |
| Free-model savings in `llm-router status` | ✅ | Ollama/Codex savings vs Sonnet baseline |
| `~/.llm-router/routing.yaml` custom overrides | ⬜ | Pin tasks to models, block providers, per-type daily caps |
| `llm-router test <prompt>` dry-run classifier | ⬜ | Show routing decision without making an API call |
| Routing explain mode (`LLM_ROUTER_EXPLAIN=1`) | ⬜ | Prepend `[→ haiku, reason: simple, 92%]` to responses |
| Provider latency tracking (`response_ms` in usage.db) | ⬜ | P50/P95 per model in `llm-router status` |
| Dashboard savings breakdown panel | ⬜ | Token volume + actual vs Sonnet/Opus baseline |
| Version bump to 1.5.0 + CHANGELOG entry | ✅ | |

---

## v1.6 — Growth & Ecosystem ⬜

**Theme**: Make savings visible, shareable, and spread the tool virally.

| Task | Status | Notes |
|------|--------|-------|
| `llm-router share` shareable savings card | ⬜ | Markdown/ASCII card, copy-to-clipboard |
| Webhook support — daily summary to Slack/Discord | ⬜ | POST digest to any webhook URL |
| `llm-router leaderboard` personal model rankings | ⬜ | Quality × cost × latency from real data |
| VS Code / Cursor status bar extension | ⬜ | Quick profile toggle in IDE status bar |
| Version bump to 1.6.0 + CHANGELOG entry | ⬜ | |

---

## v2.0 — Learning Router ⬜

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
