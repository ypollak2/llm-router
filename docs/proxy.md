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
| `--num-ctx` | `LLM_ROUTER_PROXY_NUM_CTX` | per model, from `llm_router.local_models.NUM_CTX` (`llmr-edit` 16384, `llmr-classifier` 4096; 32768 for every other model, including qwen3.5/3.8, so the server OLLAMA_CONTEXT_LENGTH is not overridden). Applies to the pinned `--model`, or to each model the policy picks when none is pinned. A value you set applies to every model. |
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
| `--local-gate` | `LLM_ROUTER_PROXY_LOCAL_GATE` | `off`. `on`: a continuation is served locally only on a lane that `local_eligibility.json` marks eligible (section below). Needs `--steps continuation`; not with `--serve local-agent`. |

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
- **Warm.** `keep_alive: 600` (seconds; `--keep-alive -1` pins forever) is sent on every call, plus one warm-up call at
  start. A cold load took 8-110 s in that spike.
- **Hedge.** 8 s to the first token.

### Local gate (`--local-gate on`, default off)

Without it the proxy picks a local model from the routing policy alone and never reads
`local_eligibility.json` (found 2026-10-17: the live proxy forwarded every step,
`routing_off`). With the gate on, a step is served locally only when its lane is qualified.
Gate off is byte-identical to before: no file read, no ledger key.

- **Which steps.** Only `continuation`. The lane comes from the class of the tools the step
  answers: `technical_op` is `tech_op/simple`; after `web` tools, policy task type
  query/research/analyze is `qa/<tier>` and `code` is `code/<tier>` (tier = the policy's
  `simple` or `moderate`). Edit, exec, agent and other tools stay on Claude. An edit-shaped
  local reply is never served.
- **Which model.** The highest-`n` entry of the lane with `eligible: true`, `verdict:
  "eligible"` and `grade: "eligible"` in `{model: {task_type: {tier: lane}}}`, as
  `ollama/<name>`. `--model` does not make an ineligible lane serve.
- **Reason codes** (`local_gate.reason` in the ledger row, beside `lane` and `model`; the first
  failing check forwards): `not_continuation`, `lane_unmapped`, `eligibility_unreadable`
  (missing, empty, unreadable or malformed file; never fails open), `ineligible_lane`,
  `breaker_open` (quality_breaker `techop_local/tech_op`, or `proxy_local/<lane>`; an error
  reading the breaker also forwards; half-open lets one probe per step budget through),
  `promote_gated` (P0.15), `needs_unknown` (capability routing off: what the request needs
  cannot be checked, so it forwards), `capability_excluded` (F14), `no_local_model`, `backend_unhealthy`,
  `pack_over_window`, `gate_error`, and `served_local`. A local failure after the gate passed
  falls back to Claude as before and the row says `local_failed`; a local reply that repeats a recent
  served tool call is dropped by the loop guard and the row says `local_repeat`.
- **Breaker feed.** A served step whose next two tool calls re-issue one of its own tool calls
  (same tool and file path, pattern or command) is one failure unit; two calls without that are
  one success. Units are in `proxy_local_units.jsonl` in the state directory.
- **Turning it on** (owner step, after a lane is eligible): `proxy --port 8797 --steps
  continuation --local-gate on --tiers conversation --tier-policy ~/.llm-router/claude_tiers.yaml`
  or `LLM_ROUTER_PROXY_STEPS=continuation LLM_ROUTER_PROXY_LOCAL_GATE=on`, then restart the
  proxy service. Keep the Opus pin and do not combine with `--serve local-agent`.

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

## Classifier shadow: log the local LLM's verdict next to the rules' (`LLM_ROUTER_LOCAL_CLASSIFIER=shadow`)

Off by default. The proxy reads the mode from its environment at start (`~/.llm-router/.env` is loaded into
it), so a change needs a proxy restart. With `shadow`, `decide_tier` hands each turn-first call to
`proxy/llm_shadow.py` after the rules have decided. Nothing is applied: the tier, the reason and the forwarded
bytes are what they are with the mode `off` (`cls_applied` is false on every ledger row). `on` is the later
canary mode; until it exists it behaves as `shadow`.

- Only turn-first calls: a continuation (never waits) and a side call (no client tools) schedule nothing.
- One call per turn: a turn already answered (cache) or being answered is skipped. At most 4 classifications are
  pending; a turn beyond that is dropped and counted, never queued. One Ollama slot (a semaphore of 1).
- The request path only creates a task. The input is assembled in a worker thread and the call is
  `local_classifier.classify_async` (2.0 s budget). `cls_input.assemble` reads the history backwards, stops once
  it holds the newest prompt, 3 earlier prompts and the assistant's last text, and reads at most 400 messages
  back (P1.7-c: the old full walk held the GIL long enough to delay concurrent continuations; `docs/BUGS.md`
  P1.7-c-1). A record whose input hit that window without its full context says `assemble_capped: true`.
- The classifier is `local_classifier`'s backend: `LLM_ROUTER_CLASSIFIER_BACKEND=systemone` makes the shadow ask
  the decision model on Ollama `/v1/systemone` (`nimble:9b` unless `LLM_ROUTER_DECISION_MODEL`).
- One record per finished call to `classifier_shadow.jsonl` in the state dir, a `classifier_shadow_drop` record
  per drop. Hashes (`text_sha`), tiers, numbers and reason codes only: no prompt text, no model `reason`.
- `llm-router kpi` prints `classifier shadow (proxy): ...` and `--json` carries `classifier_shadow`
  (`n, n_sessions, agree, llm_tier_dist, rules_tier_dist, cheap_share_llm, fallback_rate, p50_ms, p95_ms, drops,
  calls_per_turn`, plus `n_compared`, `drop_rate`, `cls_applied_true`). Organic sessions only unless
  `--include research`. The decision cost shows in G1_proxy: `tier_decision_s` includes the scheduling.
- Each record carries `requested` (the tier of the call's own `requested_model`), `backend` and
  `assemble_capped`. Under the shadow line, `kpi` prints one `classifier shadow vs rules [model]: ...` line per
  classifier model (`--json`: `classifier_shadow.vs_rules`, from `llm_router/shadow_eval.py`): turns, sessions,
  real requested tiers, fallbacks, agreement, and against rules_eff C-lambda2, M1-12 clamp-aware cost
  (min(pick, requested)) with a session bootstrap, under-route and Haiku precision. A live turn has no truth of
  its own: those numbers need `LLM_ROUTER_SHADOW_LABELS` (JSONL `{"text_sha", "session_id", "truth"}`); without
  it, or below 100 labeled turns, M1-12 reads `not informative`.

  To label live turns, `LLM_ROUTER_SHADOW_TEXT_SAMPLE` (off by default; `on` = 30% of turns, or a rate in
  (0, 1]) makes the proxy keep the text of a sampled shadow turn in `~/.llm-router/shadow_text.jsonl` (mode 0600,
  at most 20 entries a UTC day): `{text_sha, session_id, ts, context, prompt}`. It is the only file in which the
  proxy keeps prompt text. A turn with a secret pattern in it is skipped, router banners are cut out, and the
  labeller deletes entries after 7 days or once labelled. The ledger and the shadow records stay hash-only.

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

### Haiku guard (`proxy/haiku_guard.py`): `haiku_rewrite` turns itself off

While the live policy has `haiku_rewrite: true`, the proxy runs a guard at
start and then every hour. A trip opens the `proxy_haiku` class of the quality
breaker (`~/.llm-router/quality_breaker.json`) and turns the running policy's
rewrite off; the YAML is never edited. `ClaudeTierPolicy.load` applies the breaker,
so the demotion also holds across restarts. After the breaker's cooldown (24 h) the
rewrite is switched on again as a half-open probe: 5 Haiku-served turns after that
moment with a redo rate below 50% close the class, otherwise it reopens for another
24 h. The probe counts only turns the proxy itself moved to Haiku (the rewrite, or a
treatment-arm turn), not a client that asked for Haiku. Its bar (the breaker's generic
redo rate < 50%) is deliberately looser than the 15% trip bar: it decides whether the
lever is plainly broken, and a lever that is marginal trips again on the triggers. A probe
that cannot be read keeps an open class demoted; only an unreadable state file reads
closed. The guard loop also runs when the breaker, not the YAML, has the rewrite off, so
recovery works after a restart during the cooldown. After a recovery the triggers look only at newer evidence. The owner override
`~/.llm-router/tier_overrides.json` (`{"haiku_rewrite": false, "reason", "ts"}`) is
written by the owner only, beats the breaker, and can only turn the rewrite off;
the guard and the breaker never write or delete it, so no probe runs while it
exists. To lift it, delete the file and restart the
proxy. Triggers (one is enough; a trigger below its minimum n never trips):

| Trigger | Trips when |
|---|---|
| `redo` | Haiku-served human turns (O3 units and redo definition, last 7 days) n >= 30 and redo > 15% |
| `audit_batch` | newest audit summary with a `haiku` arm: n >= 30 and acceptable < 75% |
| `audit_daily` | daily blind audit acceptable < 8/10 (n >= 10) on 2 consecutive days, the newer one inside the last 7 days |
| `tier_retry` | Haiku-decided calls: share refused and retried unchanged > 1% at n >= 100 |
| `shadow` | Haiku-vs-Frontier shadow verdicts (14 days): acceptable < 26/30 at n >= 20 |

Every run appends one line to `~/.llm-router/haiku_guard.log`. The input file
formats and the env overrides (`LLM_ROUTER_HAIKU_GUARD_KINDS`,
`LLM_ROUTER_HAIKU_GUARD_AUDIT_DIR`, `LLM_ROUTER_HAIKU_GUARD_SHADOW_VERDICTS`)
are in the module docstring. The daily watch prints every trigger with its n:

    llm-router kpi --haiku-watch --since 2026-10-07 --until 2026-10-08 [--json]

The command exits 1 when a D-20 trigger (`audit_daily`, `tier_retry` or
`shadow`) is below its minimum n. That means the day cannot be judged.

### Haiku experiment arm (`proxy/haiku_arm.py`, D-31): Haiku volume despite `pinned_models`

`pinned_models: [claude-opus-5-5]` leaves GE4-a and the daily Haiku watch with no Haiku rows. The arm is a
narrow, logged exception, off unless `haiku_arm_share` is set (a fraction 0-1; absent or 0 = off).

- **Which turns:** the hash `sha256(session_id|turn|salt)` must fall under the share (turn = number of non-system
  messages, so a retried request lands in the same place), and then the existing rules must call it simple Q&A: a
  main-thread human turn (`step_kind == turn_first`; side calls, tool-result continuations, sub-agent calls
  (`subagent_first` / `subagent_turn`) and notification-only turns (`harness_turn`, arm reason `harness_turn`)
  stay pinned), past the first call, no `opus:` / `/model` pin, no correction signal, a body Haiku accepts
  (no media, no mid-conversation system message, under the context limit), and the classifier says `query` /
  `simple`. No session id = never armed. Sub-agent calls (no `Agent`/`Task` launcher in `tools`, on any call, not only the first) and sessions tagged headless / harness / research are never armed. `haiku_arm_eligible` (default `[query/simple]`) lists the `task/complexity` patterns that qualify (complexity may be `*`); the matched pattern is logged as `tier_arm_reason: matched:<pattern>`. To run the widened arm: `haiku_arm_share: 0.25` + `haiku_arm_eligible: [query/*]` (with `haiku_rewrite: true`).
- **Ledger:** only in-share turns get `tier_arm`, `tier_arm_assignment` (`treatment` / `ineligible`),
  `tier_arm_reason`, `tier_arm_bucket`, `tier_arm_turn`. If Anthropic refuses the Haiku call and the original body is retried, the row becomes `treatment_retried_original` (served on the pinned model); **Haiku counts must also require `served_model` to be Haiku**. A treated row has `tier_reason: haiku_rewrite` and `tier_body_rewrite: haiku`,
  so GE4 shadow pairs and `kpi --haiku-watch` count it with no change. No prompt text. Audit a row by recomputing
  `haiku_arm.bucket(session_id, turn)`.
- **Requires `haiku_rewrite: true`**, which also turns the ordinary Haiku rewrite on for non-pinned traffic. `haiku_fold_system` decides whether bodies with mid-conversation `role: system` messages (every real Claude Code call today) are eligible; without it most real traffic is `haiku_body_blocked`.
- **Kill switch:** the policy is read at proxy start. Set `haiku_arm_share: 0` (or delete the key) and restart the
  proxy. `haiku_rewrite: false` (or the Haiku guard's override file) also turns the arm off, with no restart for
  the guard's own live update.
- **Cost note:** the turn after a treated one returns to the pinned model, so that call re-reads the Opus cache
  prefix; a treated turn pays a cold Haiku prefix. Quota-pressure caps are unaffected (they only move calls down).

### Lane A (`proxy/lane_a.py`, D-285): Haiku for confident continuation steps (default OFF)

A continuation is a step that answers the results of the assistant's last tool calls. Lane A lets the Haiku
tier serve such a step despite `pinned_models`; turn-first, sub-agent and harness steps stay on the pinned model.
`pinned_models` is unchanged and no model name is hardcoded: the served model is the policy's `haiku` tier.
No offload or saving figure is claimed; none has been measured.

- **Serving needs all three:** `lane_a.enabled: true` in the tier policy YAML, `LLM_ROUTER_PROXY_LANE_A=on` in the
  proxy's environment (read on every request; unset or `off` is the kill switch, no restart) and `haiku_rewrite: true`.
  The YAML block is strict (`enabled` bool, `tau` in (0, 1], `prev_tool_classes` a non-empty list that never
  includes `edit`, `exec` or `agent`); a bad value stops the policy loading. `tau` is a placeholder until it is
  pre-registered. The allowlist starts as `[technical_op]`.
- **Gates, first failure wins** (run with the switches off too): continuation on a pinned model, client tools, main
  thread, organic session kind, no `opus:` / `/model` pin, no correction signal, last tool class allowlisted, a Haiku
  tier below the requested one, then the engine verdict on the newest human prompt: L1 layer (a cache hit of an L1
  verdict counts), not abstained, confidence >= `tau`, tier Haiku and the same task/complexity as the rules'
  classifier; then a body Haiku accepts. Any error leaves the step pinned.
- **Ledger, on every continuation row, even with lane A off:** `lane_a_reason` (closed set below; a value outside it
  is written as null) and `lane_a_conf` (the engine confidence, or null; null when the engine did not run or gave
  none). The ledger is JSONL, so there is no schema migration. Turn-first and sub-agent rows from a pinned model
  carry the same two fields.

  | reason | meaning |
  |---|---|
  | `served` / `retried_original` | served on Haiku / Haiku refused the body (4xx), answered a 5xx or hung past `header_timeout_s`, and the pinned model answered the client's own bytes instead |
  | `eligible` | every gate passed, a switch was off (or authority is `advise`): stayed pinned |
  | `off` | the lane was not evaluated (no tier policy, step served elsewhere) |
  | `not_pinned`, `not_continuation`, `pinned_turn`, `not_main_thread`, `session_kind` | the step is not lane A's |
  | `pinned_by_user`, `correction_signal` | an explicit pin or a redo signal |
  | `not_allowlisted` | the last tool class is not in `prev_tool_classes` |
  | `no_haiku_tier`, `haiku_body_blocked` | no Haiku tier below the request / a body Haiku would 400 |
  | `conf_unavailable`, `low_conf`, `engine_disagree` | no engine confidence / below `tau` or abstained or not L1 / engine and rules classes differ |
  | `breaker_open_reserved` | PR2: the lane A breaker is open (or a half-open probe is in flight): stayed pinned |
  | `switch_cost_high` | PR2 cache rule: the step is not yet on Haiku this turn and re-writing the prefix there would cost >= `max_switch_usd` (or the price is unknown) |
  | `consecutive_cap` | PR2 loop guard: this turn already ran `max_consecutive` lane A steps in a row |
  | `knn_veto` | D-297: `complexity_knn` is enabled and votes "needs frontier" |
  | `error` | the lane raised; the step was forwarded as sent |

  The share of `eligible` rows among continuation rows on a pinned model measures how much traffic lane A could take
  (`s_A`). **Denominator:** continuation rows whose reason is neither `off` nor `not_pinned` (those two mean the lane
  never looked at the step). **`off` vs `eligible`:** `off` = the lane was not evaluated at all (tiers off, advise
  authority, an unknown model, a decision timeout); `eligible` = it was evaluated, every gate passed and a switch is
  off. So a lane that is switched off reads `eligible`, not `off`.
- **The gates have no side effect (repair round 1).** With the lane off the gates classify with the rules
  classifier alone (`backends.tier_classify_pure`) and `engine.compute(text, cache=False)`, both run in a worker
  thread. They write no `engine_shadow.jsonl` row, no decision-ledger row (also in `LLM_ROUTER_ENGINE_MODE=decide`),
  no `decide_cache.sqlite` and no `classifier_health` row, and make no Ollama embed even with `complexity_knn: true`
  (the lane ignores that wrapper), and nothing blocks the event loop. The only new state is the two ledger keys.
  `tests/test_proxy_lane_a.py` compares the state dir after 100 steps, with `complexity_knn` on and off, in shadow
  and decide mode.
- **Confidence is quantised.** The engine's L1 confidence is `score/(score+2)`: 0.600 (score 3), 0.6667 (4),
  0.714 (5), 0.75 (6), 0.778 (7). The shipped `tau` 0.67 is a placeholder (the pre-registration sets it): it sits
  just above the score-4 bin, so short one-line questions read `low_conf` and the `eligible` set is the longer,
  multi-question prompts. Read `s_A` as that, not as "share of simple prompts". Tested at the shipped value.
- **One verdict per turn.** The verdict comes from the newest human prompt, so every continuation step of one turn
  shares the same reason and confidence. `s_A` per step is clustered by turn and says nothing about how hard the
  continuation step itself is.
- **A bad `lane_a:` block stops the whole tier policy loading** (it raises, it does not just disable the lane).
- **Cache rule and consecutive cap (PR2).** Defaults are conservative choices; the brief names the rules, not the
  numbers. Both are in the YAML `lane_a:` block: `max_switch_usd` (0.05) and `max_consecutive` (6). A step not yet on
  Haiku this turn must have an estimated prefix re-write cost (`cache_cost.switch_cost_usd` on a chars/4 estimate of
  the body) below the ceiling, else `switch_cost_high`; an unknown price also blocks. A step that follows a lane A step
  of the same turn (same hash of the newest human prompt) is already on Haiku and skips the price. After
  `max_consecutive` lane A steps in a row in one turn the rest of the turn stays pinned (`consecutive_cap`); a new
  human prompt starts a new count, and a step that stays pinned ends the run. The memory is in process (a restart
  forgets it, which can only make the rule stricter) and is written only when all three switches are on.
- **Breaker (PR2, `proxy/lane_a_breaker.py`).** Built only when the YAML has `lane_a.enabled: true`; a lane that
  is off has no object, no file and no task. It opens on 5 hard failures of Haiku-served lane A steps inside
  10 minutes, or on a redo rate above 15% over at least 30 settled served steps in 7 days (a tool call re-issued within
  two calls, `local_gate.FailureFeed`; the redo constants are `haiku_guard`'s). Open = `breaker_open_reserved`, steps
  stay pinned. After a 30 minute cooldown (24 hours after a redo-rate trip) it is half-open and admits one probe
  (`tier_detail` `lane_a_probe`); a probe that succeeds closes it, one that fails reopens it with a fresh cooldown,
  and a probe with no outcome after 120 s lets the next step probe. The decision path is in memory; the state
  (`lane_a_breaker.json`, 0600 via `state_files`) is loaded once on the first decision and flushed in a worker
  thread, so a restart keeps an open breaker and nothing blocks the event loop. An internal error does not serve.
  All numbers are in `lane_a.breaker` in the YAML; the 5, 600 s, 1800 s and 120 s are defaults chosen here, not
  measured. `quality_breaker.json` / `proxy_haiku` is not touched: `quality_breaker` reads and writes its file
  synchronously on every decision.
- **Failure feed and fallback (PR2).** A lane A serve is followed to its end. Fed to the breaker as a failure: HTTP
  status >= 400, a timeout or stream error, an empty reply, a truncated stream (no `message_stop`) or
  `stop_reason: max_tokens`, a tool call whose arguments do not parse. A 5xx, a timeout (4xx incl. 429 as before) is
  retried ONCE on the pinned model with the client's own bytes and the row reads `retried_original`
  (`tier_retry.status` is the status, or null for a timeout); the retried step no longer counts as a Haiku step for
  the cache rule. The 5xx and timeout retry applies only to lane A serves; the D-31 arm and every other call keep
  their old behaviour (4xx only). A client that goes away mid-stream is no verdict on Haiku, and a failure after the
  first byte reached the client is not retried (it is fed as `stream_error`). A 401 or 413 is not counted (PR3).
- **`haiku_units` (PR2).** The `proxy_haiku` probe's units now also include continuation rows served on Haiku by lane
  A (`lane_a_reason: served`, `tier_reason: haiku_rewrite`, served model Haiku), tagged `lane: "A"`; they were
  invisible because only the first call of each human turn counted. Their redo is the same escalation window
  (the turn or the next two). With the lane off no such row exists, so the unit list is unchanged. The guard's
  `redo` trigger still counts turns only.
- **D-297: the `complexity_knn` veto.** When the policy has `complexity_knn: true` and lane A would otherwise serve,
  the lane asks `complexity_knn.complexity_score(newest human prompt)`; a "needs frontier" vote keeps the step on the
  pinned model with `knn_veto`. An abstain (no artifact, embedder down) does not veto, as in the wrapper. The vote is
  asked only on the serve branch (all three switches on, every other gate passed), so with the lane off there is no
  embed or model call, and with `complexity_knn: false` nothing changes. So `eligible` (switch off) is the share
  after the cost-switch and consecutive-cap gates and before the knn vote and the breaker: a step the cost rule or the
  cap stops reads `switch_cost_high` / `consecutive_cap` even with the lane off, and `eligible` steps may still be
  vetoed by the vote or stopped by an open breaker once the lane is on.
- **The prereg owns tau, the allowlist and the breaker thresholds;** the numbers above are defaults until it
  freezes them (draft: `PREREG-LANEA`).

#### Lane A PR3: Frontier shadow, lane tags, quality writer, KPI split (default OFF)

Everything below is inert unless lane A actually SERVED a step: with the lane off there is no sampler, no shadow call,
no `shadow_frontier/` file, no `lane_quality.jsonl` and no `lane` key on any row (`tests/test_lane_a_pr3.py` compares
the state dir and replays the frozen main golden with `LLM_ROUTER_SHADOW_FRONTIER=on` too). The shadow runs with
`LLM_ROUTER_SHADOW_FRONTIER=on` (OD-4 = A) on top of the lane A switches. Owner flip runbook: turn all four on, then send one request through the proxy to stamp the flip. At flip + 28 d a task group the proxy could serve (`task_groups` in `lane_a_identity.json`: `code`, `qa`, and `tech_op` when `technical_op` is allowlisted) that has no lane A row closes with a NOT INFORMATIVE row (`n_below_min`, n = 0); with no identity record the group set is unknown and such a group gets no row. Concurrent first requests run one write; after a failed write the proxy waits 60 s before the next try.

- **Lane tag.** The ledger row of a step Haiku answered for lane A carries `lane: "A"` (with `tier_reason:
  haiku_rewrite`, `lane_a_reason: served`). A retried step (`retried_original`, the pinned model answered), the D-31
  arm and every other row carry no `lane` key. The shadow ledger, the pair store and the verdict rows carry `lane`,
  `task_group`, `rate_phase`, `rate`, `session_id`, `msg_count`, `prev_tool_class` and `conf` for lane A steps only.
- **Shadow sampler (`lane_a_shadow.py`, run by `shadow_frontier.FrontierShadow`).** After the Haiku reply has left, a
  background task replays the client's UNMODIFIED bytes to the model the client asked for (the proxy never awaits it;
  at most one replay in flight). The judge is the existing blind Sonnet judge (`python -m llm_router.shadow_frontier
  judge`, 20 calls a day); a lane A pair is shown to it in an order derived from its id.
  - **Which steps.** Deterministic key `sha256(session_id|msg_count|SALT)[:8] / 2^64`, no `random`, no clock; sampled
    iff `key < rate`. A step with no session id or message count is skipped (`no_key`).
  - **Fill then 2% (OD-4 = A, per task group).** `rate` is 1.0 until the task group has 60 decided pairs among the
    verdict rows of the last 14 days, then 0.02. A decided pair has two boolean verdicts and `cannot_judge` not true.
    The arm's own clock (100% for 14 days after the shadow first ran) is unchanged and never applies to lane A.
  - **Bucket (D-293).** At most 12 lane A replays and 400k lane A input tokens per UTC day, INSIDE the unchanged
    global cap (20 calls, 400k tokens): every replay counts toward the global cap, so the arm gets what lane A left.
    A skip reads `lane_a_bucket` (or `cap_calls` / `cap_tokens` when the global cap binds first). Quota pause rules
    (session >= 80%, weekly >= 85%, stale or unknown `usage.json`) apply as for the arm. Every skip is a ledger row.
  - The 12, 400k, 60, 2% and 14 days are the numbers the PREREG-LANEA draft lists; the SALT is its draft literal.
- **`lane_quality` shadow_pairs writer (`lane_quality_writer.py`).** Nothing wrote `kind: shadow_pairs` rows before; now
  `python -m llm_router.lane_quality_writer [--since T] [--until T] [--dry-run]` (also run after `shadow_frontier
  judge`) reads the lane A verdict rows and appends the FINAL row of a task group to `lane_quality.jsonl` (0600,
  `state_files`): `{ts, door: "proxy", served_class: "haiku", task_group, kind: "shadow_pairs", n, gap_pts,
  newcombe_lb, source, lane: "A", k_laneA, k_frontier, cannot_judge, cannot_judge_share, excluded, window_complete,
  informative, close_reason, not_informative_reason, identity, flip_ts, since, until}`. `gap_pts = (acc_frontier - acc_laneA) *
  100` (positive = Haiku worse), `newcombe_lb` = the unpaired Newcombe lower bound of `acc_laneA - acc_frontier`,
  both from `eligibility.evidence_from_counts`, the function `eligibility.grade` and `lane_quality.batch_passed` use.
  - **One pair per step.** Rows are reduced to the FIRST verdict of each `(session_id, msg_count)` (PREREG-LANEA
    section 3); the rest are `excluded.duplicate_step`. The sampler skips a step that already has a replay
    (`step_seen`), and the phase count uses the same rule. A client that resends a step cannot add a pair.
  - **Judge failures are cannot_judge.** For lane A pairs `judge_pending` writes a verdict row with `cannot_judge: true`
    and `cannot_judge_reason` (the exception name, `unparsed` or `model_mismatch`) when the judge errors, times out,
    returns unparseable JSON or runs on the wrong model, so they count in the cannot_judge share (prereg section 6).
    The arm writes no such row (unchanged).
  - **Pairing check (prereg section 3).** The served ledger row carries `lane_a_body_sha` (sha256 of the client's
    bytes); `replay_http` hashes the bytes it actually sent (`Replay.sent_sha`). A pair whose two shas differ, or lack
    one, is `excluded.body_sha_mismatch`.
  - **A row missing `task_group` or `rate_phase`, judged by another model or under another brief
    (`JUDGE_BRIEF_SHA256`), is not evidence** (`excluded`, written into the row).
  - **Window and the final row (D-301, amended by D-302; prereg owner).** A task group's S1 window closes at whichever
    comes FIRST: the first judge batch with at least 60 decided pairs (after the dedupe) AND a `cannot_judge` share of
    at most 20%, or 28 days after the owner flip. That batch is the one row with `window_complete: true`, and the only
    one S1 counts; `lane_quality` ignores a `shadow_pairs` row without it. A batch with n >= 60 and MORE than 20%
    `cannot_judge` is written NON-final (`window_complete: false`, `diagnostic: true`) and the window stays open, so a
    judge outage cannot burn it; a later batch at or under 20% closes it informative. At flip + 28 days the window
    closes NOT INFORMATIVE (`informative: false`, no gap / bound, S1 gets no credit) when n < 60 OR the last batch's
    share is above 20%. The pair window is clamped to `until = min(now, flip + 28 d)`: a pair after day 28 never
    counts, even when the writer first runs later. Pairs from before the flip never count and are written into the
    row as `excluded.before_flip`. Below 60 pairs and before 28 days nothing is written. Once a task group has its
    final row no new window opens and nothing reopens it, and the **sampler stops** for that group (skip reason
    `window_final`: no 2% sampling after the close). `FINAL_WINDOW_PAIRS` (60), `FINAL_WINDOW_DAYS` (28) and
    `CANNOT_JUDGE_MAX_SHARE` (0.20) are in `lane_a_shadow.CONSTANTS`.
  - **A final row does not age out, and four identities invalidate it (D-302).** The S1 28 day read cap applies to
    audit batches only; credit from a final `shadow_pairs` row does not expire by age, and `kpi` prints each final
    row's age in days (`quality_sampled_lanes[].age_days`). Each row is stamped with `identity`: `lane_a_model` (the
    `haiku` tier's model), `judge_brief_sha`, `prompt_set_sha` and
    `breaker_sha` (the `lane_a.breaker` thresholds). **Prompt set** (pending prereg confirmation: confirmed by the
    D-302 refinement) = sha of the `prev_tool_classes` allowlist + `tau` + any text-adding rewrite setting; the only
    such setting is `haiku_fold_system` (it wraps mid-conversation system text in `<system-reminder>`); the default
    Haiku rewrite adds no system prompt or template text. **The identity is the PROXY's (D-303).** The proxy computes
    the four values from the policy it loaded at startup (`--tier-policy FILE`, the env path or the bundled one) and,
    whenever lane A can serve (`lane_a.enabled`, `LLM_ROUTER_PROXY_LANE_A=on` and `haiku_rewrite`; the shadow flag
    does not matter, so a restart with the shadow off and a changed policy still refreshes the record), records them
    in `lane_a_identity.json` (state dir, 0600, replaced at each proxy start; any of those three off writes nothing)
    and, at the flip point (all four switches), in the flip row. Each sampled pair carries `identity_sha`, the sha of
    the identities of the proxy that served it (PR4); the writer counts only pairs whose `identity_sha` equals the
    stamp it writes and reports the rest as `excluded.identity_mismatch` (a pair with none, `identity_missing`), so
    pairs collected under one policy never count under another after a restart. The writer stamps final rows from
    that record and writes nothing, reporting `identity_unknown`, when none is recorded (the window stays open). S1 ignores a
    final row whose stamp differs from the proxy's most recent recorded identity on any of the four (or has no
    stamp) and reports the lane under `invalidated_lanes` (with a caveat); `kpi` prints and returns the compared
    identity's sha (`compared_identity`) next to it. Validity is computed on every read, never latched: a reverted
    change restores credit (D-303). The window itself never reopens; a new one needs a prereg amendment. Nothing else
    invalidates a final row.
  - **Owner flip (D-302).** The flip is the FIRST PROXY REQUEST that sees all four switches on: YAML
    `lane_a.enabled`, `LLM_ROUTER_PROXY_LANE_A=on`, `haiku_rewrite` and the shadow flag, whether or not that step is
    eligible. It is recorded on that request in `lane_a_flip.json` in the state dir (`{flip_ts, switches, source}`,
    0600, `state_files`, first write wins; the write runs in a worker thread, not on the event loop). `switches` holds
    the four values the request saw, so the flip can be audited. It is never written, and no directory is created,
    when any one switch is off, so a lane that is off leaves the state dir as on main. With no flip recorded the
    writer reads the trailing 14 days, the n >= 60 close still applies and no 28-day close can fire. The window starts
    at the flip (`since` defaults to it); `--since` is a formal read. Owner flip runbook: turn all four on, then send one request through the proxy to stamp the flip. At flip + 28 d a task group the proxy could serve (`task_groups` in `lane_a_identity.json`: `code`, `qa`, and `tech_op` when `technical_op` is allowlisted) that has no lane A row closes with a NOT INFORMATIVE row (`n_below_min`, n = 0); with no identity record the group set is unknown and such a group gets no row. Concurrent first requests run one write; after a failed write the proxy waits 60 s before the next try.
  - The same evidence writes the same `source`. The arm's verdict rows (no `lane`) are never read.
  - **Lane A verdicts and the arm's guard.** Lane A verdicts share `verdicts.jsonl` but are tagged `lane: "A"`: the
    arm's D-20 `haiku_guard.shadow_trigger`, `shadow_frontier.verdict_summary` (the statusline and the session report)
    skip them, so lane A cannot turn off the arm and a strong arm cannot hide a weak lane A. Lane A's own trigger is
    `haiku_guard.lane_a_shadow_trigger` (acceptable below 26/30 at n >= 20 in 14 days, one per step), read by the
    lane A breaker (lever `proxy_haiku`, task `lane_a`, PREREG-LANEA section 10): while closed, `admit` re-reads the
    verdicts at most every 5 minutes in a worker thread, and a trip opens the breaker with the 24 h quality cooldown
    (`shadow_quality:` reason). After a probe closes it only newer verdicts count (`quality_floor`, persisted).
    `verdict_summary(lane="A")` is lane A's own summary; nothing mixes the two unlabeled.
- **KPI separation, and what the S1 numerator now includes (D-285).** The S1 definition is unchanged: an action is in
  the numerator iff it was served by a local / Haiku / very-cheap model, not redone, not escalated, and its lane
  `(door, served_class, task_group)` is quality-sampled. A lane A step is a proxy action served by Haiku whose
  `prev_tool_class` is `technical_op`, so its lane is `(proxy, haiku, tech_op)`; it enters the numerator only when
  the lane's FINAL batch (`window_complete: true`, D-301) passes (n >= 60, gap <= 3 points, lower bound >= -10) and its
  four identities still match the config (D-302; a final row is not age-capped).
  **Lane-level caveat.** Sampling is per lane, not per lane A step: once the final batch passes, the whole lane
  `(proxy, haiku, tech_op)` is quality-sampled (`offload_share.py`, `a["sampled"] = lane in sampled`), so any other
  Haiku-served `tech_op` proxy action in the window (for example one where the client itself chose Haiku) joins the
  numerator too. The S1 rule did not change in this PR and this was already true of any sampled lane; the prereg should
  say it.
  Before that it is in the denominator only, as before. `kpi` `offload` now has `lane_a` (the lane A actions,
  `quality_sampled`, `lanes`) and `not_lane_a`; `action` = `lane_a` + `not_lane_a` in both `k` and `n`, so nothing is
  counted twice. The persisted `daily_offload` rows are left as they are (no new key). The D-31 arm's Haiku turns stay in
  `(proxy, haiku, qa|code)` and never enter the lane A line.
- **Carry-ins from the #731 review.**
  - *S1, header timeout.* A lane A serve waits `lane_a.header_timeout_s` (default 60, a placeholder, not measured;
    range (0, 600]) for Haiku's response HEADERS; on a timeout the pinned model answers (`retried_original`). The
    stream that follows keeps the shared client's read timeout. Non-lane calls are untouched.
  - *S2, one failure direction.* A breaker that cannot decide does not serve: an existing state file that cannot be
    read (corrupt, wrong key, locked) opens the breaker (`state_unreadable`, the 30 minute cooldown, then one probe)
    and records `LR-FO-LANE-A-BREAKER-READ`, exactly as an internal `admit()` error denies and records
    `LR-FO-LANE-A-BREAKER-ADMIT`. An absent file is a fresh start (closed). The cost: a lost 24 h redo-rate cooldown
    is not restored.
  - *N2.* A 401 or 413 from Haiku is returned to the client and is not a breaker failure (a bad token fails the
    pinned model the same way); on a half-open probe it frees the probe slot.
  - *N3.* `retried_original` covers 5xx and header timeouts as well as 4xx refusals. A failure MID-STREAM is not
    retried (bytes have already left); it is fed to the breaker as `stream_error` and the row stays `served`.
  - *N4.* The cache rule reuses the body check's context-size estimate: no second serialization of the body, with
    the lane off or on (the reason a lane-off row gets is unchanged).
  - *N5.* A half-open probe has an id; the late outcome of a probe that timed out cannot close or reopen the
    breaker after a newer probe was admitted.
  - *Trip visibility.* An open or half-open lane A breaker appears in `quality_breaker.open_classes()` as
    `proxy_haiku:lane_a`, so `llm-router status`, `northstar` and the Stop-line (`breaker: N classes off`) show it.
    Read-only: it reads `lane_a_breaker.json` and creates, writes and trips nothing.

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
   `llm-router proxy --steps off --tiers conversation` on port **8797**, and,
   once that answers, a second one for the **fail-open shim**
   (`com.llm_router.proxy-shim` / `llm_router-proxy-shim`) on **8787**, the
   port settings.json names (see "Fail-open shim" below; `shim=False` keeps the
   old single-service layout);
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

### Fail-open shim (P0.10, D-17 = A)

`llm-router proxy-shim` (`proxy/failopen_shim.py`) owns 127.0.0.1:8787 and
forwards every request, bytes in and bytes out, to the main proxy on
`LLM_ROUTER_PROXY_UPSTREAM_PORT` (default 8797). When the main proxy cannot be
reached — connection refused, connect slower than the connect budget (1 s,
`--connect-timeout-ms`), or the connection
drops before a response — or answers a 5xx of its own (one without Anthropic's
`request-id` header) before any byte reached the client, the shim sends the same request once to `https://api.anthropic.com` with the same
headers and records a `proxy_down` event (`failopen.record`, so it lands in
`~/.llm-router/fail_open.jsonl` and KPI G2; code and reason only, never headers
or content). Once a response byte has gone out nothing is retried. An Anthropic
5xx relayed by a working main proxy (for example 529 overloaded) is passed through
unchanged: retrying it would double the load and count a healthy proxy as down.
Known risk: on a disconnect before the response headers (`disconnected`), or a
5xx of the main proxy's own, the main proxy may already have forwarded the request
to Anthropic, so the direct retry can send it twice and it may be billed twice.
The shim accepts that cost in exchange for not failing the call.

Why: `settings.json` env beats the process env, so a session already running
cannot be pointed elsewhere, and the SessionStart warning below only reaches the
next session. Measured 2026-10-07 (claude 2.1.292, `--setting-sources
project,local`, base URL on a smoke port): with nothing listening, `claude -p`
retried for 222.85 s and failed with `API Error: Connection refused` (n = 1);
through the shim with the main proxy killed, 10/10 sessions answered and 10
`proxy_down` rows were written (docs/BUGS.md P010-1).

The shim imports neither `proxy/server.py` nor its dependencies, so a broken
main-proxy deploy cannot take it down. It uses httpx for the upstream leg
because aiohttp's client rejects the duplicate `Server` header the main proxy
sends (uvicorn's next to Anthropic's).

**Moving an existing install behind the shim** (the installer reuses a process
it finds on 8787 and says "No fail-open shim installed"): stop the main proxy's
LaunchAgent, change its `--port` to 8797, then install `com.llm_router.proxy-shim`.
That is a live service change and an owner step. Restart a loaded service only
with `launchctl kickstart -k gui/$(id -u)/<label>`; never `bootout` + `bootstrap`
(the bootout can cut the API connection of the session running it, and the
bootstrap right after it can fail with launchd I/O error 5 and leave 8787 dead,
docs/BUGS.md P010-2). `kickstart` does not re-read an edited plist, so a plist
change on a loaded service (such as the port move) is the one case that still
needs the service unloaded first: run `llm-router install --proxy-default off`
and install again, or `launchctl unload <plist>` then `launchctl load <plist>`
when no session depends on 8787.

### Fail-safe: what happens when the proxy is down

Because every session depends on it once installed. With the shim, a dead main
proxy no longer fails the call, but the shim still accepts on 8787, so a probe of
that port alone would read healthy while every call bypassed routing. Each check
below therefore probes the shim's port (`port` in `proxy_default.json`) **and** the
main proxy's (`upstream_port`), and reports a dead main proxy as "routing
bypassed" with the main proxy's recovery command, and a dead shim with the
shim's (`com.llm_router.proxy-shim` / `llm_router-proxy-shim`):

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
- **The statusline** shows `🔌 proxy down:<port>` (or `proxy down:<upstream_port> (bypassed)` when only the main proxy behind the shim is dead) in red the instant the probe
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

### Repairing a lost routing key: `llm-router doctor --fix-routing` (P0.14-c)

On 2026-10-08 `settings.json` lost `env.ANTHROPIC_BASE_URL` in an unrecorded
rewrite while the sentinel still said enabled, and routing stayed off for about
three hours. `llm-router doctor --fix-routing` is the explicit repair. No hook,
no plain `install` and no plain `doctor` runs it (owner decision D-R8-4). It
writes that one key (with a backup, and nothing else) only when all of these hold:
the sentinel is enabled and has no `routing_opt_out`; the key is absent from
`~/.claude/settings.json`; no project `.claude/settings{.local,}.json` and no
environment value names another endpoint; the shim's port and the main proxy's port
both answer. Otherwise it refuses and names the reason. On a terminal it shows
the diff and asks y/N. `--yes` writes without asking, and without a terminal it
is required. A second run is a no-op.

`--decline` records a deliberate removal (`routing_opt_out: true` in
`proxy_default.json`). After that the repair refuses and SessionStart no longer
prints "routing is OFF". `llm-router install --proxy-default` opts back in.
`uninstall` removes the sentinel, so nothing is left to repair or warn about.

Attribution: every SessionStart (with proxy-default installed) appends one
`observe` row to `~/.llm-router/settings_writes.jsonl` with the key's presence,
a sha256 of the `env` block and the file's mtime (never the URL). The repair
appends a `write` row with the keys changed and the backup path. The next
unexplained removal then falls between two session starts.

### Routing authority: `route` or `advise` (P2.2-A, `llm_router/authority.py`)

```bash
llm-router mode                  # "mode: route (default)" / "(file)" / "(env)"
llm-router mode advise           # the proxy forwards the model you asked for; logs its proposal only
llm-router mode route            # back to today's behaviour (the default)
llm-router install --authority route|advise   # proxy default + fail-open shim, then the mode
```

`advise` is shadow under the P2.7 precedence (D-150): no local serving, no rewrite, and a
user's own prefix (`opus:`, `local:`, ...) does not beat it. The tier policy still decides, on a
read-only view of the stickiness (a proposal that is never applied must not pin later turns), so
the ledger row keeps `tier_proposed` and the `tier_*` detail, the would-be reason moves to
`tier_reason_proposed`, and the client's ORIGINAL bytes (prefix included) go upstream:
`served_model` is the requested model, `tier` is null, `tier_reason` is `advise`, and a prefixed
turn records `prefix_outcome: advise:not_applied`. Every proxy row carries `authority`
(`route` | `advise`). The mode is read on every request from `LLM_ROUTER_AUTHORITY` (wins when
set), else the one-word file `~/.llm-router/authority`, else `route`; a toggle therefore never
restarts the proxy. An unrecognised value is read as `route` and counted as
`CHZ-FO-AUTHORITY-MODE`. Nothing here writes `settings.json` or a service definition.

**Shipped off.** A plain `llm-router install` does not turn the proxy default on: it prints
one line saying so. `commands/install.py` `PROXY_DEFAULT_ON_INSTALL = False` is the owner's
switch (D-104): when True, a fresh install (no sentinel, no `ANTHROPIC_BASE_URL` in
`settings.json`) installs the proxy default with `route`, unless `--no-authority`.

**Skips.** `~/.llm-router/skips.jsonl` holds `{ts, session_id, door, reason}`; a row with no
reason is refused. The proxy writes none: `routing_off` and the tier reasons are already on its
ledger row, and a per-call skip row would let `coverage_check` call a bypassed call "explained".
The hook sites (`headless`, `base_url_override`; round B) are the writers, and only those two
session-level skips (plus fail-open `proxy_down` rows) explain a call with no ledger row. The file
rotates at 8 MB (two generations).

**Coverage.** `scripts/coverage_check.py --since ... --until ...` counts host model calls
(assistant message ids in the Claude Code transcripts) that passed a decision point (a
ledger row with `tier_reason` or `reason`, or served), caps each session at 40, requires
every call that did not pass to be explained, and prints Wilson 95% bounds. It reads message
ids and timestamps only. `--write` leaves `coverage_check.json` for `llm-router kpi`. This
is decision-point coverage, not routing coverage: a forward with a recorded reason passes, so it
prints the routed share beside it. A passed call is `routed` only if it was `served` or the tier
policy switched the model (`tier_switch` True). A tier decision that did not switch (first_call,
side_call, sticky, pinned, unknown_model, policy-kept) is `decided_kept`, even when the row also
says `reason=routing_off` (the live config: tiers on, steps off). Other buckets: `advise`, `off`
(routing_off / tiers_off with no tier decision), `other_reason`. JSON `routed_share` carries
`routed`, `decided_kept`, `advise`, `off`, `other_reason`, `routed_share`, `decided_kept_share`,
`off_share`, `advise_share`. The flag "NOT EVIDENCE FOR R-RTE-2" (`not_evidence_for_RRTE2`)
fires when off + advise + decided_kept is at least 90% of passed calls, i.e. real switches are at
most 10%. It covers only that share: it does not judge whether the switches were right. The
header also prints `failopen_explained`; `fail_open` is a global explainer, so any no-row call
within +-120 s of a `proxy_down` row counts as explained whatever its session. `--switch-file` (the owner's recorded switch time) is required
for an informative run; without it the verdict is NOT INFORMATIVE (`switch_unknown`).

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

## Routing prefixes

A prompt that starts with `local:`, `claude:`, `opus:`, `sonnet:`, `haiku:` or `codex:` (case-insensitive; `native:` means `claude:`) forces where it runs. The prefix is read only at the start of the prompt, is stripped before the text is forwarded, and each forced choice appends a text-free row (`text_sha`, `forced`, `decided`, `door`, `ts`) to `~/.llm-router/examples.jsonl`. A prefix beats the classifier, stickiness, the Q&A-quiet policy and soft quota pressure; it does not beat `LLM_ROUTER_ENFORCE=off|shadow`, kill switches, admin `pinned_models`, hard stops (media, prompt cap, unhealthy backend, open breaker) or budget caps.

| Door | `claude:` | `opus:` `sonnet:` `haiku:` | `local:` | `codex:` |
|---|---|---|---|---|
| Hook | answers natively, nothing held | act as `claude:` (a hook cannot switch the host model) | forced to a local model, or a one-line refusal | forced to Codex, or a one-line refusal |
| Proxy | forwarded, prefix stripped | pins that tier | served locally only with `--serve local-agent`, else forwarded unchanged | forwarded unchanged (no backend) |
| MCP `llm()` | one-line "handle it yourself" | runs the API tier, or the same note in subscription mode | runs the first installed local model, or refuses | runs a Codex model, or refuses |

At the proxy, `local:` forces that one step only, together with the tool-loop calls that follow it (they still carry the same newest prompt). It does not change the conversation's pin: the next prompt without a prefix is routed as if the prefix had never been used. A conversation pinned to Claude stays on Claude. A conversation whose first prompt was `local:` and was served locally is pinned local, as it would be without the prefix. After a refused `local:` first prompt nothing is stored, so the next prompt without a prefix goes to Claude. A conversation whose start this proxy did not see goes to Claude.

P2.7-a is conditional-partial: 6/6 at the hook, 4/6 at the proxy. Unknown prefixes (`gpt:`) are ignored. Text whose first line begins with `Claude:`, `Local:`, `Opus:` and so on (a pasted dialogue, for example) is read as a prefix: put some text before it, or rephrase.
