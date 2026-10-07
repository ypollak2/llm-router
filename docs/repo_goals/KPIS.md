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

**O3 — Offload share.** (human turns served by a local model OR by Claude Haiku, and NOT redone) /
(all human turns), organic sessions only, 7-day window. Target >=60%. **The headline unit is the
human turn**: ~88% of proxy calls are tool-result continuations (about 8.5 calls per turn), so a
per-call denominator is inflated by agent loops. A turn is offloaded when its FIRST call (the
one whose `step_class` is not `continuation`) is served by Haiku or local and that call is not
redone; the per-call share is printed as a secondary line (`per call: ...`). Printed as one extra line in
`llm-router kpi` and `llm-router kpi --health` (outside NS, D1-D5 and the other KPIs: a test
pins their output byte for byte, `tests/test_kpi_offload_share.py`). It is not part of the
`--health` measured/blind/stale counts. Today: no figure is recorded here; run
`llm-router kpi --since-policy <version>`.

Code: `src/llm_router/offload_share.py`. The line also shows Haiku share, local share, and the
redo rate per class (Haiku, local) with n. Below n=50 (units, or units in a class) it prints
`too few to tell (n=N)`, never a percentage; with no unit it prints `not measurable`.

*Units.* (a) Proxy-served Claude calls: `proxy_calls.jsonl` rows with `decision` forwarded or
fallback and no 4xx/5xx upstream status. Class **Haiku** when `served_model` (else
`requested_model`) names Haiku, else Claude. Claude Code's own side calls (`tier_reason ==
side_call`: titles, summaries) are excluded and counted: the router did not choose them and
nobody redoes them. (b) Proxy rows with `decision == served` (a local backend answered): class
**local**. (c) `northstar.local_shadow_units()` (PR #284, `usage.db`
`provenance='runtime'`, `final_provider='ollama'`): class **local**. A local unit with no
`session_id` cannot be scoped to organic sessions: it is excluded and counted, the local share
prints as `unknown`, and O3 is a lower bound. Today every such row in `usage.db` has no
session id.

*Redone.* A unit is redone when any of:

1. **Escalation.** A later proxy row of the same conversation (session), in the unit's own
   human turn or the next 2 human turns, has `tier_reason` `escalation` or
   `escalation_under_pressure`: the proxy's own detector (`proxy/escalation.py`): a `claude:`,
   `native:` or `opus:` re-ask, a contradiction, or failed tools. A human turn starts at a
   non-side-call row whose `step_class` is not `continuation`.
2. **Receipt band.** `user_signals.jsonl` has a `redone` press (last press per key wins) for
   the unit's `msg_id`.
3. **usage_outcome.** A `redone` verdict (`usage_outcome.py`) for a routed event in the same
   session, within 120 s of the unit, each verdict used once. Local units only (a verdict
   exists only for routed MCP events).

4. **Transcript detector (source 4, OFF).** `src/llm_router/redo_signal.py` reads the session's Claude Code
   transcript and flags a human prompt that re-asks (`claude:` / `native:` / `opus:`), corrects, complains
   ("that didn't work", "try again" ...) or repeats the previous prompt. A unit is redone by it when a
   flagged prompt falls in the next 2 human turns. It is disabled (`offload_share.REDO_SOURCE4_ENABLED =
   False`): it changes no unit, and `o3.breakdown.redo_detector_n` still reports how many flagged prompts it saw.
   It may be enabled only by a PR that cites a validation run on blind labels with precision >= 0.80 and
   >= 15 true positives. The one run so far did not meet that: pattern version v2, test half n=150 labelled
   pairs (window 2026-08-30 to 2026-10-06, 12 labelled redo), precision 2/5 and recall 2/12
   (`~/.rsi/research/primary-plan/redo/test_result.json`, not in this repo). Until then redo-based bars are
   "not informative".

   **Population of that run (not O3's).** PLAN M0.9 asks for organic sessions plus b9f04425. The sampler applied no
   session-kind filter. Of 524 pairs in the extended window (2026-08-30 to 2026-10-06), 106 come from the PLAN's
   population (76 of them in W0) and 418 from sessions with no tag file and no ledger kind stamp (untagged: kind
   unknown, never counted as organic here). Selected: 236 of 300 pairs (19 of 22 sessions) are untagged; test half:
   115 of 150. The tag-less sessions pre-date tagging; a sidecar derived from their transcript cwd/entrypoint
   (`session_kind_backfill.jsonl`, not a live tag) calls all 418 organic. 106 < 300, so the PLAN's own population
   cannot supply the sample even after the one allowed extension; PLAN 3.4 makes the substitute population the
   owner's decision and that decision is open. Precision and recall above are therefore measured mostly on
   untagged sessions, and any PR that flips `REDO_SOURCE4_ENABLED` must quote this split with them. Split recorded in
   `sample_meta.json` and `test_result.json` (`population_split`).

A unit that shows no redo but has fewer than 2 human turns after it (a recent turn, or the end of
a session) is counted as not redone and also counted as `window_open`; the count is in the
headline value (`n=..., K window-open`) because it can still become a redo.

*Session-kind override.* `~/.llm-router/session_kind_overrides.json`, `{session_id: {"kind": ..., "reason": ...}}`,
names a session whose tag or whose rows' own stamps are wrong and cannot be rewritten (the ledgers are
append-only). Precedence for every reader: override, then the tag file, then the row's own stamp. NS, D1, D2,
D3, O3 and D4/G1-proxy all obey it, and so do the proxy's and the edit ledger's stamps for rows written after
it. G3 does not change: it measures whether the writer recorded a `session_kind`, not which kind. The first
entry is session b9f04425, a research session (p_eval REPORT.txt) that held 99.3% of the organic turn-first
rows in the pinned window W0. The JSON `joins` still counts it, so `llm-router kpi --include research` shows it.

**The redo signal is sparse.** On live data (2026-10-06) the proxy ledger holds 1 escalation
row in ~26k, so a low redo rate says little. The line prints `redo signal sparse:
n_escalations=N` (when N < 50) and the redo rate must not be read as proven low.

*Since a policy version.* `--since-policy VERSION` adds `since policy VERSION` (units from the
first proxy row stamped with that `tier_policy_version`) and `before it, same window`. If the
version is not in the ledger the line says so; it does not print a number. The Haiku redo guard
(`~/.rsi/research/local-usage/haiku_guard/`, outside the repo) uses this same redo definition and
the same headline unit: its n_haiku is Haiku-served human turns, not calls.

*Absolute windows.* `--since WHEN --until WHEN` (a date, an ISO time or epoch seconds; both
required) replace `--days`, and "now" becomes `--until`. Use them for every historical check: a
relative window empties as the ledgers go quiet. Rows outside `[since, until]` never count, in any
KPI. O1's `usage.db` estimate only exists relative to now, so under a window O1 prints the
reconciled figure or "not measurable". The JSON gains a `window` key (absent without the flags).

## Drivers

| ID | Driver | Definition |
|---|---|---|
| D1 | Offered off Claude | % of organic prompts the router sends to local/Codex. |
| D2 | Success when tried | % of local/Codex attempts that pass verification AND are used. |
| D3 | Redo rate | % of routed outputs Claude redoes within 3 turns, plus the person's own `r` redo presses on the receipt band (see below). |
| D4 | Tier mix | % of Claude turns **and** % of Claude quota cost on Opus/Sonnet/Haiku, printed side by side (source: `proxy_calls.jsonl`, tracked since PR #246). Cost is calls weighted by per-call cost, Haiku 1 : Sonnet 3.66 : Opus 6.15 (Opus is 1.68x Sonnet and 6.15x Haiku; `proxy/claude_tiers.yaml`, probe 2026-09-29, n=5 calls per model, pinned by a test). A tier with no weight (Fable) is left out of the weighted share and counted, never given a guessed weight. Organic sessions only, by the kind each proxy row was written with (rows from before tagging stay out): research and harness are excluded (`llm-router kpi --include research` adds research, never harness). |
| D5 | Classifier accuracy | Exact tier vs. a truth set; too-weak (under-route) rate <= 10%. |

## Guardrails

| ID | Guardrail | Definition |
|---|---|---|
| G1 | Added latency | Hook wall time, p50 / p95 per hook against that hook's budget; proxy decision p95 (proxy path <= +200 ms). |
| G2 | Silent failures per 100 calls | Fail-open events per 100 calls over the window, overall, per code and the top five codes — the rate must not go up, and every instance must be recorded. Truncation/overflow and Ollama-hung are not wired into this counter yet. |
| G3 | Ledger completeness | >= 99% of proxy rows with every decision field that can apply to them recorded (definition below). |
| G4 | Wrongly benched providers | Wrong benches per 100 benches, with n; target = 0. A bench is wrong when the owner clears it with `llm-router provider unban` before it lapses, or a call to that provider succeeds before its reset time. |

### How G1, G2 and G4 are measured

`llm-router kpi` prints all three. Each line carries its n and window, and any case the
data cannot support reads `not measurable: <reason>` or `too few to tell (n=N)` (N < 50),
never a number standing in for "nothing happened".

**G1 — hook latency.** Every instrumented hook appends one line per invocation to
`hook_latency.jsonl` in the state directory: hook, event, `elapsed_ms`, `timed_out`, `ts`.
`elapsed_ms` runs from the hook script's first statement (before its first `llm_router`
import) to process exit; interpreter start-up before that line and teardown after exit are
outside it. The write is one `O_APPEND` write with no lock, the file is capped at two
generations of `LLM_ROUTER_HOOK_LATENCY_MAX_BYTES` (default 4 MiB), and
`LLM_ROUTER_HOOK_LATENCY=off` disables it.

- *Budgets* live in one table, `llm_router.hook_latency.HOOK_BUDGETS_MS`: `agent-route`
  320 s and `auto-route` 60 s (the registered host timeouts); the rest are declared, not
  derived — there was no live distribution to derive them from: 2 s for the per-prompt and
  per-tool-call hooks (`status-bar`, `enforce-route`, `subagent-start`, `agent-depth-release`,
  `cc-usage-track`), 5 s for `usage-refresh`, `playwright-compress` and `bash-compress`, 10 s
  for `session-start` and `session-end`. Re-set them from the first week of real p95s. An
  unlisted hook is held to 5 s.
- `timed_out` means `elapsed_ms >= budget`. A hook the host kills at its timeout never
  reaches exit and writes no row; kills are shown beside the log from the fail-open ledger
  (`CHZ-HOOK-KILLED`).
- *Not session-kind filtered*: a row carries no session id.
- *Coverage*: the 12 hooks the installer registers for Claude Code, except `context-capture`.
  It imports `llm_router` only inside functions, so arming the recorder would add the package
  import to every call that otherwise exits early; it needs a stdlib-only recorder first. A
  test fails if the installer registers a hook that is neither instrumented nor that one
  named exception. `codex-stop.py` and the other host-specific scripts are not covered.
- *Cost of recording it* is the PR's own guardrail, measured and stated in
  [`../KPI-LEDGER.md`](../KPI-LEDGER.md): one `O_APPEND` write of ~110 bytes at exit, no lock,
  no read.

**G2 — silent failures per 100 calls.** Every `fail_open.jsonl` row now carries `ts`
(`fail_open.jsonl` had no timestamp before; the field is new, not reused). The rate is
timestamped events in the window / calls in the window x 100, overall and per code, with the
top five codes listed. *Calls* are the instrumented-hook invocations plus the proxy ledger
rows in the window, all session kinds — a fail-open row names no session, so the numerator
cannot be filtered to organic and the denominator must not be. The window starts no earlier
than the first timestamped evidence (a timestamped fail-open row or a recorded hook call),
so the denominator never counts calls from a period the numerator could not see. Rows from
before the timestamp existed cannot be placed in any window: they stay in a labelled
`all-time:` line and are never guessed into one. A zero is reported as `0.00` only once a
timestamped event is known to have been written; before that it is `not measurable`.

**G4 — wrongly benched providers.** `provider_bench.jsonl` records every bench (provider,
trigger `header` | `cli` | `text`, the persisted reset time, `ts`), every owner unban and every
success of a provider while it was benched. The rate is wrong benches / benches recorded in
the window x 100. A bench is judged only over the span it was the one in force (a later bench
of the same provider replaces it), and a bench that is both unbanned and succeeded counts
once. Zero benches in the window is `not measurable`. Below 50 benches the line gives the
counts ("n=3 benches; 1 shown wrong so far"), not a rate. Benches still in force are reported
separately: they can still turn out wrong, so the wrong count is a floor until they lapse. A
call that was already in flight when a bench was recorded and then succeeded counts under the
second rule. The providers benched right now are kept as a detail line.

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

**Backfilled kinds (sessions from before tagging).** `llm-router kpi --backfill-tags [--dry-run]` derives a kind
for each untagged session from the one record that survives it, its Claude Code transcript
(`~/.claude/projects/*/<session_id>.jsonl`: the first `cwd` and `entrypoint`), with the same function the live tagger
calls (`session_kind.classify_with_basis`), and appends it to a sidecar, `~/.llm-router/session_kind_backfill.jsonl`
(mode 0600; `session_id`, `kind`, `source`, a coarse basis code and `ts`; no prompt text, no paths). The ledgers are
never written, and deleting the sidecar restores the previous numbers exactly. The append is a locked critical
section (a sibling `session_kind_backfill.jsonl.lock` file; the sidecar is re-read inside the lock), so two runs at once
never write a session twice or interleave a line; a directory the command has to create is 0700, an existing one is
left as its owner made it. Rules: the sidecar is the LAST step of
the join (tag file, then the record's own stamp, then agreeing proxy-row stamps, then the sidecar), so a live tag
always wins; it is write-once per session; a transcript that is missing, unreadable, carries neither field, or does not show both within its first 2 MiB (256 KiB
per line) gives
`unknown`, which never enters a KPI (it is not organic and not a kind); a session whose proxy rows conflict gets no
row. Every affected line states its backfilled share, e.g. `n=29,128, 24,244 backfilled`. D4, G1 and G3 do not
consult the sidecar (D4 and G1 read the kind each proxy row was written with, G3 is not kind-filtered).
**Where the sidecar is read, and where it is not.** It is read from disk only by `llm-router kpi` (NS, D1 and D2
through `northstar.units(backfill=True)`; D3 through its own `KindIndex`) and by `--backfill-tags` /
`--validate-backfill`. It is not read by the proxy, by any hook, by the Stop line (`northstar.current_session_line`,
which runs on every turn), by the quality breaker (which the UserPromptSubmit, Agent and Stop hooks call) or by
`llm-router northstar`: `backfill` is off by default in `northstar.units`, `build_sessions` and
`_scan_proxy_ledger`, and `kpi` is the one caller that turns it on. `tests/test_session_kind_backfill.py` pins zero
`load_sidecar` calls on the Stop-line and quality-breaker paths, and that `kpi` still resolves through it.
`llm-router kpi --validate-backfill` (read-only, counts only) re-runs the rules on sessions that already have a live
kind and prints agreement and a confusion table; re-run it as live tags accumulate. One thing a transcript cannot
show is the `LLM_ROUTER_SESSION_KIND` override: a session whose live kind came from it would be derived from its
`cwd` and `entrypoint` instead, which the validation counts as a disagreement.

### The receipt band's keep and redo presses (`user_signal`)

The Claude Code mod `llm-router-receipt` (`llm-router mod install`) shows a band after a turn the proxy served
off Claude, with two keys: `k` keep and `r` redo on Claude. Each press is one `user_signal` row in
`user_signals.jsonl` under the router home (0600, locked append, capped): a route key (the served reply's
`msg_id`), `ts`, `signal` (`kept` | `redone`) and `surface`, and nothing else. No prompt or answer text. Per key
the last press wins, so pressing twice is one verdict.

**Owner rule (2026-10-05): "used" requires a passing test.** A keep press is not a test, so it is never counted as
used:

| Signal | NS | D1 | D2 | D3 | Shown as |
|---|---|---|---|---|---|
| `kept` (`user_kept`) | never | never | never | never (not in the denominator either) | its own `user_kept n=...` line under D3 |
| `redone` (`user_redone`) | never | never | never | yes: a decided redo event (numerator and denominator) | its own `user_redone n=...` line, and in D3 |

`tests/test_user_signal_kpi.py` fails if a keep ever moves NS, D1, D2 or D3. A redo row carries no session id, so
it is not session-kind filtered (the line says so).

**Known overlap (unresolved):** the redo prompt the band submits starts with `claude:`, the router's explicit
"answer on Claude" override, which `usage_outcome` / `northstar` already read as a redo of the routed turn. One `r`
press can therefore reach D3 twice: once as a `user_redone` row and once as that detected override. The signal row
has no session id, so the two cannot be matched; D3 may overstate redos by up to the number of `r` presses. The
`user_redone n=` line gives that bound.

**Accepted (2026-10-06 review follow-up):** the same `claude:` prefix also makes one `r` press mark the preceding unit
as redone in NS, D1 and D2, not only D3. This is accepted, not a bug: the press is the person asking for Claude's own
answer to that unit, which is what a redo is. The detector in `usage_outcome` skips the override only when the prompt
*ends with* the band's redo mark (`BAND_REDO_MARK`), so a prompt that merely quotes the mark is still an override.

### `llm-router kpi --health`

One line per KPI: `measured`, `blind` or `stale`, the one reason, and the n. **Blind** = no number (nothing to
count, below 50, or not instrumented) or a number whose data carries no timestamp. **Stale** = a number whose newest
data point is older than 48 h (live ledgers; `--stale-hours` to change) or 30 d (the frozen benchmark behind O2 and
D5). Exit code 0 even when KPIs are blind; `--strict` exits 1 if any is blind (a stale KPI does not trip it).
G1 (hook), G2 and G4 are read from their own logs (see "How G1, G2 and G4 are measured"); their newest data
point is the newest hook row, the newest call in the G2 denominator, and the newest bench in the window. G1 (hook)
is blind until a hook has recorded 50 invocations in the window. G4 is blind until 50 benches are recorded in the
window, and benches are rare, so expect it blind for a long while: its line still gives the counts ("n=3 benches;
1 shown wrong so far") and an old newest bench is not read as a stopped feed.

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
  tagged sessions; the rest stay untagged, never organic, until `llm-router kpi --backfill-tags` has been run
  (see "Session kind on NS, D1 and D2"). The backfill's own check has almost no live-tagged sessions to compare
  against on this machine (2 tag files on 2026-10-04), so it shows the rules and their inputs agree, not that the
  backfilled organic share is accurate; `--validate-backfill` gets more informative as live tags accumulate.
  It is also near-circular: the backfill calls the same `classify_with_basis` as the live tagger, so agreement only
  shows that a transcript's first `cwd` and `entrypoint` match what the hook saw. Two ways a backfilled "organic"
  can be wrong are unmeasured: (a) a live `LLM_ROUTER_SESSION_KIND` override that forced research or harness leaves
  no trace in a transcript, and (b) an `sdk*` entrypoint missing from a fully read transcript falls through to
  organic. Sub-agents in an ordinary project directory cannot be told apart from a main session by these signals
  either. All three push toward a false organic, so the backfilled organic population may be overstated.
- **The proxy ledger records policy version and the pre-override proposed tier since 2026-10-03.** D5 and D4
  rows written before that carry neither (G3 excludes them as before the schema start).
- **O1 has not been reconciled against Claude Code's `total_cost_usd`.** Until it is,
  O1 is an estimate, not a verified figure (see `NORTH_STAR.md`'s point 13 on unverified
  savings).
