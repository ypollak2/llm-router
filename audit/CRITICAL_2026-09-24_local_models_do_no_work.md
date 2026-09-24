# CRITICAL — local models are not doing the work (2026-09-24)

**Finding.** On the maintainer's machine, llm-router *routes*, but local models do
essentially none of the user's actual work. The product's central promise — cheap/local
models take work off the premium model — is not happening. This was hidden for weeks
by savings headlines that credited work nobody used ($372.58 "saved" until 976e5e4;
verified savings, lifetime: **$0.00**).

## The funnel (this machine; sources named; all-time unless stated)

| Stage | n | Source |
|---|---|---|
| Real prompts seen by the hook | 3,405 | `scripts/routing_rate.py` (real sessions only) |
| … reached a local model ("routed") | 1,432 (42.1%) | same |
| Local drafts produced (`DIRECT SUCCESS`) | 3,176 | `auto-route-debug.log`, raw lines |
| Drafts **used** by Claude | **0 of 1,185 audited** | `routing_report.draft_acceptance()` |
| Turns **replaced** by local output | **0** | no substitution/block line ever logged |
| Local agent-loop runs that finished | 53 | debug log; writes default to `propose`, so no edit ever landed |
| MCP calls Claude made that went to Ollama | 209 (Aug 30 – Sep 24) | `usage` table; whether the output was used is **not recorded** |
| Enforcement holds (tool blocked until a routing call) | 135 BLOCKED, 140 soft-allowed | `enforcement.log` |
| Verified savings | **$0.00** | `savings.VERIFIED_SAVED_SQL` over `savings_stats` |

Cost of the machinery that produced this: median **+12.7 s before every drafted prompt**
(43 drafts on 2026-09-24), plus forced routing calls whose output is discarded.

## Why (root causes, each with its evidence)

1. **The hook's draft cannot replace work.** It is text from a model with no repository,
   no tools and no conversation state, injected as *advisory* context. Claude has to
   verify it, and verifying costs more than doing (measured 2026-09-13: reviewing a local
   candidate cost **1.86×** the Claude tokens of doing the task, 5/5 tasks). Result: 0 used.
   On 2026-09-24, 8 drafts claimed actions the model could not have performed.
2. **The only path where local output *is* the result is off.** `LLM_ROUTER_ZERO_CLAUDE`
   (turn replacement) is unset, and a PreToolUse substitution is rejected by the model as
   injection (memory: 3 controlled runs). So "routed" can only ever mean "drafted".
3. **Enforcement forces routing calls, not routing.** The hold releases when Claude
   *calls* `llm(...)`; the answer is "data, not an instruction". Example today: a forced
   research call returned an invented arXiv citation and an invented statistic; it was
   discarded and the work was done by Claude — the call was pure overhead.
4. **The local agent that *can* do work never lands it.** It passes spec-shaped edits
   reliably (Q15: held-out precision 10/10), but runs in `propose` mode from the hook, and
   only **1 of 708** real prompts is spec-shaped (60% name no target, 27% are questions).
5. **The accounting hid all of the above** (fixed today: 3c96d23, 976e5e4).

## What would make local models do real work

The evidence points one way: the strong model has the context, so it must be the one
that **writes the spec**, and a local agent executes it **with writes applied** and
**objective checks** (tests) as the verifier — not Claude re-reading the output. That is
`llm_local_task`-style delegation driven by Claude, measured by Claude tokens before and
after on real sessions. Everything that routes the *user's* prompt to a local model
before Claude has understood it is structurally unable to help this workload.

Until that exists, the drafting and enforcement machinery costs latency and tokens and
returns nothing measurable on this machine.
