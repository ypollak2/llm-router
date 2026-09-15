# Closing the gap — where the 115 prompts actually go

> **Update 2026-09-15, after G5 and G1.** Usable drafts moved **57% -> 70%**,
> measured twice on independent runs that both landed on 70%. The noise floor:
> acceptance is stable across identical runs, draft PRODUCTION swings 7 points,
> and 24 of 115 prompts flip verdict for free — so per-prompt comparisons are
> worthless and only differences above ~5 points on acceptance mean anything.
> G1 was the whole gain. The sections below are the pre-G1 state, kept because
> the arithmetic of what is and is not addressable has not changed.

Written 2026-09-15 from the session-ordered replay (`scripts/bench_session_replay.py`,
5 real sessions in order, n=115), scored with the current scorer.

The goal is ~100% of prompts getting a draft worth using. This is what stands
between here and there, sized rather than assumed.

## The current distribution

| Outcome | Count | Share |
|---|---|---|
| **Usable draft** | 66 | **57%** |
| Drafted but unusable | 17 | 15% |
| No draft at all | 32 | 28% |

Broken down:

| Reason | Count | Share | Is it wrong? |
|---|---|---|---|
| `no free-tier model available` | 14 | 12% | **No** — complex code/research to Claude, the stated carve-out |
| `cites files that do not exist` | 15 | 13% | Yes — but only **2 unique names**, repeated |
| `continuation: bypass (strict ack)` | 7 | 6% | **No** — "continue", "yes" carry nothing to route |
| `draft rejected (ungrounded)` | 5 | 4% | Yes |
| `context-dependent prompt` | 3 | 3% | Yes |
| `asserts a status it cannot observe` | 2 | 2% | Yes |

**18% is correct behaviour, not a gap.** The honest ceiling is therefore ~82%,
and the addressable distance from 57% is **25 points**.

## G1 — The model invents plan filenames (13%, the largest single gap)

15 rejections come from **two** names: `30_CI_GAP_PLAN.md` and
`docs/PHASE_1_PLAN.md`. Verified fake — `git ls-files` finds neither, and the
check produced **zero false positives** across the run.

This is one habit: asked about "the plan" or "the phases", the model invents a
plausible filename rather than saying it cannot see one.

* **Fix**: the draft system prompt already forbids claiming actions; extend it to
  paths — *never name a file you were not shown; say "I can't see which file" instead*.
* **Gate**: re-score the persisted bodies. `cites files that do not exist` falls
  below 5, with no rise in `asks the user a question`.
* **Cost**: a prompt edit. Re-scoring is seconds because every body is on disk.
* **Risk**: low, but it pushes toward deferral, which is the other failure mode —
  so the gate must watch both numbers, not one.

## G2 — Grounding rejects 5 drafts outright (4%)

Separate from G1: these are rejected *before* relaying, by
`_draft_is_relayable`. One cited `_quarantined_tests/...`, a path that existed
when the draft was written and does not now.

* **Fix**: determine whether these are the same invented-filename habit (then G1
  covers them) or a stale-index problem (then it is a retrieval bug).
* **Gate**: classify all 5 by hand. No code until that is known.
* **Cost**: 15 minutes reading 5 drafts.

## G3 — 3 prompts still hit the context gate (3%)

Down from 95 before the rescue arms were unblocked. What remains is the residue
the OKF, session and tool-loop arms could not resolve.

* **Fix**: read the 3. If they reference live external state (CI, a PR), no
  memory can help and `_tool_loop_rescue` is the right arm — confirm it fires.
* **Gate**: each of the 3 either drafts, or has a recorded reason why it cannot.
* **Cost**: small. This is the tail, not the body.

## G4 — 2 drafts claim a status they cannot observe (2%)

Down from 10 before the system prompt was rewritten. The remaining 2 are the hard
residue: confabulation under missing information, which instruction alone does not
reliably suppress.

* **Fix**: the repo-state block (N11) now carries branch, HEAD and dirty count.
  Check whether these 2 prompts had it available; if they did and the model still
  invented, the block needs to be more prominent, not larger.
* **Gate**: 0 status claims on prompts where `<repo_state>` was injected.

## G5 — The measurement is the least trustworthy part of this

Five defects in two days, each caught only after a number had been reported:
contaminated corpus, survivorship-biased p90, a partial rate quoted as final, an
instrument that could not explain 7% of its outcomes, and a moving scorer.

* **Fix**: before any further quality claim, run `bench_session_replay.py` twice
  on an unchanged tree and report the spread. If two identical runs differ by more
  than a few points, no single-run comparison means anything and that must be said
  before, not after.
* **Gate**: two runs, same commit, same corpus, same scorer, spread reported.
* **This blocks G1–G4's gates from meaning anything**, so it goes first.

## Order

    G5  (establish the noise floor — everything else is read against it)
    G1  (13%, one prompt edit, re-scored from disk in seconds)
    G2  (4%, 15 minutes of reading before any code)
    G3, G4  (3% and 2%, the tail)

## What this plan will NOT reach

100%. Eighteen points are correct behaviour: complex work going to Claude is the
stated design, and a bare "continue" carries nothing to route. A realistic target
after G1–G4 is **~75-80% usable**, against 57% today.

Saying so now is the point. The previous plan implied the gap was all addressable,
and it is not.

## Also open, not in the ceiling arithmetic

* The hook's duplicated classifier. `HOOK_LIVE_POLICY` proves a rewire changes no
  behaviour (1000/1000), but the deletion is not done.
* Whether the hook should apply the complexity floor — a routing question needing
  a replay, deliberately separated from the consolidation.
* 32 silent fail-open handlers, down from 45. The remainder are trace and display.
* 13 quarantined tests. Eleven still need their assertions diffed against the
  upstream tests that supposedly replaced them.
