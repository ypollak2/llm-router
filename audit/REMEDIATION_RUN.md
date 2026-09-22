# REMEDIATION RUN — live state

Plan: `audit/22_REMEDIATION_PLAN.md`. Base: `5f90f24`.
Update after EVERY task. This file survives compaction; memory does not.

## Decisions taken (operator, 2026-09-22)
* **R3 = honesty now, containment later.** Rewrite the claim; do not build a
  sandbox this round. Containment becomes a tracked follow-up.
* **R8 = default capture ON with explicit consent at install.** The consent
  flow is load-bearing — a silent default flip is a privacy regression, not a
  fix. No capture without a recorded, informed opt-in.

## Rule for every task
Not done until its **red-check** has been OBSERVED FAILING — using the
narrowest mutation that reintroduces the bug, not a whole-file revert.
(A-10: a whole-file revert removes a pinned string, so a substring test goes
red and looks sound.)

| ID | Status | Task | Red-check (must be observed failing) |
|---|---|---|---|
| R11 | **done** | One canonical source per concept, enforced by AST scan; fixes cost.py `'google'` | add `Path.home()/".llm-router"` anywhere -> scan fails naming it |
| R13 | **partial** | Ban source-text assertions; convert to behavioural/AST | apply the A-10 comment-evasion -> converted test fails |
| R1 | `running` | Scrub at the write boundary; 0600 at creation | remove scrub from ONE writer -> canary scan names it |
| R2 | `todo` | Nothing leaves the machine unscrubbed; add DSN pattern | remove webhook scrub -> wire-body test fails |
| R4 | `todo` | `agent_loop.run_command` env allowlist + enumerating test | revert one call site -> enumeration names it |
| R12 | `todo` | Every counter has a reader, enforced | add a `record()` with no reader -> test names it |
| R14 | `todo` | `attempt_log._rotate` locking | remove lock -> 8-proc barrier loses records |
| R15 | `todo` | Inspect `finish_reason` | force `content_filter` -> recorded, success False |
| R6 | `todo` | One savings number across all surfaces | bypass the accessor in one surface -> test names it |
| R7 | `todo` | Label every money figure; net not gross | strip one label -> test fails |
| R9 | `todo` | Hook death visible (start marker + doctor rate) | inject 70s sleep -> doctor reports a kill |
| R10 | `todo` | Refuse unservable capability (tools/vision/schema/context) | drop refusal from one endpoint -> parametrised test names it |
| R3 | `todo` | SECURITY.md honesty + interpreter corpus | add an interpreter to the allowlist -> corpus test fails |
| R8 | `todo` | Capture ON behind explicit install consent | consent absent -> capture stays off |
| R16 | `todo` | `capture()` sees cwd/tools, or scope the claim | a repo task reaches a frozen dataset, or docs say it cannot |
| R17 | `todo` | Validator must discriminate | `len(answer)>5` -> capped at LOW |
| R5 | `todo` | Release the fixed HEAD | remove `rich` -> clean-room job fails |
| K1-K7 | `todo` | Audit-readiness kit | see plan |

## Sequencing rationale
P2 (R11, R13) runs FIRST: both change how every later fix and test is written.
Doing P1 first means writing it twice. R1/R2/R4 follow immediately — they are
active harm, not theoretical.

## Log
_(appended after each task)_

## Log

### R11 — done 2026-09-22
Found instances **10, 11, 12** of the path class, all escaping into the real
home: `direct_diagnostics._samples_path`, `seats.seats_path`,
`quality_feedback._loophole_jsonl_path`. Fixed `cost.py` to import
`GOOGLE_PROVIDERS` (google now reaches the ledger; nonsense-provider still
rejected). AST scan + reasoned allowlist now fail on any new instance.

Two things the work surfaced that the audit had not:

* `paths.py:5` documents this defect as fixed. It was not — `default_factory`
  runs at instance construction and `get_config()` is a process-lifetime
  singleton, so the MONEY DB path still froze. Reproduced, then converted to a
  property that re-resolves per access.
* **I broke the ledger writer and caught it in ten minutes.** Fixing the freeze
  made `db_path == state_path("usage.db")` always true, so
  `_refuse_unisolated_test_write` began silently refusing EVERY test write. A
  guard that blocks everything fails in the direction that looks like passing
  tests. It now compares against the operator's real home, where "production"
  means something. Three arms verified: isolated allowed, real-DB refused,
  tmp allowed.

Red-checks observed failing (narrowest mutation, per the plan's rule):
hand-copied resolver reintroduced -> scan names file and line; `cost.py`
reverted to hand-listing -> the Gemini row does not reach the ledger.

### R13 — partial, and scoped honestly
Converted the 2 assertions the audit PROVED evadable (`test_t08`) to AST
assertions via new `tests/_ast_assert.py`.

**Red-check: the exact comment evasion that let 23 tests pass this morning now
FAILS** — `router's bandit feed no longer reads quality_degraded (searched the
AST, so a mention in a comment does not count)`.

62 source-text assertions remain. Converting all 64 mechanically would be
wrong: many are genuinely structural ("this migration is in the migration
list") with no behavioural equivalent, and a forced conversion would produce
weaker tests, not stronger ones. A ratchet caps the count; a unit test proves
the AST helper rejects a commented-out call and accepts the real one.

**Not claimed as complete.** Remaining conversions are tracked work.

## PARKED — P-05 · logging-config pollution across test files

**Found while gating R11. NOT caused by R11 — reproduces identically at
`5f90f24`, before any of this work.** A new finding the audit missed.

```
pytest tests/test_calibration.py                      -> PASS
pytest tests/test_built_artifact_is_complete.py \
       tests/test_calibration.py                      -> FAIL
```

```
E   AttributeError: 'LogRecord' object has no attribute 'message'
```

Bisected to `tests/test_built_artifact_is_complete.py` as the polluter (present
in the failing window, absent from the passing one). The error is raised inside
the **logging pipeline**, not the assertion: something installs a structlog
`ProcessorFormatter` on the root logger that expects `record.message`, and
`caplog`'s handler then formats through it. Changing the assertion to
`getMessage()` does NOT fix it, which is how we know the fault is upstream of
the test.

### Why it matters beyond one test
Same class as A-07: a test that passes alone and fails in company. The
`_no_module_state_leak` guard added in F01-F06 catches module-attribute stubs
but **not logging reconfiguration**, so this class of pollution is currently
undetected. That guard's scope is narrower than its name suggests.

### Why parked rather than fixed
Restoring logging configuration correctly is its own change with its own
blast radius (every test that asserts on logs), and it is not R11. Fixing it
inside an R11 commit would hide a distinct defect inside an unrelated diff —
the exact habit this audit was convened to stop.

### Unblocks
Extend the isolation fixture to snapshot and restore `logging` root handlers
and `structlog` configuration, then delete this entry.
