# Phase 7/8/11/12 — Provider Semantics, Capability Matrix, Context Integrity, Cache Red Team

Specialist: Provider Semantics. Subject: `~/Projects/llm-router` @
`357a402e8f462f913cf9368244557eaaf7711beb`. All probes run under
`LLM_ROUTER_HOME=$(mktemp -d)`, `LLM_ROUTER_BASH_INTERCEPT=off`, `.venv/bin/python`.
No writes to `~/.llm-router`, no deletes. **Money spent: $0.00** — `XAI_API_KEY` is
present but the xAI team has zero credits (`permission-denied` on the one call
attempted); every cloud-provider claim below is therefore either static-code
evidence or explicitly marked untested. OpenAI/Anthropic/Gemini/Perplexity keys are
absent, as stated in FROZEN_STATE.md — anything about those providers is static
analysis only.

## 0. Architecture, in one paragraph

There are **three unrelated "capability" systems** in this codebase and they do not
talk to each other: (1) `model_registry.py` — a hand-curated, benchmark-flavoured
table (`quality_score`, `capabilities` tuple, `context_window`) that the docstring
says "drives routing" and "empirical lookup tables"; (2) `types.ModelCapability` /
`discover.py` — the Adaptive Universal Router v5.0 discovery cache
(`~/.llm-router/discovery.json`); (3) `capabilities.py` — a regex classifier for
whether a *task* needs agentic tools (files/commands), consulted by
`chain_builder.needs_claude_tools()`. None of the three gate model selection against
a request's actual I/O requirements (vision, tools, JSON schema, context length).

## 1. Capability matrix — registry claim vs. adapter reality

| Dimension | Registry claim (`model_registry.py`) | Actually enforced anywhere in the routing path? | Evidence |
|---|---|---|---|
| Tool/function-calling | `capabilities=("function-calling",)` tagged on qwen3.5, gpt-4o-mini/4o, gemini-1.5-*, o3 | **No.** `providers.call_llm()` — the single function every text route goes through — has no `tools` parameter, and its `extra_params` allowlist (`_ALLOWED_EXTRA_PARAMS`, `providers.py:62-79`) does **not** include `tools`/`tool_choice`. `route_and_call()` (`router.py:3654`) has no `tools` parameter either. | CONFIRMED — see §2 |
| Vision | `capabilities=("vision",)` on haiku-4-5, gpt-4o(-mini), gemini-1.5-* | **No.** `gateway._flatten()` (`gateway.py:426-435`) strips every non-`text` content part from OpenAI/Anthropic-format multipart messages before the prompt ever reaches classification or routing. | CONFIRMED — see §3 |
| JSON / structured output | `capabilities=("json",)` on gpt-4o-mini/4o, gemini-1.5-*, gemini-1.5-pro | **No.** `response_format` exists in the codebase only as an `IMAGE`-task param passthrough (`router.py:1288`, DALL·E-style). There is no `json_schema`/`response_format` handling anywhere in the chat-completion path. | CONFIRMED (grep, zero hits outside IMAGE) |
| Reasoning / thinking | `capabilities=("reasoning",)` on o3 only | Partially — `OpenAIReasoningQuirks` forces `temperature=1` for `o1/o3/o4*` (`provider_quirks.py`), and `providers.py` has a D.1 comment about `message.reasoning`. Real for OpenAI o-series *if* the key existed (untested, no key). | Design confirmed, live behaviour UNTESTED (no OPENAI_API_KEY) |
| Context window | Per-model `context_window` (e.g. `ollama/qwen3.5:latest` → 32768) | **No.** Zero references to `.context_window` anywhere in `src` outside the two dataclass definitions themselves (`model_registry.py`, `types.py`). No pre-flight token-count-vs-limit check exists at all. | CONFIRMED (grep) |
| `with_capability()` filter | Registry method meant to select models by capability tag | **Dead code.** Zero callers anywhere in `src` (only its own definition). `router.py` imports exactly one symbol from `model_registry`: the `GOOGLE_PROVIDERS` frozenset — never `ModelRegistry`/`ModelMetadata`. | CONFIRMED (grep) |
| Pricing | 47 entries in `pricing.py` | 34/47 (72%) are explicitly flagged `verified=False`; 0 explicitly flagged `verified=True`. `model_registry._priced()` sources every rate from `pricing.py` (good — WP-03 already closed the "retired-rate" class), but that just means the registry inherits `pricing.py`'s own unverified rates unchanged. | Confirmed by direct count |
| Availability (local) | Ollama entry claims context 32768 for `qwen3.5:latest` | Ollama's own `/api/show` reports the **model architecture's** `context_length` as 262144, but the *effective runtime* window is whatever `num_ctx` resolves to — and `providers.py`'s own D.2 comment states Ollama's un-overridden default is 4096, silently returning **empty content** on overflow. `LLM_ROUTER_OLLAMA_NUM_CTX` is unset in this environment. Registry's 32768 is neither the architectural max (262144) nor the default runtime value (4096, per the codebase's own comment) for the exact model tested. | STRONGLY SUPPORTED — internal contradiction between `model_registry.py`'s number and `providers.py`'s own documented Ollama default; not independently overflow-tested (would require a multi-thousand-token prompt) |

**Reachability of every "incompatible pair" above is not theoretical.** Every text
task in this product funnels through `route_and_call → _call_text → providers.call_llm
→ litellm.acompletion`. Since none of `tools`, vision content, `response_format`, or
`context_window` are checked or forwarded anywhere on that path, **any** routing
decision can pair a task that needs one of these with a model/backend that either
can't do it or is never told to. Nothing upstream blocks the pairing — the pairing
*is* the normal path.

## 2. Tool-calling: the same gap exists twice, closed once

- **Confirmed already fixed** at the FastAPI gateway boundary (H-03, `gateway.py:609-668`):
  `/v1/chat/completions` and `/v1/messages` both declare `tools`/`tool_choice` on
  their Pydantic request models specifically so they are *visible*, then call
  `_refuse_tools_if_present()` and return HTTP 400 with an explicit "cannot execute
  tool calls" message instead of silently dropping them. Good remediation, verified
  by reading the code — this class of bug is closed **for those two endpoints**.

- **NOT fixed, same bug, sibling endpoint.** `/v1/responses` (`gateway.py:686-734`,
  `_ResponsesRequest`) declares `model, input, instructions, task_type, complexity`
  — **no `tools` field at all**, and `openai_responses()` never calls
  `_refuse_tools_if_present()`. A client sending `tools` to `/v1/responses` gets
  exactly the pre-H-03 behaviour: Pydantic silently discards the field, the router
  answers in prose, and the client has no signal its tool definitions were thrown
  away. **CONFIRMED by direct code read** — this is the literal defect H-03's own
  comment describes, reopened one endpoint over.

- **Not fixed, and never had a gateway-level guard, at the Python API layer.**
  `providers.call_llm()` and `router.route_and_call()` (the internal APIs the MCP
  `llm()` tool and any future in-process caller use) have **no `tools` parameter and
  no refusal**. Empirically verified: passing `tools=[...]` via `extra_params` to
  `call_llm("ollama/qwen3-coder:30b", ...)` produces a normal prose answer with the
  tool silently discarded before it reaches `litellm.acompletion` — no error, no log
  line, no field in the response indicating anything was dropped.
  ```
  tools in allowed extra params: False
  tool_choice in allowed extra params: False
  -> content: "I'll calculate 19 × 23. ... 19 × 23 = 437"  (arithmetic done by the
     model itself; the tool call never happened)
  ```

- **Tool-calling exists in exactly one place**: `agentic/react.py`'s local ReAct
  harness, which bypasses `providers.py`/LiteLLM entirely and talks straight to
  Ollama's native `/api/chat` tool-calling API (`client()` closure,
  `agentic/react.py:250-280`). It is scoped to Ollama only, single-model, no
  chain/fallback, no cost/usage integration with the rest of the router.

**Net finding (Phase 7+8, CONFIRMED):** the product's general-purpose routing path
supports function-calling nowhere except one HTTP endpoint's explicit refusal and one
narrow local-only harness. A caller of the Python API or of `/v1/responses` gets
silent tool loss today.

## 3. Vision: never remediated, same shape as the tools bug

`gateway._flatten()`:
```python
if isinstance(c, list):  # content-parts (OpenAI/Anthropic vision format)
    c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
```
Empirically reproduced:
```python
_flatten([{"role":"user","content":[
    {"type":"text","text":"What is in CANARY_IMAGE_TOKEN_7X9 this screenshot?"},
    {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]}])
-> 'user: What is in CANARY_IMAGE_TOKEN_7X9 this screenshot? '
```
The `image_url` part is gone, with **no error, no refusal, no log**. Unlike the
tools case, this was never given an H-03-style guard: there is no
`_refuse_vision_if_present`. A vision request routed anywhere — including to an
Ollama text model with `capabilities=()` (no `"vision"` tag) — gets a confident,
plausible-looking text answer about an image it never received. There is no `TaskType.VISION`
in the `TaskType` enum (`IMAGE` = image *generation* output, not vision input), so
this class of request isn't even classified distinctly.

**CONFIRMED, and it is a routing-quality bug wearing a "weak model" disguise per the
Phase 11 framing** — a user will conclude the *model* hallucinated about an image,
when the router silently never sent it.

## 4. Context integrity — canary results (Ollama, real calls, providers.call_llm path)

All canaries below round-tripped through `providers.call_llm` → `litellm.acompletion`
→ real Ollama server (`qwen3.8:latest`).

| Canary | Result |
|---|---|
| System-prompt token (`ZEBRA-QK7731`, instructed to reveal only on direct ask) | **Survived intact** — model replied `ZEBRA-QK7731` exactly, on request, and did not leak it unprompted in the sibling test. |
| 4-turn conversation history (`FALCON-91X` codename set in turn 2, asked back in turn 4) | **Survived intact** — `"FALCON-91X."` returned; `input_tokens=92` consistent with all 4 turns being sent. |

**No context loss found at the `providers.call_llm`→Ollama layer** for system
prompts or multi-turn history — this specific path is clean. Context loss found
elsewhere in this audit is at the **edges**, not this core call:

- **Vision content**: dropped at `gateway._flatten()`, before classification/routing
  (§3). CONFIRMED.
- **Tool definitions**: dropped at `providers.call_llm`'s extra_params allowlist and
  never present in `route_and_call`'s signature at all (§2). CONFIRMED.
- **Injected context budget**: `context.build_context_messages()` caps injected
  session history / repo context at `max_context_tokens=1500` by default
  (`context.py:579`). This is a deliberate budget, not a bug, but it is a silent one
  from the call site's perspective — worth a DESIGN RISK flag since Phase 11 asks
  specifically about repo-instruction and agent-memory loss: anything beyond ~1500
  tokens of injected context is cut with no signal in the `LLMResponse` that a cut
  happened. Not independently measured how much of a real repo/CLAUDE.md instruction
  set that budget accommodates — flagged, not sized.
- **Ollama effective context window**: see §1 row — registry says 32768, the
  codebase's own comment says Ollama's un-set default is 4096 and overflow returns
  *empty content* silently (caught downstream as `EmptyResponseError` → failover, per
  the comment at `providers.py:208-213` — so this particular failure mode does have a
  safety net, unlike vision/tools, but the registry number remains wrong for the
  untouched default).

**Cross-provider (cloud) differential test: blocked, $0 spent.** The only non-Ollama
credential available, `XAI_API_KEY`, returned `XaiException: permission-denied — Your
newly created team doesn't have any credits`. No xAI call succeeded; no money was
spent (one attempted call, zero tokens billed per the error). OpenAI/Anthropic/
Gemini/Perplexity semantics (role conversion, o-series system-message handling,
Anthropic thinking blocks, streaming usage shape) are **UNTESTED** here — everything
said about them is inference from `provider_quirks.py`/`providers.py` code, not a
live observation.

## 5. Adapter-level semantic drift (static, cross-checked against what's testable)

| Adapter/provider | Quirk registered | Behaviour | Verified how |
|---|---|---|---|
| `ollama` | `OllamaQuirks` | Drops `max_tokens` unconditionally (LiteLLM Ollama transport bug: truncates at prompt length, not completion, causing empty output) | Code read; consistent with the two live canary calls (`max_tokens=30` request still returned longer completions — `output_tokens=110` and `108` in the two Ollama canaries, i.e. `max_tokens` was in fact not honoured, matching the documented quirk) |
| `openai` (o1/o3/o4) | `OpenAIReasoningQuirks` | Forces `temperature=1` | Code read only — UNTESTED live (no key) |
| `openrouter` | `OpenRouterQuirks` | Re-prepends `anthropic/` prefix; hard-caps `max_tokens` at 2048 | Code read only — UNTESTED live (no key) |
| `openai_compat` | `OpenAICompatQuirks` | Rewrites `openai_compat/model` → `openai/model`, injects `api_base` | Code read only |
| `anthropic` | `AnthropicPxpipeQuirk` | Opt-in proxy rewrite of system prompt/tool docs into PNGs for cost reasons, no-ops if pxpipe not running | Code read only, not installed here |
| streaming (`call_llm_stream_events`) | — | Usage/cost is read from `chunk.usage`, which not all providers populate on every backend the same way; Ollama streaming usage behaviour not independently exercised | UNTESTED live (streaming canary not run — time-boxed out) |

**Observed drift (live):** Ollama silently ignores the caller's `max_tokens` value —
confirmed by both canary calls (`max_tokens=30` in the request, `output_tokens=110`
and `108` actually generated). Any caller relying on `max_tokens` as a hard ceiling
for cost/latency control gets a different real ceiling on Ollama than on any other
provider, and nothing in the response signals that the cap was not honoured.

## 6. Cache red team (Phase 12) — semantic cache, `semantic_cache.py`

### 6a. Cache key composition

Scope = `(task_type, project_scope)` only (`router.py:4293-4297`,
`semantic_cache.check/store`). **Missing from the key**: model requested/selected,
system-prompt content beyond whatever text happens to be embedded, tool definitions,
session id, complexity/profile tier, configuration flags. `model_override` bypasses
the cache entirely (good — an explicit model pick never gets a cached answer from a
different model), but two *default-routed* prompts of very different complexity that
land close in embedding space and share a `task_type` can hit each other's cached
answer even if the router would ordinarily have escalated one of them to a stronger
model. This is a routing-quality risk, not just a correctness one — flag as DESIGN
RISK.

### 6b. Equivalence-guard coverage — empirically measured (real `nomic-embed-text`
embeddings via Ollama, real `_discriminator`/`_equivalence_veto` code, not simulated)

| Pair (topic) | cosine (nomic-embed-text) | Veto fires? | False-hit at threshold 0.98 (current default) | False-hit at 0.95 (previous shipped default, still settable via env) |
|---|---|---|---|---|
| "Should I merge…" / "Should I **not** merge…" | 0.964 | No | No | **YES** |
| "…delete the temp file." / "…should **not** delete…" | 0.973 | No | No | **YES** |
| "authorized to access" / "**not** authorized to access" | 0.930 | No | No | No (below 0.95, above 0.90) |
| "This request is urgent." / "…is **not** urgent." | 0.898 | No | No | No |
| "Deploy…" / "**Do not** deploy…" | 0.892 | No | No | No |
| account A / account B | 0.878 | No | No | No |
| US region / UK region | 0.857 | No | No | No |
| Python 3.11 / Python 3.12 | 0.929 | **Yes** (`nums` differ) | — | — |
| firewall allow / firewall deny | 0.949 | **Yes** (`pol` differ) | — | — |
| previous invoice total / current invoice total | 0.886 | No | No | No |
| deployment succeeded (past) / will succeed (future) | 0.856 | No | No | No |

**CONFIRMED gap: negation.** `_POLARITY_GROUPS` (`semantic_cache.py:238-247`) covers
increase/decrease, enable/disable, ascending/descending, before/after — it has **no
group for plain negation** ("not", "don't", "never"). Two of four negation pairs
tested scored **above the previously-shipped 0.95 default** (0.964, 0.973) with zero
veto, i.e. they would have been served as cache hits under the threshold this project
shipped before C-03 raised it, and are still one config change
(`LLM_ROUTER_SEMANTIC_CACHE_THRESHOLD=0.95`) away from being exploitable today. At the
**current** default (0.98) none of the measured pairs cross the line — so the
concrete incident class (cosine ≥ 0.95 serving a wrong passport's answer) is **closed
at the current default**, but the underlying discriminator has a real, reproducible
blind spot that the threshold alone is covering, not fixing — exactly the shape of
bug this project's own C-03 comment warns against ("a higher threshold alone cannot
fix this"). Author's own words apply here to a case the author didn't cover.

**Account-letter and region pairs** (a class the mission brief specifically asked
for) scored comfortably low (0.86–0.88) on this embedding model and are not
exploitable at any threshold ≤ 0.98 measured here — no finding.

**Numeric and enable/disable-style polarity pairs work as designed** — both vetoed
correctly, matching the module's own documented test cases.

### 6c. Cross-session / concurrent-write / stale-entry testing

Not exercised live (would require two concurrent DB writers against the shared
`usage.db` under a controlled race, and is a narrower risk given `aiosqlite` WAL mode
plus the per-write `_repair_shared_db_perms`/try-except-wrapped design already
observed). Time-boxed out — **UNTESTED**, not INVALIDATED.

## 7. Answer to the mission question

**Can the router select a model that cannot do the job, and does anything stop it?**

Yes, trivially, for three of the four capability dimensions in scope (tools, vision,
JSON/structured output) — and nothing stops it except one HTTP endpoint's explicit
refusal (`/v1/chat/completions`, `/v1/messages`), which does not cover `/v1/responses`
or the Python-level `route_and_call`/`call_llm` API. The capability registry that
could gate this decision (`model_registry.ModelMetadata.capabilities`) is imported
for exactly one unrelated constant and is never consulted for model selection.
Context-window mismatch is real but has an accidental safety net for Ollama specifically
(empty-content → failover) and none demonstrated for other providers.
