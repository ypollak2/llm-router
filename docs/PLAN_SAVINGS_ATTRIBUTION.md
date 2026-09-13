# Plan — stop crediting savings for drafts that were discarded

Status: planned, not started. Raised 2026-09-12.

## The defect

`src/llm_router/hooks/auto-route.py:3976` calls `log_direct_savings(...)` on every
DIRECT SUCCESS. The check that decides whether the draft ever replaced Claude's
turn happens 38 lines later:

```
3976   log_direct_savings(...)          ← credits an Opus-equivalent baseline
4014   _turn_blocked = _render_mode != "echo"
4020   if _turn_blocked:                ← log_direct_to_db is gated on this
4023       log_direct_to_db(...)
```

So the two ledgers disagree by construction. `log_direct_to_db` books only
substitutive routes; `log_direct_savings` books every draft produced. In the
default mode (`LLM_ROUTER_RENDER_MODE` unset → `"auto"`, and `LLM_ROUTER_ZERO_CLAUDE`
unset → `_resolve_auto_render_mode` returns `"echo"` → `_turn_blocked = False`)
nothing is substituted, so those savings are booked for turns Claude then
answered at full price.

Note `LLM_ROUTER_RENDER_MODE=block` also produces a turn-replacing mode without
zero-Claude (`response_formatter.py:23`) — an earlier draft of this plan said
zero-Claude was the only switch, which is wrong.

The asymmetry is not deliberate. The session-store write at :4036 carries an
explicit comment justifying why *it* is unconditional. The savings call carries
no such note — it is the odd one out.

Measured over 2026-09-12 16:30 → 09-13 07:17, re-derived independently:
`usage.db savings_stats` booked **38 rows / $0.593715**. Of those, **31 rows
($0.516110) are DIRECT drafts** and 7 ($0.077605) are receipt records from a
different path (`savings_logger.py:207`) — so not all of it is draft credit.

Matching the 26 `DRAFT UNUSED` invocation ids to their rows isolates
**$0.426410 credited for drafts that were explicitly discarded**. That is the
number this plan exists to stop booking.

Draft verdicts in the same window: **32 produced, 26 `DRAFT UNUSED`, 0 `DRAFT
USED`, 6 with no verdict** — pending records expire after an hour
(`draft_usage.py:46,85`), so a missing verdict is not a discard. An earlier
draft of this plan said all 32 were discarded; that overstated the evidence.

## Why the obvious fix is not quite right

Moving the call below :4014 and gating it on `_turn_blocked` makes the number
honest, but books **zero** in echo mode — and echo mode is not worth exactly
zero. A draft Claude actually reads as a hint can shorten the turn. The problem
is that "was it used" is not knowable at :3976: `draft_usage.py` decides it by
inspecting the *following* invocation. The verdict arrives one turn late.

So this needs a provisional-then-settled shape, not a single gate.

## Stage 1 — stop the overcount (small, correct by construction)

1. Move `log_direct_savings` below the `_turn_blocked` computation and gate it on
   the same condition as `log_direct_to_db`.
2. In echo mode, write a row with `saved = 0` and a new `mode` column recording
   `"echo"` — do not silently drop the row. A row that exists with zero credit is
   evidence; a missing row is indistinguishable from the hook not running.
3. Schema: add `mode TEXT` and `realized INTEGER DEFAULT 0` to `savings_stats`,
   with a migration that backfills existing rows as `mode='unknown',
   realized=0` — existing history must not be retroactively reinterpreted as
   verified.

Effect: the headline savings number drops to ~$0 until Stage 2 or zero-Claude
mode, which is the truth.

## Stage 2 — settle the echo-mode rows against the use verdict

4. `draft_usage.py` already derives USED/UNUSED per invocation. Be aware how
   narrow that test is: `draft_was_relayed()` checks only whether the **first
   non-empty line** of the assistant's reply contains `🎯 LLM Router routed`
   (`draft_usage.py:107`). It does not compare substance, so a draft genuinely
   used as a hint but not relayed verbatim counts as UNUSED. Any settlement
   built on it inherits that bias, and Stage 2 should say so rather than
   present the number as ground truth.
   Add `settle_savings(invocation_id, used: bool)` in `savings_logger.py`
   that updates the provisional row: `realized=1` plus a partial credit when
   used, `realized=1` with `saved=0` when not.
5. Call it from the *next* invocation's startup path, where the previous turn's
   verdict becomes available — the same place `draft_usage` already reads.
6. Partial credit model: a used hint saves some of the turn, not all of it. Do
   not invent a multiplier. Book `saved = 0` until there is a measurement, and
   open a separate task to measure it (compare turn output tokens with and
   without a used draft over a sample).

## Stage 3 — make the dashboard report the honest number

7. `llm-router summary` headline becomes `effective_rate` (already computed
   correctly in `routing_report.py`), not `routes`.
8. Drop or relabel the `routes: N · top tier: local (M)` banner in
   `auto-route.py:3073-3105` (`_build_mini_summary`). `N` is the `limit=200`
   argument to `LineageStore().recent()` saturating, and `M` counts
   classification-time table lookups from `model_tracking.jsonl` — written
   before any model is called. It has reported `research/moderate →
   ollama/qwen3.5` for tasks where `chain_builder.build_chain()` returns `[]`
   unconditionally (`chain_builder.py:153-154`), i.e. Ollama was never tried.
   Either count executed routes or stop calling them routes.

## Tests

| test | asserts |
|---|---|
| echo mode + DIRECT SUCCESS | a `savings_stats` row exists with `saved=0`, `mode='echo'`, `realized=0` |
| zero-Claude + DIRECT SUCCESS | row with real credit, `mode='block'`, `realized=1` |
| settle(used=True) | provisional row updates to `realized=1`; no duplicate row |
| settle(used=False) | row settles to `saved=0`, `realized=1` |
| migration | pre-existing rows survive with `mode='unknown'`, `realized=0`, totals unchanged |
| regression | `log_direct_to_db` gating unchanged — the two ledgers now agree on substitutive routes |

## Related defect, same area

`savings_log.jsonl` is a **claim-and-delete work queue**:
`session-end.py:305-365` does `os.replace(SAVINGS_LOG_PATH, claim)` on every
SessionEnd, then imports into `usage.db`. Anyone reading that file for a savings
total undercounts by whatever has already been drained — it read as 1 entry /
$0.018 against a real ledger of 38 / $0.5937 during this investigation. Either
expose a read API, or document at the top of the file that it is a queue and not
a ledger.
