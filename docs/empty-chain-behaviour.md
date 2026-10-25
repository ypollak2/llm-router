# Empty-chain behaviour

When the resource map is fresh and every entry of a chain is a provider the map knows and has no connected row for, `map_reader.apply_to_chain` returns `[]` (PR #423). The capability filter (P2.4) can empty a chain the same way, when every remaining entry is known to lack a capability the request needs. Each caller then does its own existing "nothing routable" thing, listed below, and `tests/test_p24_empty_chain_table.py` parses this file and fails if the observed behaviour differs from the table.

| caller | trigger | behaviour |
| --- | --- | --- |
| router | every chain entry map-known and disconnected | raises_no_providers |
| hook | every chain entry map-known and disconnected | falls_through_to_claude |
| proxy | every chain entry map-known and disconnected | forwarded_policy_kept |
| sdk | every chain entry map-known and disconnected | routing_error |
| gateway | every chain entry map-known and disconnected | http_400 |

The tokens mean:

- `raises_no_providers`: `route_and_call` (the MCP door) raises `ValueError("No providers available ...")` and the host model answers.
- `falls_through_to_claude`: `execute_chain` returns `None`; the hook lets Claude answer.
- `forwarded_policy_kept`: `choose_model` returns `model: None`; the proxy forwards the call upstream with decision `forwarded` and reason `policy_kept`.
- `routing_error`: the SDK's `route()` raises `RoutingError`.
- `http_400`: the gateway maps the router's `ValueError` to HTTP 400 with the router's message as `detail` (a client that retries only on 5xx will not retry it).

The gateway returns 400, not 503, because it maps the router's `ValueError`; a 503 would be a separate decision.

The capability filter cannot empty a chain on a tools need from the prompt text: the tools need is hard only when the request body carries a real tools array (the proxy), and the prompt-text tools regex is traced as `capability.shadow` without excluding anything (D-165). Vision and window can empty a chain.

Gateway image parts (D-165 (4)), on every completion route:

- Image parts in the newest user turn (or the Ollama generate `images` field): HTTP 400 with the images-unsupported detail; the router is not called.
- Image parts only in earlier turns: flattened as before P2.4. The history goes to the context pack, which renders each image as `[image]`; there is no vision need, and the newest turn routes as text.

The newest user turn is the trailing run of role-less content parts (a bare Responses `input` part list), else the last item whose role is `user`. `tests/test_p24_empty_chain_table.py` and `tests/test_p24_capability_hard.py` pin both rows.

## Filter-emptied chains (P2.4, F14)

The capability filter empties a chain when every entry is KNOWN to lack what the request needs. Needs are strong signals only: vision from image parts or the host's `[Image #N]` marker, window from the irreducible tokens, tools from a real tools array (proxy) or from the agent loop itself (`execute_agent` always needs tools). The prompt-text tools regex never excludes (shadow only). An unknown capability never excludes. `tests/test_f14_capability_coverage.py` parses this table and fails if an observed behaviour differs.

| door | trigger | behaviour |
| --- | --- | --- |
| router | every entry known to lack vision | raises_no_providers |
| stream | every entry known to lack vision | raises_value_error |
| hook | every entry known to lack vision | falls_through_to_claude |
| agent | every ollama entry known to lack tools | returns_none |
| subagent | every ollama entry known to lack tools | real_spawn |
| sdk | every entry known to lack vision | routing_error |
| gateway | every entry known to lack vision | http_400 |
| route | every entry known to lack vision | http_400 |
| proxy | tools array, every entry known to lack tools | forwarded_policy_kept |
| local_mode | the pinned model known to lack what the request needs | forwarded_policy_kept |

The extra tokens mean:

- `raises_value_error`: `route_and_stream` raises `ValueError("No providers available ... Capability filter excluded: ...")` on the first iteration, before any `route.started` event (the same place its existing empty chain raises).
- `returns_none`: `execute_agent` attempts nothing and returns `None`; its callers (auto-route, the SDK) treat `None` as they treat a failed loop.
- `real_spawn`: subagent delegation (`_try_direct_subagent`) returns `None`, so the real subagent is spawned.
- `forwarded_policy_kept` for `local_mode`: the pin is held to the filter as in `choose_model`; a pin the filter excludes keeps the call on Claude (`model: None`, empty `chain_head`).

Native `/route` (the gateway endpoint) refuses image parts in `prompt` and the Ollama-style `images` field with the same HTTP 400 as the other gateway routes; `messages` there is history and is flattened, never refused (D-165 (4)). The zero-dependency `route_server` refuses the same requests with the same HTTP 400 and text (F33); the shared helpers live in `route_server.py`.

Pinned local model, small window (known limit, not changed): the `local_mode` filter compares the irreducible tokens only (system prompt, newest user turn, tool schemas), as everywhere. The rule is `needs.tokens > WINDOW_MARGIN * caps.window` with `WINDOW_MARGIN = 0.9` (`src/llm_router/capability_filter.py:44` and `:276`): a pin is dropped (the call stays on Claude) only when the irreducible tokens exceed 0.9 x the model's context window. A pin whose window is unknown (`caps.window is None`) always passes that check. A pinned local model whose window is known to be small therefore passes whenever those tokens fit, and the call is forwarded to it with the untrimmed request body: nothing on the `--serve local-agent` pinned path trims or packs the history to the pin's window.

Consequence of the untrimmed forward: when the request exceeds the pinned Ollama model's `num_ctx`, Ollama's OpenAI-compatible `/v1` endpoint can silently drop leading messages. It still returns 200, and `truncate: false` is ignored there. This is known Ollama behaviour, not something llm-router detects.

Chains the map must not empty:

| situation | behaviour |
| --- | --- |
| provider with no map row | keeps its slot |
| stale map | chain unchanged |
| other-schema map | chain unchanged |
