# LLM Router — Roadmap

> Last updated: 2026-04-09

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
| Web dashboard | ✓ | ✓ | ✗ | ✓ |
| OTEL / Prometheus | ✓ | ✓ | ✗ | planned |
| Learned routing | ✗ | ✗ | ✓ | planned |
| Multi-user / team | ✓ | ✓ | ✗ | ✓ |

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

## v2.0 — Agno Adapter ✅ Complete

**Theme**: llm-router becomes the smart model layer inside multi-agent Python workflows.

Every Agno agent takes a `model=` parameter. `RouteredModel` makes llm-router a transparent drop-in — routing each prompt to the cheapest capable model without changing any agent logic.

| Feature | Notes |
|---|---|
| **`src/llm_router/adapters/agno.py`** | `RouteredModel` — drop-in Agno `Model` subclass that classifies + routes each prompt |
| **`RouteredTeam`** | Budget-aware team wrapper; `monthly_budget_usd` enforced across all agents |
| **`pip install claude-code-llm-router[agno]`** | Optional extra dep group; agno not required for core package |
| **Example: `docs/examples/agno_team_routing.py`** | 3-agent team (researcher + analyst + writer) with per-role routing |
| **Integration tests with Agno test harness** | Confirm `RouteredModel` satisfies Agno's `Model` ABC |

---

## Phase 1 — Trust & Proof (Apr–Jun 2026)

**Marketing positioning**: *"Claude Code's cost autopilot. Stop paying Opus prices for Haiku work."*

The biggest adoption blocker is not routing quality — it is trust. One visible misroute outweighs dozens of invisible cheap wins. Phase 1 builds the trust infrastructure before optimising the routing engine.

### v2.1 — Route Simulator ✅ Complete (2026-04-08)

**Headline**: *"See where your prompts would go before you trust the autopilot."*

| Feature | Notes |
|---|---|
| **`llm-router test "<prompt>"` dry-run** | Show routing decision + cost estimate without making any API call |
| **Savings DB schema expansion** | Add `baseline_model`, `potential_cost_usd`, `saved_usd`, `is_simulated` columns to `usage` table |
| **Simulator populates DB with `is_simulated=True`** | Lets users see projected savings before enabling enforce mode |
| **`llm_dashboard` MCP tool** | Returns formatted savings table: today / week / month / all-time with efficiency multiplier |
| **Enhanced status bar** | Time-bucketed savings (D/W/M), provider health icons, enforcement mode badge, Nx efficiency score |

---

### v2.2 — Explainable Routing ✅ Done (2026-04-08)

**Headline**: *"Every route has a why."*

| Feature | Status | Notes |
|---|---|---|
| **`LLM_ROUTER_EXPLAIN=1` mode** | ✅ | Prepends `[→ model · task · $cost · Nx cheaper]` to every routed response |
| **"Why not Sonnet/Opus?" comparison** | ✅ | `llm_classify` shows cost multiplier for each skipped tier |
| **`reason_code` in quality log** | ✅ | `routing_decisions` table gains `reason_code` column |
| **Per-decision explanation** | ✅ | `router.py` propagates classification reason to DB |
| **Missed-savings labelling** | 📅 v2.3 | `[unroutable]` markers for non-interceptable turns |

---

### v2.3 — Zero-Friction Activation + Savings Analytics (early Jun 2026)

**Headline**: *"Go from install to first savings in under 2 minutes. Then watch the number grow."*

| Feature | Notes |
|---|---|
| **Guided onboarding wizard** | Auto-detects Codex/Ollama, recommends profile, fails with precise remediation steps |
| **`shadow` / `suggest` / `enforce` activation modes** | `shadow` = observe only; `suggest` = hint in output; `enforce` = block + route |
| **Shareable savings card** | `llm-router share` generates ASCII card: savings D/W/M/YTD + efficiency multiplier |
| **Weekly digest hook** | Monday notification: "You saved $8.91 last week — your AI is 17x more efficient" |
| **Yearly projection** | Session summary shows: "At this rate you'll save $180/year" |

---

## Phase 2 — Smarter Routing (Jun–Aug 2026)

Once users trust the router and can inspect its decisions, Phase 2 makes routing genuinely smarter. These features have no value without the trust infrastructure from Phase 1.

### v2.4 — Repo-Aware YAML Config (late Jun 2026)

**Headline**: *"One repo, one routing policy — committed to git."*

| Feature | Notes |
|---|---|
| **`.llm-router.yml` repo config** | Pin tasks to models, block providers, per-path rules |
| **`~/.llm-router/routing.yaml` user overrides** | Per-type daily caps, preferred providers |
| **Repo fingerprinting** | Detects language/framework/size and suggests an appropriate profile |
| **`llm-router config lint`** | Validates config and previews effective routing table |
| **Config precedence**: org → user → repo → prompt | Consistent override hierarchy |

---

### v2.5 — Context-Aware Routing (late Jul 2026)

**Headline**: *"'Yes', 'continue', 'do it' — the router understands what you actually meant."*

Short continuation prompts are currently classified on their 3 words alone, losing the task context. This version fixes that with a synthetic prompt pre-processor.

| Feature | Notes |
|---|---|
| **Continuation prompt detector** | Regex: `^(yes\|no\|ok\|continue\|proceed\|go ahead\|do it\|y\|n)\.?$` + <5 word check |
| **Synthetic prompt injection** | Prepends last assistant message: `"User confirmed to proceed with: {context}"` |
| **Negative continuation handling** | "no" / "skip" → cheap model, correct task type from context |
| **Statement vs question detection** | Assistant statements → treat "ok" as acknowledgement (low complexity), not command |
| **Configurable via `.llm-router.yml`** | `context_aware_routing: true/false` |

---

### v2.6 — Latency-Aware + Personalized Routing ✅ Complete (2026-04-09)

**Headline**: *"The router learns what you actually accept — and keeps it fast."*

| Feature | Notes |
|---|---|
| ~~**p95 latency in model scoring**~~ | ✅ Latency as tiebreaker between equally cheap models |
| ~~**Cold-start penalties for local models**~~ | ✅ Ollama first-request penalty accounted for in selection |
| ~~**Per-user acceptance signals**~~ | ✅ Local tracking of `llm_rate` thumbs / re-prompts as routing feedback |
| ~~**Score blending**~~ | ✅ Per-user weights blend with global benchmarks; reset/opt-out controls |
| ~~**Latency dashboard**~~ | ✅ `llm-router status` shows P50/P95 per provider |

---

## Phase 3 — Team Infrastructure (Sep–Nov 2026)

### v3.0 — Team Dashboard ✅ Complete (2026-04-08)

**Headline**: *"See savings across the whole team, not just your laptop."*

| Feature | Notes |
|---|---|
| **Shared telemetry collector** | Optional self-hosted endpoint; `usage.db` rows replicated with `user_id` |
| **Org / project / user dashboard views** | Savings by user, route mix, expensive-call leakage |
| **Team onboarding bootstrap** | Invite token flow; shared profile defaults |
| **`llm_team_report` / `llm_team_push` MCP tools** | Push local savings to shared webhook; team-wide report |

---

### v3.1 — Multi-Host + Cross-Session Savings ✅ Complete (2026-04-09)

**Headline**: *"Savings persist across sessions. llm-router works in Codex, Desktop, and Copilot too."*

| Feature | Notes |
|---|---|
| **`llm_auto` MCP tool** | Sibling to `llm_route` for hook-less hosts; server-side JSONL flush + savings envelope |
| **Cross-session savings wiring** | `import_savings_log()` called on every `llm_savings`/`llm_usage` — root bug fixed |
| **`host` column in `savings_stats`** | Tracks origin of each routed call; idempotent migration |
| **`llm-router install --host codex\|desktop\|copilot\|all`** | Prints copy-paste config snippets for each host |
| **Per-host routing rules** | `codex-rules.md`, `desktop-rules.md`, `copilot-rules.md` |
| **Yearly projection accuracy** | 30-day month-based average; fallback chain: month → week → today |

---

### v3.2 — Policy Engine ✅ Complete (2026-04-09)

**Headline**: *"Set routing policy once — enforce it everywhere."*

| Feature | Notes |
|---|---|
| ~~**Org / project / user policy precedence**~~ | ✅ `~/.llm-router/org-policy.yaml` + `.llm-router.yaml` merge with glob allow/deny |
| ~~**Model + provider allow/deny rules**~~ | ✅ `block_models`/`allow_models` with fnmatch glob patterns; allow overrides block |
| ~~**Spend caps + fallback rules**~~ | ✅ Per-task-type daily caps in org policy |
| ~~**Audit log**~~ | ✅ `routing_decisions.policy_applied` column; `llm_policy` tool shows last 10 decisions |

---

### v3.3 — Slack + Webhook Digests ✅ Complete (2026-04-09)

**Headline**: *"Bring token savings into the team's Slack — where decisions actually get made."*

| Feature | Notes |
|---|---|
| ~~**Weekly savings digest to Slack/Discord/webhook**~~ | ✅ `LLM_ROUTER_WEBHOOK_URL`; auto-detects Slack/Discord/generic from URL |
| ~~**Spend-spike alerts**~~ | ✅ Fires when today > 2× 7-day average |
| ~~**"What if router was off?" simulation**~~ | ✅ `simulate_without_routing()` — full cost without routing |
| **Policy exception summaries** | ⬜ Weekly log of override events (deferred to v3.5) |

---

### v3.3 — Codex Plugin ✅ Complete (2026-04-10)

**Headline**: *"llm-router is now a first-class Codex plugin — one command to install."*

| Feature | Notes |
|---|---|
| ~~**`.codex-plugin/` manifest + marketplace**~~ | ✅ `plugin.json` with full marketplace metadata, `marketplace.json` for discovery |
| ~~**`.codex-plugin/.mcp.json`**~~ | ✅ MCP server declaration for automatic Codex registration |
| ~~**`llm-router install --host codex` writes files**~~ | ✅ Writes `~/.codex/config.yaml`, `hooks.json`, `instructions.md` — no more copy-paste |
| ~~**Codex PostToolUse hook**~~ | ✅ `codex-post-tool.py` flushes pending savings to `savings_log.jsonl` with `host=codex` |
| ~~**Codex Skills**~~ | ✅ `skills/routing/SKILL.md` + `skills/savings/SKILL.md` teach routing and savings commands |

---

## Phase 4 — Category Leadership (Jan–Apr 2027)

### v3.4 — Agent-Context Routing ✅ Complete (2026-04-10)

**Headline**: *"When you use Codex, route to Codex models first. When you use Claude Code, route to Claude models first."*

| Feature | Notes |
|---|---|
| ~~**`_reorder_for_agent_context()`**~~ | ✅ Pure reorder function in `router.py`; reshuffles chain by `(agent, complexity)` |
| ~~**`get/set_active_agent()`**~~ | ✅ Shared state in `state.py`; `llm_select_agent` tags the session |
| ~~**Priority matrix**~~ | ✅ Codex+simple→Ollama→Codex→rest→Claude; Claude+complex→Claude→rest→Codex→Ollama |
| ~~**34 new tests**~~ | ✅ Full coverage in `tests/test_agent_context_routing.py` |

**Community Benchmarks** (previously v3.4):

| Feature | Notes |
|---|---|
| ~~**Opt-in anonymous outcome sharing**~~ | ✅ `LLM_ROUTER_COMMUNITY=true`; local export to `~/.llm-router/community_export.jsonl` |
| ~~**Benchmark confidence metadata**~~ | ✅ `★★★ High / ★★☆ Medium / ★☆☆ Low`; `llm_benchmark` tool shows per-task accuracy |
| **Public benchmark leaderboard** | ⬜ Upload endpoint not yet live; local export only |

---

### v3.4 — Enterprise Pilot (Feb 2027, private beta)

| Feature | Notes |
|---|---|
| **Self-hosted deployment pack** | Docker Compose; isolated telemetry; no external calls |
| **SSO / RBAC admin layer** | Per-team policy admin, audit export |
| **Bedrock / Azure OpenAI / Vertex AI** | Via LiteLLM; enterprise provider support |
| **Procurement + security review docs** | SOC 2 readiness guide, data flow diagrams |

---

### v3.5 — Claude Desktop + Co-Work (Mar 2027)

**Headline**: *"Claude Desktop's cost autopilot — now with shared savings for whole teams."*

No hooks in Claude Desktop means a fundamentally different model: **tool-based delegation** instead of silent interception.

| Feature | Notes |
|---|---|
| **Task-specific MCP tools** | `generate_code`, `refactor_code`, `summarize_text`, `draft_email` — Claude delegates to these |
| **Free-first chain inside each tool** | Ollama → Codex → paid API, invisible to user |
| **Co-Work savings attribution** | `user_id` + `session_id` in DB; per-user analytics in shared sessions |
| **`show_savings_report` conversational tool** | `@show_savings_report for me today` / `for everyone this month` |
| **GUI settings panel** | No YAML editing; graphical provider config + activation mode toggle |

---

### v4.0 — VS Code + Cursor GA (Apr 2027)

**Headline**: *"Claude Code's cost autopilot becomes the routing standard across every editor."*

| Feature | Notes |
|---|---|
| **Stable VS Code extension** | Status bar integration, quick profile toggle, savings display |
| **Stable Cursor integration** | Same feature set via Cursor's extension API |
| **Cross-editor shared config** | One `.llm-router.yml` governs all editors + Claude Code |
| **Cross-editor analytics** | Team dashboard aggregates savings across Claude Code + VS Code + Cursor |

---

## Architectural notes

**Routing coverage ceiling (~70–80%):** The hook system can only intercept `UserPromptSubmit` turns. Plain-text responses (Claude answering directly without a tool call) and post-tool-result reasoning are architecturally unintercept-able. Marketing should say "routes all routable requests", not "routes everything".

**Patch release policy:** `.x` patches are for hook regressions, provider API drift, classifier bugs, and packaging fixes only. No new headline features in patch releases.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Highest-impact areas:

1. **v3.5 Enterprise Pilot** — self-hosted Docker Compose, SSO/RBAC, Bedrock/Azure/Vertex AI
2. **v2.3 Zero-Friction Activation** — `shadow`/`suggest`/`enforce` modes + guided onboarding wizard
3. **v3.5 Claude Desktop Co-Work** — task-specific MCP tools, per-user savings attribution
4. **Provider integrations** — Bedrock, Azure, Vertex AI via LiteLLM
5. **Testing** — integration tests for provider fallback chains
