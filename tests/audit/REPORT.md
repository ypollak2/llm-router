# LLM Router Router Audit Report

Command run: `.venv/bin/pytest -vv tests/audit`

Result: `6 failed, 6 passed in 0.76s`.

## Checks

- [FAIL] Section 2 execution-proof variety: `tests/audit/test_execution_variety.py::test_mixed_environment_does_not_collapse_all_tasks_to_one_head` verified QUERY/CODE/ANALYZE heads in a mixed Ollama+Codex+API environment; all three started with `ollama/qwen2.5-coder:7b`.
- [PASS] Section 2 Codex runtime fallback: `tests/audit/test_execution_variety.py::test_codex_failure_falls_through_and_logs_routing_fallback` verified a failing `run_codex` attempt falls through to `openai/gpt-4o-mini` and emits a `routing_fallback` log record.
- [FAIL] Section 4 Env A Ollama-only matrix: `tests/audit/test_provider_matrix.py::test_provider_availability_matrix_never_returns_unavailable_providers[ollama_only]` found unavailable `codex/gpt-4o` in the returned chain.
- [FAIL] Section 4 Env B Codex-only matrix: `tests/audit/test_provider_matrix.py::test_provider_availability_matrix_never_returns_unavailable_providers[codex_only]` found unavailable `ollama/qwen3:32b` in the returned chain.
- [FAIL] Section 4 Env C Claude subscription-only matrix: `tests/audit/test_provider_matrix.py::test_provider_availability_matrix_never_returns_unavailable_providers[claude_subscription_only]` found unavailable `ollama/qwen3:32b` and `codex/gpt-4o`.
- [PASS] Section 4 Env D everything-available matrix: `tests/audit/test_provider_matrix.py::test_provider_availability_matrix_never_returns_unavailable_providers[everything_available]` returned non-empty chains without unavailable providers.
- [FAIL] Section 4 Env E paid OpenAI-only matrix: `tests/audit/test_provider_matrix.py::test_provider_availability_matrix_never_returns_unavailable_providers[paid_openai_only]` found unavailable `ollama/qwen3:32b` and `codex/gpt-4o`.
- [FAIL] Section 4 Env F unusual Ollama names matrix: `tests/audit/test_provider_matrix.py::test_provider_availability_matrix_never_returns_unavailable_providers[unusual_ollama_names]` included the unusual local models but also found unavailable `codex/gpt-4o`.
- [PASS] Section 4 no-pin default ordering characterization: `tests/audit/test_provider_matrix.py::test_no_pin_default_ordering_varies_by_task_type` verified that with no pins, QUERY/CODE/ANALYZE/GENERATE chains are identical in this simulated environment.
- [PASS] Section 5 single failure fallback: `tests/audit/test_failure_fallback.py::test_single_model_failure_falls_through_and_is_logged` verified provider failure falls through and logs `routing_fallback` with `fallback_reason="provider_error"`.
- [PASS] Section 5 all failures terminal result: `tests/audit/test_failure_fallback.py::test_all_models_failing_raises_clear_terminal_error_without_hanging` verified all-model failure raises `RuntimeError` with chain failures within a 2 second timeout.
- [PASS] Section 5 rate-limit fallback: `tests/audit/test_failure_fallback.py::test_rate_limit_error_is_fallback_worthy` verified a mocked `RateLimitError` falls through and logs `fallback_reason="rate_limit"`.

## Bugs Found

- `src/llm_router/router.py:339`: the provider filter always lets `codex`, `ollama`, and `gemini_cli` models survive if they appear in the static/dynamic chain, regardless of `is_codex_available()`, `is_gemini_cli_available()`, or `config.all_ollama_models()`. This is why unavailable `codex/gpt-4o` and `ollama/qwen3:32b` appear in Env A/B/C/E/F.
- `src/llm_router/router.py:390`: cheap-tier Ollama injection prepends the entire configured Ollama list for every task type. Without a pin, QUERY/CODE/ANALYZE all start with the first configured Ollama model, so execution variety still collapses to one head model.

## Gaps / Assumptions

- The audit tests mock dynamic routing to `None` so they characterize `_build_and_filter_chain`'s static-chain path plus local/Codex/Gemini injection logic deterministically.
- The runtime fallback tests patch the candidate chain to a two-model chain; they prove `route_and_call` fallback/logging behavior, not full chain construction.
- I did not modify production files. Existing modified files in the working tree predated this audit and were left untouched.
- Honest no-pin answer: in the tested no-pin mixed environment, complexity/task type does not change ordering for QUERY/CODE/ANALYZE/GENERATE; the chains are identical.
