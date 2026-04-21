# LLM Router v7.2.0 Reliability Patch — Status Report

## 🎯 Objectives
The goal of this session was to diagnose and fix why the router allowed Claude Code usage to hit 100% without downshifting to alternatives (Ollama/GPT-4o-mini).

## ✅ Completed Fixes
The following changes are implemented and verified via a local demo script:
1. **Token Reporting**: Codex and Gemini CLI now report estimated tokens (`len // 4`), closing the feedback loop in `usage.db`.
2. **In-Flight Pressure (Pending Quota)**: Implemented a reservation system in `budget.py` and `router.py` that adds 500 tokens to the "pressure" calculation *before* a call starts. This allows the router to "guess" usage and downshift proactively.
3. **Hard Cap Safety**: Modified `claude_usage.py` to disable "optimistic reset" discounting once usage reaches 100%.
4. **String Mismatch**: Fixed a bug in `profiles.py` where `claude-sonnet-4-6` (no prefix) was not being recognized as a Claude model for demotion.

## 🛑 Blockers & Holding Progress
The release of `v7.2.0` is currently blocked by 4 failing tests in the CI suite:

### 1. Test Environment Isolation (Timeouts)
- **Problem**: `uv run pytest` is picking up local configurations (like `OLLAMA_BASE_URL` or `.env` keys). 
- **Impact**: Tests like `test_codex_routing.py` try to reach local Ollama instances that aren't responsive, causing 30s timeouts.
- **Attempted Fixes**: Tried `env -i` and unsetting variables, but Pydantic Settings is persistently finding local config files.

### 2. `test_no_providers_configured` Failure
- **Problem**: This test expects a very specific `ValueError` string that lists "Configured providers". 
- **Impact**: Because the local environment *has* providers configured, the error message generated doesn't match the "empty" message the test expects.

### 3. Subprocess Environment Leak (`test_safe_subprocess.py`)
- **Problem**: In an attempt to isolate the environment using `env -i`, I broke a test that verifies the preservation of normal variables like `USER`.

## ⏭️ Next Steps
To proceed with the release, we need to:
1. Modify `tests/conftest.py` to forcefully mock the config loader to return a truly empty `RouterConfig`, independent of local files.
2. Align the `ValueError` logic in `router.py` with the exact expectations of the legacy test suite once isolation is fixed.
3. Once tests pass, run `bash scripts/release.sh`.

**Current Version in Branch**: `7.2.0` (Committed but not pushed/published).
