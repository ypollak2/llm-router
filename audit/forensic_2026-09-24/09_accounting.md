# Domain 09 — Cost and Savings Accounting — Forensic Audit (2026-09-24)

STATUS: Research complete (read-only). Plan mode blocked writing the assigned
deliverable at `audit/forensic_2026-09-24/09_accounting.md` — this file holds the
full report instead, ready to be copied there verbatim once plan mode is
exited / a Write is approved.

Baseline: worktree `llm-router-forensic` @ 3c96d23. Live ledger measurements
below are from `~/.llm-router/usage.db`, copied read-only to a scratch file
(`/private/tmp/.../scratchpad/usage_snapshot.db`) because `sqlite3 -readonly`
against the real path returned `SQLITE_CANTOPEN` on every attempt after the
first (consistent with contention from the other 11 parallel auditors / live
routing writers on the same machine, or a WAL/-shm mismatch — not diagnosed
further since a snapshot sufficed). No write ever touched the real file;
`cp` is a read of the original. Snapshot taken 2026-09-24, DB `usage.db`
mtime 10:41 — roughly 6 minutes after commit 3c96d23 landed (10:35:52).

CLAUDE.md could not be read: it is untracked (`.gitignore:24`) and the
forensic worktree therefore does not contain it. Proceeded without it.

---

## Overview

Commit `3c96d23` (the HEAD of this baseline) introduced a real, well-evidenced
fix: it split `savings_stats` rows into VERIFIED (only the hook's
`realized`-gated rows, where a routed draft was observed to REPLACE a Claude
turn) and UNVERIFIED (everything else — router/gateway/sdk/codex MCP calls,
agentic delegations, pre-gate rows). Measured independently on the live
ledger, this holds: verified $0.00, unverified $110.91 (n=8,977, exact match
to the commit message and to my own re-derivation of `VERIFIED_SAVED_SQL`).

But that fix has a **scope boundary that is not visible from the commit
message or the CLI's own UI**: it touches exactly ONE of four money tables
(`savings_stats`). The other three (`usage`, `claude_usage`, `codex_usage`/
`gemini_usage`) are summed into the same user-facing "All time saved" figure
by `dashboard_data.query_window()` with **zero** verified/unverified
labelling and, in that specific module, **zero** provenance (`is_simulated`)
filtering either. Measured live:

| Source (lifetime, unfiltered — what `llm-router status` actually sums) | Rows | $ | Provenance known? |
|---|---:|---:|---|
| `usage` table, Opus-rate recompute | 312 | $17.23 | 50 rows `is_simulated=0`, 262 NULL, 0 marked synthetic |
| `claude_usage` table, raw `cost_saved_usd` | 37,878 | $366.90 | 7 rows `is_simulated=0` ($0.105), **37,871 (99.98%) NULL** |
| `savings_stats`, unverified | 7,958 | $110.91 | labelled "unverified" (this commit) |
| `savings_stats`, verified | 1,019 (of 8,977 total) | $0.00 | labelled, correctly |
| `codex_usage` / `gemini_usage` | 0 | $0.00 | tables empty on this machine |
| **Unfiltered lifetime total (`query_window("lifetime")`)** | — | **≈$495.04** | mixed |

Contrast with the codebase's OWN designated "honest" accessor,
`cost.get_realized_savings(period="all", platform="all")` (provenance-filtered,
`is_simulated=0` only, feeds `savings.canonical_savings()`):

| | $ |
|---|---:|
| `claude_usage`, `is_simulated=0` only | $0.105 |
| `codex_usage` / `gemini_usage` | $0.00 |
| **Provenance-filtered "canonical" lifetime gross** | **≈$0.11** |

**The two numbers the same database produces for "lifetime savings," depending
which of the twenty surfaces you read, differ by roughly 4,500×** ($495.04
unfiltered vs $0.11 canonical). Both are live on this exact machine, same
instant, same commit.

The checkpoint (`audit/CHECKPOINT_2026-09-24.md:159-161`) describes the
unaddressed remainder as *"the legacy `usage` table recomputed at Opus
rates."* That is imprecise and worth correcting: the `usage` table itself
contributes only ~$17. The dominant unaddressed contributor — **95% of the
unfiltered total, $366.90 of ~$384** combined `usage`+`claude_usage` — is the
separate `claude_usage` table ("Claude Code usage," per `cost.py`'s own
module docstring), which the checkpoint's prose does not name.

---

## Terminology table (as the code actually uses each term)

| Term | Defined where | What it actually measures | Who reads it |
|---|---|---|---|
| **Verified saving** | `savings.VERIFIED_SAVED_SQL` / `is_verified_saving` (savings.py:76-102) | A `savings_stats` row where host=`claude_code`, model isn't `llm_router-agentic%`, and timestamp ≥ the `realized`-gate cutover (2026-09-13T17:57:16) — i.e. the hook *observed* a routed draft replace a Claude turn. Scoped to ONE table. | `dashboard_data.query_window`/`query_daily` (savings_stats branch only), `savings.unverified_note` callers |
| **Unverified saving** | Same module, `UNVERIFIED_SAVED_SQL` | Everything else in `savings_stats`: MCP/gateway/sdk calls (`log_receipt_savings` credits every call as a replacement, incl. 2-token "ok" pings), agentic delegations (flat $0.20 regardless of outcome), pre-gate rows. | Same surfaces, shown as "+ $X unverified, n=N" |
| **Provenance-filtered ("production") saving** | `cost.production_only()` (cost.py:1272) — `is_simulated = 0`, fail-closed | Rows the write-time `detect_synthetic()` check stamped as real traffic, as opposed to a test/benchmark run. Orthogonal axis to verified/unverified — applies to `usage`/`claude_usage`/`codex_usage`/`gemini_usage`, which have no verified/unverified concept at all. | `cost.get_realized_savings`, `cost.get_savings_by_period`, `cost.get_team_savings`, `cost.get_cache_savings`, `cost.get_routing_savings_vs_sonnet` — **NOT** `dashboard_data.py` (zero references, confirmed by grep) |
| **Baseline-equivalent avoided / gross saved** | `CanonicalSavings.baseline_equivalent_avoided_usd`, `get_realized_savings["gross_saved_usd"]` | `(baseline_model cost) − (actual cost)`, unsigned name but the underlying `net_saved()` is signed. Baseline model hardcoded to `claude-opus-4` (`savings._baseline_model()`) in the canonical path, but Sonnet in `cli_share`/`web_dashboard_tiles`, and a per-model multiplier table in `cli_gain` — three different baselines, by design in one case (`cli_share`) and by drift in the others. | Most of the 20 surfaces |
| **Real dollars avoided** | `CanonicalSavings.real_dollars_avoided_usd` | `0.0` under `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`; otherwise equals the signed net. Only computed by `canonical_savings()`; not by `get_realized_savings` directly (see finding ACC-06). | `cli_savings_report` (via `canonical_savings`), and would be `cli_status_premium`/`hook_session_end*` if they called it — they don't |
| **Quota preserved** | Prose only (README:391-396) | No dedicated field. README correctly distinguishes it from "money saved" for subscription users, but no code path emits a "quota preserved" number — the concept exists in documentation, not in the domain model. | n/a |
| **Premium turns avoided / premium tokens avoided** | Not a named field anywhere in `savings.py`/`cost.py` | Closest proxy is `n_rows` (call count) and `tokens_used`/`input_tokens+output_tokens` sums, which mix routed-and-replaced calls with routed-but-discarded drafts and with pure telemetry pings (2-token "ok"). No field distinguishes "a premium turn was actually avoided" from "a call was logged." | — |
| **Routed turns / draft turns / accepted drafts** | `routing_report.draft_acceptance()` (referenced in CHECKPOINT §4.2, not in savings.py) | Measured once, out of band: 0 of 1,132 audited drafts accepted, 3,122 produced lifetime. Not wired into any of the 20 money surfaces — a draft's `estimated_claude_cost_saved` is written regardless of acceptance except where the `realized` gate applies (savings_stats only, and even there gated on "turn replaced," which the CHECKPOINT's own later correction says is a narrower and different question than "draft accepted"). | Not consumed by cost accounting |
| **Direct replacements** | Implicit in `mode='block'` (schema comment, `MIGRATE_SAVINGS_STATS_ADD_MODE`, cost.py:500-509) | Column is declared, documented at length ("`'block'` means the turn was replaced; `'echo'` means it was not"), and **never written** — measured on the live ledger, `mode` is NULL for all 1,673 `host='claude_code'` rows. Dead column, real docstring. | Nothing reads it because nothing writes it |
| **Free/local-provider usage** | `_FREE_PROVIDERS` (savings_report.py:39), `{ollama, codex, gemini_cli, openai_compat}`; separately, `{ollama, codex, gemini_cli}` in `cost.log_usage` (cost.py:1117) — **two different set literals for the same concept**, one includes `openai_compat`, the other doesn't | Determines both "cost forced to $0" at write time and "free vs paid" at report time from two independently maintained lists | `cost.log_usage` (write), `savings_report.py` (read) |

---

## SURFACES registry (savings.py:333-391) — verified against the live code

20 surfaces, 3 marked `canonical=True` (read `savings.canonical_savings()`):
`mcp_session_dashboard`, `cli_savings_report`, `cli_explain_dashboard`.

**Finding: even the canonical=True claim is partially wrong for one surface.**
`tools/admin.py:963` (`llm_session_dashboard`, the `mcp_session_dashboard`
surface) calls `cost.get_realized_savings()` **directly**, not
`savings.canonical_savings()`. It inherits the provenance filter (good) but
skips `CanonicalSavings.headline()` and the subscription gate entirely — a
subscription user calling this MCP tool sees `"Gross saved $X.XX"` /
`"Realized (net) $X.XX"` with no "$0.00 real dollars avoided (subscription)"
qualifier, because that logic lives only inside `canonical_savings()`, which
this call site bypasses. See ACC-06.

17 of 20 surfaces are `canonical=False`, each with a stated (self-documented,
in-source) divergence reason — this is a rare case of a codebase that already
did the survey work correctly (2026-09-22 audit, per the module comment); the
registry's own text is accurate against what I independently verified for the
surfaces I checked (`cli_status_premium` → `dashboard_data.query_window`,
confirmed zero `production_only`/`is_simulated` references in that module by
grep; `hook_session_end_headline` → raw SQL over `usage`, confirmed at
session-end.py's net-saved formatting function, unclamped, matches AUD-06's
signed-subtraction fix).

---

## Is the $372.58-class figure defensible under the project's own rules?

**No**, by the project's own written standard. Two independent lines of
evidence:

1. **CLAUDE.md's rule** (quoted in the commit and checkpoint but not present
   in this worktree to cite directly — see note above): *"'A local model ran'
   is not a saving."* The `claude_usage` table's dominant contributors
   (`claude/claude-fable-5`: $198.81 across 1,153 rows; `openai/gpt-5.4`:
   $90.00 across 30 rows — together 78.5% of the table's $366.90) are almost
   entirely **rows with `is_simulated IS NULL`** — provenance was never
   established, and the code's own migration doctrine (`cost.py:472-486`,
   `MIGRATE_SIBLING_TABLES_ADD_PROVENANCE`) says explicitly: *"NULL is the
   honest value: UNKNOWN provenance... `production_only()` is fail-closed
   (`= 0`), so unknown rows drop out of money figures rather than being
   counted as real."* `dashboard_data.py` does not apply that filter to this
   table. The project wrote the rule and then didn't wire it into the surface
   most users actually see (`llm-router status`).

2. **A concrete anomaly inside the unfiltered figure**: 96 of the 37,878
   `claude_usage` rows (all from `claude/claude-fable-5` and `openai/gpt-5.4`,
   all on a single calendar day, 2026-09-04) carry the **exact same pair of
   values** — `tokens_used = 200000` (the literal context-window ceiling
   constant for these models, per `token_budget.py:35-38`) and
   `cost_saved_usd = 3.0` flat — contributing **$288.00 of the table's
   $366.90 (78.5%)**. This is consistent with a sentinel/ceiling value being
   logged in place of a real token count on a single day, which is exactly
   the shape of defect this codebase's own docstrings describe repeatedly
   (the "$15/$75 Opus-3-rate bug," the "100/50/100/0.001/0.003 stub
   fingerprint," "28,536 synthetic rows... none separable after the fact
   because they carry real model names"). I cannot prove intent or a specific
   cause from the data alone (mark this UNCERTAIN), but I can prove the
   pattern, that `is_simulated` is NULL for all 96 rows, and that
   `dashboard_data.py` counts every one of them as "saved" with no
   qualifier — worse treatment than the $110.91 this same commit correctly
   labelled "unverified."

**Numerator/denominator/window for every headline figure I measured:**

| Figure | Numerator | Denominator (n) | Window | Baseline | Filter applied |
|---|---:|---:|---|---|---|
| "$110.91 unverified" (commit message, reproduced independently) | `SUM(estimated_claude_cost_saved)` where NOT verified-predicate | 7,958 rows | all `savings_stats` history | mixed (per-row `model_used` pricing) | verified/unverified only; no `is_simulated` filter (table lacks reliable coverage of it for the rows that matter) |
| "$0.00 verified" | same SQL, verified branch | 1,019 rows (of 8,977) | all `savings_stats` history | n/a (all $0 by construction of the gate) | verified predicate |
| `llm-router status` "All time saved" (unfiltered, live 2026-09-24) | `usage` Opus-recompute + `claude_usage.cost_saved_usd` + `savings_stats` split | 312 + 37,878 + 8,977 = 47,167 rows | unbounded (`lifetime` window = no date filter) | Opus for `usage`; whatever `_pricing` computed per-row for `claude_usage` | **none**, for 38,190 of the 47,167 rows (81%) |
| `get_realized_savings("all","all")` canonical gross | `claude_usage.cost_saved_usd` where `is_simulated=0` | 7 rows | unbounded | same per-row pricing | `production_only()`, fail-closed |

No % figure in the CLI/dashboard/status surfaces I inspected states its own
`n`, window, or baseline inline — the README does this correctly in prose
(`"35-80%" and "87%" ... single-user observations ... no stated denominator"`)
but the live numeric surfaces (`status_premium.py`, `session-end.py`) print
a bare `${saved:.2f} saved` with no such caveat attached to that specific
number.

---

## Findings register

```
ID: ACC-01
Category: Cost/savings accounting — provenance
Severity: HIGH
Confidence: HIGH (measured live, reproduced independently of the commit's own numbers)
Location: Files: src/llm_router/dashboard_data.py Symbols: query_window, query_daily Lines: 276-413, 416-536
Observation: query_window()/query_daily() sum the `usage` table (Opus-rate recompute) and the
  three per-platform tables (claude_usage/codex_usage/gemini_usage) into the SAME "saved" total
  shown by `llm-router status` (via ui/status_premium.py render_routing_savings) with NO
  is_simulated/production_only filter and NO verified/unverified label. Only the savings_stats
  branch (added by commit 3c96d23, same file) gets the verified/unverified split.
Evidence: grep -n "is_simulated\|production_only" src/llm_router/dashboard_data.py — zero hits.
  Live measurement (usage_snapshot.db, 2026-09-24): claude_usage sum(cost_saved_usd)=$366.90 over
  37,878 rows, of which only 7 rows ($0.105) are is_simulated=0 (confirmed production); 37,871
  rows (99.98%) are is_simulated NULL (never measured). usage table: $17.23 over 312 rows, 262 NULL.
Why this exists, if discoverable: dashboard_data.py's own docstring says it was written to stop
  4 distinct hand-rolled queries from disagreeing (Jun 2026) — a real problem it solved — but the
  provenance/verified-vs-unverified work (T-05, 2026-09-22 audit; this commit, 2026-09-24) was
  applied surface-by-surface and reached savings_stats without reaching the module all the other
  surfaces route through.
Why this matters: This is the single most user-visible number in the product ("llm-router status").
  It is currently ~4,500x the honest, provenance-filtered figure the codebase itself computes
  elsewhere (get_realized_savings: ~$0.11) for the same tables at the same instant.
User-visible impact: A user runs `llm-router status` and sees an "All time saved" figure built
  92% from rows whose provenance was never established, with no indication that a materially
  different, filtered number exists three files away.
Engineering impact: Any future consumer of query_window() inherits the same defect silently.
Is behavior currently used? YES — this is cli_status_premium and hook_session_end_cumulative,
  both first-tier user surfaces per the SURFACES registry (savings.py:349-356).
Recommended action: SIMPLIFY — apply `production_only()` inside query_window/query_daily for the
  usage/claude_usage/codex_usage/gemini_usage branches, matching the treatment already given to
  savings_stats in this same file. Alternatively, route query_window's non-savings_stats branches
  through the same is_verified_saving/production_only helpers so ONE filter concept governs all
  four tables.
Behavioral compatibility risk: HIGH visible-number change — the headline will drop sharply (as the
  Sep 24 commit's own $483.49→$372.58+$110.91 drop already demonstrated for one table). This is a
  correction, not a regression, per the project's own stated doctrine (cost.py:482-486).
Security risk: none. Performance impact: negligible (same query shape, added WHERE clause).
Estimated complexity removed: none (adds a filter); reduces a class of future drift.
Validation required: re-run the SURFACES-registry style measurement post-fix; add a test in the
  shape of tests/test_a31b_no_raw_savings_sum.py extended to dashboard_data's non-savings_stats
  branches.
Dependencies on other findings: ACC-02 (this fix will surface how much of the historical claude_usage
  ledger the fable-5/gpt-5.4 sentinel rows account for).

ID: ACC-02
Category: Cost/savings accounting — data quality / provenance
Severity: MEDIUM
Confidence: MEDIUM (pattern is proven; cause is UNCERTAIN)
Location: Files: (data, not code) ~/.llm-router/usage.db table claude_usage; writers:
  src/llm_router/session_spend.py:330, src/llm_router/cost.py:2402
Observation: 96 of claude_usage's 37,878 rows (all model in {claude/claude-fable-5, openai/gpt-5.4},
  all timestamped 2026-09-04) share tokens_used=200000 (the context-window ceiling constant for
  these models per token_budget.py:35-38) and cost_saved_usd=3.0 flat. These 96 rows total $288.00
  of the table's $366.90 (78.5%). All 96 have is_simulated NULL.
Evidence: sqlite queries against a snapshot of the live DB (commands and counts in report body above).
Why this exists, if discoverable: UNCERTAIN. Consistent with a single-day fixture/benchmark/context-
  exhaustion-handling run that logged a sentinel token count rather than a measured one; could also
  be a genuine repeated event (e.g. a context-limit hit) if that count is a real ceiling-hit value
  rather than a stub. Not resolved from data alone.
Why this matters: whichever it is, dashboard_data.py counts it as unqualified "saved" money (see
  ACC-01), and is_simulated being NULL means the project's own provenance mechanism cannot rule
  either way — this is exactly the "1,813 rows... not separable after the fact" class of problem
  the codebase's own comments describe as already having happened once.
User-visible impact: up to $288 of the lifetime "saved" headline traces to 96 rows with an identical,
  suspicious value pair on one day.
Engineering impact: none beyond ACC-01's fix, which would exclude these rows if is_simulated is ever
  correctly backfilled to 1; today it does not exclude them because it isn't NULL=1, and unmeasured.
Is behavior currently used? YES — counted live in the current headline (see ACC-01 measurement table).
Recommended action: DEPRECATE the historical rows from headline treatment pending investigation;
  KEEP the underlying question open (UNCERTAIN, do not silently reclassify as DEAD or as legitimate).
Behavioral compatibility risk: LOW (further reduces an already-overstated number).
Security risk: none. Performance impact: none.
Estimated complexity removed: n/a.
Validation required: find the 2026-09-04 process/session that wrote these 96 rows (session_id column
  if present) and determine whether it was a test/benchmark harness run.
Dependencies on other findings: ACC-01.

ID: ACC-03
Category: Cost/savings accounting — scope of a stated fix
Severity: MEDIUM
Confidence: HIGH
Location: Files: src/llm_router/savings.py Lines: 1-32 (module docstring), audit/CHECKPOINT_2026-09-24.md:159-161
Observation: The commit message and checkpoint both describe the $372.58 remainder as coming from
  "the legacy `usage` table recomputed at Opus rates." Measured live, the `usage` table itself
  contributes only ~$17.23; the dominant, unnamed contributor is the separate `claude_usage` table
  ($366.90), which cost.py's own docstring calls "Claude Code usage" (distinct from `usage`,
  "External LLM usage"). The commit's own SURFACES registry (savings.py:349-351) correctly names
  `dashboard_data.query_window` as the reader with "zero provenance-filter references anywhere in
  the module" — that part is accurate; only the checkpoint's one-line gloss mis-names the table.
Why this matters: whoever picks up "the next honest-number problem" (checkpoint's own words) will
  go looking at the `usage` table and find a $17 problem, not the $367 one.
User-visible impact: none directly; this is a documentation-precision gap that could misdirect the
  next engineer.
Engineering impact: wasted investigation time if not corrected before ACC-01 is picked up.
Is behavior currently used? N/A (documentation artifact).
Recommended action: KEEP the checkpoint as historical record; correct forward guidance (e.g. a new
  checkpoint entry or this audit) to name claude_usage explicitly, per ACC-01.
Behavioral compatibility risk: none. Security risk: none. Performance impact: none.
Validation required: none beyond this cross-check.
Dependencies on other findings: ACC-01.

ID: ACC-04
Category: Cost/savings accounting — dead/never-written column
Severity: LOW
Confidence: HIGH
Location: Files: src/llm_router/cost.py Symbols: MIGRATE_SAVINGS_STATS_ADD_MODE Lines: 500-509
Observation: The `mode` column on `savings_stats` is documented at length ("'block' means the turn
  was replaced; 'echo' means it was not, and such a row carries estimated_claude_cost_saved = 0")
  but is NULL for all 1,673 `host='claude_code'` rows in the live ledger — no writer sets it.
Evidence: `select mode, count(*) from savings_stats where host='claude_code' group by mode` → one
  row, mode=NULL, count=1673 (live snapshot, 2026-09-24).
Why this matters: this is the exact "direct replacement" concept the domain brief asks to
  distinguish from "draft turn," fully modelled in schema and prose, wired to nothing (same CLASS-A
  shape cost.py itself names elsewhere: "built, documented, correct, and wired to nothing").
User-visible impact: none currently (nothing reads it either, per the same grep).
Engineering impact: a future engineer may build a "direct replacement rate" report against a column
  that has never once been populated and get a silent zero, not an error.
Is behavior currently used? NO.
Recommended action: DELETE the column and its migration, or wire it — do not leave it silently
  unpopulated. Given the `realized` gate (savings.py) already answers the same question at read
  time for the verified/unverified split, this column may be fully redundant with that logic;
  worth checking before deleting (MERGE candidate, not necessarily DELETE).
Behavioral compatibility risk: LOW (nothing reads it). Security risk: none. Performance impact: none.
Estimated complexity removed: one migration, one column, ~10 lines of docstring.
Validation required: confirm zero readers repo-wide (grep "\.mode" scoped to savings_stats query
  sites) before deleting.
Dependencies on other findings: none.

ID: ACC-05
Category: Cost/savings accounting — duplicate constant / dictionary soup
Severity: LOW
Confidence: HIGH
Location: Files: src/llm_router/cost.py:1117, src/llm_router/commands/savings_report.py:39
Observation: Two independently-maintained set literals define "free provider": cost.log_usage uses
  {"ollama", "codex", "gemini_cli"}; savings_report.py's _FREE_PROVIDERS uses {"ollama", "codex",
  "gemini_cli", "openai_compat"}. A call routed to openai_compat is forced to cost_usd=0.0 at write
  time either way (log_usage's set decides that), but savings_report.py's later free/paid split
  would classify it differently from a hypothetical third reader using the narrower set.
Why this matters: exactly the "N booleans/sets → one canonical list" smell the brief asks about
  (§25). Two lists drift the way the classifier signals (`classify.py` vs `hooks/auto-route.py`)
  are independently documented (CHECKPOINT §4.1) to have already drifted.
User-visible impact: none observed yet (no openai_compat rows found in the live ledger to test
  against), so this is a latent inconsistency, not a proven live discrepancy.
Engineering impact: silent classification drift the next time a provider is added to one list
  and not the other.
Is behavior currently used? YES, both sites are live.
Recommended action: MERGE into one module-level constant (e.g. cost.FREE_PROVIDERS) imported by both.
Behavioral compatibility risk: LOW. Security risk: none. Performance impact: none.
Validation required: grep for any other provider-set literal before merging, to make the constant
  genuinely singular rather than a third copy.
Dependencies on other findings: none.

ID: ACC-06
Category: Cost/savings accounting — canonical-surface registry accuracy
Severity: MEDIUM
Confidence: HIGH
Location: Files: src/llm_router/tools/admin.py Symbols: llm_session_dashboard Lines: ~947-985;
  src/llm_router/savings.py Lines: 334 (SURFACES entry "mcp_session_dashboard", canonical=True)
Observation: The SURFACES registry marks this surface canonical=True ("reads canonical_savings").
  The actual call site invokes `cost.get_realized_savings()` directly, not `savings.canonical_savings()`.
  It inherits the is_simulated provenance filter (real benefit) but never constructs a
  `CanonicalSavings` object, so it never runs `_under_subscription()` / `CanonicalSavings.headline()`.
  A user on `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true` calling this MCP tool sees "Gross saved $X.XX" /
  "Realized (net) $X.XX" with no subscription caveat — exactly the framing CanonicalSavings.headline()
  exists to prevent ("$47.20 saved" to a subscriber is not a rounding error, it is a different claim").
Why this matters: this is one of only 3 surfaces the registry claims are fully fixed; it is not
  fully fixed, only partially (provenance yes, subscription-honesty no).
User-visible impact: subscription users (the majority of Claude Code users, by the product's own
  framing) see an unqualified dollar figure from this specific MCP tool.
Engineering impact: the registry's canonical=True is a weaker guarantee than its own docstring implies.
Is behavior currently used? YES — this is a live MCP tool (llm_session_dashboard).
Recommended action: SIMPLIFY — route llm_session_dashboard through savings.canonical_savings()
  (period="today") the same way cli_savings_report does, or extend CanonicalSavings with a
  by-platform breakdown so the subscription gate applies uniformly.
Behavioral compatibility risk: LOW (output format changes, meaning improves).
Security risk: none. Performance impact: none.
Validation required: check remaining two canonical=True surfaces (cli_savings_report,
  cli_explain_dashboard) actually call canonical_savings() (savings_report.py's docstring at
  cli_savings_report:19-20 confirms it does; cli_explain_dashboard not independently re-verified
  in this pass — UNCERTAIN, flag for a follow-up read of commands/explain_dashboard.py:225).
Dependencies on other findings: none.

ID: ACC-07
Category: Cost/savings accounting — default direction / subscription detection
Severity: LOW
Confidence: MEDIUM (default direction confirmed; population affected NOT measured)
Location: Files: src/llm_router/savings.py:295-300 (_under_subscription), src/llm_router/onboard.py:174-185
Observation: `_under_subscription()` is entirely env-var driven (`LLM_ROUTER_CLAUDE_SUBSCRIPTION`),
  set to "true" only if the user completes onboarding and answers a Pro/Max question, or runs
  `llm-router setup`/`doctor` and opts in. A user who installs manually (no onboarding), or whose
  .env is not loaded in a given process, defaults to `_under_subscription()=False` — i.e. defaults
  to treating the user as pay-per-token and showing baseline-equivalent-avoided as "real dollars,"
  which is the unsafe default direction if most users are in fact on a flat subscription (the
  product's own README frames itself as being for "individual developers... local cost savings").
Why this matters: the default fails toward the more impressive-looking (but for a subscriber, false)
  claim rather than the more conservative one.
User-visible impact: UNMEASURED — I did not determine what fraction of installs skip onboarding or
  run in an environment where the .env is not loaded (e.g. some hook invocations, some MCP host
  configs). Marking UNCERTAIN on population impact deliberately.
Engineering impact: a silent, wrong default rather than a loud one.
Is behavior currently used? YES (default path for any process not carrying the env var).
Recommended action: KEEP the mechanism, but consider flipping the fail-direction (default true,
  i.e. assume subscription unless API keys/usage patterns indicate metered billing) or add a loud
  first-run prompt so absence of the var is a decision, not a default. Needs product input, not a
  pure engineering call — flag for the do-not-change-without-owner-input register rather than a
  unilateral flip.
Behavioral compatibility risk: MEDIUM if flipped (changes headline numbers for many users).
Security risk: none. Performance impact: none.
Validation required: telemetry on how often LLM_ROUTER_CLAUDE_SUBSCRIPTION is set among real installs
  (not available to this audit — no cross-machine telemetry access).
Dependencies on other findings: none.

ID: ACC-08
Category: Cost/savings accounting — % claim provenance in code (not docs)
Severity: LOW
Confidence: HIGH
Location: Files: src/llm_router/ui/status_premium.py:108-174, src/llm_router/hooks/session-end.py (net-saved formatter)
Observation: Unlike the README (which explicitly flags "35-80%"/"87%" as anecdotal, no denominator,
  README.md:391-396 — a genuinely honest disclosure), the live numeric surfaces print a bare
  "${saved:.2f} saved · N routed calls" (status_premium.py:136) or "$X.XXXX (baseline − paid)"
  (session-end.py) with no equivalent caveat attached to that specific figure, and (per ACC-01) no
  provenance filter behind it either for 3 of the 4 summed tables.
Why this matters: the project already knows how to write an honest caveat (it did, in the README);
  the runtime surfaces that print the actual number a user acts on don't carry the same discipline.
User-visible impact: a user reading `llm-router status` gets a more confident-looking number than
  the same project's own README would tell them to trust.
Engineering impact: none beyond the fix already scoped in ACC-01.
Is behavior currently used? YES.
Recommended action: once ACC-01 lands, no separate caveat text is strictly required (a smaller,
  filtered number needs less disclaiming) — but consider printing the n_rows and window inline for
  the headline "saved" line the way `unverified_note()` already does for the unverified figure.
Behavioral compatibility risk: LOW. Security risk: none. Performance impact: none.
Validation required: none beyond ACC-01.
Dependencies on other findings: ACC-01.
```

---

## Top items for synthesis (global Top-10 candidates)

1. **ACC-01** (HIGH) — `llm-router status`'s headline "saved" figure sums two
   completely unfiltered tables (`usage`, `claude_usage`) alongside the one
   table (`savings_stats`) this repo's HEAD commit just spent real effort
   making honest. Best single "the headline number is not what it claims to
   be" finding in this domain — strong Top-10 candidate for both correctness
   risks and doc/mental-model problems.
2. **The 4,500× gap** between the unfiltered lifetime figure (~$495) and the
   codebase's own provenance-filtered accessor for the same tables (~$0.11),
   measured live, same instant, same commit. Good candidate for the "does it
   answer why a route happened / did it save anything under the stated
   definition" observability question (§33) and for a Top-10 "correctness
   risk."
3. **ACC-02** — 96 rows with an identical suspicious value pair
   (tokens_used=200000, cost_saved_usd=3.0) accounting for 78.5% of the
   `claude_usage` table's total, all unmeasured provenance. Strong candidate
   for the deletion/quarantine ledger (LEGACY or UNCERTAIN class — do not
   silently classify as DEAD).
4. **ACC-06** — a registry that says "canonical=True" for a surface that
   only partially matches; useful evidence for the brief's "one source of
   truth" audit (§73) — the registry itself is not fully authoritative about
   its own claims and should be test-enforced, not just documented (the
   module's own text says a test enforces the registry's *membership*; no
   test appears to enforce that a canonical=True entry actually calls
   `canonical_savings()`).
5. **Terminology table** above — recommend for the global naming/terminology
   section (§40): "verified/unverified" and "provenance-filtered/synthetic"
   are two orthogonal axes that this codebase has NOT unified, and only one
   of the four money tables has either axis fully wired.
6. The `mode` column (ACC-04) as a small, clean example for the deletion
   ledger: fully documented, fully dead, zero readers, zero writers.
7. **cost.py itself** (4,297 lines, one file) is worth flagging for the
   module/package-structure section (§11) independent of my domain's
   findings — it mixes schema DDL, 27 migrations, 20+ `get_*` accessors,
   provenance logic, and team-identity resolution in one file. I did not
   fully audit it end-to-end (out of scope for §09), but its size alone is
   a Top-25-largest-files candidate that whoever owns §5/§11 should pick up.

## Do-not-change register candidates

- `savings.net_saved()` and the AUD-06 signed-subtraction discipline —
  correctly implemented, well-tested per its own docstring reference
  (`tests/economics/test_savings_sign.py`), should not be touched.
- `cost.production_only()` / `is_simulated` provenance mechanism — correct
  design (fail-closed, NULL≠production); the defect is under-adoption
  (ACC-01), not the mechanism itself. Do not redesign it; wire more callers
  through it.
- The `VERIFIED_SAVED_SQL`/`is_verified_saving` parity pair (savings.py) —
  explicitly pinned by a parity test per its own docstring; do not edit one
  without the other.

## Deletion ledger candidates

| Candidate | Type | Evidence | Depends on it | Removal risk | Confidence |
|---|---|---|---|---|---|
| `savings_stats.mode` column + `MIGRATE_SAVINGS_STATS_ADD_MODE` | Dead column | ACC-04: 0 non-NULL rows, 0 readers found | none found | LOW | HIGH |
| `savings_report.py`'s private `_FREE_PROVIDERS` set | Duplicate constant | ACC-05 | savings_report.py's free/paid split | LOW (behavior-preserving if merged correctly) | HIGH |

## Consolidation ledger candidates

| Concepts | Current impls | Canonical | Deleted concepts | Risk |
|---|---|---|---|---|
| "Free provider" set | `cost.py:1117` inline set, `savings_report.py:39 _FREE_PROVIDERS` | One module constant (e.g. `cost.FREE_PROVIDERS`) | the second literal | LOW |
| Provenance filtering vs verified/unverified filtering | `cost.production_only()` (usage/claude_usage/codex_usage/gemini_usage only), `savings.VERIFIED_SAVED_SQL` (savings_stats only) | Neither is redundant (they answer different questions: "was this row real traffic" vs "was this row's saving observed to replace a Claude turn") — but `dashboard_data.query_window` should apply BOTH concepts to the tables that have them, not neither. Not a merge; a coverage gap (ACC-01). | none | see ACC-01 |
