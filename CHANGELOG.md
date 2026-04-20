# Changelog

**For releases v6.2 and earlier, see [CHANGELOG_ARCHIVE.md](docs/CHANGELOG_ARCHIVE.md).**

---

## v6.4.0 — Quality Guard (2026-04-20)

### Added

- **Quality Guard** — Hard threshold enforcement for model quality
  - Real-time quality reordering in routing chain based on judge scores
  - Automatic min_model floor escalation when rolling quality < 0.6
  - Per-model rolling quality trends in `model_quality_trends` table
  - New `llm_quality_guard` MCP tool for monitoring

- **Judge Score Integration** — Quality feedback in routing decisions
  - `judge.reorder_by_quality()` called in router hot path
  - Models with low scores (< 0.7 over 7 days) automatically deprioritized
  - Quality trends logged at session-end for historical analysis

- **Agoragentic Cross-Agent Discovery**
  - Agent registered as `llm-router-saving-tokens` on Agoragentic platform
  - Other AI agents can discover and invoke `llm_route` for model optimization
  - Free tier enabled; no wallet required for initial listing

### Changed

- `src/llm_router/model_selector.py` — `select_model()` now async with quality floor checks
- `src/llm_router/router.py` — Quality reordering integrated after chain build
- `src/llm_router/cost.py` — Added `model_quality_trends` table and `log_quality_trend()`
- `src/llm_router/tools/routing.py` — Updated all `select_model()` calls to use await

### Performance

- Added composite DB index `(final_model, judge_score, timestamp)` for fast rolling window queries
- Prevents full-table scans on quality trend lookups

---

## v6.3.0 — Three-Layer Compression Pipeline (2026-04-19)

### Added

- **RTK Command Output Compression** (Layer 1)
  - Bash/shell outputs automatically compressed via smart filters (60–90% reduction)
  - Git, pytest, cargo, docker, npm outputs simplified to essentials

- **Token-Savior Response Compression** (Layer 2)
  - 4-stage pipeline: filler removal → example consolidation → boilerplate collapse → semantic extraction
  - 60–75% token reduction on LLM responses
  - Optional via `LLM_ROUTER_COMPRESS_RESPONSE=true` (off by default)

- **Unified Dashboard**
  - `llm_gain` shows all three compression layers with token savings per layer
  - All compression metrics logged to SQLite for analytics

### Changed

- Response compression now non-blocking; falls back to original on any error
- Added compression telemetry tracking

---

## Roadmap

**v6.5** — Fine-tuning & Model Customization
**v6.6** — Real-time Team Dashboard
**v7.0** — Multi-Model Competitive Benchmarking
