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

Chains the map must not empty:

| situation | behaviour |
| --- | --- |
| provider with no map row | keeps its slot |
| stale map | chain unchanged |
| other-schema map | chain unchanged |
