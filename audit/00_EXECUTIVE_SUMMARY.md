# EXECUTIVE SUMMARY — Ultimate Adversarial Audit

Subject: `357a402` on `fix/audit-2026-09-22`. See `FROZEN_STATE.md`.
Method: 11 independent specialists; orchestrator did reconciliation only and
re-derived every CRITICAL itself. The orchestrator wrote the code under audit —
see the conflict-of-interest note in `FROZEN_STATE.md`.

## What this product actually is

A single monolithic routing core (`router.py::route_and_call`, ~5,300 lines)
behind four largely independent front doors: MCP tools, Claude Code hooks, an
HTTP gateway, and a ~50-subcommand CLI. The classification and context-injection
core is genuinely wired and behaves as documented on the paths that could be
runtime-probed. Almost everything else in the trust story is weaker than it looks.

## The one-sentence answer to the core question

**It routes, and the routing is real; it does not, and currently cannot,
demonstrate that routing preserves task success — and the dollar figure users
see is overstated by roughly 1.7-2.5x from baseline choice alone, before
counting the cases where the honest answer is $0.**

## Top risks

### 1. The agent command allowlist cannot constrain execution (CRITICAL)
Ten of 28 allowlisted programs are general-purpose interpreters. `python -c`,
`node -e`, `awk BEGIN{system()}`, `find -exec`, `sed -e …e`, `go run`,
`cargo run` and three separate `git -c` transports all pass the real
`guard_command` in default mode. This is not a patchable bypass: closing it
means removing the tools the coding agent exists to run.

Compounded by `agent_loop.run_command` inheriting the **full parent
environment** — so anything that executes also reads every credential.

### 2. Secrets reach durable storage on ordinary error paths (CRITICAL)
No attacker required. A provider call fails; the exception text is stringified
into `rejection_reason`/`fallback_reason`/`reason` with zero scrubbing and
written to `usage.db::execution_events` and to `attempts.jsonl` (mode **0644,
world-readable**). Captured live: a full OpenAI key, a labelled AWS secret, a
JWT. `execution_ledger.py` contains no scrub call anywhere.

Worse, one path **leaves the machine**: `alerts.emit_alert` POSTs its `detail`
dict to a webhook unscrubbed — captured containing a Postgres DSN with a
plaintext password.

### 3. "Preserving task success" is structurally unmeasurable (CRITICAL)
`prompt_capture.py` is **off by default** (`LLM_ROUTER_GROUND_TRUTH=1`). That
capture is the only prompt-to-decision join. In the shipped default there is no
wire by which success could be measured — and the headline savings metric has
no quality term at all, so savings accrue identically whether the cheap answer
was right or garbage.

The repo's own `discriminate.py` builds constant-policy baselines and then says
the router is "deliberately absent" because the dataset has not been shown to
discriminate. **That reasoning is correct.** The finding is that the gate was
set honestly and has never been passed: the router has never been compared to
"always cheapest".

### 4. Two savings numbers disagree on identical data (CRITICAL)
`cost.get_savings_by_period` applies the new provenance filter.
`dashboard_data.py` — which by its own comment feeds **~26 reporting surfaces**
including the status bar, TUI and session-end card — contains **zero**
references to `is_simulated`. Same DB, same rows, two answers. The fix shipped
this morning created a split brain rather than closing one.

Confirmed downstream: `savings-report`, `status` and `doctor` report three
different totals from one database at one moment, and `savings-report`'s own
docstring calls itself "the SINGLE source of truth".

### 5. The capability registry does not gate model selection (HIGH)
`ModelMetadata.capabilities` / `context_window` / `with_capability()` have zero
callers outside their own definitions. Consequences, all live-reproduced:
tools are silently dropped by the Python API; **vision input is silently
discarded** with no `TaskType.VISION` anywhere; `/v1/responses` never got the
tool-refusal fix its two sibling endpoints did; no context-window pre-flight
exists anywhere.

## Biggest architectural concern

Fixes are applied call-site-by-call-site instead of by consolidating on one
canonical source. The repo has exactly one proven structural fix — *delegate to
a canonical function rather than copy its current state* — applied successfully
to the secret scrubbers and nowhere else.

Live proof: this morning's T-20 fix created `model_registry.GOOGLE_PROVIDERS`
as the canonical provider-identity set and updated two call sites.
`cost.py:1641::VALID_PROVIDERS` still hand-lists `'gemini'` and omits
`'google'` — the name the registry actually assigns. Every real Gemini routing
decision raises `ValueError`, is swallowed at `router.py:2368`, and never
reaches `routing_decisions`. **Third instance of the class, created by the
commit that fixed the second.**

Same pattern in path resolution: 9 dated instances since May, a "repo-wide
sweep" that declared the class closed, and 5 more instances the same day.

## Biggest measurement concern

Every user-visible dollar figure is gross-vs-flat-Opus-baseline with no
subscription gating, no overhead subtraction and no retry accounting. Under the
subscription default the honest marginal saving is $0, and exactly one of four
surfaces applies that gate. `routing_overhead_usd` is dead code: 37,872 real
rows, $366.80 of claimed savings, **$0.00 of overhead ever recorded**.

The one number computed correctly end to end — `execution_ledger
.net_realized_savings_usd` — is not shown to users.

## Biggest Ground Truth concern

Deferred to the GT specialist, still running. What is already established: the
capture that feeds it is off by default, and `judge_score` is populated on
**0 of 1,599** rows, so the bandit's "quality" term is a near-constant and
`avg_cost` is what actually orders candidates.

## What actually works

* Classification and context injection are wired, causal and correct on probed
  paths. `classify.py` composes its sub-classifiers as one engine, not
  fragmented competitors.
* `LLM_ROUTER_HOME` isolation genuinely works for the main stores.
* Context integrity through `providers.call_llm` to Ollama is clean — system
  prompt and 4-turn history canaries both survived.
* The secret-scrubber consolidation worked and held.
* `pricing.py` is well-engineered against this repo's own history of stale
  price bugs.
* The team self-caught the silent-bypass bug class before this audit did.

## Released vs audited — these are different products

PyPI `14.1.0` is **26 commits behind** HEAD. On a clean install of what users
actually get today: `llm-router status` crashes with `ModuleNotFoundError: No
module named 'rich'` (undeclared), `llm-router demo` prints
`Savings: $-0.0450 (-100% cheaper)`, and `llm-router gain` does not exist
though the README documents it.

All three are **fixed on HEAD and unreleased**. Verified: `rich>=13.0.0` at
`pyproject.toml:46`; demo prints `Savings: $0.0163 (18% cheaper)`; `gain`
dispatches at `cli.py:1142`.
