# Changelog

**For releases v6.2 and earlier, see [CHANGELOG_ARCHIVE.md](docs/CHANGELOG_ARCHIVE.md).**

---

## v6.6.0 — Quality Guard & Feedback Loop (2026-04-20)

### Added

- **Quality Guard — v6.2 Feature Completion**
  - Comprehensive Quality Guard feature closes feedback loop for real-time routing adjustments
  - `model_quality_trends` table tracks rolling 7-day average judge scores per model
  - Hard threshold enforcement: upgrades min_model when avg score < 0.6 for 5+ samples
  - `llm_quality_guard` MCP tool displays model health with quality indicators
  - Quality-aware model reordering automatically prioritizes high-scoring models
  - Composite index on (final_model, judge_score, timestamp) for fast quality lookups

- **Comprehensive Test Suite** (`tests/test_quality_guard.py`)
  - 18 tests covering all Quality Guard components
  - Quality trends tracking, threshold enforcement, tool output, performance indices
  - Full suite: 1,265 tests pass

### Technical Details

- Wire `reorder_by_quality()` into router hot path (router.py, 2 locations)
- Implement `_get_quality_floor()` hard threshold check (model_selector.py)
- Add `log_quality_trend()` function for rolling quality snapshots (cost.py)
- Create `idx_routing_quality` performance index on routing_decisions table
- All 5 critical gaps from v6.2 plan now closed

---

## v6.5.0 — Security Hardening (2026-04-20)

### Added

- **Comprehensive Security Audit Remediation**
  - All 2 CRITICAL, 3 HIGH, and 5 MEDIUM-risk vulnerabilities fixed
  - 162 security tests covering all remediation items (all passing)
  - Production-grade implementations with full documentation

- **Safe Subprocess Execution** (`safe_subprocess.py`)
  - Filters all API keys, OAuth tokens, and secrets from subprocess environment
  - Prevents `/proc/[pid]/environ` exposure attacks
  - Used in all subprocess calls via `safe_subprocess_exec()` and `safe_subprocess_run()`

- **Prompt Injection Detection & Mitigation** (`prompt_injection.py`)
  - Detects 20+ common injection patterns (system prompt extraction, jailbreaks, etc.)
  - Wraps user prompts with boundary markers to prevent instruction override
  - Automatically applied in `llm_route` tool

- **Input Validation** (`input_validation.py`)
  - Validates routing parameters (task_type, complexity, temperature, max_tokens)
  - Strict enum validation for task types and complexity levels
  - Clear error messages for invalid inputs
  - Prevents bypass of routing policies

- **External API Response Validation** (`response_validation.py`)
  - Pydantic-based schema validation for LLM API responses
  - Detects null bytes and injection attempts in responses
  - Sanitizes model names and enforces provider whitelist
  - Prevents code injection via API responses

- **Error Message Sanitization** (`error_sanitization.py`)
  - Redacts sensitive information from error messages before display
  - Detects and removes: file paths, SQL queries, AWS credentials, API keys, SSH keys
  - Prevents information disclosure attacks

- **Thread-Safe Configuration** (`config.py`)
  - Double-checked locking pattern for singleton initialization
  - Safe initialization under concurrent access
  - Prevents race conditions in multi-threaded environments

- **Configurable Timeout Values** (`timeout_config.py`)
  - Makes all timeouts configurable via environment variables
  - Prevents DoS attacks via hardcoded timeout exploitation
  - 6 configurable timeout categories with sensible defaults
  - Graceful fallback for invalid environment variable values

- **OAuth Token Rotation Strategy** (`oauth_token_rotation.py`)
  - Automatic token refresh every 1 hour (configurable)
  - JWT expiry detection without external validation
  - Async-safe with concurrent refresh prevention
  - Graceful degradation on refresh failures

### Changed

- `src/llm_router/tools/routing.py` — Added prompt sanitization and input validation
- `src/llm_router/codex_agent.py` — Uses configurable timeout from environment
- `src/llm_router/hooks/session-end.py` — Uses configurable timeouts
- `src/llm_router/hooks/session-start.py` — Uses configurable timeouts

### Security

- ✅ CRITICAL #1: SQL Injection in `_column_exists()` — FIXED
- ✅ CRITICAL #2: API Key Environment Pollution — FIXED
- ✅ HIGH #3: Unvalidated External API Responses — FIXED
- ✅ HIGH #4: Prompt Injection — FIXED
- ✅ HIGH #5: Insufficient Input Validation — FIXED
- ✅ MEDIUM #6: Information Disclosure in Error Messages — FIXED
- ✅ MEDIUM #7: OAuth Token Rotation — FIXED
- ✅ MEDIUM #8: Race Condition in Config Initialization — FIXED
- ✅ MEDIUM #9: Hardcoded Timeout Values — FIXED

### Testing

- Added 162 comprehensive security tests (all passing)
- `test_safe_subprocess.py` (13 tests)
- `test_prompt_injection.py` (19 tests)
- `test_input_validation.py` (28 tests)
- `test_response_validation.py` (32 tests)
- `test_error_sanitization.py` (26 tests)
- `test_config_thread_safety.py` (8 tests)
- `test_timeout_config.py` (21 tests)
- `test_oauth_token_rotation.py` (15 tests)

### Documentation

- Added `SECURITY_REMEDIATION.md` with complete remediation details and verification steps

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
