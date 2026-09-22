# Findings register

v14.1.0 · 2026-09-21. Severity-ordered. Every CRITICAL and HIGH was
re-verified independently of the agent that reported it; the verification command
and its output are given. **No remediation was applied** — this is a discovery
pass.

Evidence conventions: counts come from the live `~/.llm-router` stores;
"real rows" means rows surviving synthetic/fixture exclusion; line numbers are at
commit `032b473`.

---

## CRITICAL

### C-01 · The quality ledger is structurally incapable of recording a failure

**Claim:** every routing-quality figure the project reports is conditioned on
success, silently.

**Evidence:**
- `routing_quality.jsonl`: **0 of 16,869** real rows carry
  `route_succeeded=False`. Not a low rate — zero, across the full history.
- `record_route()` has exactly **one** call site: `router.py:2004`, inside
  `_finalize_successful_route`.
- The failure path (`_emit_ledger_terminal('failed')`) writes to the *execution*
  ledger (SQLite) and never reaches `routing_quality.jsonl`.
- The cache-hit path is excluded by the gate at `router.py:1958`.

**Consequence:** "success rate", "quality escalation rate" and every derived
routing-quality metric are 100%/0% by construction. A reader cannot distinguish
"nothing failed" from "failures are unrepresentable". Ground Truth sampling draws
from this population, so it can never see a bad routing decision.

**Why this is the top finding:** it is not a wrong number. It is a measurement
instrument with one of its states physically absent.

---

### C-02 · Reported savings have the wrong sign once fixtures are removed

**Claim:** the headline savings figure describes test traffic, not usage.

**Evidence — three successive filters over `usage.db`:**

| Population | Net savings |
|---|---|
| Raw, as the code reports it | **+$83.49** |
| After `_is_test_model()` name filtering (the code's own filter) | **+$87.96** |
| After removing rows with stub signatures / fixture provenance | **−$1.15** |

The gap is 1,813 rows that carry **real model names** but stub-shaped
input/output token signatures — the name filter cannot see them. Plus 11 rows
whose error text contains `test-key`, and 733 rows on `badmodel` / `ollama/b`
(non-existent models used only by tests).

**Consequence:** the filter is *anti-protective*: applying it moves the number
further from the truth (+$83 → +$88, while truth is −$1). The number the user
sees at session end is a number about the test suite.

**Related:** `is_simulated` — the column the savings query filters on — is
**never written** by the single `INSERT INTO usage`. The filter
`AND is_simulated IS NOT 1` excludes nothing, ever, and reads as protective.

---

### C-03 · The semantic cache returns answers to different questions

**Claim:** at the shipped threshold, semantically distinct prompts collide.

**Evidence — measured directly against `nomic-embed-text`, threshold 0.95:**

| Prompt pair | Cosine | Collides? |
|---|---|---|
| "retry 3 times" / "retry 30 times" | **0.9925** | yes |
| "timeout 30" / "timeout 300" | **0.9903** | yes |
| "increase by 10%" / "decrease by 10%" | **0.9764** | yes |

On a hit the model is **never called**. There is no per-request bypass and no
tool to clear a single poisoned entry.

**Consequence:** a wrong answer is served fast, confidently, and is
indistinguishable from a right one. The magnitude and the *direction* of a
numeric instruction are both below the embedding's resolution at this threshold.
This matches a previously recorded real-world incident (one passport's answer
served for another).

---

### C-04 · Raw prompts, tool I/O and shell commands bypass the canonical scrubber

**Claim:** the "single source of truth" scrubber has three bypasses, one live.

**Evidence:**
- `trace.py`: **0** scrubber references. `emit()` truncates to 600 chars and
  writes verbatim. Injection test — Anthropic key, AWS key id, `password=`, home
  path, email, public IP — **all six survived in full plaintext**. Written with
  the process umask, no TTL, not covered by `commands/gc.py`.
- `tool_intercept.py`: **0** scrubber references. `_log_intercept()` writes raw
  shell commands.
- `intercepts.jsonl` **on the audited machine**: mode **644**, 177 rows, written
  during this session. Today's contents are benign; nothing prevents a
  `curl -H "Authorization: Bearer …"` landing world-readable with no expiry.

The hardening exists and was not carried across: `auto-route.py`'s transcript
shards are correctly `0600` via a private opener.

---

## HIGH

### H-01 · Provenance is computed and then not read
`summarize()` in `routing_quality.py` contains **0** references to `synthetic` or
`is_evaluable`. Demonstrated: a single synthetic row in an isolated ledger yields
`quality_escalation_rate: 1.0`. `attribution.py` — whose docstring reads *"one
definition, consumed by every surface"* — has **0 production callers**. Four
provenance mechanisms exist (`synthetic`, `is_simulated`, `is_real`,
`_is_test_model()`); three do not work.

### H-02 · The README hero statistic was never produced by any run
"105 prompts / 76% / 72%" is spliced from **three different cells** of
`docs/MEASUREMENT.md` — different N, different windows, different conditions. No
single run produced that triple. The source table is honest; the summary is not.

### H-03 · The gateway silently drops tool definitions
`gateway.py` flattens `messages` to a text blob; `tools` is discarded by the
Pydantic model **before the handler body runs**, so no code path could log it.
`finish_reason` is hardcoded `"stop"` — a client can never observe `tool_use`.
Any OpenAI-compatible client doing function calling gets a plausible prose reply
instead of a tool call, with no error.

### H-04 · `classification_method` is 0% populated
**0 of 23,773** rows. Cause: the writer reads `.get("method")` while every
builder writes the key `"classifier_type"`. The field that would explain *why* a
model was chosen is universally empty, and the mismatch is silent on both sides.

### H-05 · No completion route is ever verified
`verification_attempted` is false on **0 of 23,323** completion-kind routes —
97% of all traffic. Ground Truth's entire input population is unverified by
construction.

### H-06 · `quota_tracker` loses reads under concurrency
**32–38% read failure rate** under load. Non-atomic whole-file rewrite (no
temp-file-and-rename). **10 hooks consume it.** The correct pattern exists in
this repo — `budget_backend.py` — and was not applied here.

### H-07 · `Pool.admit()` loses increments
Measured **19 admits recorded where 21 occurred**. `accumulate.py` constructs a
fresh `Pool()` per call, so the production path hits this. Candidates are lost
silently.

### H-08 · The replay envelope has no replayer
**0** `git checkout` / `git apply` / worktree call sites anywhere in
`scripts/groundtruth/`. `run_matrix.call_model()` sends the prompt as a
single-turn completion and grades raw text. The eligibility gate is tuned to
admit EDIT/code tasks; the harness cannot grade them. The envelope captures
everything needed and nothing consumes it.

### H-09 · The measurement ledger's silent-loss fix was applied to its sibling only
`router.py:2043` — the North Star ledger emit — is `except Exception:
log.debug(...)` with the return value discarded. Its sibling
`_emit_ledger_attempt` at `router.py:1821` counts losses via `failopen.record`,
added after a documented incident: *"66 dropped events across 2400 writes
produced no error, no log and no counter."* The fix landed on the execution
ledger, not on the one Ground Truth depends on.

### H-10 · The human-approval gate is a string comparison
Verifier promotion checks `actor == "assistant"`. Any other string passes. An
autonomous agent scripting the CLI with `--by anything-else` promotes its own
verifiers to ACTIVE.

---

## MEDIUM

| ID | Finding | Evidence |
|---|---|---|
| **M-01** | `completeness()` returns `(True, [])` for an envelope whose patch was never stored, while `reconstructable` on the same object returns `False` | The exact "hash treated as reconstructable state" failure the module was written to prevent. Masked by a redundant correct check in `accumulate.py` — correct by luck |
| **M-02** | `detect_synthetic()` consults only two env signals | No `bench_*.py` sets `LLM_ROUTER_SYNTHETIC`; the sandbox-path and fixture-session detectors from `sources.py` are not consulted. A benchmark run with accumulation on would capture fixtures as production |
| **M-03** | 34 rows have `chosen_model ≠ final_model` with `fallback_occurred=False` and `fallback_reason=None` | The model that ran differs from the one recorded as chosen, with no persisted explanation |
| **M-04** | `LLM_ROUTER_HOME` is not universally honoured | 120 sites compose `~/.llm-router` directly; five modules honour five *different* override variables. This is how 7 synthetic rows reached the real ledger during development |
| **M-05** | `usage.db` cold start: 1/12 `database is locked`; 5/12 swallowed migration failures | A migration that fails leaves no trace |
| **M-06** | `result_cache` sets `busy_timeout` *after* WAL — the opposite of the project's own documented fix | 2/12 cold starts locked |
| **M-07** | `error_sanitization.py` is a fourth, weaker scrubber, orphaned rather than deleted | Misses Anthropic, OpenAI, GitHub, JWT and PEM entirely. 0 callers today; its name invites the wiring that reintroduces the leak |
| **M-08** | Gateway and `route_server` have no per-request authentication | Bind is correctly gated to `127.0.0.1`; any other local process can trigger billed model calls. `commands/sse.py` requires Bearer — the pattern exists |
| **M-09** | `~/.llm-router` is 5.0 GB, of which **4.8 GB** is a vendored RouterArena checkout | An eval framework living inside the runtime state directory |
| **M-10** | `import llm_router.control_plane.api` → `ImportError: cannot import name 'audit'` | Shipped in the wheel; the enterprise `audit` module it imports is not distributed. Its quarantined test was hiding the failure |
| **M-11** | `llm-router status` crashes on a clean install — `ModuleNotFoundError: rich` | The flagship savings command. Flagged three weeks ago |
| **M-12** | `health` and `gain` are documented and do not exist; two documented host integrations are rejected by the installer | Docs describe a wider surface than ships |

---

## LOW

- **L-01** `derive_trace_id` — the actual G-025 feature — still has 0 callers and
  remains in `__all__`, so a reader believes a trace-ID scheme shipped.
- **L-02** `judge_cascade.should_cascade` / `should_judge_inline`: the module's
  own "pure decision function", exported and unit-tested, 0 production callers;
  `streaming_judge.py` reimplements it inline.
- **L-03** Nine public `cost.py` reporting functions have no callers outside
  their own tests.
- **L-04** Five modules (`budget_lineage_reconciliation`, `feedback_handler`,
  `hook_deadlock_checker`, `oauth_token_rotation`, `service_manager`) each have a
  dedicated test file and zero production callers.
- **L-05** `context_signal.py` docstring says outright *"NOT the one in
  production"* — a live decoy for anyone reading by filename.
- **L-06** `storage/service.py::migrate_config` — 0 callers **and** contains
  `# TODO: Define target schema (mocked here)`.
- **L-07** `router.py:3978` — the bandit reorder fails silently and uncounted; if
  the bandit store corrupts, routing degrades to a static chain forever with no
  signal.
- **L-08** `accumulate_report.py::_runtime_outcomes` has two
  `except Exception: return {}` with no justifying comment — a broken read is
  indistinguishable from "no data yet".
- **L-09** `LLM_ROUTER_PERSIST_RAW=1` disables redaction across four stores at
  once with no startup warning.
- **L-10** `result_cache` has two TTL notions; setting
  `LLM_ROUTER_PERSIST_TTL_DAYS=0` disables physical purging entirely.
- **L-11** `envelope.capture_repo_state` builds `rp / rel` with no containment
  check. Not exploitable today (the caller's regex strips `../`), but the
  hardened pattern exists in `tools/fs.py`.
- **L-12** Tautological assertion at `tests/test_gateway_service.py:53`.
- **L-13** `pytest addopts` excludes `slow`, `requires_ollama`,
  `requires_api_keys`, `requires_codex` by default — a green local run is a
  narrower run than it appears.

---

## Positive findings (recorded as findings, not as consolation)

| | Evidence |
|---|---|
| **The test suite resists mutation** | 6 deliberately-injected defects in safety-critical logic, **6 caught**. The strongest single result in this audit |
| **Append atomicity is real** | 2,000 concurrent appends to `routing_quality.jsonl`, zero corruption |
| **Readers survive truncation** | All three JSONL readers handle a mid-write kill |
| **`budget_backend.py` is correct** | Proper cross-process design. It is the model the broken stores should copy |
| **`result_cache` hygiene is reference quality** | `PRAGMA secure_delete=ON`, VACUUM after purge, 0600 on the db and its `-wal`/`-shm` sidecars |
| **No `shell=True` in production code** | A real prior command-injection primitive in `tools/local_task.py::_run_check` was found and fixed 2026-09-14; now `shlex.split`, no shell |
| **`env_registry.py` validation is non-circular** | It would fail if the scan found nothing — it guards against the "0 failures because the set was empty" trap |
| **Ground Truth's AMBIGUOUS state is honoured** | Never silently collapsed; the label is withheld when an ambiguous cell sits below the cheapest pass |
| **Verifier authoring has no generative step** | Only `run_matrix.py` calls a model, and that is the model under test. The correlated-LLM-failure premise does not apply here |

---

## The pattern behind the register

Almost nothing here is carelessness. The recurring shape is:

> a correct, well-documented primitive is built to fix a specific incident, and
> is then not adopted by the consumers that caused the incident.

`attribution.py` (0 consumers), `is_evaluable` (1), `sqlite_wal.enable_wal` (3 of
9 sites), `failopen.record` (on the execution ledger, not the measurement one),
`error_sanitization` (orphaned instead of deleted), the 0600 opener (on
transcripts, not on intercepts), `budget_backend`'s atomic write (not on
`quota_tracker`).

**Most of the remediation is not "write the fix". It is "finish adopting the fix
that already exists".**
