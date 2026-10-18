# Integrating with llm-router

llm-router has four doors. Every framework below uses one of them; most use the gateway, which needs
one line of configuration. Each section links a runnable example under [`examples/`](../examples/)
that CI runs on every change against a deterministic offline fake backend (no keys, no provider is
called): see [`.github/workflows/examples.yml`](../.github/workflows/examples.yml).

| Door | Use it when | Example |
|---|---|---|
| Gateway | your framework or client takes a `base_url` | [`langgraph`](../examples/langgraph/), [`openai_agents`](../examples/openai_agents/), [`crewai`](../examples/crewai/), [`pydantic_ai`](../examples/pydantic_ai/), [`claude_agent_sdk`](../examples/claude_agent_sdk/), [`typescript_openai`](../examples/typescript_openai/) |
| SDK | Python code that prefers a function call to a server | [`python_sdk`](../examples/python_sdk/) |
| MCP | an MCP host (Claude Code, Cursor, Claude Desktop) calls routed tools | [`mcp`](../examples/mcp/) |
| Hooks / proxy | one Claude Code session, per-call | [`proxy`](../examples/proxy/) |

## The gateway: one `base_url`

```bash
llm-router gateway                                   # http://127.0.0.1:17900
export OPENAI_BASE_URL=http://127.0.0.1:17900/v1     # OpenAI-compatible clients
export ANTHROPIC_BASE_URL=http://127.0.0.1:17900     # Anthropic clients
```

Endpoints: `/v1/chat/completions`, `/v1/responses`, `/v1/messages` (plus Ollama's `/api/chat` and
`/api/generate`). Send `model: "auto"` to let the router choose, or a model name to pin it. Tool calls
work on the three `/v1` endpoints. If you set `LLM_ROUTER_GATEWAY_TOKEN` (or have a `gateway.token`
in the state dir), send it as the API key.

**Streaming.** `stream: true` returns server-sent events in each endpoint's own protocol. The stream is
the routed call's complete answer re-framed as chunks, for every provider; it is not provider token
streaming, so the first token arrives when the model has finished. If routing fails after the
response has started, the HTTP status is still **200** and the failure arrives as an `error` event in
the endpoint's own shape (a chat stream sends no `[DONE]` after it); official clients raise on it.
Requests the gateway refuses (hosted tools, images) are an HTTP 400 before any event.
The error event never carries provider error text (it can echo the prompt). A failed route sends the
exception class name and a fixed message: `<ExceptionClass>: routing failed` (HTTP 502, `api_error`),
`<ExceptionClass>: upstream rate limit` (HTTP 429, `rate_limit_error`), or `stream failed: <ExceptionClass>`
for a fault inside the gateway. The secret-scrubbed detail (200 characters) goes to the local log
(`llm_router.gateway`) only. A 429 is sent only when the exception itself is a provider `RateLimitError`
(by type, also through its `__cause__` / `__context__`); the router's "All models failed" error, budget
refusals and permanent "quota exceeded" errors stay 502. When the rate limit carries a retry-after, the
non-streamed 429 has a `Retry-After` header and the message ends `; retry after N s` (so does the stream's
error event); otherwise neither is sent. The error's `type` is `invalid_request_error` for a 400, and the
non-streamed endpoints return the same status codes.

The native `POST /route` and `POST /ground` and the zero-dep `llm_router.route_server` follow the same rule: a failure sends `<ExceptionClass>: routing failed` (`/route`, HTTP 502; the gateway's `/route` also sends 429 for a provider rate limit), `<ExceptionClass>: grounding check failed` (`/ground`), `<ExceptionClass>: feedback failed` (`/feedback`, HTTP 500) or `<ExceptionClass>: bad json` (HTTP 400), never the exception text. A 400 for an invalid request (for example a bad `model` format or an unusable `messages` list) carries the router's own message, which is written by the router and holds no provider text.

**`system` on `/v1/messages`.** A string is forwarded as is. A list of blocks (Claude Code and the Claude
Agent SDK send one) is reduced to its text blocks, joined with a single space; blocks of any other type
and `cache_control` markers are dropped, silently.

The thin adapters in `llm_router.frameworks` build the framework's own model object with this URL
(`LLM_ROUTER_GATEWAY_URL` overrides it) and the gateway token if one is set. They never import a
provider SDK, and the framework packages are not llm-router dependencies: install the one you use.

### LangGraph

```python
from llm_router.frameworks.langgraph import LangGraphAdapter
agent = create_react_agent(LangGraphAdapter().wrap_model("auto"), tools)
```
Runnable, streaming plus a tool call: [`examples/langgraph`](../examples/langgraph/).

### OpenAI Agents SDK

```python
from llm_router.frameworks.openai_agents import OpenAIAgentsAdapter
agent = Agent(name="x", model=OpenAIAgentsAdapter().wrap_model("auto"), tools=[...])
```
Runnable, `Runner.run_streamed` plus a tool call: [`examples/openai_agents`](../examples/openai_agents/).

### CrewAI

```python
from llm_router.frameworks.crewai import CrewAIAdapter
agent = Agent(role="...", goal="...", backstory="...", llm=CrewAIAdapter().wrap_model("auto"))
```
Runnable: [`examples/crewai`](../examples/crewai/).

### Pydantic AI

```python
from llm_router.frameworks.pydantic_ai import PydanticAiAdapter
agent = Agent(PydanticAiAdapter().wrap_model("auto"))
```
Runnable, `run_stream` plus a tool call: [`examples/pydantic_ai`](../examples/pydantic_ai/).

### Claude Agent SDK

```python
from llm_router.frameworks.claude_agent_sdk import ClaudeAgentSdkAdapter
async for message in query(prompt="...", options=ClaudeAgentSdkAdapter().wrap_model("auto")):
    ...
```
The SDK's Claude Code process reads `ANTHROPIC_BASE_URL`; the adapter sets it (and the key) in the
options' `env`. Runnable: [`examples/claude_agent_sdk`](../examples/claude_agent_sdk/).

### TypeScript (and any other language)

```ts
const client = new OpenAI({ baseURL: "http://127.0.0.1:17900/v1", apiKey: "unused" });
```
Runnable, streaming plus a tool call with the official `openai` client:
[`examples/typescript_openai`](../examples/typescript_openai/).

### Agno

Agno has a concrete adapter that predates this page: `llm_router.frameworks.agno.RouteredModel`.

### Hermes-format models

There is no Hermes adapter: "Hermes" names several tool-call formats, not a framework. A Hermes-format
model behind Ollama or any OpenAI-compatible server is routed like any other model; call the gateway
with an OpenAI client.

## SDK

```python
from llm_router import Router
result = Router(budget_usd=0.50).run("Summarize this ...")
print(result.text, result.model)
```
No server; the call and its ledger row happen in your process. Runnable: [`examples/python_sdk`](../examples/python_sdk/).

## MCP

`llm-router install` registers the MCP server with your host; the host then lists the routed tools
(`llm`, `llm_route`, `llm_edit`, ...). Runnable client over stdio with the official `mcp` package:
[`examples/mcp`](../examples/mcp/).

## Hooks / proxy

`llm-router proxy` sits at `ANTHROPIC_BASE_URL` for one Claude Code session and forwards each call
unchanged unless a step class is enabled (see [proxy.md](proxy.md)); it writes one row per call to
the proxy ledger. The hooks that `llm-router install` adds work beside it. Runnable, with a loopback
fake in place of Anthropic: [`examples/proxy`](../examples/proxy/).
No example runs a hook script itself: the hooks run inside Claude Code, which the proxy example
does not start. The example exercises the proxy half of this door.

## Running an example yourself

```bash
pip install -e . -r examples/langgraph/requirements.txt     # the framework, pinned, for this example only
python examples/langgraph/main.py                           # prints the answer, then OK
python examples/fake_backend.py                             # or: serve the fake-backed gateway by hand
```

### What the examples' offline guarantee covers

`examples/fake_backend.py` refuses every non-loopback `socket.connect` and `connect_ex` made by the
example's own Python process, and records it. It cannot see inside a child process (the MCP server, the
proxy, `node`, Claude Code). Those are kept local by configuration: only a loopback gateway or upstream
URL is passed to them, and `fake_backend.child_env()` points `HTTPS_PROXY`, `HTTP_PROXY` and `ALL_PROXY`
at a closed loopback port (with `NO_PROXY=127.0.0.1,localhost,::1`). A child that ignores proxy variables
is not stopped by this, so an example that spawns a new kind of child needs its own check.
