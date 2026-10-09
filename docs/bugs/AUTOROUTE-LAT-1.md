---
id: AUTOROUTE-LAT-1
status: fixed in `perf/auto-route-latency`
---
## AUTOROUTE-LAT-1. The auto-route hook did avoidable work on every prompt, and its tail was load-driven (PG4 / P2-G-3)

- **Symptom.** `router_added_ms` p95 653 ms (n = 274, `~/.llm-router/hook_latency.jsonl`, last 24 h to
  2026-10-09 13:47Z; 922 ms over all 315 rows) against the 300 ms sync-hook bar. Phase attribution on those
  rows: `import` p50 98 / p95 826 / max 2,711 ms (315 rows), `session_io` p95 106 / max 2,106 ms. The 65 rows
  over 300 ms had `import` as their dominant phase in 59. The tail follows load, not idle gaps: p(over 300 ms)
  is 15% for gaps under 10 s and 17% for 60-300 s. The only rows with `load1` recorded (n = 59, newest) give
  p50 63 / p95 84 / max 222 ms at load1 < 4 (n = 49) and p95 227 ms at load1 4-8 (n = 9): the contended rows
  come from before `load1` was recorded.
- **Cause (outside-in, `scripts/hook_wall.py` method, fixture `auto-route.json`, scratch HOME, load1 1.2-3.7).**
  Per-prompt CPU went to work no prompt needed, which is what a loaded machine stretches into a 650 ms tail:
  1. `llm_router.session_store` and `llm_router.persist_redaction` call `get_config()` for three fields no
     operator had set: `import llm_router.config` is ~31-52 ms (pydantic, pydantic-settings), inside the
     `session_io` phase (30 ms p50) on every prompt.
  2. `llm_router.logging` imported structlog (and rich, via `structlog.dev`) at import: 25-47 ms, because
     `auto-route.py` calls `configure_logging()` first thing in `main()` and `model_tracking` calls
     `get_logger()` at import.
  3. `llm_router/__init__.py` imported `response_router` and `sdk` (and `llm_router.types`) eagerly, and
     resolved `__version__` through `importlib.metadata` (~9 ms), for every `import llm_router.<x>`.
  4. `auto-route.py` ran `available_ollama_models()` at import. With a stale or missing `discovery.json` that is
     a synchronous HTTP probe (timeout 2 s) on the host's critical path, and with Ollama down the failed probe
     never refreshes the cache, so it repeated on every prompt. `urllib.request` (+ `http.client`, email
     parser; ~8 ms) was imported at start by the hook, `model_discovery`, `direct_executor` and
     `semantic_classify` although the common prompt makes no HTTP call.
- **Fix (behaviour preserved except where stated).**
  - `llm_router/__init__.py`: `route`, `RouteResult`, `RoutingError`, `route_response_explanations` and
    `__version__` resolve on first access (PEP 562). `from llm_router import route` is unchanged.
  - New `llm_router/config_lite.py`: `config_value(field)` returns RouterConfig's declared default without the
    import when `llm_router.config` is not loaded and no environment variable or `.env` (state dir, cwd) can name
    the field; otherwise it calls `get_config()` as before. Used by `session_store` (3 sites),
    `persist_redaction` and the hook's draft-context budget. **Stated change:** on the default path the hook no
    longer runs `get_config()`'s side effects (`load_disk_keys`, `apply_keys_to_env`); the hook's drafts go to
    free/local providers only (`_free_tier_draft_chain`), so provider keys in the hook's env are not read there.
  - `llm_router/logging.py`: `get_logger()` returns a lazy proxy; new `configure_logging_lazily()` installs a
    one-shot import hook that runs the same `configure_logging()` right after anyone imports structlog (direct
    importers such as `failopen` included), so structlog's stdout PrintLogger can never be the active logger. The
    stdlib stderr handler and level are set immediately. While deferred, a `debug` call the stdlib level would
    drop returns without importing structlog. A process that never calls `configure_logging_lazily` is unchanged.
  - `model_discovery.available_ollama_models_nowait()`: env, then fresh cache, then stale cache; a stale or
    missing cache is refreshed by a detached `python -m llm_router.model_discovery` child, at most one per 300 s
    (`discovery.probe_attempt`). Only the real hook process (`__name__ == "__main__"`) starts it. **Stated
    change:** a model pulled today is picked up by the next prompt after the child finishes, not the same prompt.
    `available_ollama_models()` (draft chain, session-start, CLI) still probes.
  - `urllib.request` is imported on first use (`llm_router.lazy_urllib`, same shape in the hook; the hook's
    `hook.urllib.request` still resolves, so tests that patch it are unchanged).
  - `auto-route.py` hook version 49 -> 50, both copies byte-identical.
- **Result (`hook_wall.measure`, n = 200 cold + 200 warm per arm, two interleaved passes, load1 median 2.4 / 3.1
  cold, max 3.7; no row above load 4).** Wall p95 cold 194.9 -> 124.8 ms, warm 168.8 -> 106.4 ms; in-process
  `elapsed_ms` p95 125.6 -> 73.1 ms (cold). Per-phase table (200 runs, load1 1.2-1.9): `import` 55.6 -> 21.9 ms,
  `session_io` 29.6 -> 2.1, `classify` 8.8 -> 9.5, `db_write` 1.2 -> 1.0, unattributed 20.4 -> 32.0 (module
  imports moved out of `import` and `session_io` into the code that uses them), CPU 181 -> 116 ms.
- **Not fixed.** Python start-up and teardown (~53 ms) and the hook's own 6,000-line script compile and ~86
  module-level regex compiles (~16 ms) are not reduced; a launcher stub with a cached `.pyc` would remove the first.
  `llm_router.profiles` (policy YAML, ~10 ms) and `llm_router.classify` (~8 ms) are still imported on the routed
  path. Under heavy contention (load1 above 8) the tail is still contention: one paired run at load1 ~25 (n = 60)
  gave in-process p95 561 -> 325 ms.
- **Test.** `tests/test_pg4_auto_route_latency.py` counts work instead of timing it: the hook run as `__main__`
  imports none of structlog, rich, pydantic, `llm_router.config`, the SDK, `importlib.metadata`, `urllib.request`
  and `http.client` (twice: cold and second prompt); makes no socket connect with a cold discovery cache; starts
  one detached child per window; the lazy-logging contract (structlog configured the moment anything imports it,
  nothing reaches stdout, a filtered `debug` imports nothing, an unconfigured process is unchanged);
  `config_lite.DEFAULTS` equals RouterConfig's declared defaults and the real path is taken for an env var, a state
  `.env` and a cwd `.env`; `__init__` exports and `__version__` resolve. 15 of 20 fail on main; the other 5 pin
  behaviour that must not change (including an anti-vacuity check that the routed prompt reached `classify`).
