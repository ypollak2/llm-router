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
