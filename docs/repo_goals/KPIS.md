# llm-router KPIs

> Adopted by the owner on 2026-10-03. See [NORTH_STAR.md](NORTH_STAR.md) for the
> one-sentence goal these numbers serve, and [`../KPI-LEDGER.md`](../KPI-LEDGER.md)
> for the per-change record of how each PR moved them.

Source: a private research file named `KPI-SPEC.md`, kept outside this repository. This
page is this repo's public carry-over of that spec's definitions and figures; the private
file itself is not reproduced or linked here beyond its name.

## North Star and Outcomes

**NS — Non-Claude-and-used share.** (prompts + LLM calls served by a non-Claude model AND
used as-is) / (all prompts + LLM calls), organic sessions only. Target >80%. Today: **0%**
(n=100 real, source: `results_p1_rerun.md`).

**O1 — Quota avoided (PRIMARY, per the 2026-09-28 amendment).** Claude quota-weighted cost
avoided vs. the requested model. Shown only as "est." until reconciled with Claude Code's
own `total_cost_usd`. No reconciled figure exists yet — see Gaps, below.

**O2 — Quality held.** Acceptable rate of routed work vs. Claude, measured on three frozen
benchmarks: 20 real tasks with hidden tests (`plan_implement`, n=20), graded Q&A
(`complexity-v2/p1`), and a 180-prompt tier set (`complexity-v2/p_eval`, n=180). Today:
**Claude 15/20, local 3/20** (`plan_implement`, n=20).

## Drivers

| ID | Driver | Definition |
|---|---|---|
| D1 | Offered off Claude | % of organic prompts the router sends to local/Codex. |
| D2 | Success when tried | % of local/Codex attempts that pass verification AND are used. |
| D3 | Redo rate | % of routed outputs Claude redoes within 3 turns. |
| D4 | Tier mix | % of Claude turns **and** % of Claude quota cost on Opus/Sonnet/Haiku, printed side by side (source: `proxy_calls.jsonl`, tracked since PR #246). Cost is calls weighted by per-call cost, Haiku 1 : Sonnet 3.66 : Opus 6.15 (Opus is 1.68x Sonnet and 6.15x Haiku; `proxy/claude_tiers.yaml`, probe 2026-09-29, n=5 calls per model, pinned by a test). A tier with no weight (Fable) is left out of the weighted share and counted, never given a guessed weight. Organic sessions only, by the kind each proxy row was written with (rows from before tagging stay out): research and harness are excluded (`llm-router kpi --include research` adds research, never harness). |
| D5 | Classifier accuracy | Exact tier vs. a truth set; too-weak (under-route) rate <= 10%. |

## Guardrails

| ID | Guardrail | Definition |
|---|---|---|
| G1 | Added latency | Hook p95, proxy decision p95 (proxy path <= +200 ms). |
| G2 | Silent failures per 100 calls | fail-open, truncation/overflow, Ollama hung — the rate must not go up, and every instance must be recorded. |
| G3 | Ledger completeness | >= 99% of proxy rows with every decision field that can apply to them recorded (definition below). |
| G4 | Wrongly benched providers | = 0. |

### G3 in detail

`llm-router kpi` counts completeness where a field can apply, from the schema's start, and reports what it
left out. It replaces "every row, every field non-null, organic rows only", which read **0.4% (n=2,214)** on
2026-10-04 while the data was near-complete: `tier_retry` is null on every call that was not retried (null is its
correct value), `tier_proposed` is null on side calls and first-call floors (no classifier ran), and filtering
rows by `session_kind` first removed exactly the rows that lack it.

A row is **complete** when every field that can apply to its type is recorded:

| Field | Owed on | Recorded means |
|---|---|---|
| `session_kind` | every row whose request names a session | non-null |
| `tier_policy_version` | every row when tiers are on, a row the proxy served itself included (null by design under `--tiers off`) | non-null |
| `tier_proposed` | rows where the classifier ran: not side calls, pinned/unknown models, first-call floors, locally served rows or tiers-off rows. A tier decision that raised, or is missing on a forwarded row with tiers on, is **owed** (a defect) | non-null |
| `tier_retry` | every row | key present (null = "no retry happened") |

- **Schema start.** The first row that carries a field's key defines when that field's schema began; a row counts
  once every field exists, so the start is the latest of the four. It is printed (`since YYYY-MM-DD`) and
  overridable: `--schema-since <date|ISO time|epoch>`.
- **Reported, not hidden.** The overall rate (complete rows / counted rows), each field's coverage over the rows
  it is owed on, how many rows it is not owed on, the rows before the schema start that were excluded, and rows with
  no usable timestamp, and how many sessions the counted rows come from (`--json`: `fields`, `rows_by_type`, `rows_before_schema`, `undated_rows`, `counted_sessions`, `largest_session_share`).
- **Not measurable, not a rate.** No row in the window, every row before the schema start, or no row with an
  applicable field prints `not measurable: <reason>`, never 0% or 100%. Below 50 counted rows it prints
  `too few to tell`.
- **Not session-kind filtered**, unlike NS, D1-D4 and G1: completeness is the writer's property and the tag is one
  of the fields under test. A row written while its session had no tag counts against `session_kind`.

### Session kind on NS, D1 and D2

North-star units carry `session_kind` (`llm_router.northstar.units()`), resolved in this order: the session's tag
file; the kind stamped on the unit's own ledger row when it was written (`north_star_units.jsonl`); the kind stamped
on the session's proxy rows when they all agree. A unit with no resolvable tag stays untagged and is never counted
as organic; a session whose proxy rows disagree with each other and which has no tag file stays untagged too. The
scorecard prints how many units joined a tag (by source) and how many stayed untagged, and how many sessions the
counted population comes from. A tag file is write-once: the first tag wins, so a session resumed from another
directory does not change kind. The prompt hook tags a session at its next prompt when SessionStart never did.

### `llm-router kpi --health`

One line per KPI: `measured`, `blind` or `stale`, the one reason, and the n. **Blind** = no number (nothing to
count, below 50, or not instrumented) or a number whose data carries no timestamp. **Stale** = a number whose newest
data point is older than 48 h (live ledgers; `--stale-hours` to change) or 30 d (the frozen benchmark behind O2 and
D5). G4 is a snapshot of the moment and G2 an all-time count, so their "measured" says so in its reason. Exit code 0 even when KPIs are blind; `--strict` exits 1 if any is blind (a stale KPI does not trip it). G1's
hook-side latency is not instrumented anywhere, so it is permanently blind and `--strict` will not pass until it is.

## Change rule

Every PR names its primary KPI, expected direction and size, and the guardrails it
touches. Each change is measured both ways:

- **Offline** — frozen sets, CI, with an n.
- **Live** — 7 days before vs. after, organic sessions only, research/harness traffic
  excluded.

Verdict, recorded per change:

- **helped** — the primary KPI moved beyond CI noise, and no guardrail broke.
- **neutral** — no measurable move either way.
- **hurt** — a guardrail broke, or quality dropped more than 5 points. This blocks
  release.
- **not measurable yet** — the change shipped before the measurement it needs exists
  (see Gaps, below).

One row per change goes in [`../KPI-LEDGER.md`](../KPI-LEDGER.md).

## Known gaps (blocking honest numbers today)

- **The "used as-is" signal does not exist yet.** NS and O1 cannot be computed as a rate
  without it.
- **Session tagging exists (PR #250) but covers only sessions that started or prompted after it
  deployed.** Every "live" measurement depends on excluding non-organic traffic, and on this machine
  2026-10-04 one session supplies all of the tagged units and rows. NS, D1 and D2 are readable only for
  tagged sessions; the rest stay untagged, never organic. A tag file can only be created, not backfilled.
- **The proxy ledger records policy version and the pre-override proposed tier since 2026-10-03.** D5 and D4
  rows written before that carry neither (G3 excludes them as before the schema start).
- **O1 has not been reconciled against Claude Code's `total_cost_usd`.** Until it is,
  O1 is an estimate, not a verified figure (see `NORTH_STAR.md`'s point 13 on unverified
  savings).
