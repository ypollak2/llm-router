# LLM Router — Roadmap

> Last updated: 2026-03-29

## Vision

A single MCP server that gives any AI assistant unified access to every LLM provider — automatically picking the right model for each task, managing budgets across subscriptions and APIs, and maximizing quality while minimizing cost.

---

## Phase 1: Foundation (v0.1) — COMPLETE

Core routing infrastructure, provider integrations, and budget management.

| Feature | Status |
|---------|--------|
| Text LLM routing (10 providers via LiteLLM) | Done |
| Three routing profiles (budget / balanced / premium) | Done |
| Cost tracking with SQLite | Done |
| Health checks with circuit breaker pattern | Done |
| Image generation (DALL-E, Flux, Stable Diffusion) | Done |
| Video generation (Runway, Kling, minimax) | Done |
| Audio/voice routing (ElevenLabs, OpenAI TTS) | Done |
| Monthly budget enforcement with hard limits | Done |
| Multi-step orchestration with pipeline templates | Done |
| Claude Code plugin with /route skill | Done |
| Freemium tier gating (free / pro) | Done |
| CI with GitHub Actions | Done |
| Smart complexity classification | Done |
| Progressive budget-aware model downshifting | Done |

---

## Phase 2: Intelligence Layer (v0.2) — COMPLETE

Smart routing that understands Claude subscription state, integrates local agents, and provides a unified view of all LLM usage.

| Feature | Status |
|---------|--------|
| Complexity-first routing (simple->haiku, moderate->sonnet, complex->opus) | Done |
| Live Claude subscription monitoring via claude.ai JSON API | Done |
| Time-aware budget pressure (factors in session reset proximity) | Done |
| External fallback ranking when Claude quota is tight | Done |
| Codex desktop integration (local agent, free via OpenAI sub) | Done |
| Unified usage dashboard (Claude + Codex + APIs + savings) | Done |
| `llm_setup` tool for API discovery and key management | Done |
| Per-provider budget limits (OPENAI, GEMINI, etc.) | Done |
| Gemini media routing (Imagen 3 images, Veo 2 video) | Done |
| ASCII terminal-friendly dashboard (no Unicode rendering issues) | Done |
| Updated thresholds: 85%/95% safety net instead of 50%/80% | Done |

### Key Design Decisions (v0.2)

- **Complexity routing IS the savings mechanism** — routing simple tasks to haiku and moderate to sonnet naturally preserves opus quota. Budget downshifting is only a late safety net at 85%+.
- **Time-aware pressure** — 90% usage with 5 minutes until session reset is very different from 90% with 4 hours left. The router reduces downshift urgency when reset is imminent.
- **Three independent quota pools** — Claude subscription (session/weekly limits), OpenAI subscription (via Codex, free), and API credits (pay-per-token). The router sees all three.

---

## Phase 3: Automation & Polish — IN PROGRESS

Automatic usage refresh, periodic updates, and smoother UX.

| Feature | Priority | Status |
|---------|----------|--------|
| Periodic usage pulse (`/usage-pulse` via `/loop`) | High | Skill created, needs hook wiring |
| Auto-refresh Claude usage via Playwright hook | High | Planned |
| Streaming responses for long-running LLM calls | Medium | Planned |
| `llm_setup(action='test')` — verify key validity | Medium | Planned |
| Rate limit detection and automatic provider switching | Medium | Planned |
| Smart classifier caching (avoid re-classifying identical prompts) | Low | Planned |

---

## Phase 4: Media & Multimodal

Deep integration with media generation APIs.

| Feature | Priority | Status |
|---------|----------|--------|
| Gemini Imagen 3 API integration (via LiteLLM or direct) | High | Routing added, API integration TBD |
| Gemini Veo 2 API integration | High | Routing added, API integration TBD |
| Image editing / inpainting routing | Medium | Planned |
| Voice cloning workflow (ElevenLabs) | Medium | Planned |
| Music generation routing (Suno, Udio) | Low | Planned |
| Real-time video generation (Runway Gen-3 Turbo) | Low | Planned |

---

## Phase 5: Distribution & Community

Making it easy for others to install and use.

| Feature | Priority | Status |
|---------|----------|--------|
| PyPI package distribution (`pip install llm-router`) | High | Planned |
| One-command install script for Claude Code | High | Done (./scripts/install.sh) |
| Web dashboard for usage analytics | Medium | Planned |
| Weekly quality benchmark updates | Medium | Planned |
| Plugin marketplace listing | Medium | Done (.claude-plugin/) |
| Docker image for self-hosted deployment | Low | Planned |
| REST API mode (non-MCP, for any client) | Low | Planned |

---

## Phase 6: Advanced Routing

Smarter model selection based on learned patterns.

| Feature | Priority | Status |
|---------|----------|--------|
| Learning from routing outcomes (which model performed best) | High | Planned |
| Per-user preference profiles | Medium | Planned |
| A/B testing between providers | Medium | Planned |
| Cost prediction before execution | Medium | Planned |
| Automatic profile switching based on time of day / workload | Low | Planned |
| Multi-tenant support for teams | Low | Planned |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Priority areas for contributions:

1. **Provider integrations** — especially Gemini media APIs (Imagen 3, Veo 2)
2. **Streaming support** — LiteLLM supports it, needs MCP plumbing
3. **Testing** — integration tests for more providers
4. **Documentation** — usage examples, tutorials, video walkthroughs
