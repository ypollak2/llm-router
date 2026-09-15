# Next tasks — planned, gated, ordered

Written 2026-09-15 from a review of the whole session. Every row is something
raised here and not closed, with the gate that will say it is done and the
dependency that decides when it can start.

Baseline for anything that changes routing behaviour:
`scripts/bench_session_replay.py`, 5 real sessions in order — **76% drafts / 66%
acceptable** over 115 prompts, or **80% / 70%** over the 105 routable ones.

Rule: a gate never asks a question. Repair twice, then PARK with the exact
failure recorded, and move on. Every task ends done, repaired or parked.

## Ready now — no dependency

| ID | Task | Gate | Size |
|---|---|---|---|
| N1 | Merge PR #138 | CI green on the re-run, then merged; 17 commits on main | S |
| N2 | End-to-end probe that the MCP path gets session context | A `llm()` call in a session with history receives that session's tool facts — the pointer fix is unit-tested only, never proven end to end | S |
| N3 | Decide the bare-filename grounding gap | `_DRAFT_PATH_RE` needs a `/`, so a draft citing `README.md` alone is never checked. Either a test pins the exclusion as deliberate, or the regex covers it | S |
| N4 | Clear the root cruft | `git status` shows no stray fixtures; `intercept_bench.json` retained (bench_intercept.py reads it) | XS |
| N5 | Delete `policy_versions.py` | Zero production importers, verified. Suite green after removal | XS |
| N6 | `docs/MEASUREMENT.md`: the positive session-id test | The rule says "match `^[0-9a-f]{8}$`", not "exclude unknown" — noted early, never written | XS |

## Needs a measurement first

| ID | Task | Gate | Depends on |
|---|---|---|---|
| N7 | Firm up the acceptance number | 3 runs per arm; report the mean and the spread, or state plainly that the effect is inside the noise | N1 (merge first so the arms are stable) |
| N8 | Unpark A4 — hook vs router classifier | A measurement on the replay corpus deciding which engine is correct FOR THE HOOK, then reconcile `HOOK_POLICY` to it, then delete the duplicate. They agree on 59.7% today and the hook is the more expensive of the two | N7 (needs a trustworthy baseline) |
| N9 | Decide Ollama residency | `KEEP_ALIVE=30m` holds 20.8GB on a 48GB machine and has already killed a background task. Measure the reload tax against the memory, then choose | — |

## The three steps of PLAN_CONTEXT_EVERYWHERE that were never executed

That plan was written on a wrong premise and superseded mid-implementation. These
three survive it and are still worth doing.

| ID | Task | Gate | Size |
|---|---|---|---|
| N10 | Raise tool-capture truncation | `_stringify(tool_input, 200)` / `(tool_result, 500)`: 128 events measured at p50 661, p90 710, max 711 — pinned at the cap. After: fewer events at the ceiling, and a prompt naming a recently-written file resolves | M |
| N11 | Structured facts slot | `{branch, head_sha, last_tool, last_command_head, last_exit_code}` from `git` on every PostToolUse, overwritten not appended, ~20-40 tokens, never writable by a model | M |
| N12 | Relax OKF's anchor gate for session-local tokens | `okf.py:643` requires a >=6-char identifier so "W3" and "Q-L" can never match. Scope to tokens seen this session; NEVER the bulk index — that precision collapse was fixed once already | M |

## Verified gap findings, deliberately deferred — now scheduled

| ID | Task | Gate | Why it waited |
|---|---|---|---|
| N13 | C3 retrofit: 45 fail-open sites | Lower the ratchet in `test_failopen_ratchet.py` below 45 and keep the suite green | Bulk-editing 45 sites blind is how something breaks quietly |
| N14 | P1-10: the semver violation | A public-surface diff gate in `scripts/release/`; version bumped or the policy amended | Still 13.3.1; CHANGELOG self-admits it |
| N15 | P1-4: the quarantined tests | Each of 15 either restored, rewritten, or deleted with a reason. `test_hook_equivalence.py` is unrestorable as written — it calls `classify.score_categories`, since privatised | Its rot is evidence for N8 |
| N16 | P1-5/6: CI gates | mypy, a coverage floor, and `pip-audit` in `.github/workflows/ci.yml`; the excluded marker matrix runs on a schedule | Adding gates to 8,005 tests needs its own cleanup pass |
| N17 | P1-2: the three files over 3,700 lines | `router.py` 4,889, `auto-route.py` 4,653, `cost.py` 3,763. Split with behaviour pinned by the existing suite | Real tax, no runtime effect, high risk |

## Not doing, and why

* **The "one planner" rewrite.** N8 gets the same correctness by rewiring to a
  classifier that already exists.
* **Merging session buckets.** Breaks the isolation `CHZ-AUD-024` pins.
* **Deleting the other five "unwired" subsystems.** Codex called six unwired;
  five have production importers. Only `policy_versions` is a real orphan (N5).
* **The README restructure.** Its false claims are corrected; moving sections
  around is a separate piece of work with no correctness content.

## Order

    N1 -> N2, N3, N4, N5, N6   (independent, small, clear the deck)
    N7 -> N8                   (measurement before the classifier decision)
    N9                         (independent; a settings choice, not code)
    N10, N11, N12              (independent of each other)
    N13 -> N14 -> N15          (hygiene, descending value)
    N16, N17                   (last; each needs its own cleanup pass)

Nothing merges without `./scripts/precommit_gate.sh --full`.
