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
llm-router proxy                                   # terminal 1, binds 127.0.0.1:8787
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude    # terminal 2, this session only
```

Unset the variable, or close that terminal, to stop using it. Other sessions
are not affected.

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
- **Latency budget.** Each step has a budget (`--step-budget-s`, default 30 s).
  If the budget is exceeded, the proxy falls back to Anthropic.
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

## Settings

| Flag | Env var | Default |
|---|---|---|
| `--port` | `LLM_ROUTER_PROXY_PORT` | `8787` |
| `--steps` | `LLM_ROUTER_PROXY_STEPS` | `continuation` (`off` = pass-through only) |
| `--step-budget-s` | `LLM_ROUTER_PROXY_STEP_BUDGET_S` | `30` |
| `--model` | `LLM_ROUTER_PROXY_MODEL` | from policy. A pin changes *which* tool-capable model serves, never *whether* a step is routed. |
| `--trim` | `LLM_ROUTER_PROXY_TRIM` | `tool-desc-2000`. Comma list of named trims or `module:function`. |
| `--num-ctx` | `LLM_ROUTER_PROXY_NUM_CTX` | `32768` |
| (none) | `LLM_ROUTER_PROXY_UPSTREAM` | `https://api.anthropic.com` (only loopback overrides are accepted) |

The model, the trims (`proxy/backends.py: TRIMS`) and the backends
(`BACKENDS`) are plug points. A measured speed lever can be added as a named trim
or a `module:function` without changing the server.

## Metrics

Each call writes one row to `~/.llm-router/proxy_calls.jsonl` with shape,
decision, timing and token counts. Rows never contain content or headers.

```bash
llm-router proxy stats [--days N] [--json]
```

The metrics are reported separately, because they diverge:

- **routed share**: calls served by non-Claude over all calls;
- **fallbacks**: count, and why each call was not served;
- **latency**: served vs Anthropic medians, and the latency added before
  fallbacks;
- **Anthropic tokens and est. cost**, split into input, cache read, cache write
  5m / 1h and output. Also an est. avoided cost with its n, and the cache writes
  after a served turn vs a clean history.

A high routed share is not a saving. In the spike, routing half the calls saved
about a fifth of the Anthropic cost, because cached re-reads are already cheap.

`llm-router northstar` joins served rows to transcript turns by `message.id`.
Those turns stay `claude_main_call` units, with lever `proxy`, and are judged
`used` / `redo` / `unknown` from what happened to their tool calls.

## Benchmark

`scripts/bench_proxy_steps.py` replays the golden fixture tasks through the
proxy. The serving model is real. The upstream is Claude's own replies recorded
by the spike. The script reports tasks passing, extra calls vs baseline and
validation failures, per step class. Its docstring states what it does and does
not measure.
