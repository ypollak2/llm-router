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

## Settings

| Flag | Env var | Default |
|---|---|---|
| `--port` | `LLM_ROUTER_PROXY_PORT` | `8787` |
| `--steps` | `LLM_ROUTER_PROXY_STEPS` | `continuation` (`off` = pass-through only) |
| `--step-budget-s` | `LLM_ROUTER_PROXY_STEP_BUDGET_S` | `30` |
| `--hedge-s` | `LLM_ROUTER_PROXY_HEDGE_S` | `8` (first-token deadline; `off` disables) |
| `--model` | `LLM_ROUTER_PROXY_MODEL` | from policy. A pin changes *which* tool-capable model serves, never *whether* a step is routed. |
| `--trim` | `LLM_ROUTER_PROXY_TRIM` | `fast`. Comma list of named trims or `module:function`. |
| `--num-ctx` | `LLM_ROUTER_PROXY_NUM_CTX` | `32768` |
| `--keep-alive` | (none) | `-1`: the model stays loaded until Ollama restarts |
| `--no-warm-up` | (none) | a warm-up call runs in the background at start |
| `--ollama-url` | (none) | the router's configured Ollama (e.g. a dedicated server, below) |
| (none) | `LLM_ROUTER_PROXY_UPSTREAM` | `https://api.anthropic.com` (only loopback overrides are accepted) |

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
