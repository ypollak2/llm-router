# Spike: per-call proxy for Claude Code main-loop calls (2026-09-28)

Question: can llm-router sit at `ANTHROPIC_BASE_URL` and route individual
`/v1/messages` calls of Claude Code's own agent loop, using llm-router's own
policy, instead of about 2% of prompts?

Code: `scripts/spikes/per_call_proxy.py` (proxy), `per_call_proxy_runcase.sh`
(one golden case through isolated `claude -p`), `per_call_proxy_summ.py` (log
summary). Nothing was deployed; no real settings, hooks or `~/.claude.json` were
touched.

## Conditions (apply to every number below)

- Claude Code 2.1.283, `claude -p --model sonnet` (the request reports
  `claude-sonnet-5`), Max subscription OAuth.
- Isolation: `HOME=<scratch>/home` (empty, so no user settings, hooks, plugins,
  MCP or `~/.claude.json`), `--setting-sources project --strict-mcp-config
  --no-session-persistence --permission-mode acceptEdits --max-budget-usd 0.5`.
  The only link to the real home is a symlink `<scratch>/home/Library/Keychains
  -> ~/Library/Keychains`, so Claude Code can read its own login itself. Without
  it, the output is "Not logged in". The spike never read a credential.
- Default tool set: 24 built-in tools, 0 MCP tools. A real session with MCP
  servers sends more tools and bigger requests.
- Tasks: golden constructed cases c001-c006 from
  `rsi-engine-probe/holdouts-src/routing-golden-v1` (one-function bug fixes).
  Graded by `python3 tests/test_modNN.py`, plus a check that no file besides
  `pkg/modNN.py` changed. There is also one README "add a line" task for call
  shape.
- Local model: Ollama `qwen3-coder:30b` (Q4_K_M), `num_ctx=32768`, `think=false`,
  on a 48 GB Apple Silicon Mac, run through the stock Ollama.app server.
- n is small (6 tasks per arm). These are existence proofs and shape numbers,
  not rates to extrapolate.

## 1. Auth: it works

| Check | Result (n=14 isolated sessions after the fixes, all passed) |
|---|---|
| Header that arrives | `authorization: Bearer <OAuth access token>` (token kind `sk-ant-oat…`, value never logged); no `x-api-key` |
| `anthropic-beta` | includes `oauth-2025-04-20`, `claude-code-20250219`, `prompt-caching-scope-2026-01-05`, `extended-cache-ttl-2025-04-11`, … |
| Forward the headers unchanged to `https://api.anthropic.com` | HTTP 200, streamed replies, `pong` round trip, full agent tasks complete |
| Other traffic | `HEAD /api/hello` once per session (forwarded, 200) |

One pitfall came up and was fixed. httpx adds `Accept-Encoding: gzip` on its
own, and relaying the raw gzip without its header broke Claude Code with "JSON
Parse error". Force `accept-encoding: identity` upstream.

The body carries fields a translator must know about: `thinking:
{type: adaptive}`, `context_management` (`clear_thinking_20251015`),
`output_config.effort`, `metadata.user_id`, and `role:"system"` messages placed
in the middle of `messages` (a budget reminder appears after the newest
tool_result).

## 2. The existing gateway (`~/.llm-router/live/src/llm_router/gateway.py`): not a base

| Gap | Evidence |
|---|---|
| Refuses tools outright | `/v1/messages` calls `_refuse_tools_if_present` and returns 400 whenever `tools` is set (H-03/R10, test `test_r10_refuse_what_cannot_be_served.py`). Every Claude Code main-loop call has 24+ tools. |
| Cannot parse Claude Code's body | `_AnthropicRequest.system: str \| None`, but Claude Code sends `system` as a list of blocks with `cache_control`, so the request would fail validation. Content is flattened to one text prompt. |
| No tool_use / tool_result translation | Replies are always `[{"type":"text"}]`, and the core is `route_payload_async` → `route_and_call`, which takes text in and gives text out. |
| No streaming | No SSE path at all. Claude Code sends `stream: true` on every call. |
| No pass-through to Anthropic | It always answers from a routed model, so it cannot forward to Anthropic, which is the path taken for ~50-100% of calls. |
| Auth collision | A configured gateway token is checked against `Authorization: Bearer`, which is the same header that carries Claude Code's OAuth token. |
| Semantic cache | `route_and_call` includes the semantic cache that has served wrong answers before (cosine ≥ 0.95). Per-step tool calls must bypass it. |

Reusable: the classifier (`classify_signals`, `GATEWAY_POLICY`), the chain
builder (`router._build_and_filter_chain`), and the loopback/cross-origin guard.
The proxy itself has to be a new module: a streaming pass-through, an
Anthropic↔Ollama/OpenAI tool translator, and a validator. The spike does this in
about 430 lines.

## 3. Call shape (pass-through, no routing)

Every `/v1/messages` call re-sends the whole preamble.

| Metric | Value | n |
|---|---|---|
| Calls per task | 3, 3, 5, 3, 4, 4 (golden c001-c006); 3 (README task) | 7 tasks, 25 calls |
| Side calls without tools (titles, probes) | 0 in `-p` mode | 25 calls |
| Tools per request | 24, the same list on every call | 25 |
| System prompt | 27.7k chars | 25 |
| Tool definitions | 87.5k chars JSON | 25 |
| Request size | 125-128 KB (median 126 KB) | 22 golden calls |
| Input tokens per call | ~43.5k, of which ≥99% is cache_read after the first call | 17 non-first calls |

Step types across the 22 baseline golden calls:

| Step (previous tool → reply) | Calls | Easy-step shaped? |
|---|---|---|
| user prompt → Read named file | 6 | yes, obvious next tool call |
| Read → Edit (the actual fix) | 6 | **no**, substantive |
| Edit → Bash run test | 3 | yes, obvious next tool call |
| Edit → final text | 3 | yes, final answer |
| Bash → final text | 3 | yes, summarise tool output / final answer |
| Bash → Bash | 1 | yes |

16 of 22 (73%) are easy-step shaped. These tasks are toy-sized, so read this as
the upper end. Longer real tasks will have more substantive steps.

## 4. One routed step class, end to end

**Step class routed:** a "continuation", meaning a main-loop call whose newest
non-system user turn contains only `tool_result` (plus reminder text). The
question it answers is "given this tool output, what next?". The first call per
task is never routed.

**Policy:** `classify_signals(GATEWAY_POLICY)` runs on (original ask + newest
tool output), then `router._build_and_filter_chain(task, complexity)`. On this
machine that returns `ollama/qwen3-coder:30b` first for code/moderate. The
code/complex chain has no Ollama entry and no tool-capable backend, so those
calls are forwarded to Anthropic. The policy made that choice; it is not a
failure. A served reply must pass validation, or the call falls back to
Anthropic and is counted as a failure: the tool name must exist in the request,
the args must be a JSON object, required keys must be present, and primitive
types must match.

| Metric (6 golden tasks, c001-c006) | Baseline (all Anthropic) | Routed |
|---|---|---|
| Tasks passing fixture test, test file untouched | 6/6 (c001 re-graded by hand after a grader-command fix) | **6/6** |
| Total calls | 22 | 30 |
| Calls to Anthropic | 22 | **11** (6 first calls + 5 kept by policy) |
| Continuation calls considered | – | 24 |
| Kept on Claude by the policy (code/complex) | – | 5 (5 of the 6 Read→Edit steps) |
| Served by qwen3-coder:30b | – | **19** |
| Served replies that failed validation (fallback) | – | **0 / 19** |
| Served replies with tool_use | – | 13 (Read, Edit, Bash, ReportFindings); 6 text-only final answers |
| Routed tool_use executed by Claude Code | – | 10 of 13 confirmed by a matching `tool_result` in the next request (1 had `is_error`: the model ran `python`, which does not exist, and recovered next step). The other 3 could not be matched because the log preview was cut at 400 chars. Every run exited 0 with no API error. |
| Anthropic 400 after a routed turn (missing thinking block) | – | 0 in 5 mixed-history Anthropic calls |
| Median latency per routed call | – | **61 s** (min 3.5, max 100, n=19) vs Anthropic median 2.2 s (n=11) |
| Wall clock for 6 tasks | 53 s | **938 s (17.7×)** |

Quality notes (n=6, so qualitative):

- The routed model adds steps. It re-reads the test file and re-runs tests, for
  30 calls instead of 22.
- In c003 it called `ReportFindings`, a Claude Code tool meant for a different
  purpose. Claude Code accepted it and the task still passed.
- Final answers sometimes assert "All tests now pass" without running them.

## 5. Prompt-cache impact (Anthropic calls only)

| | Baseline, 22 calls | Routed, 11 calls |
|---|---|---|
| cache_read tokens | 887,472 | 404,788 |
| cache_creation tokens | 78,362 | 78,103 |
| uncached input | 44 | 22 |
| output | 3,055 | 1,859 |
| cache_creation on a non-first call | 203-384 (median ~230, n=16) | **787-850 (n=5)** |
| cache_read on a non-first call | 43.5-44.4k | 43.5k (prefix intact) |

- Routed turns in between do not break the cached prefix. System prompt and
  tools stay byte-identical. The next Anthropic call only has to cache-write the
  routed turns it never saw, which costs about +600 tokens per call.
- About 94% of cache_creation is the first call of each task (12.3k 1h-TTL write
  per task). Routing continuations cannot touch this.
- Priced as Sonnet-equivalent input, the Anthropic side drops about 21%
  (read 0.1×, 1h write 2×, output 5×: 260.8k → 206.0k). Half the calls went away
  but saved only a fifth of the cost, because cached re-reads are already cheap.
  This is an estimate from these 6 tasks only.

## Verdict: GO for a build, but only behind a latency gate and not on this local stack

Mechanically the design works: OAuth pass-through is fine, the translation is
accepted by Claude Code (0/19 validation failures), tasks still pass (6/6), and
cache stays intact. The economics are weaker than the call count suggests, and
latency with a local model is unacceptable today.

**Top 3 risks for the full build**

1. **Latency / prefix cache on the local backend.** Each routed step re-sends
   about 21k prompt tokens. Ollama's prefix cache hit only sometimes (3-9 s
   warm, 55-100 s cold), which made the whole run 17.7× slower. A real session
   with MCP tools is bigger still. The build needs a fast tool-capable backend
   (a cloud API with prompt caching; none is configured on this machine: the
   policy's openai/deepseek/moonshot entries have no keys, and codex/* is a
   subprocess with no tool channel) or a pinned-slot local server.
2. **Savings are bounded by caching.** Main-loop input is already ≥99%
   cache_read, and first-call cache writes dominate. Routing half the calls saved
   about 21% here, not 50%. The North Star "routed" share can rise sharply while
   dollar and quota savings stay small. Measure both, and never claim one from
   the other.
3. **Quality drift that validation cannot see.** Schema-valid tool calls can
   still be wrong: an extra re-read, a nonexistent `python`, a misused
   `ReportFindings`, and "tests pass" claims made without running the tests.
   The step classifier is also the whole safety story (it kept 5/6 Edit steps on
   Claude). The build needs a per-step-class quality benchmark on real sessions,
   not toy fixtures, plus handling for thinking-block and `context_management`
   semantics on mixed histories (0 errors seen in n=5 is not proof).
