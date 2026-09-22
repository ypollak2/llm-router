# 13 — Concurrency, Durability, Hook Reliability, Provider Failure Matrix

Specialist: Reliability/SRE. Phases 27, 28, 34, 37. HEAD `357a402e`
(`fix/audit-2026-09-22`). All probes under `export LLM_ROUTER_HOME=$(mktemp -d)`,
`LLM_ROUTER_BASH_INTERCEPT=off`, `.venv/bin/python`. No repo file modified, no
deletions, nothing written to `~/.llm-router` or `~/.claude`. Probe scripts live
in this session's scratchpad, not the repo.

---

## Phase 27 — concurrency torture (race results)

| Store | Forcing method | Result | Class |
|---|---|---|---|
| `attempt_log.py` (`attempts.jsonl`) | Pre-filled file to 399,492B (just under the 400,000B `_rotate` threshold), 8 processes × `mp.Barrier`, 40 appends each, all racing the same `_rotate()` | **3 trials: 2, 12, 17 of 320 new records LOST** (0.6–5.3%); one trial produced 2 corrupt/unparseable JSON lines. `_rotate()` does `read_text()` → `write_text()` with **no lock, no `os.replace`** — a classic non-atomic read-modify-write, unprotected even though `file_lock.exclusive_lock` exists in the same repo and is used by the near-identical `session_store.py`/`pool.py` cases. | **CONFIRMED** |
| `routing_quality.py` (`record_route`, plain append, no rotation exists for this file) | 12 processes × barrier, 100 appends each (1200 total) | 0 lost, 0 corrupt, 0 duplicate ids | **CONFIRMED — safe** (small single-`write()` appends are atomic in practice on APFS; there is no rotation path for this file to race) |
| `execution_ledger.py` (`usage.db`, sqlite, WAL, `busy_timeout=20s`) | 32 processes × barrier, 150 `INSERT OR IGNORE` events each (4,800 total); re-ran at 16×50 first | **0 dropped, `PRAGMA integrity_check` = ok** at both scales. `dropped_event_count()` fail-open counter present and correctly 0. | **CONFIRMED — fixed.** The repo's own comment cites a prior measurement of 66/2,400 dropped (RED5-02); current WAL + 20s busy-timeout + per-call fresh connection holds under 2× that load with zero loss. **Re-measured, not assumed.** |
| Ground Truth pool `admit()` (`scripts/groundtruth/pool.py`) | Not re-torture-tested from scratch (code already carries its own documented forcing measurements: H-07 "19 recorded where 21 occurred" and T-12 "2 canonical rows where 1 was correct", both now inside one `exclusive_lock()` critical section that re-`load()`s before deciding) | Code-reviewed: the whole find-duplicate→decide→append sequence is now one locked, freshly-loaded critical section. Lock releases cleanly even under `SIGKILL` (see crash table below). | **CONFIRMED fixed by code + our own SIGKILL re-test** (no new corruption found) |
| `quota_savings.py` reads of `usage.db` | Not separately raced — reads only, standard sqlite reader isolation; the writer side is `execution_ledger.py`/`budget_backend.py`, already tested above | — | not separately probed |
| `quota_tracker.py` `usage.json` | Already fixed in-tree (H-06 comment): temp-file + `os.replace`, confirmed durable under our SIGKILL torture (below) | 0 corrupt reads across 25 kills | **CONFIRMED fixed** |

**Cross-cutting fact, not previously stated this plainly:** `execution_ledger.py`
and `budget_backend.py`/`quota_savings.py` **write to the same physical file**,
`usage.db` (`paths.state_path("usage.db")`), in different tables. Concurrency
headroom is therefore shared across "the ledger" and "the cost/quota system" —
a load test of one alone (as the codebase's existing regression test does)
does not prove the other's headroom under simultaneous load from both.

**Anti-vacuity note:** the attempt_log race was found on the *actual, unmodified*
`_rotate()` — not a deliberately-broken copy — so detection is proven by
construction: real loss, real corrupt lines, three independent trials, no
fixture. The other three stores were tortured at 2–32× the load of the
prior-documented incidents and held, which is itself the falsifiable claim
(a test that can't detect a fault would have shown 0 loss everywhere,
including on `attempt_log.py` — it didn't).

---

## Phase 28 — crash consistency (SIGKILL, not SIGTERM)

Method: subprocess loops writing to one store; a `ready` flag file marks "past
import, inside the write loop"; driver waits for the flag, sleeps a
**randomized 0.2–10ms**, then `SIGKILL`s. 25 kills per store, hunting the
narrow mid-syscall window rather than a fixed delay that would just land
between iterations.

| Store | State after SIGKILL | Lies? |
|---|---|---|
| `execution_ledger` (`usage.db`) | `PRAGMA integrity_check` = ok on every trial; no `-wal`/`-shm`/`-journal` sidecar left behind (clean checkpoint or clean abort each time); no row with null/zero `ts`. | **No** — sqlite's own transaction boundary makes "provider called but ledger not written" and "route started with no terminal state" both resolve to "the whole `INSERT` never happened," which is correct, not a lie. |
| `attempt_log.jsonl` | 4,308–4,860 lines every trial, 100% valid JSON, file always ends in `\n`. | **No** — each `record()` call opens, writes one line, and closes (flush-on-close), so a kill lands either fully before or fully after one line; no torn lines observed in 25 kills. (Contrast with Phase 27: the *rotation* path is the unsafe one, not the per-call append.) |
| GT pool (`ground_truth_candidates.jsonl` + `.lock`) | Lock file, when present, was **acquirable immediately after kill** on every trial — `fcntl.flock` releases on process death regardless of signal, so no starvation. Pool JSONL always 100% valid. | **No** |
| `quota_tracker` `usage.json` | `usage.json` itself was valid JSON on every trial (atomic `os.replace` holds). | **No**, for the target file itself. **But**: 13 of 25 kills left an orphaned `usage.json.<pid>.tmp` in the state dir — the code's own comment says "never leave a stray temp file behind for the next `gc` run to puzzle over," but **`gc.py` has no sweep for `*.tmp`** (`grep -n '\.tmp\|glob' commands/gc.py` = no matches). This is litter, not corruption, but it is unbounded: every crash during a quota write adds one file that nothing ever removes. | **DESIGN RISK** (not a lie, but an unaccounted-for accumulation) |

**Impossible-combination hunt, specifically requested by the brief:** none of
the four stores produced a "started with no terminal state," "cost with no
call," or "claimed-but-unimported" row under SIGKILL. The reason is
structural, not incidental: every write observed here is a single-syscall
JSONL append, a lock-protected append, or a sqlite transaction — there is no
multi-step store in this set where step 1 (e.g., "route decision made") and
step 2 (e.g., "provider called") are two different files or two different
uncoordinated writes that a kill could split apart. **This does not clear the
five boundaries the brief named** (route→provider, provider→ledger,
success→cost, GT-capture→pool, savings-JSONL→import) as end-to-end sequences —
those are cross-module flows through `router.py`/`gateway.py`/the savings
importer that were not exercised as a whole under kill in the time available;
only the individual store-writes at each end were. **Mark the five end-to-end
sequences HYPOTHESIS, not tested; the individual persistence layers underneath
them are CONFIRMED durable.**

`dropped_event_count()` in `execution_ledger.py` is itself a second instance
of the "counter nobody reads" defect this repo's own history already
diagnosed once (T-07, `failopen.py`'s docstring): it exists, increments
correctly, is logged via `_log.warning` at the drop site — and `grep -rln
dropped_event_count` across the whole tree returns only the module itself and
its own test. `doctor.py` does not read it. **CONFIRMED.**

---

## Phase 34 — hook reliability (this dominates)

Hook: `~/.claude/hooks/llm_router-auto-route.py` (UserPromptSubmit,
`timeout: 60` in the installed `settings.json`). Measured with real
subprocess invocations, isolated `LLM_ROUTER_HOME`, real local Ollama
(`qwen3.8:latest` primary per `FROZEN_STATE.md`'s ambient config).

| Condition | n | min | p50 | max | vs 60s kill timeout |
|---|---|---|---|---|---|
| Trivial (skip-pattern prompt, e.g. `ls -la`) | 5 | 0.150s | 0.156s | 0.174s | fine |
| Short ambiguous Q&A ("what is the capital of France?"), **cold Ollama** | 5 | 1.566s | **11.75s** | **14.125s** | 19–24% of budget on ONE hook of four that share the UserPromptSubmit stage |
| Same prompt, Ollama **already warm** (immediately after prior calls) | 4 concurrent | — | ~3.3s each | 3.67s | fine once warm |
| Code-shaped ambiguous prompt (matched heuristically, no Ollama escalation) | 5 | 0.199s | 0.259s | 0.274s | fine |
| **50KB prompt** (Ollama escalation + full pipeline) | 5 | 27.1s | **35.1s** | **55.265s** | **92% of the 60s kill window on one run** |

Findings, in order of severity:

1. **CONFIRMED — the documented Ollama latency is wrong by 5–15×.** The
   hook's own docstring says "Ollama local LLM (free, 1-3s)"; measured cold
   latency for a short prompt is 11.75s p50, 14.1s max — and that's before
   the second and third UserPromptSubmit hooks in the same stage
   (`secrets-guard.py`, `council-advisor.mjs`, `status-bar.py`, each with its
   own 60s budget) even run.
2. **CONFIRMED — large prompts are one bad day from being killed.** 55.265s
   observed against a 60s kill timeout is a 4.7s margin, on ordinary hardware,
   with no injected slowness. Under FROZEN_STATE's own admitted ambient load
   (`LLM_ROUTER_SIDECAR_PREFETCH=1`, an ensemble of two local models sharing
   one Ollama process) or any additional disk/CPU contention, this **will**
   cross 60s regularly, not occasionally.
3. **CONFIRMED — the hook's internal "budget" (30s, per `_report_if_slow`,
   `_SLOW_HOOK_SECONDS`) is not the host's budget (60s), and the diagnostic
   that would explain a slow run only fires from a single call site at the
   very end of `main()` (line 4718).** A `SIGKILL` from the host at any point
   before that line — which is most of the 55s window above — means
   `_report_if_slow()`/`failopen.record("CHZ-FO-HOOK-SLOW", ...)` **never
   runs**. The self-diagnostic the codebase built for exactly this situation
   cannot fire in the situation it exists for.
4. **CONFIRMED (by design, cross-referenced) — a killed hook is
   indistinguishable from a hook that decided not to route.** Both produce: no
   stdout, no `pending_route_*` state file, no `failopen` record, no debug-log
   entry guaranteed flushed. Claude Code's documented behavior for a
   UserPromptSubmit hook that produces no output is "proceed unmodified" —
   identical to a legitimate `DIRECT SKIP`. **This directly answers the
   brief's question: "if the hook is regularly killed, router accuracy is
   irrelevant" — because from every observable surface (user's screen,
   `routing_quality.jsonl`, `auto-route-debug.log`) a killed hook and a
   correctly-skipped hook are the same event.** There is no counter anywhere
   in this codebase for "hook did not complete" as distinct from "hook chose
   not to route" (searched: no `SIGKILL`/`SIGTERM`/timeout-specific counter
   exists in `failopen.py`'s call sites or `coverage.py`).
5. Not measured in the time available: p95/p99 over a large real sample (only
   5-run micro-batches per condition here), a genuinely large repository, and
   a truly cold semantic/embedding cache. The cold-vs-warm Ollama gap (14s →
   3.3s) strongly suggests the FIRST prompt of any session, or the first
   after any idle gap long enough for Ollama's keep-alive to evict the model,
   pays the full 11–14s tax — i.e., the worst case is not rare, it is "every
   session start." **Mark p95/p99 across real traffic HYPOTHESIS**, not
   measured here — the n=5 batches are enough to demonstrate the shape and
   the margin against the timeout, not a distribution.

---

## Phase 37 — provider failure matrix

Ollama is live locally; `XAI_API_KEY` is present (not exercised live here —
see below). `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`GEMINI_API_KEY` are absent —
**every row for those providers is UNTESTED**, not assumed working.

Method: a local mock OpenAI-compatible HTTP server (`http.server`, this
session's scratchpad only) simulating each failure at the transport layer,
called through the router's **real, unmodified** `litellm.acompletion` path
and then through the **real** `router.py` classifiers
(`_is_rate_limit_error`, `_is_content_filter_error`, `_extract_retry_after`)
and `inference_robustness.py` (`extract_content`, `ensure_non_empty_content`).
This exercises the actual classification code, not a guess about it; only the
transport target (mock vs. real OpenAI) differs, which is why OpenAI/
Anthropic/Gemini rows below are marked "code path tested via mock, live
provider untested" rather than fully confirmed.

| Failure | litellm exception surfaced | Router classification | Elapsed | Verdict |
|---|---|---|---|---|
| 429 rate limit (`Retry-After: 7` header sent) | `RateLimitError` | `_is_rate_limit_error`=True (correct) | **14.13s** | litellm's own internal retry burned 14s before the exception even reached router code — that's 23% of the hook's 60s budget consumed by ONE retried 429, before failover to the next model even starts. **`_extract_retry_after()` returned `None` despite the server sending a real `Retry-After: 7` header** — the backoff hint is silently discarded. **CONFIRMED bug.** |
| 401 invalid key | `AuthenticationError` | not rate-limit, not content-filter (correctly excluded) | 0.00s | fails fast, correctly typed |
| 403 forbidden/no access | `APIError` (generic) | Not classified as an auth-class error — `_AUTH_MARKERS` list has no "forbidden"/"403" entry | 0.00s | **DESIGN RISK**: a 403 gets the router's generic-failure message, not the auth-specific "run `llm-router setup`" remediation a 401 gets, even though both are "your credentials/access are the problem." |
| 500 internal error | `InternalServerError` | generic | 1.39s | fails over correctly, not misread as content-filter or rate-limit |
| Malformed JSON body (truncated mid-string) | litellm itself catches the parse failure and raises `InternalServerError` ("Unterminated string…") | generic | 0.01s | **loud, not silent** — good: a malformed body cannot masquerade as a successful empty completion |
| Truncated SSE stream (closed mid-`data:` line, no `[DONE]`) | not exercised end-to-end (non-streaming call used) | — | — | **UNTESTED** — router's non-streaming `call_llm` path was probed; the separate `call_llm_stream_events` streaming path (referenced in `providers.py`'s own comments) was not exercised against a truncated stream in the time available. Mark HYPOTHESIS. |
| Partial/malformed tool call (`finish_reason=tool_calls`, `content=None`, truncated `arguments` JSON) | `acompletion` returns normally | `extract_content` → empty → `ensure_non_empty_content` correctly raises `EmptyResponseError` → chain failover | 0.00s | **CONFIRMED correct** for the router's own dispatch path. Scope note: the OpenAI-compat gateway (`gateway.py`) refuses `tools`-bearing requests outright (H-03), so this scenario cannot reach *that* surface at all; it can only occur via the agentic tool-execution path (`agentic/adapters.py`), which was **not** separately probed for malformed tool-call arguments — mark that path HYPOTHESIS. |
| Unexpected finish_reason (`content_filter`) **with non-empty content present** | `acompletion` returns normally | **Accepted as a normal success** — router's dispatch loop only checks content emptiness, never inspects `finish_reason` | 0.01s | **CONFIRMED gap.** `_finish_reason()` exists only in `gateway.py`, to report the field to an *external OpenAI-compat client* — nothing in the router's own model-dispatch decision path reads `finish_reason` to decide whether to trust/retry a response. A provider that returns partial content flagged `content_filter` or `length` is indistinguishable, to the router, from a clean `stop`. |
| Context overflow (400, `context_length_exceeded`) | `ContextWindowExceededError` | correctly and distinctly typed by litellm | 0.00s | correct classification; whether `router.py` takes a *different* action for this type specifically (vs. generic failover) was not traced further |
| Network reset / connection drop mid-request | `InternalServerError` ("Connection error.") — note: the mock's attempt at a hard `SO_LINGER` RST itself raised `OSError: [Errno 22]` on macOS, so this is an abrupt-close proxy, not a true TCP RST | generic | 1.36s | fails over, not silent; caveat the imperfect simulation |
| Hang / no response (client `timeout=2.0s`) | `litellm.Timeout` | — | **7.40s to surface a 2.0s timeout** (3.7× overhead) | **CONFIRMED**: litellm's own timeout enforcement has material slack beyond the configured value. `config.request_timeout` defaults to **120s** (`config.py`) — combined with this ~3.7× overhead factor and the hook's ~60s kill window (Phase 34), a genuinely hung provider is *guaranteed* to outlive the hook, not merely likely to. |
| OpenAI / Anthropic / Gemini specifics (real endpoints) | — | — | — | **UNTESTED — no key present.** Code path is shared with the mock-tested cases above (same `litellm.acompletion` call, same classifiers), so classification logic is exercised; provider-specific response shapes (e.g. Anthropic's distinct error envelope) are not. |
| Ollama-specific failures (model not found, OOM, context overflow at the Ollama layer) | not separately probed beyond the generic mock matrix | — | — | **UNTESTED this pass** — Ollama is live and reachable but a dedicated Ollama-error probe (e.g. requesting a model that isn't pulled) was not run; time budget went to the hook-latency finding instead, which is the higher-severity result for a machine where Ollama IS the routing hot path. |
| XAI (`XAI_API_KEY` present) | not exercised live | — | — | **UNTESTED — key present but not spent in this pass**, by the same time-budget tradeoff as Ollama above. |

---

## What breaks first under 100x traffic, and would anyone notice?

The hook gets killed, not the stores. `attempt_log.py`'s rotation race
(Phase 27) is real but low-severity — a few percent of one low-value
telemetry file. `execution_ledger`/`usage.db` and the GT pool held clean
under 2-32× the load of prior documented incidents. The actual failure mode
at 100x is **UserPromptSubmit hook timeouts**, because the cold-Ollama tax
(11-14s) and large-prompt tax (up to 55s measured, against a 60s kill) are
already close to the ceiling at 1x traffic with zero contention — more
concurrent sessions sharing one Ollama process, plus a single stuck 429 that
burns 14s before failover even starts, push routine prompts over the line.
**Nobody would notice deterministically**: a killed hook and a hook that
legitimately chose not to route are the same observable event everywhere in
this codebase — no stdout, no state file, no counter, no log line — so the
first symptom is an unexplained, silent decline in the routing rate that
looks exactly like the "workload shift" this repo's own `CLAUDE.md` already
warns readers not to mistake for a regression.
