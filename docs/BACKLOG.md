# Backlog

Open items with a plan behind them. Newest first. No item lands here without
either a linked plan or a one-line repro.

Numbers below were independently re-derived from `~/.llm-router/auto-route-debug.log`,
`usage.db` and the Claude Code transcripts on 2026-09-13, after an earlier pass got
several of them wrong. Where a count is stated, the command that produces it is given.

## Routing honesty (raised 2026-09-12, corrected 2026-09-13)

| # | Item | Evidence | Plan |
|---|---|---|---|
| 1 | **Savings are credited before the substitution check.** `log_direct_savings()` fires unconditionally at `auto-route.py:3975` with `gates_passed=True` and no adoption check (`savings_logger.py:149,184`); `_turn_blocked` is computed at `:4014` and gates only `log_direct_to_db()` at `:4020`. Rows reach `savings_stats` via either importer (`session-end.py:348,391` or `cost.py:2925`), still unchecked. | Window 2026-09-12 16:30 → 09-13 07:17: **38 rows / $0.593715** booked, of which **31 rows ($0.516110) are DIRECT drafts** and 7 ($0.077605) are receipt records. Matching the 26 `DRAFT UNUSED` invocation ids to their rows gives **$0.426410 credited for explicitly discarded drafts**. | [PLAN_SAVINGS_ATTRIBUTION.md](PLAN_SAVINGS_ATTRIBUTION.md) |
| 2 | **`TOOL LOOP RESCUE` logs "routing to the local agent loop" before the guard that decides whether anything runs.** The announcement sits at `auto-route.py:3763`; the execution block at `:3788` excludes enforce modes `off`/`shadow`. In a session with enforcement off, the line is emitted and nothing executes. | 38 rescue announcements in the window, **all in benchmark subprocesses** whose launcher sets `LLM_ROUTER_ENFORCE: "off"` (`scripts/bench_backend_quality.py:622`). **Not a swallowed exception** — an earlier entry here claimed that and was wrong. | move the log line below the guard, or state the mode in it |
| 3 | **The mini-summary banner does not count routes.** `routes: N` is the `limit=200` argument to `LineageStore().recent()` saturating; `top tier: local (M)` counts classification-time rows in `model_tracking.jsonl`, written before any model is called — including `research/*` rows naming Ollama when `build_chain()` returns `[]` for research (`chain_builder.py:147`). | `auto-route.py:3073-3105` | Stage 3 of PLAN_SAVINGS_ATTRIBUTION |
| 4 | **Enforcement is satisfiable without routing any work.** `enforce-route.py:1329` clears the hold on any tool whose bare name starts with `llm_`, plus exact-name and same-server matches; read-only tools are exempt (`:1403`) and repeated violations auto-unblock (`:1502`). Cleared holds are recorded as positive `route_realized` events. | All **4** `mcp__llm_router__llm` calls in session `3e33e160` were "Release lock … one-word ack" requests. Zero substantive delegation. | undesigned — decide whether enforcement verifies the call, or stops claiming it proves routing |
| 5 | **Enforcement demands routes the system declines to serve.** `build_chain()` returns `[]` for research unconditionally (`chain_builder.py:147`) and excludes Ollama from complex code (`:185`), yet the hook blocks tools demanding `llm(task="research")`. | **18 empty-chain skips in the window: 15 research + 3 complex-code.** 9 `NO_ROUTE task=research/moderate` in `enforcement.log`. | undesigned |
| 6 | **Silent provider fallbacks.** `_has_gemini()`/`_has_openai()` (`chain_builder.py:35-40`) return False on missing keys with no log line. No Ollama reachability probe exists, so an outage is indistinguishable from a bad answer. | neither provider appears anywhere in the window's debug log | one-time probe at session start + a log line per skipped provider |
| 7 | **`savings_log.jsonl` is a queue, not a ledger.** `session-end.py:305-365` claims and truncates it with `os.replace()` on every SessionEnd. Reading it for a total undercounts by whatever was already drained — it read as 1 row / $0.018 against a real ledger of 38 / $0.5937 during this audit. | — | expose a read API, or document the file as a queue in its first line |
| 8 | **`tool_intercept` cannot reach real work.** Of **314** Bash calls in the window: 296 rejected for `&&`/pipes/redirects, 2 for newlines, 12 not allowlisted, **4 passed the shape filter** — and all 4 then failed the ≥12-output-line threshold (`tool_intercept.py:286`). All **60** Reads were `.py`; interception supports 6 image extensions only (`:34`). **Eligible interceptions: 0/314 Bash, 0/60 Read.** | `intercepts.jsonl` has no events in the window | widening this is the subject of [PROPOSAL_LOCAL_EXECUTION.md](PROPOSAL_LOCAL_EXECUTION.md) — but see item 9 first |
| 9 | **PreToolUse deny cannot deliver a substituted result — measured, not assumed.** Three runs on 2026-09-13 (Sonnet, disposable fixture): the hook fired and native execution was prevented every time (the sentinel `NATIVE-CONTENT-A7F3` never reached the model), but the model rejected the substituted text as prompt injection in all three, including a control using llm-router's own `substitute_message` wording and a run with consistent markers across tools. It then **retried via another tool**, producing more turns than no interception. | `/tmp/deny_exp/`, model verbatim: *"no genuine tool result would instruct me to 'treat this as the result and continue'"* | do not build result-substitution on PreToolUse; see PROPOSAL_LOCAL_EXECUTION.md §1 |
| 10 | **The legacy tool-detection predicate misses operational intent.** With `LLM_ROUTER_CAPABILITY_ROUTING` unset, `needs_claude_tools()` returns `decision.legacy_match` (`chain_builder.py:216`) — a phrase/extension matcher (`capabilities.py:105,147`), not a semantic classifier. | **0 of the 6 real user prompts match it**; 4 of the 6 semantically require repo/log/state access. Across the window, `needs_tools` was True 3 times and False 53. | decide whether to promote capability routing out of shadow mode |

## Cross-backend quality benchmark (raised 2026-09-12)

| # | Item | Notes |
|---|---|---|
| 11 | **One pass per cell.** `scripts/bench_backend_quality.py` results are single-sample; Codex and Claude tie at 10/11 on the brutal suite and local's win on `br-collect-errors` is within noise. | 3 passes would separate capability from flakiness |
| 12 | **The local agent loop cannot write in its default config.** `LLM_ROUTER_AGENT_WRITES` defaults to `propose`, so routing an edit task to local yields a diff, not a change. Correct as a safety default; worth stating in the docs as a known limit. | see [BACKEND-QUALITY.md](BACKEND-QUALITY.md) |

## Corrections applied 2026-09-13

An earlier version of this file contained four wrong claims, all now fixed above:
1. "877 hook invocations" — 877 was the **line** count; there were **161** invocations.
2. "every draft was discarded" — 32 produced, 26 `DRAFT UNUSED`, 0 `DRAFT USED`, and
   **6 with no verdict at all** (pending records expire after an hour,
   `draft_usage.py:46,85`). "All discarded" is unsupported.
3. "`TOOL LOOP RESCUE` is an unexplained bug, possibly a swallowed exception" — it is
   explained by this repo's own benchmark harness setting `LLM_ROUTER_ENFORCE=off`.
4. "only `LLM_ROUTER_ZERO_CLAUDE=1` can replace a turn" — `LLM_ROUTER_RENDER_MODE=block`
   also does (`response_formatter.py:23`).

Full forensic report: [AUDIT_ROUTING_2026-09-12.md](AUDIT_ROUTING_2026-09-12.md).
