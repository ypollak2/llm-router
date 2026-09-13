# Plan: make the routing rate measurable, then make it honest

Raised 2026-09-13 after an investigation that produced two wrong answers before
the right one. The wrong answers are the reason this plan exists.

## What the investigation found

| # | Finding | Bug? |
|---|---|---|
| 1 | **The routing rate is a property of the workload, not the router.** Release/CI days route at ~62%, ordinary code days at 78–96%. The gate `_is_context_dependent` fired 1.5% on 31 Aug and 32.2% on 1 Sep — a 21x jump with **no commit touching it** (last change 19 Aug, next 10 Sep) — and recovered to 4.4% by 10 Sep with no fix. Code regressions do not heal themselves. | No |
| 2 | **Silent no-reach.** `LLM_ROUTER_ENFORCE=off` or `shadow` skips the DIRECT block and logs *nothing*. Verified across all four modes: `smart`/`advise` reach, `off`/`shadow` vanish. | **Yes** |
| 3 | **The metric is unmeasurable.** Test-suite runs write `session_id=unknown` into the production log — **1,037 of 1,938 entries on 31 Aug**. Every rate derived from that log is wrong. | **Yes** |

Two rates were reported to the user during this investigation before the
denominator was understood: "35% baseline, now 9%" and "a step change on 4/5
Sep". Both were artifacts. The real baseline was 93-100%.

## Fix 1 — no unlogged outcome, ever

`auto-route.py:3788` reads:

```python
if _direct_enabled and _enforce_mode not in ("shadow", "off"):
```

with no `else`. Every other skip in that function logs its reason; this one does
not, and it is the branch that produced 28.5% of one day's prompts.

**Change:** give it an else that logs `DIRECT SKIP: enforcement disabled
(mode=<mode>)` and `DIRECT SKIP: direct execution disabled by env`, as separate
reasons so they are distinguishable.

**Invariant to enforce by test, not by care:** every invocation that logs
`prompt_len=` must also log exactly one of `DIRECT:`, `DIRECT SKIP: <reason>`,
`BYPASS`, or `CONTINUATION`. A prompt whose fate is unrecorded is a bug
regardless of what that fate was.

## Fix 2 — the production log must not contain test runs

`_debug_log_path()` already carries a comment about 227 fake-model invocations
polluting the log. They are still arriving.

**Change:** when `PYTEST_CURRENT_TEST` is set, or `session_id` is absent/`unknown`,
write to `auto-route-debug.test.log` instead. Same format, different file.

**Why not filter at read time:** every consumer would have to remember, and two
did not — which is how this investigation went wrong twice.

## Fix 3 — one supported way to compute the rate

Nothing in the repo computes the routing rate, so everyone who needs it writes
an ad-hoc parser and gets a different answer.

**Add `scripts/routing_rate.py`** with a single definition:

```
routing rate = DIRECT SUCCESS / (prompts from a real session)
```

where *real session* means a `session_id` that is present and not `unknown`.
It must print, alongside the rate:

- the denominator, so a small sample is visible as a small sample
- the skip-reason breakdown, so a workload shift is visible as a workload shift
- a warning when any day has fewer than 50 real prompts

The warning matters: 5-9 Sep showed rates of 2.5%, 1.6%, 0%, 4.3% on samples of
21-64 prompts, and those numbers were meaningless.

## Tests

| test | asserts | catches |
|---|---|---|
| `test_every_prompt_logs_an_outcome` | for each enforce mode in (smart, advise, off, shadow), a prompt produces `prompt_len=` AND exactly one terminal outcome line | Fix 1 regressing |
| `test_enforce_off_logs_a_reason` | `ENFORCE=off` emits `DIRECT SKIP: enforcement disabled` | the specific silent branch |
| `test_pytest_runs_do_not_touch_the_production_log` | with `PYTEST_CURRENT_TEST` set, `auto-route-debug.log` is unchanged and the test log grew | Fix 2 regressing |
| `test_unknown_session_goes_to_the_test_log` | a hook input with no `session_id` writes to the test log | the other pollution path |
| `test_routing_rate_excludes_unknown_sessions` | a fixture log of 10 real + 90 unknown yields a rate over 10, not 100 | the exact error made twice today |
| `test_routing_rate_warns_on_small_samples` | a 20-prompt day is flagged | the 2.5%-on-40-prompts class of false alarm |

The whole suite runs against **fixture logs**, not the live one, so it is
deterministic and cannot itself pollute anything.

## What this plan does NOT promise

- It does not raise the routing rate. It makes the rate *knowable*. The rate
  itself is set by what you ask for, and on release days it will legitimately be
  low.
- It does not fix `_is_context_dependent` being conservative. That gate exists
  to stop a blind model answering about files it cannot see, and the 10 Sep
  session already has a plan for narrowing it. That is a separate change with
  its own evidence requirement.
