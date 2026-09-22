# Findings register — 2026-09-22

HEAD `8c7366b`. Severity is not inflated: CRITICAL means wrong results, a
realised security exposure, or an invalidated core claim. Every CRITICAL was
re-reproduced by the summary author after the reporting agent.

---

## CRITICAL

### T-01 · The test suite reports 0 failures while 8 tests fail
**Confidence:** CONFIRMED · **Component:** `tests/qa/test_h08_gate_admits_only_gradable_tasks.py`, `tests/test_groundtruth_accumulation.py`

**Expected:** a green suite means no test is failing.
**Actual:** 8 tests fail; ordering hides them.

```
accumulation file alone                      ->  8 FAILURES
h08 first, then accumulation                 ->  0 failures
accumulation first, then h08                 ->  8 FAILURES
full suite (h08 #52, accumulation #377)      ->  0 failures, exit 0
```

**Evidence:** `test_replay_detection_looks_for_a_real_runner` sets both
`sys.modules["groundtruth.run_matrix"]` and `sys.modules["groundtruth"].run_matrix`,
but its `finally` restores only the first. `from groundtruth import run_matrix`
resolves via the package attribute, so the fake persists for the process;
`replay_available()` then returns True permanently and the H-08 gate reopens.

The 8 failures are real: they are pre-existing accumulation tests never updated
when H-08 tightened the gate (commit `9c5325a`).

**Impact:** every "suite green" claim made during the 2026-09-21 remediation was
true and meaningless for these 8. Any reordering — `pytest-randomly`, `-x`,
xdist, or adding one test — surfaces them.

**Why tests missed it:** the corrupting test asserts only its own effect inside
the `try:`; nothing asserts post-cleanup state.

**Compounding:** `_quarantined_tests/TRIAGE_2026-09-15.md` records ~90 further
failing assertions excluded from the default run, and 3 config tests flip when a
concurrent `llm-router update` writes to `~/.claude/` (see C-06).

---

### T-02 · `llm-router demo` reports negative savings as a win
**Confidence:** CONFIRMED · **Component:** `commands/demo.py:170-186`

Reproduced verbatim:
```
Always-Opus:    $0.0450 per batch
Smart Routing:  $0.09001 per batch
Savings:  $-0.0450 (-100% cheaper)
```

**Evidence:** `total_opus += 0.015` runs unconditionally per row, so the
"baseline" is a constant unrelated to the row's real cost. One of the three
example rows *is* a $0.075 Opus call, so routing exceeds the fabricated baseline.
Printed in green regardless of sign. The same line runs for the real-history
branch (`_load_real_routing_history`), so a user's own data can show this.

**Impact:** the product's showcase of what routing buys asserts, in green, that
it is "cheaper" while displaying negative savings.

**Why tests missed it:** no test file targets `commands/demo.py`.

---

### T-03 · The gateway inflates complexity using the system prompt
**Confidence:** CONFIRMED · **Component:** `gateway.py::_flatten` + `classify.classify_signals`

```
classify_signals("user: hi")                          -> Complexity.SIMPLE
classify_signals("system: <1.6KB boilerplate>\nuser: hi") -> Complexity.COMPLEX
```

**Evidence:** all four wire-compatible endpoints (`openai_chat`,
`openai_responses`, `anthropic_messages`, `ollama_chat`) flatten system and
history into one string *before* classification; `_resolve_profile`
(`router.py:1024`) thresholds on character length (<600 simple / 600-2000
moderate / >2000 complex). `route_and_call` never receives a distinct
`system_prompt` from any gateway endpoint.

**Impact:** every IDE/SDK integration — the module's own advertised audience
("route ANY LLM client, no code change") — systematically over-routes trivial
turns to expensive models, defeating the product's stated purpose on its
flagship path.

**Not affected:** the native `/route` endpoint, the MCP tool surface, and
`hooks/auto-route.py` never concatenate a system prompt into the classified text.

**Why tests missed it:** no test constructs a multi-role message list and asserts
the resulting complexity.

---

### T-04 · Two live scrubbers miss three secret classes and never delegate
**Confidence:** CONFIRMED · **Component:** `library/store.py::scrub_secrets`, `hooks/agent-route.py::_scrub_agent_prompt`

Measured against the canonical scrubber:

| class | canonical | `library/store` | `hooks/agent-route` |
|---|---|---|---|
| slack | redacts | **MISSES** | **MISSES** |
| JWT | redacts | **MISSES** | **MISSES** |
| google key | redacts | **MISSES** | **MISSES** |
| openai / github / PEM | redacts | redacts | redacts |

Neither imports `scrub_text` or `persist_redact`.

**Reachability:**
* `library/store.py` is called by the `library-harvest` PostToolUse hook on every
  Bash/Edit tool call, writing `raw/events.jsonl`. Files land **0644**.
* `hooks/agent-route.py` is **installed in `~/.claude/settings.json` on this
  machine** and `agent_calls.json` was written at 08:37 today.

**Impact:** a secret in a Bash command or a subagent delegation prompt is
persisted in cleartext. The library path is worse: `pack.py` re-injects that
content verbatim as `additionalContext` into later prompts, which then go to
whichever model answers next — a realised
`untrusted input → filesystem → re-injected context → remote model` chain.

**Why tests missed it — and this is the important part:**
`tests/security/test_m07_no_second_scrubber.py` asserts the *canonical* scrubber
covers every rival shape. It never calls the rival functions. **It tested the
abstraction, not the adoption** — the exact failure pattern the 2026-09-21 audit
named and this fix claimed to close.

---

### T-05 · Provenance covers 1 of 6 money-reporting surfaces
**Confidence:** CONFIRMED · **Component:** `cost.py`

The 2026-09-21 C-02 fix added write-time provenance to `usage` and a
`is_simulated = 0` filter to `get_savings_by_period`. Five sibling surfaces read
the same data with no filter:

| surface | provenance filter | reproduced |
|---|---|---|
| `get_savings_by_period` | **yes** | $0.00 for a synthetic row |
| `get_team_savings` (broadcast to Slack/Discord) | **none** | **$3.00 synthetic broadcast** |
| `get_realized_savings` / `get_lifetime_savings_summary` | **no column exists** | $0.24 synthetic counted |
| `get_quality_report`, `get_routing_savings_vs_sonnet`, `get_router_efficiency` | **none** | `is_real` never appears in any WHERE clause |
| `get_daily_spend` / `get_monthly_spend` (gate real caps) | **none** | $25 synthetic counted toward a real cap |

`claude_usage`, `codex_usage`, `gemini_usage`, `savings_stats` have **no
provenance column at all** (`PRAGMA table_info`), and their write guards only
catch a pytest process writing to the real DB — not `LLM_ROUTER_SYNTHETIC=1` or a
benchmark sandbox cwd.

**Evidence of a false comment:** `cost.py:622` states *"All downstream analytics
queries use 'WHERE is_real = 1' to filter them out."* `grep` shows `is_real` is
written once by the migration and appears in **no** WHERE clause anywhere.

**Impact:** the surface that posts to a shared channel is the one with no filter.
`get_daily_spend` fails in the opposite direction — a benchmark run can trip a
real budget cap and throttle legitimate routing.

---

### T-06 · The Ground Truth verifier pipeline is orphaned
**Confidence:** CONFIRMED · **Component:** `accumulate.py`, `author_tasks.py`, `run_matrix.py`, `verifier_registry.py`

A candidate that passes eligibility → envelope → pool → propose → mutation
validation → human approval → ACTIVE **can never contribute a label.**

**Evidence:**
1. Pool candidates are keyed `gtc-<content-hash>`; frozen dataset tasks are keyed
   `gt-<seq>` from the unrelated legacy `extract_corpus` path. `run_matrix`'s only
   bridge is `reg.active_for(t.task_id)` — the namespaces never intersect.
   Live: `reg.active_for('gtc-a854745f…')` → the ACTIVE record;
   `reg.active_for('gt-0001')` → `None`.
2. `--use-registry` adopts only `proposal["verifier_snippet"]`, populated solely
   for schema/file-state strategies. The pytest/mutation-tested strategies
   populate `proposed_files`, which `run_matrix` never reads.
3. `freeze.py` freezes `author_tasks.py`'s output, which is hand-authored by its
   own docstring and has no code path from the pool or the registry.

**Impact:** the rigorous half of the subsystem — mutation testing, human
sign-off — is decorative. Every label that exists comes from the older manual
path.

**Why tests missed it:** no test exercises `--use-registry` end to end; it
appears only in a code comment.

---

## HIGH

| ID | Finding | Evidence |
|---|---|---|
| **T-07** | **The fail-open counter is write-only.** 58 `record()` call sites in `src/`, **0** `snapshot()` readers outside tests. Probed: with the store unwritable — the condition most likely to *cause* fail-opens — `record()` swallows its own write (`failopen.py:112`) and the only fallback is `structlog.debug` while the effective level is `WARNING`. Recorded nowhere, printed nowhere. 3 of the 58 sites were added on 2026-09-21 believing they made losses visible | own probe |
| **T-08** | **Two terminal paths still write no quality-ledger row.** The idempotency-dedupe path and the exhaustion floor both call `_finalize_successful_route(served_from_cache=True)`, which the ledger gate skips. Reproduced: a floor-served route returned 432 chars of real content and `routing_quality.jsonl` was never created. The floor is exactly the case `mis_route` / `quality_escalation_occurred` exist to measure | agent probe |
| **T-09** | **The bandit's reward divides by a 1e-9 cost floor.** `success_rate / max(avg_cost, 1e-9)` gives any free provider an expected value ~1e8× a paid one regardless of quality: free at 0.50 success → 5.0e8; paid at 0.99 → 99. On by default, runs *after* the complexity-aware ordering that deliberately puts Ollama last for deep reasoning. The "success" signal is `response_is_usable` — non-empty and not a deferral — so a confidently wrong answer scores success | `telemetry.py:70`, `bandit.py:97` |
| **T-10** | **A degraded answer is returned as a clean one.** When every candidate is gate-rejected, the exhaustion floor returns the best-rejected response through `_enrich_response` with no degradation marker, renders it with the same success tick, and recomputes `success=_response_is_usable(...)` on content the router just rejected — feeding it back to the bandit as a win | `router.py:2917`, `:3490`, `tools/text.py:403` |
| **T-11** | **A shipped function raises on every installed call.** `budget_lineage_reconciliation.reconcile_budget_lineage_audited` does `from llm_router.control_plane import audit` *inside its body*. Not excluded from the wheel, not in `NOT_SHIPPED`. Verified against the extracted wheel: `ImportError`. Invisible to `test_shipped_modules_import.py`, which only imports modules and never calls functions — and `conftest.py` auto-skips the one test that would exercise it | wheel probe |
| **T-12** | **`Pool.admit` has a second, unlocked race.** The H-07 fix locked only the duplicate-increment branch; both append branches remain unlocked. 20 threads, same prompt → 2 canonical rows where 1 was correct | agent probe |
| **T-13** | **The H-08 gate's capability lookup is fragile.** `replay_available()` resolves `run_matrix` via the package attribute, so any code that ever sets it — a test double, a notebook, a plugin — flips the gate permanently for the process, with no error, log or counter. This is the mechanism behind T-01 | `eligibility.py` |

---

## MEDIUM

| ID | Finding |
|---|---|
| **T-14** | **84 silent `except…: pass` sites wrap a state mutation.** AST census of `src`+`scripts`: 1046 broad excepts, 276 with a bare `pass`; 84 MUTATION, 12 mutation+telemetry, 49 telemetry, 131 other. A failed *write* at any of the 84 reports nothing. This is the structure that hid a real `NameError` at `server.py:115` — found by ruff, invisible to 723 test files |
| **T-15** | **`LLM_ROUTER_HOME` does not sandbox host-integration commands.** `llm-router update` wrote 15 hook files, a rules file and a statusline script into the real `~/.claude/` while `LLM_ROUTER_HOME` pointed at a tmp dir. Content verified byte-identical, but any isolated test or CI job running `install`/`update`/`dev-refresh` touches the operator's real config — and this is what flipped 3 config tests during the audit |
| **T-16** | **The "in sync" fallback scrubber is not in sync.** `hooks/auto-route.py:2035` claims to mirror the canonical table; it misses slack, JWT, `pk-`/`rk-` and PEM. Three of those drifted *because* the 2026-09-21 fix widened canonical without updating the copy. Sink is `transcript_*.jsonl` |
| **T-17** | **`propose.py` and `eligibility.py` disagree about what is verifiable.** A FACTUAL checkable question — the shape eligibility is proudest of admitting — falls through every `select_strategy` branch to `no_reliable_verifier` |
| **T-18** | **`llm-router profile` and `llm-router dev-refresh` are 100% broken.** `profile`: `ImportError: cannot import name 'PROFILE_PATH'`. `dev-refresh`: shells out to `llm_router-install-hooks` (underscore); the registered script is `llm-router-install-hooks` |
| **T-19** | **28 of 51 subcommands are absent from `--help`; 22 appear nowhere in any docs.** `llm-router tui` dies with a raw `ModuleNotFoundError` traceback rather than naming the optional extra |
| **T-20** | **`quota_tracker` queries `provider='gemini'`; real Gemini rows are tagged `provider='google'`.** The only path that tries to compute real Gemini spend returns $0 by construction. Impact limited — it currently feeds only an audit table |
| **T-21** | **The provenance cutover silently zeroes lifetime savings.** Pre-upgrade rows become NULL and drop out of `all_time`; the count is written to `provenance_meta` and never read back by anything. Expected support shape: "my lifetime savings dropped to $0 after upgrading" |
| **T-22** | **`test_h03_gateway_refuses_tool_calls.py` is not hermetic.** Makes real outbound calls — OpenAI auth error, Ollama connect, and a live Codex call returning 200 in 8.2s. 14s wall clock, network- and host-dependent |
| **T-23** | **`direct_diagnostics` labels every DIRECT failure `timed_out=True`**, including "no free model" and grounding rejection (both ~0s elapsed). 17 of 20 live samples read `elapsed_s: 0.0, timed_out: true`, so `llm-router doctor` advises setting `LLM_ROUTER_OLLAMA_TIMEOUT=0` — which guarantees every call fails instantly |
| **T-24** | **Only 14 tests cover the money ledger end to end, all behind `requires_ollama`,** deselected by default. 62 of 9029 tests are deselected; these 14 are the only full-stack exercise of ledger + quota + pool |

---

## LOW / INFO

| ID | Finding |
|---|---|
| **T-25** | `session_store.py` still uses `open()`-then-`chmod` where `private_opener` exists and is used by five siblings — brief world-readable window on creation |
| **T-26** | `get_savings_by_period` increments `calls` before skipping subscription rows, so `calls` and the dollar figures have different denominators |
| **T-27** | `SECURITY.md` says 6 of 12 commands are refused by the allowlist; reproduced count is 7. The doc's self-disclosed gaps (`cat ../../.ssh/id_rsa` allowed) remain accurate |
| **T-28** | `env_registry` claims to cover "every environment variable this codebase reads"; an independent AST scan found 18 `scripts/`-only variables outside that scope |
| **T-29** | Routing overhead from the real log, n=1536 after excluding 67.4% test/unknown traffic: **p50 0.0s (1s log resolution floor), p90 5.0s, p95 43.0s, p99 55.0s, max 136s** against an advertised ~4s timeout |
| **T-30** | `verifiers.run_verifier` passes the full operator environment (`env=dict(os.environ)`, live API keys) to a subprocess that only checks an answer string, and the preamble's `read()` has no path confinement. **Not currently exploitable** — the only caller interpolates through a regex that excludes quotes — but `safe_subprocess.py` already implements an env allowlist and this does not use it |
| **T-31** | HEAD is one unreleased commit ahead of v14.1.0/PyPI, and that commit is the dashboard auth-token path fix — **still live as a bug in the published package** |
| **T-32** | Correlated LLM failure **does not exist** in the GT subsystem: one model call in the whole tree, and it grades the subject. The pipeline stages are deterministic template code. `propose.py`'s "verifier authoring assistant" docstring oversells what it is |

---

## What did not reproduce

* **3 config tests failing** (`BUDGET` vs `BALANCED`) — reported by an agent, but
  they pass for me alone and together. Caused by that agent's own concurrent
  `llm-router update`/`config` mutating ambient state. Recorded as T-15 instead.
* **The hook classifier being silently unequal to the router's** — it is unequal
  (59.7% agreement, n=750) but documented, parked with reasoning, and covered by
  a test that encodes the divergence rather than denying it.

---

## The pattern, restated

The 2026-09-21 audit found: *a correct primitive is built to fix an incident and
then not adopted by the consumers that caused it.*

The 2026-09-22 audit finds the same shape one level up: **the fixes corrected the
canonical implementations and the tests asserted the canonical implementations,
so neither noticed that the call sites were untouched.** A superset scrubber with
two rival call sites. Write-time provenance on one of six readers. A ledger that
records failure on three of five terminal paths.

The remediation was real. The verification was aimed at the wrong object.
