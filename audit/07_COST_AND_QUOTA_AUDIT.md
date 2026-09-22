# Phase 13/14 — Cost & Quota Accounting Audit

Auditor: Cost/Accounting specialist. Subject: HEAD `357a402e8f`,
branch `fix/audit-2026-09-22`. All code citations are `src/llm_router/<file>:<line>`
at this HEAD. DB numbers are read-only queries against the REAL
`~/.llm-router/usage.db` (marked **OBSERVATIONAL** — not a clean install, not
reproducible, and per FROZEN_STATE.md the store was contaminated once today and
hand-repaired). No writes were made to it. All code-level claims were verified
by reading the executing function, not its docstring.

---

## 1. The formulas, as they actually execute

### 1a. Reported "actual cost"

Per platform table (`claude_usage`/`codex_usage`/`gemini_usage`, written by
`cost.log_claude_usage` / `log_codex_usage` / `log_gemini_usage`,
`cost.py:2275-2564`, the only production call site is `router.py:2325-2367`):

```
actual_cost = input_tok*rate.input + output_tok*rate.output
            + cache_write_tok*rate.cache_write + cache_read_tok*rate.cache_read
```

using **only the winning attempt's** `response.input_tokens/output_tokens`
(`router.py:2333-2336`). Rates come from `pricing.py`, a single well-maintained
table (see §4 — this part is solid).

### 1b. Reported "baseline" (the counterfactual)

```
baseline = pricing.savings_baseline_model()   # = "claude-opus-5", flat, always
baseline_cost = input_tok*opus5.input + output_tok*opus5.output + cache terms
```

`pricing.py:99-119` locks this to one flat model regardless of `task_type`/
`complexity`. This is a **deliberate, documented policy** (WP-05) replacing an
earlier system where 3 different baseline pickers disagreed by up to 5x on the
identical call. The stated justification: "a subscriber runs their top model,
they do not hand-pick a cheaper Claude per prompt."

### 1c. Reported "saved"

```
gross_cost_saved = baseline_cost − actual_cost
cost_saved       = gross_cost_saved − routing_overhead_usd     # cost.py:2261-2262
```

`routing_overhead_usd` is documented as "classifier + Ollama cost for this
call" and is a real column on all three usage tables
(`cost.py:236,257,266-267`).

### 1d. My independently derived formula (what TRUE cost should be, per the brief)

```
TRUE_cost = provider_cost(winning attempt)
          + Σ provider_cost(failed/rejected attempts on this route)
          + judge/verifier calls
          + classifier inference (primary + tiebreak secondary, ensemble.py)
          + embeddings for semantic cache
          + cache writes
          + sidecar/ensemble/draft overhead
```

---

## 2. CONFIRMED — `routing_overhead_usd` is always 0.0 in production

Grep across the whole tree: `routing_overhead_usd` is accepted as a kwarg by
`calc_savings`/`log_claude_usage`/`log_codex_usage`/`log_gemini_usage`
(default `0.0`), subtracted in the formula above, stored in a real column, and
read back by `get_realized_savings` (`cost.py:2564-2664`) as "honest savings:
gross_saved − routing_overhead". **No caller anywhere in `src/` ever passes a
non-zero value.** The one production write site, `router.py:2325-2367`, calls
all three logging functions with no `routing_overhead_usd=` argument, so the
default fires every time.

Observational confirmation, real `claude_usage` table:

```
37,872 rows · SUM(cost_saved_usd) = $366.80 · SUM(routing_overhead_usd) = $0.00
rows with routing_overhead_usd != 0: 0
```

Every dollar of "realized" savings this installation has ever logged is
**gross**, not net. `get_realized_savings()` — the function whose own
docstring says it "surfaces the case where routing cost more money than it
saved" — has never had anything to subtract, across the table's entire
history. The classifier IS separately tracked (`classifier_cost_usd` on
`routing_decisions`, populated correctly by `ensemble.py` → `router.py:3086-3108`)
but it is **priced at $0** because the classifier always runs on Ollama, and
that $0 classifier cost is never folded into `routing_overhead_usd` on the
usage tables anyway — the two live in different tables and are never joined.

Net effect: the dollar-cost side of "classifier + Ollama overhead" (item 5 of
the brief's TRUE-cost checklist) is genuinely $0 (local, free), so omitting it
from `routing_overhead_usd` doesn't misprice dollars — but the column exists,
is documented as doing a subtraction, is read by a function whose whole
purpose is that subtraction, and the subtraction has fired **zero times**.
The number this system would need if the classifier ever ran on a paid model,
or if `LLM_ROUTER_ENSEMBLE_SECONDARY` pointed at something non-free, does not
exist as a live code path.

**Classification: CONFIRMED, DESIGN RISK-in-practice.** Currently dollar-inert
because local inference is genuinely free; would silently under-subtract the
moment any overhead source acquires a real dollar cost.

---

## 3. CONFIRMED — retries/fallbacks are tracked in a second ledger the savings numbers don't read

`router.py` (`_emit_ledger_attempt`, `_actual = _final_cost + failed_attempt_cost`,
lines ~1753-2132) and `execution_ledger.py` (`Accounting`, `_aggregate`,
lines 480-745) **do** carry `failed_attempt_cost_usd` — every rejected/failed
attempt's cost is captured and rolled into a scope-level P&L:

```
net_realized_savings_usd = realized_savings_usd
                          − classifier_cost_usd_total
                          − failed_attempt_cost_usd_total
                          − hook_overhead_usd
```

with realization-gating (Gate 18: a route's saving only counts if verifiably
`verified_used`, not `verified_overridden`/`unknown`) and adoption-gating
(Phase 0). This is a genuinely rigorous accounting layer — more rigorous than
the brief's checklist asks for.

**But it is a parallel, disconnected system.** The user-facing savings
surfaces do NOT read it:

| Surface | Function called | Reads execution_ledger? | Subtracts retries/overhead? |
|---|---|---|---|
| `tools/routing.py::llm_track_usage` ("💰 This call saved: $X") | `cost.log_claude_usage` → `cost.get_savings_summary` | No | No |
| `tools/routing.py::llm_auto` footer ("~$X net saved lifetime") | `cost.get_lifetime_savings_summary` | No | No |
| `hooks/session-end.py` "ROUTING SAVINGS" section | `cost.get_savings_summary` | No | No |
| `team.py` Slack/Discord broadcast | `cost.get_team_savings` | No | No (but does gate on subscription-vs-metered, see §5) |
| `dashboard_data.py` | `execution_ledger.get_route_accounting`/scope accounting | **Yes** | **Yes** |
| `commands/soak.py` | `execution_ledger` report | **Yes** | **Yes** |

So the *only* honest, overhead-and-retry-adjusted number in the codebase
(`net_realized_savings_usd`) reaches the dashboard and the internal soak-test
report — not the number a user sees in chat, at session end, or on Slack.
Those all show `gross_cost_saved` under a label that just says "Saved" or
"saved lifetime."

**Classification: CONFIRMED** (by reading every call site of the five
savings-producing functions; no interpretation required).

---

## 4. CONFIRMED — baseline choice inflates the reported number by the Opus/Sonnet multiple, independent of whether the user's own default is Opus

`SAVINGS_BASELINE_MODEL = "claude-opus-5"` (`pricing.py:99`), $5/$25 per
million. The session this audit itself runs in reports its own model as
`claude-sonnet-5` (`$2-3/$10-15` per million depending on the 2026-08-31 intro
cutoff) — i.e. the actual default many Claude Code users run day to day is
Sonnet, not Opus. Comparing every routed call to Opus rather than to Sonnet:

```
opus5 / sonnet5 (standard) = 5/3 = 1.67x on input, 25/15 = 1.67x on output
opus5 / sonnet5 (intro, before 2026-08-31) = 5/2 = 2.5x / 25/10 = 2.5x
```

So for any user whose actual manual alternative to routing would have been
"just run Sonnet," the reported saving is inflated **1.67-2.5x** on that
portion of traffic, before any other adjustment. This is not a bug — it's a
named, argued design decision (WP-05, `pricing.py:83-97`) chosen specifically
to be the *larger* number ("This yields the larger number, so it carries the
heavier burden: every surface reporting it must label WHAT it is measured
against"). The self-imposed obligation ("every surface must label it") is
**not met**: none of the five user-facing surfaces in §3's table say "vs
Opus" in the headline number; `session-end.py` does label its lifetime
section "(vs Claude host baseline)" and one status line says "gross saved vs
Claude host" (`statusline_hud.py:874`) — but the two most-frequently-seen
numbers (`llm_track_usage`'s per-call "💰 saved $X", `llm_auto`'s "~$X net
saved lifetime") carry no baseline label at all.

**Classification: STRONGLS SUPPORTED / DESIGN RISK.** The choice is defensible
and disclosed once, centrally; the "label every surface" obligation the
module itself imposes is unmet on the two loudest surfaces.

---

## 5. Already-fixed: subscription-vs-metered gating (do not re-flag)

`cost._host_is_metered()` (`cost.py:3486-3502`) and `get_team_savings`
(`cost.py:3725-3842`) correctly split `baseline_equivalent_avoided_usd`
(counterfactual) from `real_dollars_avoided_usd` (0 unless
`LLM_ROUTER_CLAUDE_SUBSCRIPTION` is explicitly turned off). This is the right
answer to "if the host call is already paid for, is the marginal saving
zero?" — and it is implemented, not just discussed. **This surface is
correctly conservative.** The defect is that the same gate is absent from
`get_savings_summary`, `get_lifetime_savings_summary`, and `calc_savings`
itself — i.e. the fix landed on one surface (`get_team_savings`, per its own
comment: "the surface team.py broadcasts to Slack... P0-2") and not on the
others, repeating the exact "one site fixed, eleven left" pattern
`savings.py`'s own docstring warns about (for a different bug, AUD-06's
clamp). `hooks/session-end.py`'s "ROUTING SAVINGS" section and
`tools/routing.py`'s `llm_track_usage`/`llm_auto` all present the
Opus-baseline number as an unqualified dollar figure with no
subscription-vs-metered split.

**Classification: CONFIRMED**, and worth stating precisely: this audit's
ambient environment has `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true` (subscription
mode), under which the *correct* real-dollars-avoided figure for a
subscription user is $0 on Claude-routed traffic — and yet the two loudest
user-facing surfaces will print a positive dollar "saved" figure anyway.

---

## 6. CONFIRMED, on real data — the provenance fail-closed filter collapses "lifetime" totals to near-zero, unannounced at the point of display

`production_only()` (`cost.py:1217-1245`) requires `is_simulated = 0` exactly
(NULL/unknown provenance is excluded — a deliberate, correctly fail-closed
design per its own docstring). `get_savings_summary`, `get_lifetime_savings_summary`,
and `get_team_savings` all apply it.

Observational, real DB, all three ledgers:

| Table | Total rows | `is_simulated IS NULL` | `is_simulated = 0` (passes filter) | $ that survives `production_only()` |
|---|---:|---:|---:|---:|
| `claude_usage` | 37,872 | 37,871 | 1 | $0.008 of $366.80 |
| `savings_stats` | 8,848 | 8,847 | 0 | **$0.00** of $110.81 |
| `usage` | 282 | 262 | 20 | $0.031 of $5.32 |

`get_lifetime_savings_summary(days=0)` — which feeds `llm_auto`'s "📊 N tasks
routed — ~$X net saved lifetime" banner — reads `savings_stats` through this
filter and on this installation returns **`tasks_routed=0`, `net_savings=$0.00`**,
despite 8,848 real historical routing decisions and ~$107 of net historical
saving sitting in the table.

This is not a bug in the filtering logic — it is the correct, disclosed
consequence of a recent migration (the code even anticipates the exact support
ticket: `provenance_exclusion_summary()`'s docstring, `cost.py:325-341`, says
verbatim "the user-visible consequence is a support ticket shaped like 'my
lifetime savings dropped to $0 after upgrading'... the figure is correct...
but a correct number that appears without explanation is indistinguishable
from a bug"). The explanatory function exists but is wired into exactly one
place, `llm-router doctor` (`commands/doctor.py:1473-1475`) — **not** into any
of the five savings-displaying surfaces themselves. A user who sees "0 tasks
routed, $0 saved" from `llm_auto` has no signal, at the point of display, that
this is a provenance cutoff rather than the router doing nothing.

**Classification: CONFIRMED** on real data. Direction: this one **deflates**
rather than flatters — but it means none of the "lifetime"/"all-time" figures
in this repo's own live install are currently readable as literal totals, and
a user citing the $366.80 or $110.81 numbers instead of the filtered ~$0 would
be citing exactly the unmeasured-provenance rows the code says not to trust.

---

## 7. HYPOTHESIS, supported — judge/verifier calls are wired but never populate the reward

`ModelStats.judge_mean`/`judged_samples` (`telemetry.py:91-99`) is meant to
upgrade the bandit's quality signal once `judge.evaluate_response_async` has
graded enough responses (`MIN_JUDGED_FOR_SIGNAL = 20`). Observational, real
DB: `routing_decisions` has 1,599 rows, `judge_score` populated on **0** of
them — exactly matching the code comment's own claim ("Measured 2026-09-22...
0 of 1,598"). Whatever dollar/latency cost the judge call would add is,
empirically, currently $0 because **the judge has never run** on this
installation's history. This means the TRUE-cost checklist item "judge /
verifier calls" is presently a non-issue in practice (nothing to
under-account) but is untested — the accounting path for judge cost, if the
judge starts firing, was not exercised anywhere I could find in `cost.py`'s
overhead columns.

---

## 8. Flattering-assumption table

| Assumption | Flatters? | Evidence | Verdict |
|---|---|---|---|
| Baseline = flat Opus, not the user's actual default model | Yes, 1.67-2.5x on Sonnet-default users | pricing.py:83-119, this session's own model=`claude-sonnet-5` | CONFIRMED, disclosed once, mislabeled downstream |
| `routing_overhead_usd` subtraction | No net $ effect today (classifier is free) but the mechanism is dead | grep: 0 non-default callers; 0/37,872 nonzero rows | CONFIRMED dead code path |
| Retries/failed attempts excluded from user-facing "saved" | Yes — user sees gross, not net of the honest ledger | §3 table | CONFIRMED |
| Subscription flat-rate ⇒ marginal $0 | Correctly handled in 1 of ≥4 surfaces | `_host_is_metered()`, §5 | PARTIALLY FIXED |
| Free local inference treated as "saved" | Consistent with policy (quota, not cash) in `quota_savings.py`'s own framing — but the $ savings surfaces (§3) do not distinguish "routed to a $0 local model" from "routed to a cheaper paid model," both just show a dollar delta vs Opus | quota_savings.py:28-31 vs cost.py `calc_savings` | DESIGN RISK — the "quota not cash" discipline exists in one module and is absent from the dollar-savings modules |
| Stale price tables / model aliases | Actively defended against (family-keyed-by-ID, `lint_pricing.py`, `PRICES_AS_OF` staleness check) | pricing.py | LOW RISK, well engineered |
| Cache hits credited every time served | `get_cache_savings` sums `SUM(cache_read_input_tokens)` per call, i.e. per serve, not per unique cache write — a cache entry served 50 times is credited 50x. This is arguably correct (each serve is a real avoided generation) but is never reconciled against the ONE-TIME cost of the cache write itself, which is priced into `actual_cost` on the write call only if `cache_creation_input_tokens` was passed | cost.py `_claude_cost` cache_write term vs `get_cache_savings` | STRONGLY SUPPORTED — asymmetric: writes cost once (if tracked), reads credited every time (correct), but no reconciliation surface shows write-cost vs cumulative read-credit for the same entry |
| Output-token counts when actual is absent | `calc_savings`'s lumped fallback path (`tokens_k * MODEL_COST_PER_1K`) is used whenever sub-component tokens are all 0 — i.e. whenever a caller didn't wire through structured counts | cost.py:2255-2259 | DESIGN RISK, not measured how often the lumped path fires vs the 4-component path on real traffic |
| Provenance fail-closed on NULL | Deflates, does not flatter | §6 | CONFIRMED, wrong direction from what the brief was looking for, but a real accounting fragility |

---

## 9. Answer: what does "$1 saved" mathematically mean in this system?

**It means: "this call, priced at the actual model's list rate for the tokens
it used, cost $1 less than the identical token count would have cost at
`claude-opus-5`'s list rate — with no adjustment for classifier calls, retry
attempts, judge calls, or whether the host call was already paid for by a flat
subscription."**

It does **not** mean:
- $1 was returned to a bank account, wallet, or invoice (on the default
  subscription assumption, the real marginal cash saved is $0 — this is
  correctly computed in one place, `get_team_savings`, and absent from the
  two loudest surfaces).
- $1 of quota headroom was necessarily preserved 1:1 — `quota_savings.py`
  converts this same dollar figure into "percentage points" via a **flat,
  unvalidated calibration constant** (subscription price ÷ 100), not from an
  observed before/after quota delta. The module's own comment marks the
  observed-calibration path as an unbuilt follow-up (T-QS-2). So "N pp of
  quota saved" inherits every assumption baked into the dollar figure, plus
  an unverified linear conversion on top.
- Net of what routing itself cost to decide (classifier inference, tiebreak
  votes, retries) — that number exists (`execution_ledger.net_realized_savings_usd`)
  but is not what gets shown.
- A verified-used, adopted answer — `execution_ledger`'s realization/adoption
  gating (the most rigorous check in the codebase, Gate 18) is not applied to
  any of the five dollar-savings surfaces in §3; they count every routed call
  as a "save," whether or not the routed answer was the one the host actually
  used.

---

## Phase 14 — Quota accounting

### `quota_savings.py`: percentage points are a linear transform of dollars, not a quota measurement

```
weekly_pp_saved = SUM(usage.saved_usd since Monday 00:00 UTC) / (subscription_$/month / 4.345 / 100)
```

`saved_usd` here comes from the `usage` table's own `saved_usd` column
(written elsewhere, same Opus-baseline arithmetic as §4). The pp figure is
**dollars-saved, divided by a constant** — it is not derived from any
before/after reading of the user's actual Anthropic-reported `weekly_pct`.
The module's own docstring is explicit that this is the "configured" not
"observed" calibration path, and that observed calibration (deriving the $/pp
ratio from the user's own history) "remains a follow-up." **Classification:
CONFIRMED** — the formula is exactly a linear rescale of the dollar claim, so
every flattering assumption in §2-§6 propagates unchanged into the quota
claim, plus the calibration constant itself is unvalidated against reality.

### "Traffic not sent to Claude" vs "quota actually saved" — these ARE kept distinct in one place and conflated in another

`execution_ledger.py`'s `realized_quota_tokens_saved` (lines ~715-733) is the
**correct** version of this metric: it only counts tokens from a route's
FINAL (accepted, verified-used, adoption-gated) attempt, and only when that
final provider is non-Claude — i.e. it answers "how many tokens were actually
served by something other than Claude" (traffic not sent to Claude), not
"how much subscription pressure this is worth." It explicitly does NOT
double-count the old self-subtracting formula it replaced (documented as a
"structural tautology" that always evaluated to 0 — a defect already found
and fixed in this codebase's own history).

`quota_savings.py`'s pp figure is the OTHER claim — it converts a dollar
saving (any dollar saving, including Claude-Opus→Claude-Sonnet routing, which
is still Claude traffic and does hit the subscription's own weekly/session
caps) into "percentage points of quota," without checking whether the traffic
was actually diverted away from Claude at all. **These are the two different
claims the brief warns about, and the codebase computes both — under
different names, in different modules, and nothing cross-checks that the
`quota_savings.py` figure is bounded by `execution_ledger`'s actual non-Claude
token count.** A user who sees "saved 7pp this week" from the routing-notice
hook cannot tell whether that 7pp came from genuinely-diverted-to-Ollama
traffic or from Opus→Sonnet substitution within the same Claude subscription.

**Classification: STRONGLY SUPPORTED.**

### `ANSWER_VALUE_USD = $0.05` (bandit reward)

`expected_value = quality_signal * ANSWER_VALUE_USD − avg_cost`
(`telemetry.py:133-155`). This is a real, meaningful improvement over the
prior ratio formula (documented and correctly reasoned: bounded, has units,
doesn't let "free" dominate by 8 orders of magnitude). But `quality_signal`
currently degrades to `success_rate` — "non-empty, non-refusal" — because
`judge_mean` has 0 samples system-wide (§7). Observational, real
`routing_decisions`:

```
final_model            n     usable_rate   avg_cost   expected_value
openai/gpt-4o          1057  1.00          $0.0100    0.0400
anthropic/claude-opus  330   1.00          $0.0100    0.0400
ollama/lfm2.5:8b       99    1.00          $0.0000    0.0500
ollama/qwen3-coder:30b 94    0.94          $0.0000    0.0468
```

With `usable_rate` clustered at 0.94-1.00 across every model (because
"non-empty" is a low bar essentially every model clears), the `quality *
ANSWER_VALUE_USD` term is nearly constant (0.047-0.050) and `avg_cost` is what
actually orders the candidates — i.e., **until judge grading produces real
data, the bandit is, in practice, still close to a pure cost-minimizer**, the
exact failure mode the reward redesign (T-09) was built to fix, just less
extreme (additive/bounded rather than a 1e8-ratio blowup) because the free
models no longer win by 8 orders of magnitude, only by the cost term (here,
~$0.01 per call, ~20-25% of the $0.05 constant). $0.05 itself is a defensible
order-of-magnitude ("roughly the cost of the premium baseline answering") but
is not calibrated to any actual measured value-of-a-correct-answer, and its
sensitivity cannot be assessed properly until `quality_signal` carries real
variance — today, changing `ANSWER_VALUE_USD` mostly rescales a
near-constant term and has limited effect on ranking; the ranking is
currently governed almost entirely by `avg_cost`.

**Classification: STRONGLY SUPPORTED**, self-disclosed in the code's own
comments (telemetry.py:91-97) and independently corroborated on real data.

---

## Summary table

| # | Finding | Severity | Confidence |
|---|---|---|---|
| 1 | `routing_overhead_usd` subtraction never fires in production (0/37,872 rows) | Medium (currently $-inert, latent) | CONFIRMED |
| 2 | Retries/failed-attempt cost tracked in `execution_ledger` but excluded from all 4 user-facing "saved" surfaces | High | CONFIRMED |
| 3 | Flat Opus baseline inflates vs a Sonnet-default user 1.67-2.5x; "label every surface" obligation unmet on the 2 loudest surfaces | High | CONFIRMED |
| 4 | Subscription-vs-metered $0-marginal gate fixed in 1 surface (`get_team_savings`), absent from `get_savings_summary`/`llm_track_usage`/`llm_auto`/session-end | High | CONFIRMED |
| 5 | Provenance fail-closed filter collapses "lifetime" totals to ~$0 on this real install, unexplained at point of display | Medium (deflates, not flattering) | CONFIRMED on real data |
| 6 | Judge/verifier cost path untested — judge has never populated a row | Low (currently moot) | CONFIRMED on real data |
| 7 | Quota "pp saved" is a linear rescale of the (already-flattered) dollar figure via an unvalidated calibration constant | High | CONFIRMED |
| 8 | "Traffic not sent to Claude" (execution_ledger) vs "quota % saved" (quota_savings.py) are computed by different mechanisms with no cross-check | Medium-High | STRONGLY SUPPORTED |
| 9 | `ANSWER_VALUE_USD=$0.05` reward is sound in form but degenerates to a cost-minimizer today because quality_signal has ~no variance (0 judged samples) | Medium | STRONGLY SUPPORTED |
| 10 | Cache-hit credit is asymmetric (write priced once, reads credited every serve) with no reconciliation surface | Low-Medium | DESIGN RISK |

Pricing table itself (`pricing.py`) is well engineered against the codebase's
own documented history of stale-price bugs and should NOT be re-flagged.
