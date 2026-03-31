# LLM Router — Roadmap

> Last updated: 2026-03-31

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

---

## v1.1 — Observability (next)

**Theme**: You should always know what the router is doing and why.

| Feature | Priority | Notes |
|---|---|---|
| **Auto-refresh stale usage** | High | Hook triggers `llm_check_usage` automatically when data >30min, not just warns |
| **Web dashboard** | High | `localhost:7337` — routing breakdown, cost/day, model distribution, savings chart |
| **OTEL / Prometheus export** | Medium | Optional `--metrics-port`; counters for routed calls, cost, fallback rate per provider |
| **`llm_rate` feedback tool** | Medium | Per-response thumbs up/down stored in `routing_decisions`; feeds classifier confidence |
| **Hard budget alerts** | Medium | Desktop notification + hook warning when daily spend crosses threshold |

---

## v1.2 — Cost Intelligence

**Theme**: Stop paying for tokens you don't need.

| Feature | Priority | Notes |
|---|---|---|
| **Anthropic prompt caching** | High | Auto-inject `cache_control` breakpoints on system prompts >2000 tokens (up to 90% savings on repeated context) |
| **Semantic deduplication cache** | High | Embed-then-nearest-neighbour using local Ollama embeddings; threshold 0.95 cosine similarity |
| **Hard daily spend cap** | Medium | `DAILY_SPEND_LIMIT_USD` env var; router refuses calls when exceeded, returns clear error |
| **Cost forecasting** | Medium | Extrapolate hourly burn → "at current rate, weekly quota exhausted in Xh" |
| **Token budget per task type** | Low | Cap max_tokens per task type in config (prevents runaway research queries) |

---

## v1.3 — Routing Intelligence

**Theme**: Right model for the right job, not just right cost tier.

| Feature | Priority | Notes |
|---|---|---|
| **Task-aware model preferences** | High | Code → DeepSeek/Codex first; math → Gemini; writing → Claude/GPT-4o; overrides profile default |
| **Reasoning model tier** | High | New `deep_reasoning` complexity → routes to o3 / Gemini 2.5 Pro thinking / Claude extended thinking |
| **Context length routing** | Medium | Long conversations (>8k tokens) routed to models with large context windows; compact before sending to small-context models |
| **Learned routing** | Medium | Record (prompt_hash, model, was_good) from `llm_rate` → fine-tune local Qwen 0.5B classifier on Ollama after ~500 samples |
| **Benchmark auto-update** | Low | Weekly cron fetches latest MMLU/HumanEval scores from public leaderboards; updates routing weights |

---

## v1.4 — Agentic & Team Features

**Theme**: Works as well for 10-agent pipelines as for single prompts.

| Feature | Priority | Notes |
|---|---|---|
| **Agent-tree budget tracking** | High | Track total token spend across all sub-agents spawned in a session, not just top-level calls |
| **Tool-use routing** | High | Route tool-heavy prompts to GPT-4o/Sonnet; reasoning-only to Haiku |
| **User-defined YAML pipelines** | Medium | `~/.llm-router/pipelines/` — custom multi-step workflows without code |
| **Multi-user profiles** | Medium | Per-user quota pools with shared team budget; `.llm-router/users/` config |
| **Secrets manager support** | Medium | HashiCorp Vault, AWS Secrets Manager, 1Password CLI as alternatives to `.env` |

---

## v2.0 — Learning Router

**Theme**: The router gets smarter the more you use it.

Every routing decision is already recorded in SQLite. After ~500 calls, there's enough signal to train a tiny local classifier (Qwen 0.5B via Ollama) on *your specific usage patterns* — better than generic heuristics. Unlike RouteLLM (which trains on human preference datasets), this trains on your own history: your prompts, your providers, your quality ratings. Completely local, zero cloud cost, self-improving.

| Feature | Notes |
|---|---|
| **Routing history → training data** | Export `routing_decisions` rows with `was_good` labels to JSONL |
| **Local fine-tune pipeline** | `llm-router-train` CLI: fine-tunes Qwen 0.5B on Ollama, saves to `~/.llm-router/models/` |
| **Hot-swap classifier** | Server loads custom model on startup; falls back to heuristic chain if unavailable |
| **Continuous improvement loop** | Every `llm_rate` feedback → queue for next training run |
| **Model drift detection** | Alert when custom classifier disagrees with heuristic >20% — suggests retraining |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Highest-impact areas:

1. **v1.1 dashboard** — React + Vite, reads from `~/.llm-router/` SQLite + JSON
2. **v1.2 prompt caching** — LiteLLM `cache_control` integration
3. **v1.3 task-aware preferences** — extend `profiles.py` with task-type model affinity scores
4. **Provider integrations** — Bedrock, Azure, Vertex AI via LiteLLM
5. **Testing** — integration tests for provider fallback chains
