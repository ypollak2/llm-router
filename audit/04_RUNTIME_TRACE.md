# 04 — Runtime Trace (Runtime Architect, Phase 3)

Companion to `audit/01_ACTUAL_ARCHITECTURE.md`. Traces real call sites and one
live reproduction. All commands were run with `export LLM_ROUTER_HOME=$(mktemp -d)`
and `LLM_ROUTER_BASH_INTERCEPT=off`, using `.venv/bin/python`. Nothing under
`src/`, `scripts/`, `tests/`, `config/` was edited or deleted; no writes reached
`~/.llm-router` or `~/.claude`.

## Live reproduction: fresh-DB migration fail-open (CONFIRMED)

Command:
```
export LLM_ROUTER_HOME=$(mktemp -d)
.venv/bin/python - <<'EOF'
import asyncio, sys
sys.path.insert(0, "/Users/yaliandrona/Projects/llm-router/src")
from llm_router.router import route_and_call, TaskType

async def main():
    resp = await route_and_call(
        TaskType.QUERY, "Reply with exactly the single word: PONG",
        model_override="ollama/qwen3.5:latest", max_tokens=20,
    )
    print("MODEL:", resp.model, resp.provider, "COST:", resp.cost_usd)

asyncio.run(main())
EOF
```

Actual stdout/log (first call ever against a brand-new `LLM_ROUTER_HOME`):
```
route_start ... model_chain=qwen3.5:latest ... top_model=ollama/qwen3.5:latest
quality_recorded ... score=0.3
fail_open code=CHZ-FO-COST-MIGRATE-ALTER exc=OperationalError ...   (x4, identical)
routing_decision ... cost_usd=0.0 latency_ms=49802.9 model=ollama/qwen3.5:latest provider=ollama
MODEL: ollama/qwen3.5:latest ollama COST: 0.0
```

**What happened, traced to code**: `cost.py:_get_db()` re-runs the ENTIRE
~28-migration-group list on every call that touches cost logging (not once at
process start — `_get_db()` is called from `log_usage`, and nothing caches the
"already migrated" state across calls within this probe). Each statement goes
through `_safe_migrate()`, which tries to recognize `ALTER TABLE t ADD COLUMN c`
via regex and skip it if the column already exists; anything that doesn't match
that exact shape falls to a bare `try/except Exception` and, on failure, calls
`failopen.record("CHZ-FO-COST-MIGRATE-ALTER", exc)` — logged, swallowed, routing
continues. On a genuinely fresh DB, four of those non-standard-shaped migration
statements raised `OperationalError` and were silently absorbed. The end user
gets a correct-looking completion (`MODEL:`, `COST:` both printed fine); nothing
in the visible response indicates that a fourth of the schema-migration list is
currently failing on every single request against a fresh install. The evidence
of failure exists only in `fail_open.jsonl` inside `LLM_ROUTER_HOME`, which no
default path surfaces to the user.

**Severity note**: this is the exact failure MODE the code's own comment warns
about ("a spike means schema migration is silently not happening, and every
later query then fails on a missing column somewhere far from here") — except
this audit observed the spike, not a hypothetical. Whether the four specific
migrations are cosmetically harmless or eventually break a query that assumes
their column exists was not further traced in the time available; flagged for
the cost/telemetry specialist to identify by statement.

**Isolation side-finding (CONFIRMED)**: the same run populated
`$LLM_ROUTER_HOME/{usage.db, receipts.db, session_spend.json, savings_log.jsonl,
fail_open.jsonl, routing_quality.jsonl, knowledge/, projects/}` — i.e. this
process's state stayed inside the sandbox for every file this probe touched.

---

## Path 1 — Claude Code hook (`UserPromptSubmit`)

1. User types a prompt in Claude Code. Claude Code's own hook runner invokes
   `hooks/auto-route.py` (installed by `install_hooks.py` into the user's
   `settings.json`) with the prompt on stdin.
2. **Classification chain** (per the file's own docstring, confirmed by function
   layout, not taken on faith): skip-patterns → heuristic scoring → local Ollama
   model → cheap API model → weak heuristic → `llm_route` fallback.
3. Hook writes `~/.llm-router/last_classification_<CLAUDE_SESSION_ID>.json`
   containing `{complexity, route_id, session_id, issued_at}`.
4. Hook's ONLY output channel back to the running Claude Code turn is
   `hookSpecificOutput.additionalContext` (confirmed: `additionalContext` is the
   sole field renamed-into by `_normalize_context_key`, no `permissionDecision`
   or tool-substitution field was found in the file). **This is advisory
   context, not enforcement** — Claude (the calling model) reads it as text and
   decides what to do; nothing here can force a tool call.
5. Separately, `hooks/bash-compress.py` (or an equivalent PreToolUse hook) was
   observed live during this audit intercepting a plain `find` Bash call and
   replacing its output with a compression banner
   (`[llm-router] This command was run locally by the router and its output
   compressed...`) — a second, independent hook mechanism from the
   classification hook, operating on tool *output* rather than the prompt.
   **CONFIRMED by direct observation in this session's own transcript.**

**Information-loss finding**: the hint bridge (`last_classification_<sid>.json`)
has a **120-second freshness window** (`max_age_sec=120.0` in both
`_read_hook_complexity_hint` and `_read_hook_route_directive`, `tools/text.py`).
An agentic turn that runs tools for >120s before its first MCP `llm()` call
(entirely plausible for `llm_act`/long tool-use loops) gets **zero** benefit
from the hook's classification — the router silently falls back to a raw prompt-
length heuristic on whatever prompt the MCP tool call was given, which by then
has typically been wrapped/elaborated by the calling agent and no longer
resembles the user's original, already-classified ask.

---

## Path 2 — MCP tool `llm(prompt, task="auto", tier="balanced", context=…)`

1. `tools/consolidated.py:llm()` dispatches by `task` to one of
   `llm_query/llm_analyze/llm_code/llm_research/llm_generate` in `tools/text.py`,
   mapping `tier` → `complexity` via `_TIER_TO_COMPLEXITY`
   (`fast→simple, balanced→moderate, best→complex`).
2. **No independent classification happens here** — complexity comes from the
   caller-supplied `tier`, optionally overridden by the hook's 120s-fresh hint
   (`_effective_complexity`). If neither is present, `route_and_call` falls back
   to a prompt-length heuristic deep in `router.py`.
3. `route_and_call()` (`router.py:3654`): resolves identity
   (`current_identity()`), builds the model chain from the routing profile,
   checks budget, checks `semantic_cache`, dispatches via
   `_dispatch_model_loop` → `_call_text`.
4. `_call_text()` (`router.py:4819`) builds `messages` as:
   `[system_prompt or caveman-mode system] + build_context_messages(caller_context
   or prompt) + [user: prompt]`. **`context` IS forwarded** — this was verified
   by reading the actual message-assembly code, not the docstring: `caller_context`
   flows unmodified from the tool signature into `build_context_messages`, whose
   output is spliced into the messages list actually sent to `providers.call_llm`.
   `context` is genuinely load-bearing here, not decorative.
5. On success: `_cache_result` (semantic cache store), `_record_quality`,
   `cost.log_usage` (→ the migration/fail-open path above), `prompt_capture.capture`
   (real call, inert unless `LLM_ROUTER_GROUND_TRUTH=1`), `RouteLedgerRecord`
   written to the audit ledger.
6. Response passes through `_apply_response_router` and `_format_response`
   before returning to the MCP caller as plain text.

**Information-loss finding**: `llm_query`'s `context` parameter is documented as
"Optional conversation context to help the model understand the broader task" —
true, but it competes for a shared token budget
(`config.context_max_tokens`, default 1500) with retrieved session history and
persistent OKF context inside `build_context_messages`. A caller who passes a
large `context` string has no visibility into how much of it survives truncation
against that shared budget; this audit did not trace `build_context_messages`'s
internal truncation/priority order (out of time budget) — flagged as an open
question for the context/OKF specialist, not resolved here.

---

## Path 3 — Gateway `/v1/chat/completions`, `/v1/responses`, `/v1/messages`

1. Request lands in `gateway.py`. Pydantic models (`_OAIRequest`,
   `_ResponsesRequest`, `_AnthropicRequest`) parse the wire format.
   `_OAIRequest`/`_AnthropicRequest` **declare** `tools`/`tool_choice` fields
   specifically so they are visible to `_refuse_tools_if_present` — if present,
   the endpoint returns HTTP 400 with an explicit refusal message rather than
   silently dropping them (confirmed fix for the documented H-03 finding; this
   audit re-derived it from the current code, not from the comment).
2. `classify_text` is computed as **the last user-role message only**
   (`_latest_user_turn` / `_latest_user_turn_from_responses_input`) — explicitly
   excludes system prompt and prior assistant turns from the complexity
   heuristic. `prompt` sent to the model keeps the full transcript, including
   system prompt (prepended as `"system: {req.system}\n..."` for `/v1/messages`).
   This is the confirmed-current T-03 fix: classification input and model input
   are deliberately different strings, traced at the call sites in `_route()`.
3. `_route()` → `route_payload_async()` (`route_server.py`) → same
   `route_and_call()` as the MCP path. Gateway traffic gets the same budget caps,
   semantic cache, cost logging as every other entry point (per `_route`'s own
   docstring, verified against the shared call target).
4. Response is reshaped per wire format (`_RoutedResult` adapter) and returned.

**Information-loss finding, confirmed structural, not yet a bug**: the gateway
has **no tool-call output channel** at all — `route_and_call`/`_call_text` return
plain text; there is no code path in `router.py`'s dispatch loop that returns a
`tool_use`/`function_call` block. An OpenAI- or Anthropic-compatible client
integrated against this gateway for agentic/function-calling use is refused
outright (400) rather than degraded — a deliberate, confirmed design choice
(see `_TOOLS_UNSUPPORTED` message and the code comment explaining why refusing
beats half-implementing). Net effect: **the gateway can only ever serve
plain-text completion clients**, permanently, by design — not a gap to be
closed later without a new subsystem.

**Reachability caveat**: this endpoint set is real and wired to the router, but
per `audit/01_ACTUAL_ARCHITECTURE.md`, the FastAPI app that serves it
(`gateway.py`'s `uvicorn.run`) has no console-script entry point and is not
started by `llm-router serve`, `install_hooks`, `onboard`, or `quickstart`.
Everything in this section is real code on a real path — but nothing in the
packaged product currently puts that path in front of an HTTP port for a typical
user.

---

## Path 4 — Native `/route`

`gateway.py:route()` (`POST /route`) passes `payload` (with `project_root`
resolved from header or body) straight to `route_payload_async` with **no
classify_text/prompt split** — the caller's `prompt` field IS both what gets
classified and what gets sent, by design (per `_route`'s docstring: "there, the
caller's prompt IS the ask"). This is the same underlying function as `/route`'s
zero-dependency sibling, `route_server.py` (not independently traced further in
this audit; same module backs both).

---

## Path 5 — CLI

`cli.py:main()` dispatches to ~40 lazily-imported `commands/*.py` modules by
subcommand string. This audit traced the dispatch table (confirmed real, no
placeholder branches observed) but did **not** runtime-execute any individual
CLI subcommand beyond the MCP-level `route_and_call` probe above. Each
subcommand's own reachability to real state (SQLite, JSONL ledgers, dashboards)
is unverified here — recommend a dedicated CLI-surface pass.

---

## Cross-cutting information-loss summary

| Layer boundary | What the sender has | What the receiver gets | Verified |
|---|---|---|---|
| Claude Code turn → hook | full prompt + tool history | hook only sees the prompt text handed to it on stdin (UserPromptSubmit payload) | code read |
| Hook → MCP tool call (same turn) | hook's classification + minted route_id | only if `CLAUDE_SESSION_ID` matches AND call happens within 120s; else silently dropped to heuristic default | code read (`_read_hook_complexity_hint`/`_read_hook_route_directive`) |
| MCP `context` param → model | caller's full context string | shares a token budget with retrieved history/OKF; truncation order not traced | partially verified |
| Gateway request → classifier | full transcript incl. system prompt | last user turn only (deliberate, confirmed correct per T-03) | code read |
| Gateway request → model | — | full transcript incl. system prompt (deliberately NOT truncated) | code read |
| Any caller → gateway with `tools` | tool/function definitions | HTTP 400, refused, not silently dropped | code read |
| `route_and_call` success → Ground Truth store | prompt, route_id, chosen_model, classification_method | written only if `LLM_ROUTER_GROUND_TRUTH=1`; call site is live either way | code read |
| `route_and_call` success → cost/usage DB | usage row | written, but preceded by up to 4 silently-swallowed migration errors per call on a fresh DB | **live-reproduced** |
