# LLM Router — Roadmap

> Last updated: 2026-04-01

## Vision

A single MCP server that gives Claude Code unified access to every LLM provider — automatically picking the right model for each task, managing budgets across subscriptions and APIs, and maximising quality while minimising cost. The router gets smarter the more you use it: routing history trains a local classifier that learns *your* patterns, not generic heuristics.

---

## What makes this different

Most LLM routers (LiteLLM proxy, OpenRouter, Portkey, RouteLLM) are API gateways — they sit between your code and providers. This router is **IDE-native**:

| Capability | LiteLLM / OpenRouter | Portkey | RouteLLM | **llm-router** |
|---|---|---|---|---|
| MCP integration | ✗ | ✗ | ✗ | ✓ |
| Claude Code hook injection | ✗ | ✗ | ✗ | ✓ |
| Subscription-quota routing | ✗ | ✗ | ✗ | ✓ |
| Free local classifier chain | ✗ | ✗ | partial | ✓ |
| Semantic caching | partial | ✓ | ✗ | planned |
| Web dashboard | ✓ | ✓ | ✗ | planned |
| OTEL / Prometheus | ✓ | ✓ | ✗ | planned |
| Learned routing | ✗ | ✗ | ✓ | planned |
| Multi-user / team | ✓ | ✓ | ✗ | planned |

---

## Completed

### v0.1–v0.9 (Foundation → Global Enforcement)

- Core text LLM routing (20+ providers via LiteLLM)
- Budget / balanced / premium profiles with fallback chains
- Cost tracking with SQLite + lifetime savings analytics
- Health checks with circuit breaker + stale reset
- Image / video / audio generation routing
- Monthly budget enforcement with hard limits
- Multi-step orchestration with pipeline templates
- Claude Code plugin: 24 MCP tools + 6 hooks
- Complexity-first routing (simple→Haiku, moderate→Sonnet, complex→Opus)
- Live Claude subscription monitoring (session %, weekly %, Sonnet %)
- Pressure cascade (85%/95%/99% thresholds, tier-by-tier external fallback)
- Codex desktop integration (local agent, free via OpenAI subscription)
- Prompt classification cache (SHA-256 exact-match, in-memory LRU, 1h TTL)
- Auto-route hook (UserPromptSubmit multi-layer classifier: heuristic→Ollama→API)
- SubagentStart hook (injects routing context into spawned agents)
- Structural context compaction (5 strategies)
- Quality logging (`routing_decisions` table + `llm_quality_report` tool)
- Savings persistence (JSONL + SQLite import, lifetime analytics)
- Global hook installer + routing rules auto-update
- UUID session IDs, stale circuit-breaker reset
- OAuth-based Claude usage refresh (replaces AppleScript)
- Published to PyPI as `claude-code-llm-router`

### v1.3 — Observability (2026-04-04 → 2026-04-05)

- **Web dashboard** `localhost:7337` — routing breakdown, cost/day, model distribution, savings chart, animated Liquid Glass redesign
- **Anthropic prompt caching** — auto `cache_control` breakpoints on system prompts ≥1024 tokens (up to 90% savings on repeated context)
- **Semantic dedup cache** — Ollama embeddings + 0.95 cosine similarity; skips LLM call for near-duplicate prompts
- **Hard daily spend cap** — `LLM_ROUTER_DAILY_SPEND_LIMIT` env var; raises `BudgetExceededError` when exceeded
- **`llm-router doctor`** — comprehensive ANSI-colored health check command
- **`llm-router setup`** — interactive provider wizard
- **`llm-router demo`** — routing table with color-coded complexity, ANSI-aware layout
- **`deep_reasoning` complexity tier** — extended thinking via Claude models; new classifier heuristics
- **Visible routing indicator** — `⚡ llm-router →` shown in terminal on every hook fire
- **Shareable savings line** in session-end summary
- **Smithery marketplace** — `smithery.yaml` for one-click install
- **Cross-IDE docs** — Cursor, Windsurf, Zed install snippets in README
- **Friendly auth error messages** — names exact env var to set, explains subscription vs API key

### v1.4 — Developer Ergonomics (2026-04-05 → 2026-04-06)

- **`llm-router status` real savings** — today/7d/30d/all-time with ASCII bar charts and subscription pressure
- **`llm-router update`** — re-installs hooks + rules, checks PyPI for newer version
- **`llm-router demo` real routing history** — shows last 8 actual decisions from usage.db
- **`llm-router uninstall --purge`** — deletes `~/.llm-router/` after confirmation
- **Linux/Windows compat** — `sys.executable` in hooks, chmod skip on Windows
- **CI hang fix** — `pytest-timeout` + `timeout-minutes: 10`
- **Animated SVG demo** in README via svg-term + asciinema cast
- **Dashboard fixes** — savings gauge + recent traffic read from `usage` table; version from metadata
- **Railway SSE deployment** — reads `$PORT`/`$HOST` for PaaS hosting

### v1.0.0 (Routing Integrity — 2026-03-31)

Eight correctness fixes making the routing guarantees production-solid:

1. **Subscription flag enforcement** — `available_providers()` now actually excludes Anthropic when `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`
2. **Unified pressure path** — `llm_route` now calls `select_model()` (same as `llm_classify`), eliminating profile divergence under quota pressure
3. **Staleness warnings** — all 3 routing hooks flag `⚠️ STALE` when `usage.json` is >30 minutes old
4. **Ollama config clarification** — `OLLAMA_URL` (classifier) vs `OLLAMA_BASE_URL` (answerer) documented
5. **Frozen config fix** — `llm_set_profile` uses module-level `_active_profile` instead of `object.__setattr__()` on frozen Pydantic model
6. **Health-aware classifier** — classifier chain sorts unhealthy providers to the back
7. **Atomic counter writes** — `usage-refresh.py` uses `os.replace()` to prevent concurrent hook race
8. **Research fallback escalation** — no Perplexity key → PREMIUM chain instead of silent BALANCED downgrade

### v1.1.0 (Subscription-Aware Routing — 2026-04-01)

- **Codex-first routing** — Codex injected before paid externals for CODE tasks (free-prepaid-first hierarchy)
- **Prepaid capacity threshold fix** — simple tasks stay on Haiku subscription until Sonnet exhausted (≥ 95%), not at 85% session
- **`llm_rate` feedback tool** — per-response thumbs up/down, stored in `routing_decisions`
- **Daily spend alerts** — warning + desktop notification when daily spend threshold crossed
- **Session delta display** — CC subscription usage delta shown at session end

### v1.2.0 (Foundation Hardening — 2026-04-01)

- **Codex-first for all task types** — ANALYZE, GENERATE, QUERY also prefer free Codex over paid externals
- **Server decomposition** — 2,300-line `server.py` split into `state.py` + 8 tools modules (`tools/routing.py`, `tools/text.py`, `tools/media.py`, `tools/pipeline.py`, `tools/admin.py`, `tools/subscription.py`, `tools/codex.py`, `tools/setup.py`)
- **`llm-router install` CLI** — unified `install / --check / --force / uninstall` replaces `llm-router-install-hooks`
- **MCP registry manifest** — `mcp-registry.json` for modelcontextprotocol.io submission

---

## v1.3 — Observability ✅ Complete

**Theme**: You should always know what the router is doing and why.

| Feature | Priority | Notes |
|---|---|---|
| ~~**Web dashboard**~~ | ✅ v1.3.0 | `localhost:7337` — routing breakdown, cost/day, model distribution, savings chart |
| **OTEL / Prometheus export** | Medium | Optional `--metrics-port`; counters for routed calls, cost, fallback rate per provider |
| ~~**Anthropic prompt caching**~~ | ✅ v1.3.0 | Auto-inject `cache_control` breakpoints on system prompts ≥1024 tokens (up to 90% savings on repeated context) |
| ~~**Semantic dedup cache**~~ | ✅ v1.3.0 | Embed-then-nearest-neighbour using Ollama embeddings; 0.95 cosine similarity, 24h TTL |
| ~~**Hard daily spend cap**~~ | ✅ v1.3.0 | `LLM_ROUTER_DAILY_SPEND_LIMIT` env var; router raises `BudgetExceededError` when exceeded |

---

## v1.5 — Configuration & Transparency

**Theme**: Power users should be able to customize and understand routing without touching source code.

| Feature | Priority | Notes |
|---|---|---|
| **`~/.llm-router/routing.yaml` custom overrides** | High | Pin tasks to models, block providers, per-type daily caps (`image: $2.00`) |
| **`llm-router test <prompt>` dry-run** | High | Show routing decision without making an API call; helps tune and debug |
| **Routing explain mode** (`LLM_ROUTER_EXPLAIN=1`) | Medium | Prepend `[→ haiku, reason: simple query, 92% confidence]` to every routed response |
| **Provider latency tracking** | Medium | Store `response_ms` in usage.db; `llm-router status` shows P50/P95 per model |
| **Dashboard savings breakdown panel** | Medium | Token volume + actual spend vs Sonnet/Opus baseline side-by-side |

---

## v1.6 — Growth & Sharing ✅ Complete

| Feature | Status | Notes |
|---|---|---|
| ~~`llm-router share`~~ | ✅ v1.6.0 | ASCII savings card + clipboard + pre-filled tweet |
| ~~One-time star CTA in session summary~~ | ✅ v1.6.0 | Fires once when lifetime savings > $0.50 |
| **Webhook support** | ⬜ | Daily routing summary to Slack/Discord/generic URL |
| **`llm-router leaderboard`** | ⬜ | Personal model rankings: quality × cost × latency |
| **VS Code / Cursor status bar** | ⬜ | Quick profile toggle in IDE status bar |

---

## v1.7 — Multi-Harness Docs + claw-code Layer 1

**Theme**: llm-router works everywhere the Claude Code architecture runs.

| Feature | Priority | Notes |
|---|---|---|
| **README: claw-code MCP snippet** | ✅ v1.7.0 | Config snippet + savings context (no subscription = even more valuable) |
| **README: OpenClaw MCP snippet** | ✅ v1.7.0 | `openclaw mcp add llm-router` one-liner |
| **README: Agno MCP example** | ✅ v1.7.0 | `MCPTools(command="llm-router")` pattern |
| **ROADMAP: v1.8–v2.0 ecosystem phases** | ✅ v1.7.0 | Documented below |
| **Smithery `smithery.yaml`: add claw-code/OpenClaw compatibility** | High | Signals to marketplace that it works beyond Claude Code |
| **`llm-router install --claw-code`** | High | Detect `~/.claw-code/settings.json`, write hooks |

---

## v1.8 — claw-code Hook Install

**Theme**: First-class hook integration for claw-code users.

| Feature | Priority | Notes |
|---|---|---|
| **`install_hooks.py`: claw-code path detection** | High | `~/.claw-code/settings.json` + XDG fallback |
| **Adapted `session-end` hook (no-subscription mode)** | High | Drop CC pressure section; keep free/paid/savings |
| **Adapted `status-bar` hook (no-subscription mode)** | High | Drop `CC N%s` prefix; show only routing stats |
| **`llm-router doctor` detects claw-code** | Medium | Reports hook status for claw-code installs |
| **`llm-router install` auto-detects claw-code** | Medium | Offers to install in claw-code when detected |

---

## v1.9 — OpenClaw Skill Package

**Theme**: Passive distribution via the OpenClaw Skills marketplace.

| Feature | Priority | Notes |
|---|---|---|
| **`src/llm_router/skills/openclaw/` package** | High | `skill.json` + `before-prompt.py` + `session-end.py` + `mcp.json` |
| **`llm-router install --openclaw`** | High | Packages and registers the skill in one command |
| **Publish to OpenClaw skill registry** | High | Passive discovery from the marketplace |
| **OpenClaw-specific hook adaptations** | Medium | Respect OpenClaw's `BeforeToolCall` hook format |

---

## v2.0 — Agno Adapter

**Theme**: llm-router becomes the smart model layer inside multi-agent Python workflows.

Every Agno agent takes a `model=` parameter. `RouteredModel` makes llm-router a transparent drop-in — routing each prompt to the cheapest capable model without changing any agent logic.

| Feature | Notes |
|---|---|
| **`src/llm_router/adapters/agno.py`** | `RouteredModel` — drop-in Agno `Model` subclass that classifies + routes each prompt |
| **`RouteredTeam`** | Budget-aware team wrapper; `monthly_budget_usd` enforced across all agents |
| **`pip install claude-code-llm-router[agno]`** | Optional extra dep group; agno not required for core package |
| **Example: `docs/examples/agno_team_routing.py`** | 3-agent team (researcher + analyst + writer) with per-role routing |
| **Integration tests with Agno test harness** | Confirm `RouteredModel` satisfies Agno's `Model` ABC |

### Why this matters

In a 3-agent Agno Team without routing, all agents pay Sonnet rates. With `RouteredTeam`:

```python
# Before: 3 × Sonnet = 3× cost
team = Team(agents=[researcher, analyst, writer])

# After: writer → Haiku (90% cheaper), researcher → Perplexity, analyst → Sonnet
team = RouteredTeam(
    agents=[researcher, analyst, writer],
    monthly_budget_usd=20.0,
)
```

---

## v2.1 — Learning Router

**Theme**: The router gets smarter the more you use it.

Every routing decision is already recorded in SQLite. After ~500 calls, there's enough signal to train a tiny local classifier (Qwen 0.5B via Ollama) on *your specific usage patterns*.

| Feature | Notes |
|---|---|
| **Routing history → training data** | Export `routing_decisions` rows with `was_good` labels to JSONL |
| **Local fine-tune pipeline** | `llm-router-train` CLI: fine-tunes Qwen 0.5B on Ollama |
| **Hot-swap classifier** | Server loads custom model on startup; falls back to heuristic chain |
| **Continuous improvement loop** | Every `llm_rate` feedback → queue for next training run |
| **Model drift detection** | Alert when custom classifier disagrees with heuristic >20% |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Highest-impact areas:

1. **v1.8 claw-code hooks** — adapt session-end and status-bar for no-subscription mode
2. **v1.9 OpenClaw skill** — `skill.json` format + hook compatibility with OpenClaw's pipeline
3. **v2.0 Agno adapter** — `RouteredModel` implementing Agno's `Model` ABC
4. **Provider integrations** — Bedrock, Azure, Vertex AI via LiteLLM
5. **Testing** — integration tests for provider fallback chains
