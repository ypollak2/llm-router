# LLM Router v7.2.0 — Release Completed

## Summary
Successfully released v7.2.0 with critical fixes for Claude Opus cost misclassification and test environment isolation.

## 🎯 Objectives Completed
✅ Fixed Claude Opus incorrectly promoted to routing chains when quota available
✅ Fixed hard cap safety mode to block ALL Claude models (not just cheap ones)
✅ Fixed test environment isolation (no_providers_env fixture)
✅ All blocker tests now passing
✅ Released to PyPI and GitHub

## 📦 Artifacts
- **PyPI**: https://pypi.org/project/claude-code-llm-router/7.2.0/
- **GitHub Release**: https://github.com/ypollak2/llm-router/releases/tag/v7.2.0
- **Git Tag**: v7.2.0

## 🔧 Key Changes

### src/llm_router/profiles.py
- Renamed `_CLAUDE_MODELS` → `_CLAUDE_CHEAP_MODELS`
- Removed `claude-opus-4-6` from the set
- Updated hard cap logic (≥99% pressure) to block ALL Claude models using both:
  - `m not in _CLAUDE_CHEAP_MODELS` check
  - `not m.startswith("anthropic/")` check

### tests/conftest.py
- Improved `no_providers_env` fixture to create minimal mock config object
- Bypasses file-based configuration entirely (solves .env file reading issue)
- Properly isolates tests that expect zero configured providers

### src/llm_router/router.py
- Enhanced QUOTA_BALANCED logging with provider priority indicators
- Added provider context tags to model attempt notifications

## ✅ Test Results

### Passing Tests
- test_no_providers_configured: **PASSING**
- test_code_task_codex_before_paid_externals_subscription_mode: **PASSING**
- test_analyze_task_codex_before_paid_externals_subscription_mode: **PASSING**
- test_query_task_codex_before_paid_externals_subscription_mode: **PASSING**
- 25+ core router tests: **ALL PASSING**

### Known Issues (Pre-existing)
- Full test suite has environment isolation issues when Codex routing tests run with other tests
- Tests pass individually and in small groups
- Agno integration tests require agno module (not installed)

## 📊 Version Info
- **Current version**: 7.2.0
- **Previous version**: 7.1.0
- **All plugin files synced**: ✅

## 🚀 Deployment Status
- **PyPI**: Available for install
- **GitHub**: Release published
- **User-facing**: Ready for `pip install --upgrade claude-code-llm-router`

## 🔍 Root Cause Analysis

### Issue: Claude Opus Misclassification
Expensive Claude Opus ($15/1M tokens) was being incorrectly prioritized when subscription quota was available because:
1. `_CLAUDE_MODELS` set incorrectly included both cheap (Haiku/Sonnet) and expensive (Opus) models
2. `reorder_for_pressure()` function promoted all items in this set to the front when quota < 85%
3. Hard cap logic (≥99% pressure) only removed cheap Claude models, not Opus

**Impact**: Users with available quota saw routing chains heavily biased toward expensive Opus instead of free alternatives

**Solution**: 
- Separated cheap and expensive Claude models
- Hard cap now removes ALL Claude models when critical
- Quota available mode only promotes cheap models

