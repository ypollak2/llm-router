---
id: PROVIDER-BENCH-LEAK-1
status: fixed
---
## PROVIDER-BENCH-LEAK-1. Subprocess tests polluted the provider bench ledger

- **Symptom.** Fake benches for codex and provider-0 through provider-9 appeared repeatedly in the operator's ~/.llm-router/provider_bench.jsonl, polluting KPI G4.
- **Cause.** Two persistence tests supplied subprocess environments containing PATH and LLM_ROUTER_PROVIDER_RESET_PATH but omitted HOME and LLM_ROUTER_HOME. The reset file was isolated, but the bench ledger fell back to the account's real home. Both production paths already resolve at call time; pytest already isolates HOME and LLM_ROUTER_HOME, but those settings were discarded by these children.
- **Fix.** Pass both isolated homes and USERPROFILE explicitly to both subprocess tests.
- **Test.** `tests/test_provider_bench_isolation.py` runs both subprocess tests with a fake operator home holding a sentinel `provider_bench.jsonl`, asserts every child gets the isolated `HOME`/`LLM_ROUTER_HOME`, that the isolated ledger receives exactly the expected rows (`codex`; `provider-0..9`) and that the sentinel is byte-identical afterwards. Reproduced before the fix (coordinator, 2026-10-11): one run of `tests/test_provider_reset.py` on main added 11 rows to the real `~/.llm-router/provider_bench.jsonl` (5,505 -> 5,516); with the fix the real file stayed at 5,505 lines.
