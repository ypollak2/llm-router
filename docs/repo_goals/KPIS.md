# llm-router KPIs

> Adopted by the owner on 2026-10-03. See [NORTH_STAR.md](NORTH_STAR.md) for the
> one-sentence goal these numbers serve, and [`../KPI-LEDGER.md`](../KPI-LEDGER.md)
> for the per-change record of how each PR moved them.

Source: a private research file named `KPI-SPEC.md`, kept outside this repository. This
page is this repo's public carry-over of that spec's definitions and figures; the private
file itself is not reproduced or linked here beyond its name.

## North Star and Outcomes

**NS — Non-Claude-and-used share.** (prompts + LLM calls served by a non-Claude model AND
used as-is) / (all prompts + LLM calls), organic sessions only. Target **> 80%, pooled, organic,
7 days**. Today: **0.0%** (0 of 4,300 organic units, 7 days, 2026-10-06, `llm-router kpi --days 7
--json` on c2ed278; 6 sessions, one research session, b9f04425, held 81.4% of those units). That
session is now tagged research by the override file (see "Session-kind override"), so the same
command no longer counts it: on the pinned window W0 (2026-09-29T09:41:51Z to 2026-10-06T09:41:52Z)
the count is 0.0% of 59 organic units, 25 of them backfilled (`llm-router kpi --since ... --until
... --json` on a copy of `~/.llm-router`, 2026-10-07). Read the 4,300 as an upper bound on volume. These figures were taken with the heuristic numerator (`outcome == "used"`), before the strict rule below merged (#294); they have not been recomputed under it.

**Q&A never counts toward NS.** This is the owner's rule, and the code enforces it:
`llm-router kpi` computes NS and D2 with the **strict-used** rule (`northstar.is_strict_used`, owner
decision D-2): served by a non-Claude model AND `verify.verify_status` in {`pass_f2p`, `pass_f2p_model`}
AND task type not Q&A (`northstar.QA_TASK_TYPES`) AND outcome not redo. A unit with no verify record never
counts, a keep press adds nothing, and pass-to-pass never counts. The heuristic numerators
(`outcome == "used"`) stay in `kpis_diag.NS_heuristic` / `D2_heuristic`, outside the KPI codes: "not a target".

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
`--health` measured/blind/stale counts. Today: **0.6%** (7 of 1,183 turns, all Haiku, 7 days, 2026-10-06, c2ed278; 1,175 of those 1,183
turns are the research session b9f04425, so without it O3 has 8 organic turns from 2 sessions and
is not informative); per call 0.2% (23 of 10,085). Run `llm-router kpi --since-policy <version>`.

Code: `src/llm_router/offload_share.py`. The line also shows Haiku share, local share, and the
redo rate per class (Haiku, local) with n. Below n=50 (units, or units in a class) it prints
`too few to tell (n=N)`, never a percentage; with no unit it prints `not measurable`.

*Units.* (a) Proxy-served Claude calls: `proxy_calls.jsonl` rows with `decision` forwarded or
fallback and no 4xx/5xx upstream status. Class **Haiku** when `served_model` (else
`requested_model`) names Haiku, else Claude. Claude Code's own side calls (`tier_reason ==
side_call`: titles, summaries) are excluded and counted: the router did not choose them and
nobody redoes them. (b) Proxy rows with `decision == served` (a local backend answered): class
**local**. (c) `northstar.local_shadow_units()` (PR #284, `usage.db` `provenance='runtime'`,
`final_provider='ollama'`): a local MCP `llm()` answer given INSIDE a Claude turn. Claude made
that turn's first call, so it is **not a turn** (M0.3a): it sits outside both the numerator and the
denominator and is printed on its own line (`local MCP answers inside Claude turns`, and as
`o3.breakdown.local_assist_n` / `local_assist_redone`). One with no `session_id` cannot be scoped
to organic sessions: it is excluded and counted (`local_no_session`). One local unit per MCP
call: every `route_and_call` writes a row, so `llm_edit`'s retries (up to 3) and `llm_act`'s planner
write several rows with one `tool_use_id`; those rows are one unit. A row with no `tool_use_id` is
its own unit. A call whose every row has `success=0` (the router's own verdict: degraded, a
failure finish reason, unusable output) is not served work: counted as `local_failed`, never a
unit. Rows written before `call_identity` have no session id and stay unknown; they are never
guessed into a session. Count them on a copy of `usage.db` with
`select count(*) from routing_decisions where provenance='runtime' and final_provider='ollama' and session_id is null`.
Rows are stamped only by an MCP server started after a release reaches its install: a
long-lived Claude Code MCP server keeps running the code it started with, and Claude.app's
servers run from whatever tree they were installed from. (d) **Zero-Claude edit
turns** (M0.3c): the hook applied the edit and the whole turn was served locally, so there is
no proxy row. `edit_outcomes.jsonl` rows with `source=zero_claude` and `applied=true` count as
**one local turn per (session_id, turn_id)** (`turn_id` = `prompt_key.key(prompt)`, a text-free
hash); a turn that edits a source file and a test file is still 1 turn. A `claude:` re-ask in
the next turns marks it redone. Rows with `source=llm_edit` (or none) are never turns, and a
row with no session id or no turn id is excluded and counted
(`o3.excluded.edit_no_session`, `edit_no_turn_id`).

*Session id and tool_use id on `routing_decisions`.* The MCP path (`llm()`, `llm_edit`, through
`router.py`'s finalizer) stamps `session_id` from `CLAUDE_CODE_SESSION_ID`, which Claude Code puts
in the MCP server's environment (checked on a live server 2026-10-06), and `tool_use_id` from the
`tools/call` request's `_meta["claudecode/toolUseId"]` (Claude Code 2.1.291 sends it). The env id
is stamped only inside an MCP tool call (a `tool_use_id` is bound): every Bash tool shell carries
it too, so a gateway or `route_server` started from one would stamp that session on calls it
serves for anyone. The hook's DIRECT path and agent-route stamp the hook payload's session id
(agent-route's own id falls back to the machine-wide `session_id.txt`, so it is not used). The
machine-wide `current_session.json` pointer is NOT used: the last session to prompt wins it, so
it would attribute an answer to the wrong conversation. Ids only, shape-checked
(`[A-Za-z0-9_.:-]{1,128}`); free text, a non-string or a placeholder (`sdk`, `unknown`) is stored
as NULL (`src/llm_router/call_identity.py`).

*Which proxy calls are turns* (M0.3b; the owner's definition, PLAN section 1.2 O3). A turn is a proxy
row that is not a side call and whose `step_class` is `turn_first`, minus sub-agent first calls. A row with
no `step_class` (written before GE1) or labelled `subagent_first` is never a turn; they are counted in
`o3.excluded.no_step_class` and `o3.excluded.step_subagent_first` (`docs/bugs/O3-STEP-1.md`).
Since TURNFIRST-1 (`docs/bugs/TURNFIRST-1.md`) the proxy splits two kinds out of `turn_first`: a
`harness_turn` (a main-thread turn whose newest user message is only a notification or command echo)
is still a turn, as it was; a `subagent_turn` (a later sub-agent call) follows the `subagent_first`
rule and is counted in `o3.excluded.step_subagent_turn` when no transcript join puts it on the main thread.
The label is read from the tool list, so a main session without the `Agent`/`Task` launcher (tool
disabled, a restricted `-p` run) records its later turns as `subagent_turn` too: they drop out of the
turn count unless the transcript join marks them `turn` or `meta`.
The proxy row's `msg_id` is joined to the transcript's assistant `message.id` (`o3_transcripts`). The
join changes exactly one thing; the rest of its roles are only counted:

| Transcript role of the row | Counted as | Counter |
|---|---|---|
| sub-agent call (`isSidechain: true`, or `<session>/subagents/**/agent-*.jsonl`) | not a turn (per-call unit only, does not end a redo window) | `o3.excluded.subagent_first` |
| first answer to injected input: slash command, sub-agent hand-back, peer message, task notification | **turn** (kept in) | `o3.kept_in.meta_first` |
| message id in no message of a session that HAS a transcript (permission classifier, prompt suggestion, side query) | **turn** (kept in: "unjoined rows stay in") | `o3.kept_in.unjoined` |
| session with no transcript at all, or a row with no message id | **turn** (proxy-only rule) | `o3.kept_in.no_transcript` |
| anything else | by `step_class` | |

A row the proxy flags `side_call` is never promoted to a turn, whatever the transcript says. The
`kept_in` counters are the size of the gap between this definition and "the first answer to a typed
prompt": they are reported so the owner can see it, and they do not change n.

Typed prompt (used only by `o3_integrity.py`, never by the scorecard) = a `user` entry that is not a
sidechain, not `isMeta`, has non-blank text and does not start with `<command-`. **Open owner decision:
background task notifications have that shape too** (`promptSource=system`: 113 of the 209 typed prompts
of the one qualifying W0 session, 54%), so a "human turn" here includes turns the harness started on a
sub-agent completion, and the redo window of 2 human turns can span notifications rather than human
replies. The definition is not changed here; O3 should not feed an M2/M4 bar before the owner decides.

Measured on W0 (2026-09-29T09:41:51Z to 2026-10-06T09:41:52Z, `proxy_calls.jsonl` md5 627eef56, 26,096
rows, one qualifying session b9f04425): the owner's rule counts **813 turns against 209 typed prompts =
3.890**, outside the M0-2 band 0.90-1.10. Of the 813: 548 are calls in no transcript message, 162
injected-input first calls and 103 first answers to a typed prompt (the transcript-decided count would be
177 against 209 = 0.847, or 177 against the 184 prompts the proxy could have seen = 0.962; it is a
different definition and needs a PLAN edit).

*Bound* (M0.3d). When G3 `session_kind` completeness is below 95%, O3 prints `o3.bound` with
`lower` and `upper`: the headline (untagged rows treated as non-organic) and the same number with
untagged rows treated as organic, whichever is smaller and larger.

*Integrity.* `$PP/scripts/o3_integrity.py --since --until` compares proxy turns with every typed
transcript prompt per session and pooled over an absolute window (PLAN M0-2: 0.90-1.10; prompts older than
the session's first proxy row are printed apart and still counted). Zero qualifying sessions is a FAIL. Two
checks do not go through `offload_share`: no msg_id is counted as a turn twice, and the O3 turn count must
equal a recount made from the ledger rows.

*Owner-accepted warning (M0-2, 2026-10-07).* On W0 the integrity ratio is 813 / 209 = 3.89 on the only
measurable session (b9f04425, research), outside the bar, so M0-2 FAILED. The owner accepted O3 as is,
with a warning: `kpi`, `kpi --health`, `kpi --json` (`o3.caveat`, `o3.integrity`) and the weekly markdown
all print "O3 turn count unvalidated: may overcount turns (integrity 3.89x ...)". The figures live in
one place, `commands/kpi.py` `O3_INTEGRITY_GATE`. Do not read O3 as a bar for M2 or M4 until the turn
count is validated; remove the warning only after `o3_integrity.py` passes.

*Redone.* A unit is redone when any of:

1. **Escalation.** A later proxy row of the same conversation (session), in the unit's own
   human turn or the next 2 human turns, has `tier_reason` `escalation` or
   `escalation_under_pressure`: the proxy's own detector (`proxy/escalation.py`): a `claude:`,
   `native:` or `opus:` re-ask, a contradiction, or failed tools. A human turn starts at a
   non-side-call row whose `step_class` is not `continuation`.
2. **Receipt band.** `user_signals.jsonl` has a `redone` press (last press per key wins) for
   the unit's `msg_id`.
3. **usage_outcome.** A `redone` verdict (`usage_outcome.py`) for the unit's routed event. Local
   units only (a verdict exists only for routed MCP events). Joined exactly when the unit's
   `tool_use_id` equals the verdict's `event_id` (the transcript's `tool_use` id); then the
   transcript's session id replaces the stamped one (a server kept across `/clear` can still
   carry the old id). `claude --fork-session` copies the history, ids included, so one id can
   sit in two transcripts: the copy in the stamped session
   wins; with none, a `redone` copy wins; ties break on session id, never on file order. A unit
   with no `tool_use_id` falls back to: same session, within 120 s, each verdict used once and
   never one already joined exactly.

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
For an exactly joined local unit the human turns after it are read from its transcript
(`usage_outcome`'s `turns_after`), and the transcript decides both ways: it closes a window the
proxy never saw, and it keeps a window open that the proxy would close. The proxy counts every
non-continuation request as a human turn, a Task subagent's first call included, so it can
overcount; the transcript cannot. A unit that is not joined has only the proxy's count.

*Local answers accepted* (its own line under O3: `local answers: n served, n accepted, accept
rate`). Reporting only and **never part of NS**: NS stays strict ("used" needs a passing test,
owner rule 2026-10-05). Over local turns (proxy-served, zero-Claude) plus local MCP answers
(`local_assist`, which are explicitly not turns and stay out of O3's n and local share):

- **redone** -- redone by the definition above;
- **accepted** -- not redone, and either 2 human turns of the same session followed (no re-ask,
  correction or redo in the unit's turn or the next 2) or the person's last receipt-band press for
  its `msg_id` is `kept`. A keep loses to a redo that lands later in the window;
- **unjudged** -- not redone, and its exactly joined usage_outcome verdict is `unknown` for a
  reason that will not resolve (`no_result`, `no_pairs`, `not_applied_seen`, `partly_applied`).
  usage_outcome never rounds these to either side, and neither does this line: a local `llm_edit`
  that failed every attempt is not an accepted answer, however many turns follow;
- **pending** -- neither yet: counted and stated, never on either side. This includes an edit
  whose verdict is `window_open` while its `so_far` says it is not applied yet.

Accept rate = accepted / (accepted + redone). Below 50 decided it prints `too few to tell
(n=N)`; with none decided, `not measurable`; never `0%` for unknown. Pending, unjudged and
failed units and units with no session id are listed beside it, not in the rate. Because the usage_outcome verdict looks 3
human turns ahead (`WINDOW_TURNS`), a redo in the third turn after an exactly joined unit also
counts as redone; that is O3's own redo definition, kept as is. A routed call in a single-turn
session has no human turn after it and stays pending.

Known limits of the session id: (1) The stale-stamp correction (the transcript's session id
replaces the stamped one) reads main-thread transcripts only. A call made by a subagent after
`/clear` or an in-process resume is not joined, keeps the MCP server's old session id, and is
judged against that session's turns. It usually ends pending, because the old session has no
later turns, but it can be accepted when it does. (2) Claude Desktop and Cursor MCP servers
have no `CLAUDE_CODE_SESSION_ID`; their rows stay NULL (unknown) by design.
Local answers the line cannot judge are stated beside it and never dropped silently: no session
id, flagged failed, from a session with no kind tag, or from a research/other-kind session (the
last two are counted among O3's `untagged` / `other_kind` exclusions as well). Tests:
`tests/test_local_session_accepted.py`.

*Session-kind override.* `~/.llm-router/session_kind_overrides.json`, `{session_id: {"kind": ..., "reason": ...}}`,
names a session whose tag or whose rows' own stamps are wrong and cannot be rewritten (the ledgers are
append-only). Precedence for every reader: override, then the tag file, then the row's own stamp. NS, D1, D2,
D3, O3 and D4/G1-proxy all obey it, and so do the proxy's and the edit ledger's stamps for rows written after
it. The G3 proxy audit does not change: it measures whether the writer recorded a `session_kind`, not which
kind; the G3 verdict's organic population (below) follows the override. The first
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

### Plan metrics (O3-int, CU, Cλ2, CRAW, HP/HR, CFB, CLAT, QAUD, LPREC)

These come from the owner's primary plan (a private research file, §1.2; not reproduced here).
Most are computed by research scripts outside this repository, not by `llm-router kpi`. The
"Computed by" column says where each one is, so that a number here is never mistaken for a
`kpi` output. A rate carries its n; below its minimum n it is "not informative", never a pass.

| ID | Definition | Today (n, source) | Computed by |
|---|---|---|---|
| O3-int | GUARD. O3 proxy turns / typed transcript human prompts, per session and pooled, over the pinned window W0. Bar 0.90-1.10. | 424/370 = 1.146 in b9f04425 (research findings, 2026-10-06) | plan task M0.3: `o3_integrity.py --since --until` (not in this repo) |
| CU | GUARD. Under-route on real prompts: #{rank(pred) < rank(truth)} / n, tiers ordered local < haiku < sonnet < opus. | rules_eff 39/89 = 43.8% (E2 held-out, `~/.rsi/research/complexity-v2/p_eval/REPORT.txt`) | `eval_router.py` results |
| Cλ2 | LEAD. Quality-weighted cost: per item c(pred) if pred >= truth, else c(pred) + 2·c(truth); c = haiku 1, sonnet 2.7, opus 6.15 (the p_eval table; D4 above uses its own weights, 3.66 for Sonnet, and the two are never mixed). | rules_eff 8.24, always-Opus 6.15, qwen rule 6.05 (n=89, p_eval) | `eval_router.py` results |
| CRAW | GUARD. Raw cost mean c(pred); stops a disguised always-Opus. | rules_eff 2.86, qwen rule 3.91, always-Opus 6.15 (n=89, p_eval) | `eval_router.py` results |
| HP / HR | LEAD. Haiku precision = #{pred in {local, haiku} and truth = haiku} / #{pred in {local, haiku}}; recall = same numerator / #{truth = haiku}. | HP <= 36% for every classifier tested (n=89, p_eval) | `eval_router.py` results |
| CFB | GUARD. Classifier fallback: timeout, parse error, cold model or over budget, divided by LLM calls. | 8/30 = 27%, contended run (`~/.rsi/research/local-classifier/results/qwen3.5_latest__v2__tune.run1_contended.json`) | classifier eval; `classifier_shadow.jsonl` once shadow runs |
| CLAT | GUARD. Warm LLM classifier latency p50 / p95 in ms, uncontended runs only (free memory >= 20%). | p50 1,350 / p95 2,297 ms, n=30, contended, so excluded by the rule (same file) | classifier eval |
| QAUD | GUARD. Blind audit: share of sampled turns judged acceptable, `cannot_judge` excluded; model names stripped, 6 calibration items per batch. | none yet | audit scripts under the plan's `audit/` (not in this repo) |
| LPREC | GUARD. Verifier precision against hidden tests; also false-used rate, unavailable rate, tampering. | not measured; the toolkit's own "used" was right 3 of 7 (toolkit `bench20.log`, interim) | plan task M3.6, `phase_d/report.json` |

## Drivers

| ID | Driver | Definition |
|---|---|---|
| D1 | Offered off Claude | Attempted units / all organic units, pooled over the window (`kpi._ns_d1_d2`). A unit is *attempted* when its `kind` is in `northstar.ATTEMPTED_KINDS` or its `lever` is `proxy`. Today **4.5%** (193 of 4,300 units, 7 days, 2026-10-06, c2ed278; includes b9f04425, see NS above). |
| D2 | Success when tried | Strict-used units / attempted units, same population (`kpi._ns_d1_d2`). D2 uses the strict-used rule (see NS above), not the heuristic `outcome == "used"`. Today **0 of 193** (7 days, 2026-10-06, c2ed278, heuristic numerator; not recomputed under the strict rule). |
| D3 | Redo rate | % of routed outputs Claude redoes within 3 turns, plus the person's own `r` redo presses on the receipt band (see below). **D3 uses a window of 3 turns (`usage_outcome.WINDOW_TURNS`); O3 uses 2 (`offload_share.REDO_TURNS`). The two are different windows: never mix them.** |
| D4 | Tier mix | % of Claude turns **and** % of Claude quota cost on Opus/Sonnet/Haiku, printed side by side (source: `proxy_calls.jsonl`, tracked since PR #246). Cost is calls weighted by per-call cost, Haiku 1 : Sonnet 3.66 : Opus 6.15 (Opus is 1.68x Sonnet and 6.15x Haiku; `proxy/claude_tiers.yaml`, probe 2026-09-29, n=5 calls per model, pinned by a test). A tier with no weight (Fable) is left out of the weighted share and counted, never given a guessed weight. Organic sessions only, by the kind each proxy row was written with (rows from before tagging stay out): research and harness are excluded (`llm-router kpi --include research` adds research, never harness). |
| D5 | Classifier accuracy | Exact tier vs. a truth set; too-weak (under-route) rate <= 10%. |

## Guardrails

| ID | Guardrail | Definition |
|---|---|---|
| G1 | Added latency | Hook wall time, p50 / p95 per hook against that hook's budget; proxy decision p50 / p95 with n, turn-first (`step_class == turn_first`, P0.9-e decision phases) and continuation (`tier_decision_s`) apart (`G1_proxy`). |
| G2 | Silent failures per 100 calls | Fail-open events per 100 calls over the window, overall, per code and the top five codes — the rate must not go up, and every instance must be recorded. Truncation/overflow and Ollama-hung are not wired into this counter yet. |
| G3 | Ledger completeness | Verdict: every writer with traffic (usage, routing_decisions, DIRECT, proxy) has >= 100 organic rows and >= 99% of them carry every PRD field (R-EVL-1), with n per writer. Beside it, >= 99% of proxy rows with every decision field that can apply to them recorded (definition below). |
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

- *Session id (PG9).* Every writer puts the host's `session_id` on its row, taken from the
  payload the hook already parsed (`hook_latency.set_session`; the statusline passes it to
  `record-raw`). A payload without one leaves the key out (null); it is never invented, and
  no prompt text is written. The P0.9-g live clause prints `n`, `n_sessions` and the
  largest session's share per hook (`hook_wall.judge_live`). Rows with no session id count
  as `without session id`. Below `hook_wall.MIN_SESSIONS` (2) sessions the clause prints
  "not informative (need >=2 sessions)" and cannot PASS (a p95 over budget still FAILs),
  the same rule as the P0.9-e turn-first decision. Rows written before this change carry
  no session id, so a live window that predates the deploy reads "not informative".

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

**G1 — proxy decision latency (`G1_proxy`).** p50 and p95 from `proxy_calls.jsonl`, with n, in two
segments. *Turn-first*: rows with `step_class == turn_first`, measured as the P0.9-e decision, the sum
of `tier_phases_ms` over `proxy.tiers.DECISION_PHASES` (`classify`, `quota_read`, `stickiness`,
`haiku_checks`); it prints n, the session count and the largest session's share, and says `not
informative` below n=100 or with fewer than 2 sessions (P0.9-e MUST). Rows with a null `step_class`
(pre-GE1 rows of any kind), `subagent_first`, `subagent_turn` and `harness_turn` rows (TURNFIRST-1) and
turn-first rows without the decision phases are left out and counted on the line (`excluded` in the JSON); `tier_decision_s` (wall time, shadow
scheduling included) is never substituted for a missing phase sum (`docs/bugs/P09-11.md`).
The phase sum does not include scheduling the classifier shadow, so that cost has its own
segment (P09-13): `proxy/server.py` records `tier_shadow_schedule_ms` on a row only when a shadow
call was actually scheduled (milliseconds only, no request content), and `G1_proxy` appends
`| shadow schedule p50=..ms p95=..ms (n=.., sessions=..; within|OVER 30ms target)` from turn-first
rows that carry it. The p95 is compared with the "shadow <= 30 ms" target below; the same gate
applies: `not informative` below n=100 or with fewer than 2 sessions, and no percentile below n=50
(`shadow_schedule` in the JSON). With the shadow off (the default) no row carries the field and
the segment is absent from the line. `tier_decision_s` keeps its meaning (wall time, scheduling
included) and is what the continuation segment reads.
*Continuation* (a tool-result follow-up): `tier_decision_s`. Claude Code side calls
(`tier_reason == side_call` or `step_class == side_call`) run no classifier and are left out; their
count is in the JSON (`side_call_excluded`). A segment below n=50 prints no percentiles and they are
null in the JSON. Percentiles are nearest rank on n-1. Same session-kind filter as D4 (organic; research
with `--include research`).
Why it changed: this KPI used to take the p95 of `added_latency_s`, which is 0.0 on 25,924 of 25,963
forwarded rows (all-time ledger copy, 2026-10-07; it is only set on a few decision paths), so it printed
0 ms (`docs/BUGS.md`). `tier_decision_s` is present on 25,397 of those 25,963 rows. First reading, W0,
`--include research` (organic plus research, n=10,086 rows, side calls excluded): turn-first p50=4ms
p95=83ms (n=1,181); continuation p50=4ms p95=21ms (n=8,905). An independent recompute from the raw
ledger gave turn-first n=1,183 p50=4ms p95=83ms and continuation n=8,905 p50=4ms p95=21ms. (Those turn-first readings predate P09-11: that bucket was every row that was not a
continuation, null-step rows included, measured as `tier_decision_s`.) Targets
(primary plan, `PLAN.md` G1-proxy "Shadow <= 30 ms"; `PLAN-v16.md` carries no separate figure): shadow <= 30 ms; with the LLM decision, turn-first <= its wait budget + 100 ms and
continuation <= 30 ms.

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

### G3 verdict: the PRD field list per writer (PLAN v16 R8, P0.8-c)

The audit above reads the proxy's tier fields only; on 2026-10-08 it read 93.0% while no other writer was checked.
The verdict (`--json`: `kpis.G3.prd`) scores four writers apart, each over the PRD field list: `session_id`,
`task_id`, model, tier, reason, tokens, cost, latency, outcome. NULL is missing.

| Writer | Rows | model / tier / reason / tokens / cost / latency / outcome |
|---|---|---|
| usage | `usage` table | `model` / `complexity` / **no column** / `input_tokens`+`output_tokens` / `cost_usd` / `latency_ms` / `success` |
| routing_decisions | `routing_decisions`, `reason_code` not `direct` | `final_model` / `complexity` / `reason_code` / `input_tokens`+`output_tokens` / `cost_usd` / `latency_ms` / `success` |
| DIRECT | `routing_decisions`, `reason_code = 'direct'` | as routing_decisions |
| proxy | `proxy_calls.jsonl` | `model` if served, else `served_model` (tiers on) or `requested_model` (tiers off) / `tier` / `tier_reason`, else `reason` / `anthropic_usage` / `anthropic_cost_usd` / `upstream_latency_s`, else `route_latency_s` / `decision` if served, else `upstream_status` |

- **Per writer, with n.** A writer with rows owes n >= 100 organic rows (below: not informative) and >= 99% of
  them carrying every scored field. A writer with 0 rows prints `no traffic` and is never a pass; with no writer
  carrying traffic the verdict is `NOT INFORMATIVE`. A database that cannot be read is `unreadable`, not empty.
- **`task_id`** is scored like every other field since P1.10 (it is a column on `usage` and `routing_decisions` and a key of every proxy row). Rows written before P1.10 carry NULL and count as missing until they leave the window; `unscored` is empty.
- **A field with no column is missing**, never dropped from the list: `usage` has no reason column, so the usage
  writer fails until one exists. A NOT NULL column (tokens, cost, latency on `usage`) is never NULL, so a
  placeholder 0 written there reads as recorded: G3 cannot see it.
- **Population.** Harness, headless and (without `--include research`) research rows are excluded and counted
  apart, the kind resolved as for NS and D3 (override, tag file, row stamp). Untagged rows stay in: dropping them
  would drop exactly the rows that lack a session id.

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
