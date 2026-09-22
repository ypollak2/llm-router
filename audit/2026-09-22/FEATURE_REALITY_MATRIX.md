# Feature reality matrix — 2026-09-22

Every row traced or executed. **Trust** is the audit's verdict on relying on it.

| Feature | Documented | Implemented | Prod wired | E2E tested | Observed working | Trust |
|---|---|---|---|---|---|---|
| **Routing** |
| Prompt → tier → model chain | yes | yes | yes | yes | yes | **HIGH** |
| Fallback chain on provider error | yes | yes | yes | yes | yes | **HIGH** |
| Local-first preference | yes | yes | yes | yes | yes | **HIGH** |
| Complexity classification (MCP / hook / native `/route`) | yes | yes | yes | partial | yes | **MEDIUM** |
| Complexity classification (gateway) | yes | yes | yes | **no** | **inflated by system prompt** | **NONE** (T-03) |
| Bandit self-improvement | partial | yes | yes (default on) | unit only | yes, **reward is broken** | **NONE** (T-09) |
| Exhaustion floor (return best-rejected) | yes | yes | yes | content only | yes, **unmarked as degraded** | **LOW** (T-10) |
| Hook classifier ≡ router classifier | implied | **no** | n/a | yes | 59.7% agreement, n=750 | **documented divergence** |
| **Gateway** |
| OpenAI / Anthropic / Ollama wire shapes | yes | yes | yes | partial | yes | **MEDIUM** |
| Tool / function calling | yes | **refused (400)** | yes | yes | yes | **HIGH** (honest refusal) |
| `finish_reason` fidelity | no | yes | yes | yes | yes | **HIGH** |
| Cross-origin / DNS-rebinding guard | yes | yes | yes | yes | yes | **HIGH** |
| Per-request auth | partial | yes (opt-in) | yes | yes | yes | **MEDIUM** (off by default) |
| `route_server` auth | no | **none** | n/a | no | n/a | **NONE** (no opt-in exists) |
| **Caching** |
| `result_cache` (BM25) | yes | yes | yes | yes | yes | **HIGH** |
| Semantic-cache equivalence guard | yes | yes | yes | yes | yes | **HIGH** |
| Per-request bypass / single-entry evict | yes | yes | yes | yes | yes | **HIGH** |
| **Money** |
| Per-call cost computation | yes | yes | yes | yes | yes | **HIGH** |
| Pricing table single-source | yes | yes | yes | yes | lint passes | **HIGH** |
| `get_savings_by_period` provenance | yes | yes | yes | yes | yes | **MEDIUM** |
| `get_team_savings` provenance | implied | **none** | yes | no | synthetic $ broadcast | **NONE** (T-05) |
| `get_realized_savings` / lifetime | yes | **no column** | yes | no | synthetic counted | **NONE** (T-05) |
| Daily/monthly spend caps | yes | yes | yes | partial | **synthetic inflates the cap** | **LOW** (T-05) |
| `llm-router demo` savings arithmetic | yes | **wrong** | yes | **no** | **negative shown as "cheaper"** | **NONE** (T-02) |
| Quota tracking (Gemini) | yes | **wrong provider key** | yes | no | returns $0 always | **NONE** (T-20) |
| **Telemetry** |
| Quality ledger records success | yes | yes | yes | yes | yes | **HIGH** |
| …records failure | yes | yes | yes | yes | yes | **HIGH** |
| …records cache hit | yes | yes | yes | yes | yes | **HIGH** |
| …records idempotency dedupe | implied | **no** | n/a | no | 0 rows | **NONE** (T-08) |
| …records exhaustion floor | implied | **no** | n/a | no | 0 rows | **NONE** (T-08) |
| `classification_method` populated | yes | yes | yes | yes | yes | **HIGH** |
| Provenance filtering in `summarize()` | yes | yes | yes | yes | yes | **HIGH** |
| Fail-open loss counting | yes | yes | yes | tests only | **0 readers in prod** | **NONE** (T-07) |
| **Privacy** |
| Canonical scrubber coverage | yes | yes | yes | yes | superset verified | **HIGH** |
| …adoption at every call site | yes | **no** | n/a | **no** | 2 live bypasses | **NONE** (T-04) |
| `persist_redact` fail-closed | yes | yes | yes | yes | yes | **HIGH** |
| `prompt_capture` fail-closed | yes | yes | yes | yes | yes | **HIGH** |
| 0600 on debug logs | yes | yes | yes | yes | yes | **HIGH** |
| PII scrubbing | **never claimed** | no | n/a | xfail pin | n/a | **honestly absent** |
| **Ground Truth** |
| capture → scrub → eligibility → pool | yes | yes | yes | yes | yes | **HIGH** |
| Three-state PASS/FAIL/AMBIGUOUS | yes | yes | yes | yes | yes | **HIGH** |
| HARD/SOFT split at the label | yes | yes | yes | yes | yes | **HIGH** |
| Mutation validation | yes | yes | yes | yes | yes | **HIGH** |
| Verifier lifecycle → run_matrix | yes | **no bridge** | **no** | no | ACTIVE verifiers grade nothing | **NONE** (T-06) |
| Replay of captured state | partial | **no** | no | no | `run_verifier` gets no `cwd` | **NONE** |
| Eligibility gate stability | yes | yes | yes | **contaminating test** | flips permanently on module-cache write | **LOW** (T-13) |
| **Packaging / CLI** |
| `llm-router status` / `doctor` / `verify` / `config` | yes | yes | yes | yes | yes | **HIGH** |
| `llm-router gain` | yes | yes | yes | yes | yes | **HIGH** |
| `llm-router profile` | **no** | yes | yes | no | **ImportError** | **NONE** (T-18) |
| `llm-router dev-refresh` | **no** | yes | yes | no | **FileNotFoundError** | **NONE** (T-18) |
| `llm-router tui` | **no** | yes | optional extra | no | raw traceback | **LOW** |
| 28 of 51 subcommands in `--help` | **no** | yes | yes | no | reachable | **undocumented** |
| `reconcile_budget_lineage_audited` | docstring | yes | **shipped broken** | **auto-skipped** | ImportError on install | **NONE** (T-11) |
| HEAD == published package | implied | **no** | n/a | n/a | 1 commit ahead | **stale** |
| **Test suite** |
| "all tests pass" | implied | — | — | — | **0 reported, 8 real failures** | **NONE** (T-01) |
| Mutation resistance of new tests | no | yes | n/a | yes | **8 of 8 caught** | **HIGH** |

---

## Count

| Trust | Rows |
|---|---|
| HIGH | 25 |
| MEDIUM | 5 |
| LOW | 4 |
| **NONE** | **16** |

**The distribution is the finding.** HIGH clusters in execution, caching,
scrubber *coverage*, and the new tests' mutation resistance. NONE clusters in
reporting, adoption, and anything that answers "is this still true?" — the
gateway's classification, five of six money surfaces, two terminal paths, the
fail-open counter, the verifier bridge, and the suite's own pass/fail signal.
