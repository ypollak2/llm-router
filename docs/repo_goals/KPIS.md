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
| D4 | Tier mix | % of Claude turns and cost on Opus/Sonnet/Haiku (source: `proxy_calls.jsonl`, tracked since PR #246). |
| D5 | Classifier accuracy | Exact tier vs. a truth set; too-weak (under-route) rate <= 10%. |

## Guardrails

| ID | Guardrail | Definition |
|---|---|---|
| G1 | Added latency | Hook p95, proxy decision p95 (proxy path <= +200 ms). |
| G2 | Silent failures per 100 calls | fail-open, truncation/overflow, Ollama hung — the rate must not go up, and every instance must be recorded. |
| G3 | Ledger completeness | >= 99% of turns with full decision fields. |
| G4 | Wrongly benched providers | = 0. |

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
- **Session tagging (research vs. harness vs. organic) does not exist yet.** Every "live"
  measurement above depends on being able to exclude non-organic traffic.
- **The ledger does not yet record policy version or the pre-override proposed tier.**
  D5 and D4 rows in this ledger are therefore partial until it does.
- **O1 has not been reconciled against Claude Code's `total_cost_usd`.** Until it is,
  O1 is an estimate, not a verified figure (see `NORTH_STAR.md`'s point 13 on unverified
  savings).
