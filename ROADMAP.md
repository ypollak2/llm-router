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

## Phase 3: Caching & Automation (v0.3) — COMPLETE

Prompt caching, automatic usage refresh, and smoother UX.

| Feature | Priority | Status |
|---------|----------|--------|
| **Prompt cache — exact match** (SHA-256 hash, in-memory LRU, 1h TTL) | High | Done |
| **`llm_cache_stats` + `llm_cache_clear` MCP tools** — expose hit rate, size, entries | High | Done |
| **Auto-route hook** — UserPromptSubmit heuristic classifier, zero-latency routing hints | High | Done |
| Periodic usage pulse (`/usage-pulse` via `/loop`) | High | Done |
| Auto-refresh Claude usage via PostToolUse hook | High | Done |
| Streaming responses (`llm_stream` tool + `call_llm_stream`) | Medium | Done |
| `llm_setup(action='test')` — verify key validity | Medium | Done |
| Rate limit detection (429/rate_limit, 15s cooldown) | Medium | Done |

### Cache Design (v0.3)

- **Two-tier cache**: Exact match (hash of prompt + quality_mode + min_model) for O(1) lookup, plus semantic similarity (cosine over embeddings, threshold 0.95) when embedding classifier is available (Phase 4).
- **Caches `ClassificationResult`**, not `RoutingRecommendation` — budget pressure is applied fresh every time, so cached results stay valid even as quota changes.
- **In-memory LRU**: Max 1,000 entries (~1.5MB with embeddings), 1-hour TTL, `asyncio.Lock` for thread safety. No external dependencies.
- **Zero overhead for misses**: Hash lookup is O(1). Short prompts that never repeat skip caching entirely.

---

## Phase 4: Smart Classification (v0.4)

Replace heuristic LLM-based classification with fast local embeddings, add context compaction, and build a quality framework for systematic evaluation.

| Feature | Priority | Status |
|---------|----------|--------|
| **Embedding-based classifier** — `all-MiniLM-L6-v2` + LogisticRegression, <15ms | High | Planned |
| **Training pipeline** — bootstrap 500+ labeled prompts from LLM classifier | High | Planned |
| **Structural context compaction** — collapse whitespace, dedup, truncate long blocks | High | Planned |
| **Quality logging** — log every routing decision to SQLite `routing_decisions` table | High | Planned |
| **`llm_quality_report` MCP tool** — classification accuracy, savings, downshift harm rate | Medium | Planned |
| **LLM-based context compaction** — summarize bloated prompts via cheap model (opt-in) | Medium | Planned |
| **Semantic prompt cache** — cosine similarity over embeddings (extends Phase 3 cache) | Medium | Planned |
| **A/B testing** — run embedding + LLM classifiers in parallel on 10% sample, log disagreements | Medium | Planned |
| **`llm_compact` MCP tool** — manual prompt compaction for testing | Low | Planned |

### Embedding Classifier Design (v0.4)

- **Model**: `all-MiniLM-L6-v2` (22M params, 384-dim vectors, ~80MB). Prefer ONNX runtime (~200MB) over full torch (~2GB) to keep the server lightweight.
- **Approach**: Encode prompt → LogisticRegression predicts complexity (simple/moderate/complex) and task_type (5 classes) → calibrated confidence via `predict_proba`.
- **Training**: Bootstrap labeled data from existing LLM classifier on ~500 curated prompts, train sklearn model, store in `~/.llm-router/models/`. Quality framework (below) provides human-corrected labels over time.
- **Fallback**: If embedding model unavailable or confidence < 0.7, silently fall back to LLM classifier. Zero behavior change for users who don't opt in.
- **New optional deps**: `onnxruntime`, `scikit-learn` (in `[project.optional-dependencies] ml` group).

### Context Compaction Design (v0.4)

- **When**: Prompt exceeds 4,000 tokens (estimated via `len(text) // 4` or `tiktoken`).
- **Strategy 1 — Structural** (free, default): Collapse redundant whitespace, strip code comments, deduplicate repeated sections, truncate long code blocks to first/last N lines.
- **Strategy 2 — LLM summarization** (opt-in `compaction_mode=full`): Send to a cheap classifier model with "summarize preserving all actionable requirements, code refs, and constraints."
- **Never removes**: Code snippets, error messages, stack traces, file paths, explicit constraints.
- **Config**: `compaction_mode` = `off` | `structural` | `full`, `compaction_threshold` = 4000 tokens.

### Quality Framework Design (v0.4)

- **Decision logging**: Every classify + route → SQLite row with prompt hash, complexity, classifier type (embedding/llm/cached), recommended model, budget state, outcome (success/retry).
- **Metrics**: Classification accuracy, cost savings vs opus-baseline, downshift harm rate, classifier agreement rate.
- **A/B testing**: When both classifiers exist, 10% sample runs both. Secondary is for logging only — primary always routes. Disagreements stored for review.

---

## Phase 5: Media & Multimodal

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

## Phase 6: Distribution & Community

Making it easy for others to install and use.

| Feature | Priority | Status |
|---------|----------|--------|
| PyPI package distribution (`pip install claude-code-llm-router`) | High | Done |
| One-command install script for Claude Code | High | Done (./scripts/install.sh) |
| Web dashboard for usage analytics | Medium | Planned |
| Weekly quality benchmark updates | Medium | Planned |
| Plugin marketplace listing | Medium | Done (.claude-plugin/) |
| Docker image for self-hosted deployment | Low | Planned |
| REST API mode (non-MCP, for any client) | Low | Planned |

---

## Phase 7: Advanced Routing

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
