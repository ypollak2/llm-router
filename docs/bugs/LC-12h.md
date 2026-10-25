---
id: LC-12h
status: fixed in this change
---
## LC-12h. "Secrets never leave your machine" had no code behind it, and loopback Ollama calls followed HTTP proxies

- **Symptom.** README.md said "Secrets never leave your machine. A prompt containing an API key, token or
  private key routes to local models only — fail-closed." A prompt with a GitHub token sent through
  `route_and_call` with a chain `[openai/gpt-4o, ollama/<local>]` went to the cloud model. Separately, with
  `HTTP_PROXY` / `ALL_PROXY` set and `NO_PROXY` unset, an ordinary loopback Ollama call (`/api/generate` with the
  prompt, LiteLLM's `/api/show` lookups, the `/api/tags` probe, the semantic cache's `/api/embeddings`) went to
  the proxy (LC-12g measured it for the text-job path; `tests/test_lc12h.py` for the rest).
- **Cause.** `engine.Decision.local_only`, `engine.secret_enforcement` and `engine.guard` were read only by
  tests; no door consumed them. LiteLLM 1.103.0 and `urllib.request.urlopen` honour proxy env for `localhost`
  too, and LC-12g's direct client covered the pass-only text job only.
- **Fix.** `local_privacy.py`: every Ollama call to a loopback daemon goes through a per-call client that ignores
  proxy env; LiteLLM's model-info lookup is pre-empted by a direct `/api/show`; loopback urllib requests use a
  no-proxy opener. The secret door in `route_and_call`, `route_and_stream` and `llm_stream` cuts the chain to
  loopback local models or refuses, and its dispatch is private (no cache, no judge queue, no emergency fallback).
  README reworded to what is true, with its limits. A test-isolation follow-on: the model-map entries are
  process-wide, so `tests/conftest.py::_reset_local_privacy` removes them after each test (a combined run failed
  5 tests before it).
- **Test.** `tests/test_lc12h.py` (socket-level trace, both transports). Mutants, each red: dropping the direct
  client for ordinary routes (5 tests), re-enabling the semantic-cache check on the secret door (2), reverting
  `semantic_cache._cache_disabled` to the pass-only marker (2).

### Repair 1 (PR #721 review FAIL)

- **Symptom.** The door refused placeholders (`AKIAIOSFODNN7EXAMPLE`, `ghp_` + `x` * 36, `postgres:postgres@db`,
  `YOUR_API_KEY_HERE_...`, JWT fixtures) and scanned all history, so one placeholder refused every later turn of a
  chat; the refusal had no category, no remedy, no override and was a 502 at the gateway. `sdk.route` bypassed the door.
  `db_url_password` took 1.6 s on 48k chars. `reset_secret_dispatch` had no test. A failed `/api/show` was memoized forever.
- **Cause.** The detector's shapes were built to log, then reused to refuse. Nothing separated placeholders from values.
- **Fix.** `engine.secret_findings` (high-precision set, placeholder and entropy tests, bounded quantifiers, windows);
  `local_privacy.scan_secrets` / `refusal_message` / `split_override`; 422 at the gateway and route server; the SDK
  door; TTLs in `ensure_ollama_model_info`; `local_privacy.open_url` at every prompt-carrying urllib Ollama site.
- **Test.** `tests/test_lc12h_repair.py`: 0 refusals on 31 placeholders and 260 ordinary prompts, refusals on 250
  random real-shaped secrets (10 kinds x 25 seeds), the marker test, the SDK test, the straddle and ReDoS tests.
  Rule: a shape that logs is not a shape that refuses; a refusal needs a measured placeholder corpus first.
