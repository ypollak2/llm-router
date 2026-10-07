# Per-call proxy (opt-in)

`llm-router proxy` sits at `ANTHROPIC_BASE_URL` for **one** Claude Code
session. Every `/v1/messages` call is passed through to Anthropic unchanged,
except the step classes the router's policy allows, which a non-Claude,
tool-capable model serves in Anthropic format.

It is off by default. Nothing installs it, and nothing changes your settings,
hooks or `~/.claude.json`. It only affects a session you start with the variable
set.

## Enable it for one session

```bash
llm-router proxy                                                          # terminal 1, binds 127.0.0.1:8787
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ENABLE_TOOL_SEARCH=true claude    # terminal 2, this session only
```

Unset the variables, or close that terminal, to stop using it. Other sessions
are not affected. `llm-router proxy`'s own startup line prints this exact
command with your actual host/port (`server.enable_hint`) — copy it rather
than retyping it, so the `ENABLE_TOOL_SEARCH` half is never dropped.

### Cost parity: always set `ENABLE_TOOL_SEARCH=true`

Measured 2026-09-28, same 3 fixture tasks, `claude -p` with and without the
proxy: the proxied arm cost **3.5x** the no-proxy baseline ($2.44 vs $0.69)
for the same tasks passing the same way. Root cause is in Claude Code, not
this proxy: Claude Code disables its Tool Search / dynamic-tool-loading
feature — which keeps most MCP and skill tool schemas out of the request
until a step actually needs them — the instant `ANTHROPIC_BASE_URL` is not a
first-party Anthropic host. It cannot tell that this proxy passes every
`/v1/messages` body through byte-for-byte (`forward()` never inspects or
rewrites it) when it isn't serving the step itself. With Tool Search off,
every request inlines every deferred tool's full schema, which inflates the
cacheable prefix (`Dynamic tool loading: 0/134 deferred tools included`
becomes `134/134` in `claude --debug api` output) and, on a cache miss, is
billed at the 1-hour cache-write rate — 2x the input rate. In a debug capture,
the first Anthropic call of a proxied session went from ~50k tokens
(input + cache write + cache read) with Tool Search on, to ~128k with it off.

`ENABLE_TOOL_SEARCH=true` (or `auto` / `auto:N`) restores first-party
behaviour. It is safe with this proxy specifically because `forward()` never
touches the tools array: the `tool_reference` blocks Claude Code emits with
Tool Search on reach Anthropic unchanged, exactly as they would without the
proxy. `llm-router proxy`'s startup banner and `--help` both print the full
command with this set; if you started a session without it, cost and cache
behaviour will not match a no-proxy baseline.

## What gets routed

| Step class | Shape | Default |
|---|---|---|
| `continuation` | The newest user turn holds only `tool_result` blocks: "given this tool output, what next?" The first call of a task is never a continuation. | on |

For an eligible step, the router's own policy decides:
`classify_signals(GATEWAY_POLICY)` runs on the original request plus the newest
tool output, then `router._build_and_filter_chain` builds the chain. The step is
served by the first tool-capable entry in that chain (today `ollama/*`). If the
chain has no tool-capable entry, the call stays on Claude.

Calls with a forced `tool_choice`, images or documents in the newest turn, or no
client tools are never routed.

## Safety

- **Validation.** A served reply is used only if every tool call names a tool
  from the request, its arguments are a JSON object, required keys are present,
  and primitive types and enums match. Otherwise the original request goes to
  Anthropic.
- **Hedge and latency budget.** If the local model's first token has not
  arrived within 8 s (`--hedge-s`), the proxy falls back to Anthropic. The
  whole step also has a budget (`--step-budget-s`, default 30 s). A reply cut
  off at the output cap is never served.
- **Errors.** Any backend or policy error falls back to Anthropic. Every
  fallback is recorded with its reason.
- **No cache.** Served replies are never cached. This path has no semantic cache.
- **Credentials.** `authorization` / `x-api-key` go to Anthropic unchanged and
  are never written anywhere. The ledger records only the kind
  (`oauth` / `api_key`). The upstream can only be `https://api.anthropic.com`
  or a loopback test double.
- **Mixed histories.** Served turns carry no thinking block. If Anthropic
  rejects a later request over thinking blocks, the proxy retries it once with
  `thinking` off and `clear_thinking` context edits removed. The row is flagged
  `thinking_retry`.
- **Streaming.** Pass-through calls stream as they arrive. A served reply is
  sent as a complete SSE sequence after it passes validation, because a
  streamed tool call cannot be taken back.
- **Binding.** Loopback only unless you opt in explicitly. Browser cross-origin
  requests are refused.
- **Loop guard.** A live trial (2026-09-28) found one session with 44
  consecutive served replies re-issuing the same `Read` of the same file —
  correct in the end, but 195s and an inflated routed share/avoided-cost. Per
  session, in-memory only (`llm_router.proxy.loop_guard`): a served tool call
  that exactly repeats one of the session's last `LLM_ROUTER_PROXY_LOOP_REPEAT_WINDOW`
  served tool calls, or a session that has served `LLM_ROUTER_PROXY_LOOP_MAX_CONSECUTIVE`
  steps in a row (the cap is checked BEFORE the backend is tried), is handed to
  Anthropic instead, recorded as `reason: "loop_guard"` with a `detail` naming
  the repeated call or the cap. Any non-served step for that session (a real
  Anthropic call, or a policy decision to keep the step on Anthropic) clears
  the streak.
- **Backend health.** On 2026-09-30 the dedicated Ollama server's Metal
  backend ran out of GPU memory under swap pressure (`command buffer 0 failed
  with status 5`, `kIOGPUCommandBufferCallbackErrorOutOfMemory`). Until it was
  restarted, every step got an empty reply in ~0.1 s (29/29 empties in that
  A/B came from those windows; 0/24 while healthy), and every one fell back.
  Per proxy process, per serving model (`llm_router.proxy.backend_health`):
  `LLM_ROUTER_PROXY_BACKEND_FAIL_N` consecutive empty or sub-second invalid
  replies, or one crash signature in an Ollama error, stop local serving for
  `LLM_ROUTER_PROXY_BACKEND_COOLDOWN_S`. Those steps go to Anthropic without an
  attempt, recorded as `reason: "backend_unhealthy"`, and one warning goes to
  stderr. After the cooldown, a one-token probe (same `num_ctx`, so the model
  is not reloaded) decides whether serving resumes. Timeouts neither count
  nor reset the streak.
- **Step-budget cancel.** A step that misses the budget closes its Ollama
  connection and Ollama cancels the request. The Metal faults were checked
  against this: in the 2026-09-30 logs 2 of 12 budget cancels were followed by
  a fault, and both cancelled requests had processed only 650 of ~4.4k prompt
  tokens in 30 s, so the GPU was already starved; the 2026-09-28 fault began
  mid-prefill with no cancel before it. The fault is GPU memory, not the
  cancel, so the cancel is unchanged. Letting a cancelled request finish would
  keep a starved GPU busy and queue the next step behind it.

## Settings

| Flag | Env var | Default |
|---|---|---|
| `--port` | `LLM_ROUTER_PROXY_PORT` | `8787` |
| `--steps` | `LLM_ROUTER_PROXY_STEPS` | `continuation` (`off` = pass-through only) |
| `--step-budget-s` | `LLM_ROUTER_PROXY_STEP_BUDGET_S` | `30` |
| `--hedge-s` | `LLM_ROUTER_PROXY_HEDGE_S` | `8` (first-token deadline; `off` disables) |
| `--model` | `LLM_ROUTER_PROXY_MODEL` | from policy. A pin changes *which* tool-capable model serves, never *whether* a step is routed. |
| `--trim` | `LLM_ROUTER_PROXY_TRIM` | `fast`. Comma list of named trims or `module:function`. |
| `--num-ctx` | `LLM_ROUTER_PROXY_NUM_CTX` | per model, from `llm_router.local_models.NUM_CTX` (`qwen3.5:latest` and `qwen3.8:latest` 131072, `llmr-edit` 16384, `llmr-classifier` 4096; 32768 for `qwen3-coder:30b`, `qwen3.6:35b-a3b-coding` and any unlisted model). Applies to the pinned `--model`, or to each model the policy picks when none is pinned. A value you set applies to every model. |
| `--keep-alive` | (none) | `-1`: the model stays loaded until Ollama restarts |
| `--no-warm-up` | (none) | a warm-up call runs in the background at start |
| `--ollama-url` | (none) | the router's configured Ollama (e.g. a dedicated server, below) |
| (none) | `LLM_ROUTER_PROXY_UPSTREAM` | `https://api.anthropic.com` (only loopback overrides are accepted) |
| `--loop-max-consecutive` | `LLM_ROUTER_PROXY_LOOP_MAX_CONSECUTIVE` | `8` (served-in-a-row per session before the next step is forced to Anthropic; `0` disables) |
| `--loop-repeat-window` | `LLM_ROUTER_PROXY_LOOP_REPEAT_WINDOW` | `3` (recent served tool calls a new one is checked against for an exact repeat; `0` disables) |
| `--tiers` | `LLM_ROUTER_PROXY_TIERS` | `off`. `on` enables the per-turn Claude-tier rewrite; `conversation` enables the conversation-level rewrite (below). |
| `--tier-policy` | `LLM_ROUTER_PROXY_TIER_POLICY` | the bundled `proxy/claude_tiers.yaml` |
| `--backend-fail-n` | `LLM_ROUTER_PROXY_BACKEND_FAIL_N` | `3` (consecutive empty or sub-second invalid replies before local serving pauses; a crash signature pauses at once; `0` disables) |
| `--backend-cooldown-s` | `LLM_ROUTER_PROXY_BACKEND_COOLDOWN_S` | `60` (pause before a one-token probe checks the backend) |

The model, the trims (`proxy/backends.py: TRIMS`) and the backends
(`BACKENDS`) are plug points. A measured speed lever can be added as a named trim
or a `module:function` without changing the server.

### Defaults come from the local-speed spike

`docs/spikes/local-speed-2026-09-28.md` measured 24 replayed continuation calls
with `qwen3-coder:30b`: median 2.19 s, p90 3.59 s, 0/24 validation failures.
That run used a dedicated tuned server. The proxy's defaults follow it:

- **Trim `fast`.** A 366-character condensed system prompt, and only the tools
  the step class needs (`Read`, `Edit`, `Write`, `Bash`), with names and schemas
  unchanged. History is capped to the first user turn plus the last exchange.
  That is about 3-5k prompt tokens instead of about 21k.
- **Output cap.** `num_predict` is 200 after `Read`/`Bash`/no tool, and 700
  otherwise.
- **Warm.** `keep_alive: -1` is sent on every call, plus one warm-up call at
  start. A cold load took 8-110 s in that spike.
- **Hedge.** 8 s to the first token.

### Optional: a dedicated tuned Ollama server (not auto-configured)

The spike's numbers came from a hand-run server. Ollama.app discards these
settings, so the proxy never starts or configures one for you:

```bash
OLLAMA_HOST=127.0.0.1:11500 OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 \
OLLAMA_KEEP_ALIVE=-1 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 \
OLLAMA_CONTEXT_LENGTH=32768 ollama serve
llm-router proxy --ollama-url http://127.0.0.1:11500
```

Do not load the same 20 GB model in two servers at once on a 48 GB machine. In
the spike, that produced empty or corrupted replies and a stuck runner.

## Local agent: capability gating + compaction (`--local-agent`, off by default)

`llm-router proxy --local-agent` (or `LLM_ROUTER_LOCAL_AGENT=on`) switches the
serving path to `llm_router.local_agent`:

- **Capability** (`local_agent/capability.py`). Before a local call, a step
  must be an eligible continuation, the policy must name a tool-capable model,
  the task type must be one of `research`/`analyze`/`code`/`query`, the
  session's ask must not carry a multi-file-write signal (both gates are the
  Codex sub-agent hook's own values), and the quality breaker for lever
  `proxy` must be closed. Each refusal is a ledger `reason`.
- **Compaction** (`local_agent/compact.py`). Each tool's name and description
  start is embedded once with `nomic-embed-text` (cached in
  `~/.llm-router/local_agent_tool_embeddings.json`). A step keeps the top-K
  tools for (original ask + newest tool output), the tools the session already
  used, and the edit tools; names and schemas are unchanged. History becomes
  the user's ask (reminder blocks removed), the environment lines, the last N
  tool exchanges and any older `Read` of a file they mention, at most ~5k
  prompt tokens. The embedding model is called on the same Ollama as the
  serving model, so a dedicated server needs `OLLAMA_MAX_LOADED_MODELS=2`
  (with `1`, every embed call evicts the 20 GB model).
- **Edits.** An `Edit`/`Write`/`MultiEdit`/`NotebookEdit` reply from the raw
  tool-call loop is **never served, with or without `--local-agent`** (0/20
  edits passed through that loop on fixtures). With `--local-agent`, a
  single-file `Edit` whose target the session has read and that has no
  uncommitted changes goes through `llm_router.edit`'s validated protocol
  (JSON instructions, exact-once match against the file on disk, syntax gate,
  up to 3 attempts with the rejection fed back) and is served as `Edit` calls;
  anything else goes to Claude. `LLM_ROUTER_LOCAL_AGENT_EDIT=claude` sends
  every edit-shaped step to Claude.

| Env var | Default |
|---|---|
| `LLM_ROUTER_LOCAL_AGENT` | off |
| `LLM_ROUTER_LOCAL_AGENT_TOP_K` | `6` retrieved tools (plus used and edit tools) |
| `LLM_ROUTER_LOCAL_AGENT_PROMPT_BUDGET` | `5000` estimated prompt tokens |
| `LLM_ROUTER_LOCAL_AGENT_KEEP_RESULTS` | `3` most recent tool exchanges |
| `LLM_ROUTER_LOCAL_AGENT_TOOL_DESC_CHARS` | `600` chars of each offered tool's description |
| `LLM_ROUTER_LOCAL_AGENT_EMBED_MODEL` | `nomic-embed-text` |
| `LLM_ROUTER_LOCAL_AGENT_EDIT` | `protocol` (or `claude`) |

**Measured 2026-09-30 (paired A/B, 15 pairs, the 6 Bash-heavy fixture tasks of
the 2026-09-28 run, `qwen3-coder:30b` on a dedicated server, production
defaults): no saving, so it stays off by default.** Compacted requests were
4.1-4.9k real prompt tokens. While the Ollama backend was healthy, 9 of 24
local attempts were served and none came back empty; 12 missed the 30 s step
budget. The router policy kept 46 of 99 steps on Claude. Realized Claude cost
(off − on, proxy ledger): −1.1% per task, 95% CI −6.4% to +4.3%; pass rate
15/15 both arms; wall-clock 1.32x median. Twice the Metal backend faulted
(`command buffer ... failed with status 5`) and every later request returned
an empty reply in ~0.1 s until the server was restarted: all 29 empties in the
run came from those windows, and the 2026-09-28 run's "18/18 empty" server log
shows the same fault. Restart the dedicated server if `proxy stats` shows a run
of fast `empty response` fallbacks. Report:
`~/.rsi/research/llm-router-cursor-parity/p3-compaction-ab.md`.

## Serve mode: Claude Code on a local model (opt-in, `--serve local-agent`)

Off by default (`--serve off`, env `LLM_ROUTER_PROXY_LOCAL_AGENT_MODE`), and with it off the proxy is
byte-identical to what it was before the mode existed: `tests/test_proxy_off_golden.py` replays 19
request scripts plus the startup banner against a golden recorded from `main` before the mode was written.

```
ollama serve   # OLLAMA_NUM_PARALLEL=1 OLLAMA_CONTEXT_LENGTH=32768, hand-run (Ollama.app cannot set the first)
llm-router proxy --serve local-agent --model ollama/qwen3.6:35b-a3b-coding --ollama-url http://127.0.0.1:11434
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ENABLE_TOOL_SEARCH=true claude
```

Why it exists: the 2026-10-04 route probes found that the stock proxy served 0 of 9 Claude Code steps
locally (the first call of every turn always went to Anthropic, the routing policy could keep a step on
Claude, and an Edit/Write reply was never served), and that the default `fast` trim hides Skill, Agent, MCP
and ToolSearch from the local model. This mode makes the three in-memory patches of that experiment real, for
this mode only:

| Patch | In this mode |
|---|---|
| P1 step class | every tool-carrying main-loop call is eligible (`local_mode.is_agent_turn`), not only tool_result continuations |
| P2 policy | `choose_model` is not consulted; the pinned `--model` serves |
| P3 edits | an Edit/Write reply is served: through the validated edit protocol when the file qualifies, else raw, and **every served edit is checked after it is applied** (at the next step, against the file on disk: the new text is there, a Write holds what was written, the file still parses; a failure is written into the tool result the local model sees, never forwarded to Anthropic) |

**Pinning.** A conversation is decided at its first call and stays there. A conversation the proxy did not see
start (a restart, `/compact`) is pinned to Claude and never moved local. A local step that fails moves the
conversation to Claude once, with the reason in the ledger.

**Eligibility** (`decide_local`, a rule stub the resolver will replace): no media anywhere in the conversation;
estimated prompt <= 25,000 tokens (a digit-aware estimate fitted to real Ollama counts, within 1.02-1.21x of them on 7 payloads;
`local_context_guard`'s chars/3.04 is 1.4x high on JSON tool schemas and 0.90x low on a 450-line log, so it stays the 32k window
backstop in the backend instead); no media anywhere; backend healthy; quality breaker closed.

**Never silent.** Every step not served locally has a ledger `reason` and `egress: true`, and prints one line on
stderr saying it is being sent to Anthropic. Media and over-cap prompts are escalated with that reason, never
replaced by a placeholder. The startup banner states the mode and the egress rule.

**Kill switch.** `touch <state>/local_agent_kill` (the path is in the banner) sends every step to Anthropic from
the next request, no restart; remove the file to resume.

**Preflight.** The proxy refuses to start (message on stderr, exit 2) unless: `--model ollama/<tag>` is given
and resident with `num_ctx >= 32768` (it is loaded first if absent); the Ollama server's own
`OLLAMA_NUM_PARALLEL` is 1 (read from its process environment, so only a loopback server can be checked) and
no runner has `-np` other than 1; `--trim none` (the default in this mode); `--tiers off`; compaction off; the
overflow guard passes a self-test; the ledger and kill-switch directories are writable. In this mode the first-token
hedge defaults to off and the step budget to 120 s (a conversation's first call evaluates a ~12k-token prompt).

Not in this mode (later redesign PRs): thinking passthrough, images served locally, `--no-egress`, the ToolSearch
loop-guard fix, AskUserQuestion repair, sub-agent inheritance.

## Shadow mode: measure the local model against Claude (opt-in, `--shadow on`)

Off by default (`LLM_ROUTER_PROXY_LOCAL_SHADOW=on` is the env form). Needs `--model ollama/<tag>` and
`--serve off`: shadow never serves. Every step is answered by Anthropic exactly as without the proxy;
the local model answers a deep copy of each agent step in parallel and one `local_shadow` record per step
goes to `proxy_local_shadow.jsonl` in the state dir (not `proxy_calls.jsonl`, so NS, D1 and D2 never read it).

- Latency isolation: the local job is a detached task. It is dropped (`dropped_claude_first`) the moment
  Claude's reply is complete, and has its own budget (`--shadow-budget-s`, default 20 s, `budget_exceeded`).
- One local job at a time; a step that arrives while one runs is recorded as `skipped_busy`.
- Local reads no file and runs no tool: no repo-knowledge attach, no post-apply check.
- A record holds: `step_id`, `agree` (same tool names in the same order; two text-only replies agree),
  `args_equal` (key order and surrounding whitespace ignored; null when names differ), `local_latency_s`,
  `schema_valid`, `fallback_reason`. Reason codes and numbers only: no prompt, tool name, argument or reply.
- `llm-router kpi` shows one informational line, `local shadow (proxy): ...`, next to `local (shadow)`.

## Metrics

Each call writes one row to `~/.llm-router/proxy_calls.jsonl` with shape,
decision, timing and token counts. Rows never contain content or headers.

```bash
llm-router proxy stats [--days N] [--json]
```

The metrics are reported separately, because they diverge:

- **calls per session**: `n` sessions, calls per session (median and max) — a
  runaway loop shows up here as one session far above the rest;
- **routed share**: calls served by non-Claude over all calls, and a second
  figure, **routed share excl. loop-guard repeats**, that drops calls the loop
  guard flagged (`reason: "loop_guard"`) from both sides of the fraction, so a
  runaway session cannot inflate it;
- **fallbacks**: count, and why each call was not served (`loop_guard` is one
  reason among the others);
- **latency**: served vs Anthropic medians, and the latency added before
  fallbacks;
- **Anthropic tokens and est. cost**, split into input, cache read, cache write
  5m / 1h and output. Also a **net avoided** cost with its n — priced per
  served step as the cached-prefix read plus its own reply, minus the extra
  cache-write the next real Anthropic call paid to re-establish its cache
  across the served gap. It is computed only over calls that were actually
  served, so a loop-guard-flagged call is never counted as a saving, and it
  **may be negative** — and the cache writes after a served turn vs a clean
  history.

A high routed share is not a saving. In the spike, routing half the calls saved
about a fifth of the Anthropic cost, because cached re-reads are already cheap.

`est_cost_usd` reconciles against Claude Code's own `total_cost_usd` (from
`claude -p ... --output-format json`) only when nothing was served locally:
a served step's reply reports Ollama's own prompt/eval token counts as its
`usage` (`translate.py`), and Claude Code's own cost tracker bills those as
Sonnet-5 tokens even though Anthropic never saw the call. On a session with
served steps, expect `total_cost_usd` to run ahead of `est_cost_usd` by
roughly that much — it is Claude Code overcounting a reply it received, not
the ledger undercounting one it sent.

`llm-router northstar` joins served rows to transcript turns by `message.id`.
Those turns stay `claude_main_call` units, with lever `proxy`, and are judged
`used` / `redo` / `unknown` from what happened to their tool calls.

## Claude-tier rewrite (opt-in, `--tiers on` or `--tiers conversation`)

A forwarded call can be sent to a cheaper Claude tier by rewriting
`body["model"]`. All Claude tiers share one API schema, so client tools stay
native. The Phase 0.4 probe (2026-09-29, n=5 calls per model) found Max quota
is one shared weekly pool that drains in proportion to per-call cost. Moving a
call down a tier therefore drains the pool more slowly, **but only if the
conversation is not paying to re-write its prompt cache on the new tier**.

- **Decision** (`proxy/tiers.py`): the router's own classifier (`choose_model`,
  i.e. `classify_signals(GATEWAY_POLICY)` plus `router._build_and_filter_chain`)
  runs over the newest human prompt. `ClaudeTierPolicy` then maps
  (task_type, complexity) to a tier. Model ids come from the YAML file, never
  from code. A future Phase 2 kNN scorer replaces only this classifier call
  (`classify=` on `decide()`, or a `ClaudeTierPolicy(..., classify=...)`
  constructor argument) -- no `server.py` callsite changes.
- **Never downgraded:** a requested model that is not a configured tier; ids in
  `pinned_models`; calls with no client tools; a conversation whose transcript
  shows `/model`. A call is never moved above the tier it requested
  (`allow_upgrade: false`), and never to a tier that rejects its
  `thinking.type` or `output_config.effort`. Claude Code sends adaptive
  thinking plus effort, which Haiku 4.5 takes neither of, so main-loop calls
  stay on Sonnet or above.
- **The first call, two ways:**
  - `--tiers on` (per-turn, PR #215): the conversation's first call is exempt
    (unchanged) -- rewriting it would re-write a prompt cache that does not
    exist yet.
  - `--tiers conversation` (Phase 1.2b): the first call is classified and
    rewritten like any other call. This is the point of conversation mode --
    see "Key finding (1.2)" below for why a mid-task switch is the wrong place
    to make this decision, and the conversation's start is the right one.
- **Cache stickiness** (`proxy/cache_cost.py`): a conversation stays on its
  last model. In `on` mode it moves when the complexity class changes in
  either direction, or at a cold point (no call for `cold_gap_s`). In
  `conversation` mode it moves only when the class change is an
  **escalation** (the new class ranks a higher tier than the one the
  conversation is already on) or at a cold point -- a class that would rank
  the same or a cheaper tier never pulls a committed conversation back down.
  Each move is recorded with an estimated cache re-write cost.
- **Fail-safe:** an error in the decision forwards the call unchanged
  (`tier_reason: decision_error`, with the scrubbed error text). If Anthropic
  refuses a rewritten call with a 4xx (other than 401 or 413), the client's own
  bytes are sent once more, unchanged (`tier_retry`).
- **Stats:** `llm-router proxy stats` adds the served-model mix, the reasons,
  the switch rate, and the Anthropic cost against a counterfactual in which
  every call ran on its requested model. That comparison is an **estimate**.
  The validated number is a paired A/B. Every row also carries `tier_mode`
  (`off` / `on` / `conversation`), so a ledger spanning both A/B arms can be
  split without relying on the port a session ran on.

Measured so far (per-turn mode, `on`): the live smoke of 2026-09-29 (3 golden
fixture tasks, real `claude -p --model opus`, 23 calls) ran with
`switch_after_first_call: true` (execute on Sonnet after an Opus first call).
All 3 tasks passed. Anthropic replied with the rewritten model on 8 of 8
rewritten calls. The estimated cost was **$1.07 against $0.78 on all-Opus**, a
net loss: each of the 3 switches re-wrote 25-34k prefix tokens on Sonnet, and
Opus 5.5 and Sonnet 5.5 read cache at the same rate. That handoff is therefore
off by default. With the default policy, a single-prompt task stays on its
first-call model (replay: 22/22 calls on Opus, 0 switches), so the rewrite
only takes effect after a cold gap or a complexity change.

### Key finding (1.2): per-turn switching does not pay; conversation-level might

Per-turn tier switching loses money under prompt caching: a switch mid-task
re-writes the whole cached prefix (25-34k tokens in the smoke above), and
Opus 5.5 and Sonnet 5.5 read cache at the SAME rate ($0.20/M), so only output
and genuinely new content get cheaper after a switch -- a few tenths of a
cent a call, never enough to earn back a 3-6 call task's re-write. The
conclusion was not "tiering doesn't work," it was "tiering pays only when the
tier is chosen once, at the conversation's start" -- which is what
`--tiers conversation` (Phase 1.2b) does: classify the first human prompt,
pick the tier for the whole conversation, and hold it via stickiness,
escalating only at a cold point or a clear rise in complexity. See
`~/.rsi/research/llm-router-cursor-parity/p12b-conversation-tiers-ab.md` for
the paired A/B that gates making this the default.

## Cost accounting: real Anthropic spend (`proxy.cost_accounting`)

Claude Code's own `total_cost_usd` is **phantom** on a proxied session: it prices locally
served tokens at list price (2026-10-03: $21.09 over 178 trials against a real spend of
$0). The ledger is the source of truth. Every row carries `served_by` (`local` |
`anthropic`), `anthropic_usage` (the real usage with the cache read/write split; all zeros
when local, `null` when unknown), `anthropic_cost_usd` and `counterfactual_cost_usd` (a
step-level estimate on the requested model; not summed). `proxy_session_cost()` gives per
session the real spend, the estimated avoided amount and the Claude Code figure labelled
`unreliable`. `llm-router kpi` prints O1 as `reconciled:` when at least 50 ledger calls are
complete and consistent, else keeps the `est.` figure.

`python scripts/reconcile_proxy_cost.py [--exclude SID ...]` (read-only) compares the
ledger with transcript totals (sub-agent transcripts included): a fully forwarded session
must match within 3%, a fully local one must carry exactly 0 Anthropic tokens. Calls the
ledger bills that the transcript never records (title/side calls, interrupted turns) show
as `not_in_transcript` and are real spend.

## Benchmark

`scripts/bench_proxy_steps.py` replays the golden fixture tasks through the
proxy. The serving model is real. The upstream is Claude's own replies recorded
by the spike. The script reports tasks passing, extra calls vs baseline and
validation failures, per step class. Its docstring states what it does and does
not measure. The `tiered` arm (`--arms tiered [--tier-policy F]
[--requested-model M]`) runs the tier rewrite with no local serving. It reports
the tier decisions; the oracle replays recorded replies whatever model is
named, so it cannot say how a cheaper tier would have answered.

## Proxy-default: making the proxy the DEFAULT for every session

Everything above is opt-in, one session at a time. `llm-router install
--proxy-default` is the opposite risk profile: it makes this proxy the
`ANTHROPIC_BASE_URL` for **every** Claude Code session on the machine, which
means a dead proxy fails every session's first API call, not just one that
opted in. Owner-approved 2026-09-30 after a trial on real prompts
(`~/.rsi/research/llm-router-cursor-parity/trial-real-prompts.md`, n=6):
72-80% lower cost, every conversation landed on Sonnet (the classifier never
picked Opus), and 1 of 6 routed answers was unacceptable — a moderate,
investigation-heavy first prompt Sonnet answered shallowly. Everything below
exists because of that last finding as much as the first two.

### Install / uninstall

```bash
llm-router install --proxy-default        # on
llm-router install --proxy-default off    # off (same as `llm-router uninstall`)
```

Implemented in `llm_router.proxy_default` (service rendering + health probe)
and `llm_router.commands.proxy_default` (orchestration). The install:

1. writes a supervised service — a macOS LaunchAgent (`KeepAlive`) or a Linux
   systemd user unit (`Restart=on-failure`), running
   `llm-router proxy --steps off --tiers conversation`;
2. **reuses** a proxy already answering on the target port instead of
   installing a second one that would fight it for the port — this is how it
   stays compatible with a proxy the owner already runs by hand
   (`com.ypollak2.llm-router-proxy` on :8787) without assuming that exact
   label;
3. polls the proxy's health (a raw TCP connect — see `proxy_health()`'s own
   docstring for why not an HTTP probe) before doing anything else;
4. only once healthy, backs up `~/.claude/settings.json`
   (`install_hooks._backup_before_overwrite`) and sets `env.ANTHROPIC_BASE_URL`
   / `env.ENABLE_TOOL_SEARCH=true`, recording the WHOLE previous `env` value
   in the install manifest (`install_manifest`, kind `json_key`) so uninstall
   restores it exactly rather than deleting keys blindly;
5. **refuses** — no settings.json write at all — if the proxy never answers.
   Nothing in `~/.claude/settings.json` changes on a refused install.

`llm-router uninstall` (or `install --proxy-default off`) stops and removes
the service, removes the sentinel (`~/.llm-router/proxy_default.json`), and
restores `env` via the manifest replay — all three happen whether or not the
proxy is currently up.

### Fail-safe: what happens when the proxy is down

Because every session depends on it once installed:

- **KeepAlive/`Restart=on-failure`** restarts a crashed process in place — the
  supervisor IS the watchdog. A separate polling watchdog process was
  considered and not built: it would duplicate what the health checks below
  already do on every doctor run, every statusline render and every session
  start, for a failure mode (hung-but-not-crashed) KeepAlive already doesn't
  cover either, and this project's own working agreement asks for the
  smallest thing that works.
- **`llm-router doctor`** has a "Proxy-default" section: reads the sentinel,
  TCP-probes the port, and prints the exact recovery command
  (`launchctl kickstart -k gui/$(id -u)/com.llm_router.proxy` /
  `systemctl --user restart llm_router-proxy`) plus the log path on failure.
- **The statusline** shows `🔌 proxy down:<port>` in red the instant the probe
  fails — gated on the sentinel, so a user who never installed proxy-default
  pays nothing extra here.
- **The SessionStart hook** (`_check_proxy_default_health` in
  `hooks/session-start.py`) warns at the start of every session with the same
  recovery command. It cannot fix the session already starting: **investigated
  2026-09-30 — there is no SessionStart hook mechanism that overrides the base
  URL for the session already in flight.** `settings.json`'s
  `env.ANTHROPIC_BASE_URL` is read by Claude Code before constructing its API
  client, and every hook (including this one) only runs after that. This is
  the same timing `_sync_pxpipe_anthropic_base_url` already documents for its
  own self-heal ("takes effect next session, not this one") — proxy-default's
  hook only warns rather than also rewriting settings.json, because unlike
  pxpipe there is no dynamically-reachable fallback endpoint to switch to;
  the fix genuinely is "go start the proxy."
- **Tested for real, 2026-09-30**: started a real `llm-router proxy` process on
  a test port, confirmed `doctor`/statusline/SessionStart hook all report it
  healthy, killed the process, and confirmed all three immediately and
  consistently report it down with the exact recovery command above (see
  `tests/test_proxy_default_orchestration.py`,
  `tests/test_session_start_proxy_default.py`,
  `tests/test_statusline_proxy_default.py`).

### Escalation to Opus (`proxy/escalation.py`)

Directly motivated by the trial's one unacceptable answer:

- **Explicit: `opus:`.** A prompt starting with `opus:` pins the conversation
  to the Opus tier for the rest of the conversation (via the existing
  stickiness mechanism), bypassing classification. Matches only the literal
  `opus:` prefix — the owner's own `claude:` convention is a different,
  narrower signal (below) and is never touched, stripped, or treated as a
  routing keyword by this check.
- **Automatic.** A contradiction opening a prompt ("no,", "that's wrong",
  "you missed", …), a re-ask starting with `claude:`, or two or more
  consecutive tool calls coming back as errors, escalates the conversation to
  Opus on the call where the signal appears — since the tier decision runs on
  every call, this is "immediately", a strict superset of "at the next cold
  point". Stickiness then holds it on Opus (a correction-signal escalation is
  treated as a class-change event so it cannot be pulled back down by the
  normal "same class, stay sticky" branch — see the `detail is not None`
  block in `ClaudeTierPolicy.decide()`).
- **Logged.** Every escalation's reason (`explicit_opus_pin`, `escalation` +
  `detail` = `contradiction`/`claude_reask`/`tool_failures`) is written to the
  proxy ledger's `tier_reason`/`tier_detail` columns.
- **Never downgraded:**
  - a request naming a model that isn't a configured tier, or a `pinned_models`
    id, or a conversation whose transcript shows `/model` — all pre-existing
    `ClaudeTierPolicy` guarantees, unchanged;
  - a first prompt that is long (≥120 words, `LLM_ROUTER_PROXY_LONG_PROMPT_WORDS`)
    or multi-part (≥3 bullet/numbered items, `LLM_ROUTER_PROXY_LONG_PROMPT_PARTS`)
    skips the rewrite for that call entirely and keeps whatever model Claude
    Code itself requested — a paraphrase of the trial's own failure
    (t7-northstar: a moderate-classified investigation-heavy first prompt) is
    a regression test for this
    (`test_long_first_prompt_never_rewritten` in `tests/test_proxy_escalation.py`).

| Env var | Default |
|---|---|
| `LLM_ROUTER_PROXY_ESCALATION_TOOL_FAIL_N` | `2` consecutive failed tool turns before escalating |
| `LLM_ROUTER_PROXY_LONG_PROMPT_WORDS` | `120` |
| `LLM_ROUTER_PROXY_LONG_PROMPT_PARTS` | `3` |
