# llm-router — second adversarial audit, executive summary

**HEAD:** `8c7366b` on `fix/audit-2026-09-21-remediation` · **Date:** 2026-09-22
**Subject:** the post-remediation tree, one day after a 34-task fix run.

**Declared conflict of interest.** Most of the code under audit was written by the
same author who is writing this summary. Verification was therefore delegated to
six agents that never saw the remediation conversation and were briefed to treat
every claim — including the fixes' own commit messages — as a hypothesis. Every
CRITICAL below was then re-reproduced by hand. Where an agent's finding did not
survive that check, it is recorded as not reproducing rather than dropped.

---

## Overall state

**The routing engine works. The layer that reports on it still cannot be
trusted, and yesterday's remediation fixed the abstractions without fixing the
adoption.**

The previous audit's pattern was: *a correct primitive is built to fix an
incident, then not adopted by the consumers that caused it.* Yesterday's fixes
were supposed to close that. They repeated it — at one level up.

  * The canonical scrubber was made a genuine superset of every rival table.
    Two live persistence paths still scrub with a weaker local copy.
  * `usage` gained write-time provenance. Five other money-reporting surfaces
    read the same tables with no provenance filter at all.
  * The quality ledger learned to record failure. Two more terminal paths still
    write no row.

And the test that was supposed to prove the scrubber fix — one I wrote — asserts
the canonical scrubber's coverage without ever checking that call sites use it.
**It tested the abstraction, not the adoption.**

---

## The single most important finding

**The test suite reports 0 failures while 8 tests are genuinely failing.**

    tests/test_groundtruth_accumulation.py alone          -> 8 FAILURES
    tests/qa/test_h08_*.py first, then accumulation       -> 0 failures
    accumulation first, then tests/qa/test_h08_*.py       -> 8 FAILURES
    full suite (h08 collected at #52, accumulation #377)  -> 0 failures, exit 0

`test_h08_gate_admits_only_gradable_tasks.py` injects a fake `run_matrix` into
the `groundtruth` package object and its cleanup restores only
`sys.modules["groundtruth.run_matrix"]`, not the package attribute. The fake
survives, `replay_available()` returns True for the rest of the process, and the
eligibility gate silently reopens — masking 8 real failures caused by the H-08
change itself.

**I wrote that test, and every "suite green, exit 0" reported during the
remediation was true and meaningless for those 8 tests.**

Two further ways the same number moves: `_quarantined_tests/` holds ~90 known
failing assertions excluded from the default run, and a concurrent
`llm-router update` flips 3 config tests because that command writes to
`~/.claude/` regardless of `LLM_ROUTER_HOME`.

---

## Top 10 risks

| # | Risk | Severity |
|---|---|---|
| 1 | **A green suite is not evidence.** 8 real failures masked by cross-test module-cache contamination; ~90 more quarantined; 3 more flip on ambient host state | CRITICAL |
| 2 | **`llm-router demo` reports negative savings as a win** — `Savings: $-0.0450 (-100% cheaper)`, in green. Hardcoded $0.015 baseline per row. First thing a new user sees | CRITICAL |
| 3 | **The gateway inflates complexity from the system prompt.** `"hi"` → SIMPLE; the same `"hi"` behind boilerplate → COMPLEX. Defeats the cost purpose on the flagship SDK path | CRITICAL |
| 4 | **Two live scrubbers miss Slack/JWT/Google keys** and never delegate to canonical. One runs on every Bash/Edit tool call and writes 0644; one is installed in `settings.json` now | CRITICAL |
| 5 | **Provenance covers 1 of 6 money surfaces.** `get_team_savings` (broadcast to Slack/Discord) has no filter; `claude_usage`/`codex_usage`/`gemini_usage`/`savings_stats` have no provenance column at all | CRITICAL |
| 6 | **The Ground Truth verifier pipeline is orphaned.** Pool IDs are `gtc-<hash>`, frozen task IDs are `gt-<seq>`. The two namespaces never intersect, so an approved, mutation-validated, ACTIVE verifier can never grade anything | CRITICAL |
| 7 | **The fail-open counter is write-only** — 58 writers, 0 production readers, and it silently loses its own losses when the store is unwritable | HIGH |
| 8 | **Two more terminal paths write no quality-ledger row** — idempotency dedupe and the exhaustion floor. The floor is exactly the case the quality fields exist to measure | HIGH |
| 9 | **The bandit's reward divides by a 1e-9 cost floor**, so any free model outranks any paid one by ~1e8 regardless of success rate — on by default | HIGH |
| 10 | **A degraded answer is returned as a clean one.** The exhaustion floor returns a gate-rejected response through the success path with no marker, and feeds it back as "success" | HIGH |

---

## Top 10 strengths — measured, not assumed

1. **8 of 8 deliberate defects caught** by the 25 new tests, in 1–4s, with messages naming the exact broken behaviour.
2. **Zero tautologies** in the entire suite; no `@pytest.mark.skip`; 2 xfails, both deliberate pins.
3. **The canonical scrubber's own coverage is real** and is a genuine superset of every rival pattern table.
4. **`persist_redaction` and `prompt_capture` fail closed** — verified by code path, not docstring.
5. **`scripts/groundtruth/scrub.py` refuses to fall back** to a weaker local copy, and actually does.
6. **Correlated LLM failure does not exist here.** One model call in the whole GT subsystem, and it grades the subject — the pipeline stages are deterministic template code.
7. **Pricing is a single, self-consistent source** — the signed-savings lint passes across 10 money modules.
8. **No `shell=True` anywhere**; all subprocess use is argv-list.
9. **The cross-origin guard is real and global**, applied before auth in both HTTP surfaces.
10. **The hook classifier divergence is documented and parked, not hidden** — 59.7% agreement, n=750, with reasoning.

---

## Ready for wider real-world usage?

**As a router: yes, with the gateway caveat.** The engine, fallback and
local-first behaviour are sound. The gateway path over-routes.

**As a measurement platform: no, and less so than it appears.** Yesterday's fix
made one surface honest and left five. Publishing any figure today publishes a
number whose provenance filter covers a sixth of the readers.

**As a dependency: not yet.** `llm-router demo` prints impossible arithmetic,
three CLI commands are broken, 28 of 51 subcommands are undocumented, and a
shipped function raises `ImportError` on every installed call.

---

## The biggest blocker to trustworthy router evaluation

**It is no longer the ledger. It is that nothing tells you when a claim stops
being true.**

Every mechanism built for that job is itself unwatched: the fail-open counter has
no reader, 84 silent swallows can drop a write with no signal, and the test suite
— the last line of defence — returns 0 while 8 tests fail.

The instrument can now record a failure. Nothing yet guarantees anyone is told.
