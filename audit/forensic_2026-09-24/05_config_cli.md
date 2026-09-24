# Domain 05 — Configuration and CLI

Auditor scope: brief §8 (env-var/config-key/CLI-flag dead-code parts), §25 (config
precedence, boolean explosion), §27 (API surface: imports, env vars, YAML, hooks
contracts), §28 (CLI hierarchy/help/consistency vs docs), §73 (one source of truth for
env vars and CLI commands). Baseline: git worktree `llm-router-forensic`, detached at
`3c96d23`. All Python execution below used
`HOME=$(mktemp -d) PYTHONPATH=.../src <repo>/.venv/bin/python`
against the baseline; no full test suite was run (targeted files only); no Ollama/paid
APIs were invoked.

**Methodology note, in the spirit of this repo's own CLAUDE.md:** a mechanical `grep`
inventory of every `LLM_ROUTER_[A-Z0-9_]+`-shaped string across the tree was cross-checked
against `src/llm_router/env_registry.py`'s hand-curated `ENV_REGISTRY` (itself validated
by an independent AST scan in `tests/test_env_registry.py`). The first cross-check pass
produced ~85 false "missing from registry" results that turned out to be a `comm`
locale-collation bug (`sort` without `LC_ALL=C` orders `_` differently across locales,
so `comm -23` on two differently-sorted files silently fabricates diffs). Redone with
`LC_ALL=C` throughout, and every remaining candidate was individually re-verified with a
literal grep against `env_registry.py` before being reported below. This is recorded
because it is exactly the kind of computed-number error the brief's evidence rules exist
to prevent, and because a wrong "100+ vars missing" headline was one editing pass away
from shipping.

## 1. Overview

- 44 CLI subcommand modules under `src/llm_router/commands/`, dispatched from a single
  hand-rolled `if/elif` chain in `src/llm_router/cli.py:main()` (not argparse
  subparsers). 5 `[project.scripts]` console entry points (`llm-router`,
  `llm-router-onboard`, `llm-router-install-hooks`, `llm-router-quickstart`,
  `llm-router-isolation-test`).
- `src/llm_router/env_registry.py` declares **244** environment variables read under
  `src/llm_router/` (explicitly scoped — it does not cover `scripts/`, which it says
  has ~18 more). This is a genuinely good, self-defending "one source of truth"
  mechanism (see §7 below) — but it has a real, evidenced blind spot (CFG-007).
- Config precedence is not one system: at least three separate modules
  (`repo_config.py`, `enforce_config.py`, `safe_config.py`) each state a *different*
  precedence order for different config domains, and one of them (`safe_config.py`)
  directly contradicts the actual behavior of the code that consumes it (CFG-004).
- The CLI's biggest issue is not missing features but inconsistency: only 1 of 44
  commands supports machine-readable output, ~45% have no `--help` handling at all
  (confirmed by executing them), and at least one command's documented exit-code
  contract is silently discarded by the dispatcher — the exact bug class a code
  comment says was already fixed once, for a different command (CFG-010).

## 2. Complete `LLM_ROUTER_*` inventory (mechanical, from `ENV_REGISTRY`)

Generated from `env_registry.ENV_REGISTRY` (244 entries: name → category, first/owning
module, module-count-at-registration), joined with `test`/`doc` reference counts from a
repo-wide grep (`tests/`, `docs/`, `guide/`, `architecture/`, `README.md`, `SECURITY.md`,
`CHANGELOG.md`) and `.env.example` presence. "Notes" are manually verified findings, not
part of the mechanical generation. `?` in test/doc columns = not part of the
`LLM_ROUTER_`-scoped grep pass (applies to provider keys / platform / external-tool
vars, listed with their category instead).

| Env var | Category | Owner module (first reader) | Test refs | Doc refs | `.env.example` | Notes |
|---|---|---|---|---|---|---|
| `ANTHROPIC_ADMIN_KEY` | provider_credential | `invoice_reconciliation/anthropic.py` | ? | ? | | |
| `APPDATA` | platform | `commands/doctor.py` | ? | ? | | |
| `BENCH_SANDBOX` | test_only | `routing_quality.py` | ? | ? | | |
| `CLAUDE_CODE_PATH` | external_tool | `claude_agent.py` | ? | ? | | |
| `CLAUDE_CODE_SESSION_ID` | external_tool | `hooks/agent-depth-release.py` | ? | ? | | |
| `CLAUDE_SESSION_ID` | external_tool | `hooks/context-capture.py` | ? | ? | | |
| `CODEX_PATH` | external_tool | `codex_agent.py` | ? | ? | | |
| `DEEPSEEK_API_KEY` | provider_credential | `commands/doctor.py` | ? | ? | Y | |
| `GEMINI_ACCESS_TOKEN` | provider_credential | `invoice_reconciliation/gemini.py` | ? | ? | | |
| `GEMINI_API_KEY` | provider_credential | `commands/demo.py` | ? | ? | Y | |
| `GEMINI_CLI_PATH` | external_tool | `gemini_cli_agent.py` | ? | ? | | |
| `GEMINI_CLI_TIER` | external_tool | `gemini_cli_quota.py` | ? | ? | | |
| `GEMINI_PROJECT_ID` | provider_credential | `invoice_reconciliation/gemini.py` | ? | ? | | |
| `GOOGLE_API_KEY` | provider_credential | `commands/demo.py` | ? | ? | | Deliberate alias for `GEMINI_API_KEY` via `or` fallback (auto-route.py:392 etc.) — not a bug. |
| `HELICONE_API_KEY` | provider_credential | `integrations/helicone.py` | ? | ? | | |
| `HOST` | external_tool | `server.py` | ? | ? | | |
| `LLM_ROUTER_ADMIN_ACTIONS_PATH` | llm_router | `admin_actions.py` | 3 | 0 | | |
| `LLM_ROUTER_AGENTIC_MODEL` | llm_router | `hooks/agent-route.py` | 2 | 0 | | |
| `LLM_ROUTER_AGENTS_CONFIG` | llm_router | `tools/agents.py` | 0 | 0 | | |
| `LLM_ROUTER_AGENT_COMMANDS` | llm_router | `hooks/agent_writes.py` | 11 | 3 | | |
| `LLM_ROUTER_AGENT_LOOP_BUDGET_S` | llm_router | `hooks/auto-route.py` | 3 | 0 | | |
| `LLM_ROUTER_AGENT_NUM_CTX` | llm_router | `hooks/agent_loop.py` | 0 | 0 | | |
| `LLM_ROUTER_AGENT_POLICY_MODE` | llm_router | `router.py` | 7 | 0 | | |
| `LLM_ROUTER_AGENT_ROUTE_ALLOW` | llm_router | `hooks/agent-route.py` | 0 | 0 | | |
| `LLM_ROUTER_AGENT_TEMPERATURE` | llm_router | `hooks/agent_loop.py` | 0 | 0 | | |
| `LLM_ROUTER_AGENT_WINDOW` | llm_router | `hooks/context_budget.py` | 1 | 0 | | |
| `LLM_ROUTER_AGENT_WRITES` | llm_router | `hooks/agent_writes.py` | 14 | 5 | | Mode var (propose/write), not boolean. See CFG-013. |
| `LLM_ROUTER_ALERT_WEBHOOK` | llm_router | `alerts.py` | 8 | 1 | | |
| `LLM_ROUTER_ALLOWED_HOSTS` | llm_router | `route_server.py` | 0 | 0 | | |
| `LLM_ROUTER_ALLOW_PUBLIC_BIND` | llm_router | `net_bind.py` | 2 | 0 | | Indirect-read exemplar; hand-declared in `_INDIRECT_READS`. |
| `LLM_ROUTER_ALLOW_STUBS` | llm_router | `cost.py` | 13 | 0 | | |
| `LLM_ROUTER_ALLOW_SUBAGENTS` | llm_router | `hooks/agent-route.py` | 2 | 0 | | |
| `LLM_ROUTER_ANOMALY_THRESHOLD` | llm_router | `session_spend.py` | 2 | 0 | | |
| `LLM_ROUTER_ANSWER_VALUE_USD` | llm_router | `telemetry.py` | 0 | 0 | | |
| `LLM_ROUTER_AUDIT_DISABLED` | llm_router | `misroute_audit.py` | 14 | 8 | | Indirect-read exemplar; hand-declared in `_INDIRECT_READS`. |
| `LLM_ROUTER_AUDIT_PATH` | llm_router | `enterprise/audit.py` | 0 | 0 | | |
| `LLM_ROUTER_BANDIT` | llm_router | `router.py` | 19 | 1 | | |
| `LLM_ROUTER_BASH_COMPRESS` | llm_router | `hooks/bash-compress.py` | 9 | 0 | | |
| `LLM_ROUTER_BASH_INTERCEPT` | llm_router | `hooks/tool_intercept.py` | 8 | 0 | | Observed live in this audit session (router compresses its own auditor's shell output). |
| `LLM_ROUTER_BENCHMARK_TTL_DAYS` | llm_router | `hooks/session-start.py` | 0 | 0 | | |
| `LLM_ROUTER_BLOCK_PROVIDERS` | llm_router | `router.py` | 12 | 0 | | |
| `LLM_ROUTER_BOUNDED_OPERATIONAL` | llm_router | `bounded_operational.py` | 6 | 8 | | |
| `LLM_ROUTER_BROKER_CONCURRENCY` | llm_router | `session_broker.py` | 0 | 0 | | |
| `LLM_ROUTER_BROKER_SECRET_FILE` | llm_router | `session_broker.py` | 2 | 0 | | |
| `LLM_ROUTER_BROKER_SOCK` | llm_router | `session_broker.py` | 1 | 0 | | |
| `LLM_ROUTER_BUDGETS_DB_PATH` | llm_router | `budget_backend.py` | 0 | 0 | | |
| `LLM_ROUTER_BUDGET_BACKEND` | llm_router | `budget_backend.py` | 6 | 0 | | |
| `LLM_ROUTER_BUDGET_FORECAST_HORIZON_SECONDS` | llm_router | `budget_backend.py` | 4 | 0 | | |
| `LLM_ROUTER_BUDGET_FORECAST_MODE` | llm_router | `budget_backend.py` | 20 | 0 | | |
| `LLM_ROUTER_BUDGET_FORECAST_WINDOW_SECONDS` | llm_router | `budget_backend.py` | 4 | 0 | | |
| `LLM_ROUTER_BUDGET_POSTGRES_DSN` | llm_router | `budget_backend_postgres.py` | 11 | 0 | | |
| `LLM_ROUTER_CAPABILITY_ROUTING` | llm_router | `capabilities.py` | 5 | 8 | | |
| `LLM_ROUTER_CLASSIFY_LOCAL_ONLY` | llm_router | `hooks/auto-route.py` | 0 | 0 | | |
| `LLM_ROUTER_CLAUDE_DIR` | llm_router | `install_hooks.py` | 9 | 0 | | |
| `LLM_ROUTER_CLAUDE_SUBSCRIPTION` | llm_router | `commands/demo.py` | 37 | 5 | | Boolean, default off. See CFG-013. |
| `LLM_ROUTER_CLAUDE_TIMEOUT` | llm_router | `claude_agent.py` | 0 | 0 | | |
| `LLM_ROUTER_CODEX_BASELINE` | llm_router | `cost.py` | 5 | 0 | | |
| `LLM_ROUTER_CODEX_MODELS` | llm_router | `codex_agent.py` | 6 | 0 | | |
| `LLM_ROUTER_COMPRESS_EMIT` | llm_router | `hooks/bash-compress.py` | 3 | 0 | | |
| `LLM_ROUTER_COMPRESS_RESPONSE` | llm_router | `tools/text.py` | 13 | 0 | | |
| `LLM_ROUTER_CONFIDENCE_THRESHOLD` | llm_router | `hooks/auto-route.py` | 0 | 0 | | **CONFLICTING DEFAULTS**: `"2"` (canonical `src/llm_router/hooks/auto-route.py:291`) vs `"4"` (`.claude/hooks/auto-route.py:64`, stale committed copy). Untested, undocumented. See CFG-002. |
| `LLM_ROUTER_CONSTRAINED_TOOLS` | llm_router | `hooks/agent_loop.py` | 0 | 0 | | |
| `LLM_ROUTER_CONTEXT_INJECTION` | llm_router | `context_injection.py` | 1 | 1 | | |
| `LLM_ROUTER_CONTEXT_OPTIMIZER` | llm_router | `context.py` | 0 | 0 | | |
| `LLM_ROUTER_COST_PROFILE` | llm_router | `repo_config.py` | 19 | 9 | | Canonical name; `LLM_ROUTER_PROFILE` is its legacy fallback. See CFG-003. |
| `LLM_ROUTER_CP_AUDIT_PATH` | llm_router | `control_plane/audit.py` | 1 | 0 | | |
| `LLM_ROUTER_CP_ED25519_PRIVATE_KEY` | provider_credential | `control_plane/signing.py` | 2 | 0 | | |
| `LLM_ROUTER_CP_POSTGRES_DSN` | llm_router | `control_plane/store_postgres.py` | 2 | 0 | | |
| `LLM_ROUTER_CP_SIDECAR_TOKEN` | provider_credential | `control_plane/api.py` | 0 | 0 | | |
| `LLM_ROUTER_CP_STORE_PATH` | llm_router | `commands/cp.py` | 0 | 0 | | |
| `LLM_ROUTER_DB_PATH` | llm_router | `agentic/telemetry.py` | 77 | 1 | | |
| `LLM_ROUTER_DELEGATE` | llm_router | `hooks/enforce-route.py` | 23 | 0 | | Boolean, default ON (opt-out). See CFG-013. |
| `LLM_ROUTER_DEPLOYMENT_PROFILE` | llm_router | `hooks/auto-route.py` | 24 | 2 | | Canonical name for the identity/enterprise axis; `LLM_ROUTER_PROFILE` is its legacy fallback. See CFG-003. |
| `LLM_ROUTER_DEV_SRC` | llm_router | `commands/dev_refresh.py` | 1 | 0 | | |
| `LLM_ROUTER_DIRECT_EXECUTION` | llm_router | `hooks/auto-route.py` | 24 | 7 | | Boolean, default ON (opt-out). See CFG-013. |
| `LLM_ROUTER_DISABLE_CONTINUATION_BYPASS` | llm_router | `hooks/auto-route.py` | 0 | 0 | | |
| `LLM_ROUTER_DISABLE_LLM_CLASSIFIERS` | llm_router | `hooks/auto-route.py` | 12 | 0 | | |
| `LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS` | llm_router | `router.py` | 13 | 0 | | |
| `LLM_ROUTER_DISCOVERY_TTL_HOURS` | llm_router | `model_discovery.py` | 1 | 0 | | |
| `LLM_ROUTER_DYNAMIC_LEADERBOARD_ORDERING` | llm_router | `dynamic_routing.py` | 4 | 0 | | |
| `LLM_ROUTER_ENFORCE` | llm_router | `commands/doctor.py` | 147 | 8 | | **CONFLICTING DEFAULTS**: canonical resolver defaults to `"smart"`; `stop-enforce.py`/`status-bar.py` bypass it and default to `"hard"`. See CFG-001. |
| `LLM_ROUTER_ENSEMBLE` | llm_router | `ensemble.py` | 38 | 2 | | |
| `LLM_ROUTER_ENSEMBLE_PRIMARY` | llm_router | `ensemble.py` | 20 | 1 | | |
| `LLM_ROUTER_ENSEMBLE_SECONDARY` | llm_router | `ensemble.py` | 12 | 1 | | |
| `LLM_ROUTER_ENSEMBLE_TIMEOUT` | llm_router | `ensemble.py` | 0 | 0 | | |
| `LLM_ROUTER_ESCALATE_DEADLINE_S` | llm_router | `router.py` | 0 | 0 | | |
| `LLM_ROUTER_ESCALATE_MIN_PROMPT_TOKENS` | provider_credential | `router.py` | 0 | 0 | | Mis-categorized: this is a routing threshold, not a credential. Minor registry hygiene item. |
| `LLM_ROUTER_ESCALATE_ON_QUALITY` | llm_router | `router.py` | 9 | 0 | | |
| `LLM_ROUTER_ESCALATE_THRESHOLD` | llm_router | `router.py` | 0 | 2 | | |
| `LLM_ROUTER_EXECUTION_LEDGER_DB` | llm_router | `execution_ledger.py` | 59 | 0 | | |
| `LLM_ROUTER_EXPLAIN` | llm_router | `tools/routing.py` | 21 | 0 | | |
| `LLM_ROUTER_FORCE_COLOR` | llm_router | `surface_status.py` | 0 | 0 | | |
| `LLM_ROUTER_FREE_TIER_DRAFTS` | llm_router | `hooks/auto-route.py` | 0 | 0 | | |
| `LLM_ROUTER_GATES` | llm_router | `gates.py` | 20 | 0 | | |
| `LLM_ROUTER_GATEWAY_HOST` | llm_router | `presets.py` | 2 | 0 | | |
| `LLM_ROUTER_GATEWAY_PORT` | llm_router | `presets.py` | 2 | 0 | | |
| `LLM_ROUTER_GATEWAY_TOKEN` | provider_credential | `gateway.py` | 13 | 0 | | |
| `LLM_ROUTER_GATEWAY_URL` | llm_router | `presets.py` | 3 | 0 | | |
| `LLM_ROUTER_GEMINI_BASELINE` | llm_router | `cost.py` | 7 | 0 | | |
| `LLM_ROUTER_GEMINI_SUBSCRIPTION` | llm_router | `commands/demo.py` | 3 | 1 | | |
| `LLM_ROUTER_GEMINI_TIMEOUT` | llm_router | `gemini_cli_agent.py` | 0 | 0 | | |
| `LLM_ROUTER_GROUNDING_CHECK` | llm_router | `hooks/auto-route.py` | 2 | 1 | | |
| `LLM_ROUTER_HARNESS` | llm_router | `scripts/routerarena/apply_divert_router.py` | 0 | 0 | | Owner module is under `scripts/`, outside the registry's declared scope — an edge case of its own boundary. |
| `LLM_ROUTER_HEALTH_SNAPSHOT` | llm_router | `health.py` | 7 | 0 | | |
| `LLM_ROUTER_HF_TOKENIZERS` | provider_credential | `token_budget.py` | 0 | 0 | | |
| `LLM_ROUTER_HISTORY_RELAY` | llm_router | `hooks/auto-route.py` | 0 | 0 | | Privacy-relevant gate (`auto-route.py:4189`: "=off keeps direct..." from a privacy audit) — zero test refs, zero doc refs. Boolean, default ON. |
| `LLM_ROUTER_HOME` | llm_router | `hooks/agent_writes.py` | 331 | 2 | | Read at 60+ call sites, nearly all defaulting to `""` (falls through to `Path.home()` elsewhere); 3 sites (`prompt_capture.py`, `groundtruth/verifier_registry.py`, `groundtruth/pool.py`) instead default inline to `Path.home()` directly — same intent, two expressions. |
| `LLM_ROUTER_HOOK_BUDGET_S` | llm_router | `hooks/auto-route.py` | 0 | 0 | | |
| `LLM_ROUTER_HOOK_SLOW_SECONDS` | llm_router | `hooks/auto-route.py` | 0 | 0 | | |
| `LLM_ROUTER_HTTP_TIMEOUT` | llm_router | `hooks/session-end.py` | 10 | 0 | | |
| `LLM_ROUTER_IDEMPOTENCY_PATH` | llm_router | `idempotency.py` | 4 | 0 | | |
| `LLM_ROUTER_IDENTITY_PATH` | llm_router | `enterprise/identity.py` | 3 | 0 | | |
| `LLM_ROUTER_IMAGE_INTERCEPT` | llm_router | `hooks/tool_intercept.py` | 8 | 0 | | |
| `LLM_ROUTER_INDICATOR` | llm_router | `surface_status.py` | 1 | 1 | | |
| `LLM_ROUTER_INVOICE_DISCREPANCY_PCT` | llm_router | `invoice_reconciliation/__init__.py` | 1 | 0 | | |
| `LLM_ROUTER_JUDGE_CASCADE_SAMPLE_RATE` | llm_router | `judge_cascade.py` | 5 | 0 | | |
| `LLM_ROUTER_JUDGE_CASCADE_THRESHOLD` | llm_router | `judge_cascade.py` | 14 | 0 | | |
| `LLM_ROUTER_JUDGE_MODEL` | llm_router | `judge_cascade.py` | 0 | 0 | | |
| `LLM_ROUTER_JUDGE_SAMPLE_RATE` | llm_router | `judge.py` | 4 | 1 | | |
| `LLM_ROUTER_LIBRARIAN_MODEL` | llm_router | `library/sealer.py` | 0 | 0 | | |
| `LLM_ROUTER_LOCAL_AGENT_LOOP` | llm_router | `hooks/auto-route.py` | 10 | 1 | | |
| `LLM_ROUTER_LOCAL_VISION` | llm_router | `vision_registry.py` | 2 | 0 | | |
| `LLM_ROUTER_LOG_JSON` | llm_router | `logging.py` | 0 | 0 | | |
| `LLM_ROUTER_LOG_LEVEL` | llm_router | `logging.py` | 1 | 2 | | |
| `LLM_ROUTER_MAX_AGENT_DEPTH` | llm_router | `hooks/agent-route.py` | 7 | 0 | | |
| `LLM_ROUTER_MAX_TOOL_RESULT_CHARS` | llm_router | `hooks/context_budget.py` | 1 | 0 | | |
| `LLM_ROUTER_METRICS_INCLUDE_PRESSURE` | llm_router | `admin_api.py` | 0 | 0 | | |
| `LLM_ROUTER_MINI_SUMMARY_EVERY` | llm_router | `hooks/auto-route.py` | 1 | 0 | | |
| `LLM_ROUTER_OIDC_AUDIENCE` | llm_router | `enterprise/oidc.py` | 1 | 0 | | |
| `LLM_ROUTER_OIDC_DEFAULT_ORG` | llm_router | `server.py` | 0 | 0 | | |
| `LLM_ROUTER_OIDC_DEFAULT_TEAM` | llm_router | `server.py` | 0 | 0 | | |
| `LLM_ROUTER_OIDC_EMAIL_CLAIM` | llm_router | `enterprise/oidc.py` | 0 | 0 | | |
| `LLM_ROUTER_OIDC_GROUPS_CLAIM` | llm_router | `enterprise/oidc.py` | 0 | 0 | | |
| `LLM_ROUTER_OIDC_ISSUER` | llm_router | `enterprise/oidc.py` | 1 | 0 | | |
| `LLM_ROUTER_OIDC_JWKS_URI` | llm_router | `enterprise/oidc.py` | 1 | 0 | | |
| `LLM_ROUTER_OIDC_ROLE_MAP` | llm_router | `enterprise/oidc.py` | 0 | 0 | | |
| `LLM_ROUTER_OKF` | llm_router | `okf.py` | 17 | 0 | | |
| `LLM_ROUTER_OKF_AUTOINDEX` | llm_router | `hooks/session-start.py` | 2 | 0 | | |
| `LLM_ROUTER_OKF_AUTOINDEX_TTL_H` | llm_router | `hooks/session-start.py` | 0 | 0 | | |
| `LLM_ROUTER_OKF_MIN_SCORE` | llm_router | `okf.py` | 2 | 0 | | |
| `LLM_ROUTER_OLLAMA_MODEL` | llm_router | `model_discovery.py` | 7 | 0 | | **CONFLICTING DEFAULTS**: `""` (canonical) vs `"gemma4:latest"` (`.claude/hooks/auto-route.py`, stale copy — also not a real Ollama model name). See CFG-002. |
| `LLM_ROUTER_OLLAMA_NUM_CTX` | llm_router | `providers.py` | 7 | 0 | | |
| `LLM_ROUTER_OLLAMA_TIMEOUT` | llm_router | `hooks/auto-route.py` | 7 | 3 | | **CONFLICTING DEFAULTS**: `"45"` (canonical) vs `"4"` (`commands/doctor.py`) vs `"5"` (stale `.claude/hooks/` copy). See CFG-002. |
| `LLM_ROUTER_OLLAMA_URL` | llm_router | `hooks/agent_loop.py` | 9 | 0 | | **CONFLICTING DEFAULTS**: some sites `"http://localhost:11434"`, others `""`. See CFG-002. |
| `LLM_ROUTER_OLLAMA_WARMUP` | llm_router | `hooks/session-start.py` | 4 | 0 | | |
| `LLM_ROUTER_OLLAMA_WARMUP_MODEL` | llm_router | `hooks/session-start.py` | 1 | 0 | | |
| `LLM_ROUTER_PLAYWRIGHT_COMPRESS` | llm_router | `hooks/playwright-compress.py` | 0 | 0 | | |
| `LLM_ROUTER_POLICY` | llm_router | `cli_init_policy.py` | 7 | 3 | | |
| `LLM_ROUTER_POLICY_PATH` | llm_router | `control_plane/migration.py` | 0 | 0 | | |
| `LLM_ROUTER_PREMIUM_MAX_PRESSURE` | llm_router | `router.py` | 0 | 0 | | |
| `LLM_ROUTER_PRESET` | llm_router | `presets.py` | 2 | 0 | | |
| `LLM_ROUTER_PROFILE` | llm_router | `repo_config.py` | 70 | 20 | Y | **Overloaded name**: legacy fallback for BOTH `LLM_ROUTER_COST_PROFILE` (routing tier) and `LLM_ROUTER_DEPLOYMENT_PROFILE` (developer/enterprise), in different subsystems. Mitigated by disjoint value domains. `.env.example` tells new users to set this legacy name. See CFG-003, CFG-006. |
| `LLM_ROUTER_PROJECT_ALLOWLIST` | llm_router | `gateway.py` | 5 | 1 | | |
| `LLM_ROUTER_PROJECT_DIR` | llm_router | `semantic/scope.py` | 6 | 0 | | |
| `LLM_ROUTER_PROJECT_ID` | provider_credential | `session_store.py` | 20 | 0 | | Mis-categorized: an identifier, not a credential. |
| `LLM_ROUTER_PROJECT_ROOT` | llm_router | `semantic/scope.py` | 36 | 3 | | |
| `LLM_ROUTER_PROVIDER_REGISTRY_PATH` | llm_router | `provider_registry.py` | 3 | 0 | | |
| `LLM_ROUTER_PXPIPE_ENABLED` | llm_router | `hooks/session-start.py` | 4 | 0 | | |
| `LLM_ROUTER_PXPIPE_HEAVY_MODELS` | llm_router | `hooks/session-start.py` | 1 | 0 | | |
| `LLM_ROUTER_PXPIPE_URL` | llm_router | `hooks/session-start.py` | 2 | 0 | | |
| `LLM_ROUTER_QUALITY_MIN_CALLS` | llm_router | `quality_feedback.py` | 4 | 2 | | |
| `LLM_ROUTER_QUALITY_SKIP` | llm_router | `quality_feedback.py` | 8 | 4 | | |
| `LLM_ROUTER_QUALITY_SKIP_THRESHOLD` | llm_router | `quality_feedback.py` | 4 | 2 | | |
| `LLM_ROUTER_QUOTAS_PATH` | llm_router | `enterprise/quotas.py` | 0 | 0 | | |
| `LLM_ROUTER_QUOTA_DELAY` | llm_router | `quota_tracker.py` | 0 | 0 | | |
| `LLM_ROUTER_QUOTA_RETRY` | llm_router | `quota_tracker.py` | 0 | 0 | | |
| `LLM_ROUTER_QUOTA_TTL` | llm_router | `hooks/auto-route.py` | 0 | 0 | | |
| `LLM_ROUTER_RENDER_MODE` | llm_router | `hooks/response_formatter.py` | 5 | 3 | | |
| `LLM_ROUTER_RESPONSE_ROUTER` | llm_router | `commands/doctor.py` | 8 | 0 | | |
| `LLM_ROUTER_RESPONSE_ROUTER_TOKEN_THRESHOLD` | provider_credential | `response_router.py` | 0 | 0 | | Mis-categorized: a numeric threshold, not a credential. |
| `LLM_ROUTER_ROUTE_BANNER` | llm_router | `hooks/agent-route.py` | 9 | 0 | | |
| `LLM_ROUTER_ROUTING_LEDGER` | llm_router | `routing_quality.py` | 19 | 1 | | |
| `LLM_ROUTER_SCIM_TOKEN` | provider_credential | `admin_api.py` | 4 | 0 | | |
| `LLM_ROUTER_SEATS_AUTO` | llm_router | `subscription_local_routing.py` | 4 | 1 | | |
| `LLM_ROUTER_SECRETS_BACKEND` | llm_router | `secrets_vault.py` | 5 | 0 | | |
| `LLM_ROUTER_SEMANTIC_ARM` | llm_router | `semantic/modes.py` | 16 | 1 | | |
| `LLM_ROUTER_SEMANTIC_CACHE` | llm_router | `semantic_cache.py` | 7 | 0 | | |
| `LLM_ROUTER_SEMANTIC_CACHE_THRESHOLD` | llm_router | `semantic_cache.py` | 3 | 0 | | |
| `LLM_ROUTER_SEMANTIC_CENTROIDS` | llm_router | `semantic_classify.py` | 4 | 0 | | |
| `LLM_ROUTER_SEMANTIC_CLASSIFIER_BACKEND` | llm_router | `semantic_classify.py` | 1 | 0 | | |
| `LLM_ROUTER_SEMANTIC_HISTORY` | llm_router | `semantic/modes.py` | 4 | 0 | | |
| `LLM_ROUTER_SEMANTIC_INTERVENTION` | llm_router | `semantic/modes.py` | 1 | 0 | | |
| `LLM_ROUTER_SEMANTIC_SOURCE` | llm_router | `semantic/modes.py` | 17 | 2 | | |
| `LLM_ROUTER_SEMANTIC_ST_MODEL` | llm_router | `semantic_classify.py` | 0 | 0 | | |
| `LLM_ROUTER_SERVICE_PORT` | llm_router | `hook_client.py` | 0 | 0 | | |
| `LLM_ROUTER_SESSIONS_PATH` | llm_router | `agents/session.py` | 2 | 0 | | |
| `LLM_ROUTER_SESSION_BUDGET` | llm_router | `hooks/enforce-route.py` | 0 | 0 | | |
| `LLM_ROUTER_SESSION_CONTEXT` | llm_router | `session_store.py` | 26 | 1 | | |
| `LLM_ROUTER_SESSION_CONTEXT_DRAFT_BUDGET` | llm_router | `hooks/auto-route.py` | 5 | 0 | | |
| `LLM_ROUTER_SESSION_ID` | llm_router | `hooks/auto-route.py` | 16 | 0 | | |
| `LLM_ROUTER_SESSION_PAID_CAP` | llm_router | `hooks/auto-route.py` | 3 | 0 | | |
| `LLM_ROUTER_SESSION_RESCUE` | llm_router | `hooks/auto-route.py` | 1 | 0 | | |
| `LLM_ROUTER_SIDECAR_PREFETCH` | llm_router | `commands/doctor.py` | 11 | 0 | | |
| `LLM_ROUTER_SLIM` | llm_router | `tool_surface.py` | 30 | 9 | | Mode var (consolidated/slim), not boolean. See CFG-013. |
| `LLM_ROUTER_SSE_ALLOW_PUBLIC` | llm_router | `net_bind.py` | 7 | 0 | | Indirect-read exemplar; hand-declared in `_INDIRECT_READS`. |
| `LLM_ROUTER_STALE_PRESSURE_FLOOR` | llm_router | `budget.py` | 0 | 0 | | |
| `LLM_ROUTER_STATE_DIR` | llm_router | `surface_status.py` | 3 | 1 | | |
| `LLM_ROUTER_STATUS_EVERY` | llm_router | `hooks/status-bar-clawcode.py` | 0 | 0 | | |
| `LLM_ROUTER_STATUS_MODE` | llm_router | `hooks/status-bar.py` | 0 | 0 | | |
| `LLM_ROUTER_STOP_HOOK` | llm_router | `hooks/codex-stop.py` | 3 | 0 | | |
| `LLM_ROUTER_STREAMING_JUDGE` | llm_router | `streaming_judge.py` | 4 | 0 | | |
| `LLM_ROUTER_SUBAGENT_CLI_DELEGATION` | llm_router | `hooks/agent-route.py` | 3 | 0 | | |
| `LLM_ROUTER_SUBAGENT_CLI_TIMEOUT` | llm_router | `hooks/agent-route.py` | 0 | 0 | | |
| `LLM_ROUTER_SUBAGENT_DIRECT` | llm_router | `hooks/agent-route.py` | 6 | 0 | | |
| `LLM_ROUTER_SUBAGENT_DIRECT_MAX_COMPLEXITY` | llm_router | `hooks/agent-route.py` | 1 | 0 | | |
| `LLM_ROUTER_SUBAGENT_GOVERNANCE` | llm_router | `hooks/agent-route.py` | 2 | 0 | | |
| `LLM_ROUTER_SUBAGENT_MODEL_PIN` | llm_router | `hooks/agent-route.py` | 2 | 0 | | |
| `LLM_ROUTER_SUBPROCESS_TIMEOUT` | llm_router | `hooks/session-end.py` | 9 | 0 | | |
| `LLM_ROUTER_SUBSCRIPTION_PROVIDER` | llm_router | `subscription_local_routing.py` | 36 | 8 | | |
| `LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH` | llm_router | `quota_savings.py` | 2 | 0 | | |
| `LLM_ROUTER_SUPPRESS_PRICING_STALENESS` | llm_router | `pricing.py` | 0 | 0 | | |
| `LLM_ROUTER_SYMBOL_GROUNDING` | llm_router | `hooks/auto-route.py` | 1 | 1 | | |
| `LLM_ROUTER_SYNTHETIC` | llm_router | `routing_quality.py` | 16 | 1 | | |
| `LLM_ROUTER_TOKEN` | provider_credential | `identity.py` | 2 | 0 | | Indirect-read exemplar (via `LLM_ROUTER_TOKEN_ENV` constant); hand-declared in `_INDIRECT_READS`. |
| `LLM_ROUTER_TRACE` | llm_router | `trace.py` | 11 | 1 | | |
| `LLM_ROUTER_TRACE_FILE` | llm_router | `trace.py` | 8 | 1 | | |
| `LLM_ROUTER_URL` | llm_router | `commands/doctor.py` | 0 | 0 | | **CONFLICTING DEFAULTS within the same file**: `""` at `doctor.py:695` vs `"http://127.0.0.1:17900"` at `doctor.py:1137`. |
| `LLM_ROUTER_USAGE_DB_PATH` | llm_router | `quota_savings.py` | 10 | 0 | | |
| `LLM_ROUTER_USAGE_PATH` | llm_router | `commands/invoice.py` | 0 | 0 | | |
| `LLM_ROUTER_VISION_MODEL` | llm_router | `vision_registry.py` | 3 | 0 | | |
| `LLM_ROUTER_WEEKLY_QUOTA_USD` | llm_router | `quota_savings.py` | 10 | 0 | | |
| `LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV` | llm_router | `quota_savings.py` | 7 | 0 | | |
| `LLM_ROUTER_ZERO_CLAUDE` | llm_router | `hooks/auto-route.py` | 15 | 4 | | Boolean. See CFG-013. |
| `LOCALAPPDATA` | external_tool | `install_hooks.py` | ? | ? | | |
| `NO_COLOR` | platform | `commands/budget.py` | ? | ? | | |
| `OLLAMA_BASE_URL` | external_tool | `agentic/react.py` | ? | ? | | |
| `OLLAMA_BUDGET_MODELS` | external_tool | `model_discovery.py` | ? | ? | | |
| `OLLAMA_HOST` | external_tool | `hooks/playwright-compress.py` | ? | ? | | |
| `OLLAMA_MODELS` | external_tool | `model_discovery.py` | ? | ? | | |
| `OLLAMA_URL` | external_tool | `commands/doctor.py` | ? | ? | | Non-namespaced sibling of `LLM_ROUTER_OLLAMA_URL` — two different env vars for the same concept, one prefixed and one not. |
| `OPENAI_ADMIN_KEY` | provider_credential | `invoice_reconciliation/openai.py` | ? | ? | | |
| `OPENAI_API_KEY` | provider_credential | `commands/demo.py` | ? | ? | Y | |
| `OPENROUTER_API_KEY` | provider_credential | `commands/doctor.py` | ? | ? | | |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | external_tool | `observability.py` | ? | ? | | |
| `OTEL_EXPORTER_OTLP_INSECURE` | external_tool | `tracing.py` | ? | ? | | |
| `OTEL_SERVICE_NAME` | external_tool | `observability.py` | ? | ? | | |
| `PERPLEXITY_API_KEY` | provider_credential | `commands/demo.py` | ? | ? | Y | Canonical user-facing name; `config.py` re-exports it to `PERPLEXITYAI_API_KEY` for LiteLLM. Verified NOT a bug — deliberate normalization, documented at `config.py:610`. |
| `PORT` | external_tool | `server.py` | ? | ? | | |
| `PYTEST_CURRENT_TEST` | test_only | `config.py` | ? | ? | | |
| `RESPONSE` | external_tool | `hooks/response-router.py` | ? | ? | | |
| `VAULT_ADDR` | external_tool | `org_policy.py` | ? | ? | | |
| `VAULT_TOKEN` | provider_credential | `org_policy.py` | ? | ? | | |
| `XDG_CONFIG_HOME` | platform | `install_hooks.py` | ? | ? | | |
| `_SESSION_BUDGET_WARNING` | external_tool | `hooks/enforce-route.py` | ? | ? | | |

**Aggregate stats** (category = `llm_router`, 198 of 244 rows): 151/198 (76%) have zero
doc references anywhere in `docs/`, `guide/`, `architecture/`, `README.md`,
`SECURITY.md`, `CHANGELOG.md`; 54/198 (27%) have zero test references; **53/198 (27%)
have BOTH zero test and zero doc references** — a fully unverified, unexplained
configuration surface (full list in §3). Only 5 of 244 registered vars appear in
`.env.example` (`LLM_ROUTER_PROFILE`, `LLM_ROUTER_MONTHLY_BUDGET`†, `OPENAI_API_KEY`,
`GEMINI_API_KEY`, `PERPLEXITY_API_KEY`, `ANTHROPIC_API_KEY`, ... — provider keys plus 2
router vars; †`LLM_ROUTER_MONTHLY_BUDGET` is itself unregistered, see §4).

## 3. The 53 fully "dark" `llm_router`-category vars (zero test AND zero doc refs)

`LLM_ROUTER_AGENTS_CONFIG`, `LLM_ROUTER_AGENT_NUM_CTX`, `LLM_ROUTER_AGENT_ROUTE_ALLOW`,
`LLM_ROUTER_AGENT_TEMPERATURE`, `LLM_ROUTER_ALLOWED_HOSTS`, `LLM_ROUTER_ANSWER_VALUE_USD`,
`LLM_ROUTER_AUDIT_PATH`, `LLM_ROUTER_BENCHMARK_TTL_DAYS`, `LLM_ROUTER_BROKER_CONCURRENCY`,
`LLM_ROUTER_BUDGETS_DB_PATH`, `LLM_ROUTER_CLASSIFY_LOCAL_ONLY`, `LLM_ROUTER_CLAUDE_TIMEOUT`,
`LLM_ROUTER_CONFIDENCE_THRESHOLD`, `LLM_ROUTER_CONSTRAINED_TOOLS`,
`LLM_ROUTER_CONTEXT_OPTIMIZER`, `LLM_ROUTER_CP_STORE_PATH`,
`LLM_ROUTER_DISABLE_CONTINUATION_BYPASS`, `LLM_ROUTER_ENSEMBLE_TIMEOUT`,
`LLM_ROUTER_ESCALATE_DEADLINE_S`, `LLM_ROUTER_FORCE_COLOR`, `LLM_ROUTER_FREE_TIER_DRAFTS`,
`LLM_ROUTER_GEMINI_TIMEOUT`, `LLM_ROUTER_HARNESS`, `LLM_ROUTER_HISTORY_RELAY`,
`LLM_ROUTER_HOOK_BUDGET_S`, `LLM_ROUTER_HOOK_SLOW_SECONDS`, `LLM_ROUTER_JUDGE_MODEL`,
`LLM_ROUTER_LIBRARIAN_MODEL`, `LLM_ROUTER_LOG_JSON`, `LLM_ROUTER_METRICS_INCLUDE_PRESSURE`,
`LLM_ROUTER_OIDC_DEFAULT_ORG`, `LLM_ROUTER_OIDC_DEFAULT_TEAM`,
`LLM_ROUTER_OIDC_EMAIL_CLAIM`, `LLM_ROUTER_OIDC_GROUPS_CLAIM`, `LLM_ROUTER_OIDC_ROLE_MAP`,
`LLM_ROUTER_OKF_AUTOINDEX_TTL_H`, `LLM_ROUTER_PLAYWRIGHT_COMPRESS`,
`LLM_ROUTER_POLICY_PATH`, `LLM_ROUTER_PREMIUM_MAX_PRESSURE`, `LLM_ROUTER_QUOTAS_PATH`,
`LLM_ROUTER_QUOTA_DELAY`, `LLM_ROUTER_QUOTA_RETRY`, `LLM_ROUTER_QUOTA_TTL`,
`LLM_ROUTER_SEMANTIC_ST_MODEL`, `LLM_ROUTER_SERVICE_PORT`, `LLM_ROUTER_SESSION_BUDGET`,
`LLM_ROUTER_STALE_PRESSURE_FLOOR`, `LLM_ROUTER_STATUS_EVERY`, `LLM_ROUTER_STATUS_MODE`,
`LLM_ROUTER_SUBAGENT_CLI_TIMEOUT`, `LLM_ROUTER_SUPPRESS_PRICING_STALENESS`,
`LLM_ROUTER_URL`, `LLM_ROUTER_USAGE_PATH`.

Most of these are plausibly-fine low-traffic knobs (paths, ports, per-feature timeouts).
Two stand out as worth attention on their own: `LLM_ROUTER_HISTORY_RELAY` (privacy gate,
§CFG-014) and `LLM_ROUTER_CONFIDENCE_THRESHOLD` (has drifted defaults, §CFG-002) —
both untested AND undocumented AND, in the second case, already inconsistent.

## 4. Vars read only through an indirect Python constant (invisible to the registry's own guard)

`env_registry.py`'s validating test (`tests/test_env_registry.py`) can only see
`os.environ.get("LITERAL")` / `os.getenv("LITERAL")` / `os.environ["LITERAL"]` calls
where the argument is a string constant in the AST; it has a `_INDIRECT_READS`
frozenset (4 entries: `LLM_ROUTER_ALLOW_PUBLIC_BIND`, `LLM_ROUTER_SSE_ALLOW_PUBLIC`,
`LLM_ROUTER_TOKEN`, `LLM_ROUTER_AUDIT_DISABLED`) for variables its authors already knew
were read through a variable rather than a literal. The following are read the same
way (through a named Python constant or loop variable) but are declared in **neither**
`ENV_REGISTRY` **nor** `_INDIRECT_READS` — verified individually by literal grep against
`env_registry.py` after fixing a `comm` locale bug that first over-reported this list by
~7x (see Methodology, above):

| Env var | Read site | Registered? |
|---|---|---|
| `LLM_ROUTER_REQUEST_TIMEOUT` | `timeout_config.py:62`, via loop var `env_var` | No |
| `LLM_ROUTER_REASONING_TIMEOUT` | `timeout_config.py:62`, via loop var `env_var` | No |
| `LLM_ROUTER_MEDIA_REQUEST_TIMEOUT` | `timeout_config.py:62`, via loop var `env_var` | No |
| `LLM_ROUTER_CODEX_TIMEOUT` | `timeout_config.py:62`, via loop var `env_var` | No |
| `LLM_ROUTER_BENCHMARK_TIMEOUT` | `timeout_config.py:62`, via loop var `env_var` | No |
| `LLM_ROUTER_AGENT_ID` | `identity.py:184/250/380`, via `LLM_ROUTER_AGENT_ID_ENV` constant | No |
| `LLM_ROUTER_USER_ID` | `identity.py`, via `..._ENV` constant | No |
| `LLM_ROUTER_USER_EMAIL` | `identity.py:369`, via `LLM_ROUTER_USER_EMAIL_ENV` constant | No |
| `LLM_ROUTER_ORG_ID` | `identity.py`, via `..._ENV` constant | No |
| `LLM_ROUTER_TEAM_ID` | `identity.py`, via `..._ENV` constant | No |
| `LLM_ROUTER_TENANT_ID` | `identity.py`, via `..._ENV` constant | No |
| `LLM_ROUTER_MONTHLY_BUDGET` | pydantic `Settings` field (implicit env binding, no literal `os.environ` call at all) | No |

`LLM_ROUTER_SUBPROCESS_TIMEOUT` and `LLM_ROUTER_HTTP_TIMEOUT` are, confusingly, also
read via the same `timeout_config.py:62` indirect loop **and yet ARE registered** —
meaning they must have a second, literal-arg read site elsewhere that the scanner did
catch; the other 5 timeout vars apparently have no such second site. See CFG-007.

## 5. Config precedence — three modules, three different orders

| Module | Domain | Stated precedence (high → low) | Verified against implementation? |
|---|---|---|---|
| `repo_config.py` (docstring, top of file) | Routing policy (profile, enforce, block_providers, model pins, daily caps) | env vars > repo `.llm_router.yml` > user `~/.llm-router/routing.yaml` > built-in defaults | Matches `RepoConfig` merge code read. |
| `enforce_config.py` (`resolve_enforce_mode`, its sole consumer for enforce mode specifically) | Enforcement mode only | env var > **session file** (`~/.llm-router/sessions/<id>/enforce`) > repo `.llm_router.yml` > global `~/.llm-router/routing.yaml` > `"smart"` | Matches code (GH#49 comment explains the session tier's insertion point). Adds a tier `repo_config.py` doesn't have. |
| `safe_config.py` (module docstring) | Provider API keys / router settings (the pydantic `Settings` class) | **`.env` file (1) > `~/.llm-router/config.yaml` (2) > environment variables (3) > hardcoded defaults (4)** | **Contradicted by the actual code.** `config.py`'s `RouterConfig` is a `pydantic_settings.BaseSettings` with `env_file=(...)`; pydantic-settings' documented default source order is init > env vars > dotenv > secrets > field defaults — i.e. **real env vars outrank `.env`**, the opposite of what this docstring claims. `model_post_init` then applies `~/.llm-router/config.yaml` values **only into fields still empty** after that — i.e. yaml is the *lowest*-priority override actually applied, not tier 2. See CFG-004. |

Net effect: an operator troubleshooting "why isn't my env var taking effect" has no
single place to read the real answer, and the one place that tries
(`safe_config.py`'s docstring) states an order that is backwards from what the code it
describes actually does.

## 6. Boolean / mode explosion

| Var | Shape | Default | Notes |
|---|---|---|---|
| `LLM_ROUTER_ENFORCE` | 8-value string enum (`off/shadow/soft/suggest/smart/hard/advise/advisory`, plus `"enforce"` accepted by `repo_config.VALID_ENFORCE` but not observed as an actual behavior branch anywhere else — UNCERTAIN whether reachable) | `"smart"` (canonical) / `"hard"` (2 files, see CFG-001) | Safety-critical; single largest source of behavioral variance. |
| `LLM_ROUTER_ZERO_CLAUDE` | boolean | off | Gates whether a routed answer can replace Claude's own turn outright. |
| `LLM_ROUTER_DELEGATE` | boolean (inverse: any value other than `off/0/false/no` counts as on) | **on** | |
| `LLM_ROUTER_DIRECT_EXECUTION` | boolean (`1/true/yes/on`) | **on** | |
| `LLM_ROUTER_AGENT_WRITES` | mode (`propose`/`write`, per prior audit notes) | `propose` (no writes) | |
| `LLM_ROUTER_HISTORY_RELAY` | boolean | **on** | Privacy gate; untested, undocumented (§3). |
| `LLM_ROUTER_CLAUDE_SUBSCRIPTION` | boolean | off | |
| `LLM_ROUTER_SLIM` | mode (`consolidated`/other) | `consolidated` | Affects MCP tool-surface size, not routing per se. |

Just the 4 clean, independently-read booleans above (`ZERO_CLAUDE`, `DELEGATE`,
`DIRECT_EXECUTION`, `CLAUDE_SUBSCRIPTION`) combined with the 8-value `ENFORCE` enum
already yield 2⁴ × 8 = **128 nominally reachable states** before `AGENT_WRITES`,
`HISTORY_RELAY`, or `SLIM` are even added — and nothing in the codebase enumerates which
combinations are meaningful versus which silently no-op (e.g., does `ZERO_CLAUDE=on`
mean anything when `ENFORCE=off`?). Flagged as `NEEDS EVIDENCE`, not asserted as a bug:
the interaction matrix was not exercised in this audit and may already be a no-op in the
harmless direction everywhere. See CFG-013.

## 7. What's genuinely good here (do-not-change candidates)

- `src/llm_router/env_registry.py` + `tests/test_env_registry.py`: a hand-committed
  declaration checked against an *independently re-implemented* AST scanner
  specifically to avoid the "validator checks itself" trap this repo has been burned by
  twice before (cited in the module's own docstring: `tool_surface.unregistered()` and
  `lint_tool_surface.py`). Explicitly scopes its own claim (`src/llm_router/` only, not
  `scripts/`) rather than over-claiming. This is exactly the "check the check" pattern
  this repo's `CLAUDE.md` asks for elsewhere. **Residual gap**: CFG-007 (indirect reads).
  Recommendation: KEEP the mechanism; consider extending `_INDIRECT_READS` to cover the
  12 vars in §4, or extending the scanner to also resolve simple same-module constant
  aliases (`NAME = "LITERAL"` then `os.environ.get(NAME)`), which would close most of
  the gap without a rewrite.
- `LLM_ROUTER_PROFILE` collision handling (`repo_config.py:34-56`, `config.py:325-341`):
  the collision is real (CFG-003) but the mitigation — value-domain filtering plus a
  one-shot deprecation warning plus an explicit GH-issue-numbered comment trail — is a
  textbook example of defusing a hazard you can't immediately remove. KEEP.
  `docs/archive/AUDIT_2026-08-30.md:130-132` already checked the sibling var
  `LLM_ROUTER_TIER` for the same collision shape and found it clean — corroborating
  evidence this project actively watches for this exact defect class.
  `identity.py`'s `LLM_ROUTER_TOKEN`/`LLM_ROUTER_AUDIT_DISABLED` `_INDIRECT_READS`
  entries (with an inline comment naming the GH issues that removed their last literal
  reader) are the same discipline applied to the registry.
- `cli.py`'s `_KNOWN_SUBCOMMANDS` frozenset: an explicitly-acknowledged, low-risk
  secondary list ("Used only to power a 'did you mean' typo suggestion for typos — NOT
  a second source of truth for dispatch itself, so it can drift without breaking
  anything except the suggestion quality"). This is the right way to have a duplicate:
  named as one, scoped to a cosmetic feature. Low-cost improvement available (derive it
  from the dispatch table via a lint) but not urgent. KEEP as-is or lint it.
- `cli.py`'s module docstring usage block is enforced by
  `tests/test_f38_every_command_is_documented.py` (self-cites a prior state: "28 of 51
  subcommands were absent from this text, and 22 appeared in no documentation at all").
  Verified live: `llm-router install --help`, `llm-router sessions --help`,
  `llm-router semantic --help` all print accurate, current, subcommand-specific help
  text with exit code 0. A prior audit's F7 ("--help text is 96% wrong", wrong binary
  name) is fixed in this baseline (docstring now says `llm-router`, not `llm_router`,
  throughout). KEEP.
- `commands/verify.py`'s exit code: `cli.py` explicitly wraps it as
  `sys.exit(_verify_main(args[1:]))` with a comment (`CHZ-PKG-005`) explaining that
  discarding the return value used to make `llm_router verify` always exit 0. This is
  the fix CFG-010 asks to be extended to `last` and `retrospect`.

## 8. CLI command tree and `--help` / exit-code audit

44 subcommand modules under `commands/`, dispatched by literal string match in
`cli.py:main()`. Argument-parsing style split:

| Style | Count | Behavior |
|---|---|---|
| `argparse` | 16/44 | Free `--help`/`-h`, consistent unknown-flag errors, usage strings. |
| Hand-rolled `sys.argv` loop | 28/44 | No shared convention; correctness of `--help`/error handling is per-file. |

**`--help` handling, confirmed by direct execution** (not just grep) against the
baseline for a representative sample:

| Command | `--help` handled? | What actually happens |
|---|---|---|
| `install`, `sessions`, `semantic` | Yes (argparse-free but hand-written, correct) | Accurate subcommand-specific help, exit 0. |
| `status` | **No** | Silently runs and prints the live status dashboard; exit 0. |
| `config` | **No** | Silently runs and prints the resolved config; exit 0. |
| `doctor` | **No** | Silently runs the full health check (network/file probes); prints failures; **exit 1** — a health-check failure code, not a help-request code. |
| `budget` | **No** | Silently runs and prints the live budget table; exit 0. |
| `team` | **No** | Silently runs and prints team report (including the resolved user identity); exit 0. |

20 of 44 command modules (`budget`, `config`, `dashboard`, `demo`, `doctor`,
`explain_dashboard`, `gain`, `onboard`, `probe`, `profile`, `routing`,
`savings_report`, `set_enforce`, `setup`, `share`, `status`, `team`, `test`,
`uninstall`, `update`) contain no `--help`/`-h`-handling code at all (grep-confirmed;
5 spot-checked by execution above). A user or script running `llm-router <any of
these> --help` gets that command's real (sometimes side-effecting) behavior instead of
help text, and for `doctor` specifically gets a nonzero exit code that a script would
misread as "command not available" or "help failed."

**Exit-code discard, confirmed by direct execution:**

```
$ HOME=$(mktemp -d) ... python -c 'sys.argv=["llm-router","last"]; from llm_router.cli import main; main()'
Error: /var/.../tmp.xxx/.llm-router/usage.db not found. Run some routed calls first.
$ echo "shell exit code: $?"
shell exit code: 0
```

`commands/last.py:main()` is typed `-> int`, documents "Returns: Exit code (0 on
success)", and does `return 1` on this exact path (line 155) — but `cli.py`'s dispatch
(`elif args[0] == "last": from llm_router.commands.last import main as _last_main;
_last_main(args[1:])`) never calls `sys.exit()` on the result, so the process always
exits 0 regardless. `commands/retrospect.py:main()` has the identical shape (`-> int`,
`return 1` at line 94 when `run_session_retrospective` returns nothing, dispatched via
`_retrospect_main(args[1:])` with no `sys.exit`) — not separately executed in this
audit, but the source pattern is unambiguous. `commands/snapshot.py:main()` shares the
same dispatch shape (`-> int`, no `sys.exit` at the call site) but currently has no
`return 1`/nonzero path in its source, so it is a latent instance rather than a live
bug. `cli.py` itself documents having already fixed this exact class for `verify`
(comment tagged `CHZ-PKG-005`): "discarding it made `llm_router verify` always exit 0
... a CI/install gate keying on the exit code treated a broken install as healthy." The
fix was not generalized to the other commands with the same shape.

**Machine-readable output:** only `commands/audit.py`'s `misroute` subcommand exposes a
`--json` flag (`add_argument("--json", action="store_true", ...)`, `audit.py:91`). No
other command among the 44 — including `status`, `config`, `doctor`, `budget`,
`stats`, `sessions`, `savings-report` — exposes `--json`/`--format` for scripted
consumption; all are human-text-only.

**Naming**: subcommand names use hyphens (`savings-report`, `set-enforce`,
`explain-dashboard`) while their backing module filenames use underscores
(`savings_report.py`, `set_enforce.py`, `explain_dashboard.py`) — a consistent,
unremarkable Python-vs-CLI convention split, not a finding.

## 9. Findings register

```
ID: CFG-001
Category: Config precedence / correctness
Severity: HIGH
Confidence: HIGH
Location: Files: src/llm_router/hooks/stop-enforce.py:180, src/llm_router/hooks/status-bar.py:117, src/llm_router/enforce_config.py:39,119-141
Symbols: main() (stop-enforce.py), module-level ENFORCE_MODE (status-bar.py), resolve_enforce_mode(), DEFAULT_ENFORCE
Lines: stop-enforce.py:180; status-bar.py:117; enforce_config.py:39,141
Observation: Nearly every enforcement-aware module (auto-route.py, enforce-route.py, commands/doctor.py) resolves LLM_ROUTER_ENFORCE via the canonical `enforce_config.resolve_enforce_mode()` (env > session file > repo YAML > global YAML > "smart"), falling back to a raw `os.environ.get("LLM_ROUTER_ENFORCE", "").strip().lower() or "smart"` only if that import fails, and each says so in an explicit "single source of truth" comment. `stop-enforce.py` and `status-bar.py` instead go straight to `os.environ.get("LLM_ROUTER_ENFORCE", "hard")` — never attempting the canonical resolver, and defaulting to "hard" instead of "smart".
Evidence: enforce_config.py:39 `DEFAULT_ENFORCE = "smart"`; enforce_config.py:141 `return DEFAULT_ENFORCE`; auto-route.py:3802-3807 and enforce-route.py:994-1000 and commands/doctor.py:657-660 all show the try/resolve_enforce_mode/except-fallback-to-"smart" pattern with an explicit comment citing it as shared with the others; stop-enforce.py:180 `enforce = os.environ.get("LLM_ROUTER_ENFORCE", "hard").lower()`; status-bar.py:117 `ENFORCE_MODE = os.environ.get("LLM_ROUTER_ENFORCE", "hard").lower()`.
Why this exists, if discoverable: status-bar.py and stop-enforce.py are lightweight, frequently-invoked hooks (status line render, Stop-hook violation tracking) and likely took the cheapest read rather than importing enforce_config; no comment explains the divergence, unlike every other file that touches this variable.
Why this matters: If enforcement mode is set via session file, repo `.llm_router.yml`, or the global `routing.yaml` (all three exist specifically because "env vars don't propagate to GUI/desktop/other-host sessions", per enforce-route.py's own comment) rather than the env var, these two files see an unset env var and silently assume "hard" instead of the actual resolved mode. status-bar.py then displays a mode that may not match what actually enforces; stop-enforce.py's violation-skip check (`if enforce in ("off","shadow","soft","suggest"): sys.exit(0)`) will fail to skip when the real mode is one of those but was set through session/repo/global config rather than env, so it tracks routing violations for a session that is not actually in a violation-tracking mode.
User-visible impact: Status line can show the wrong enforcement mode; violation tracking/ledger entries can be recorded for sessions where the operator deliberately configured a non-enforcing mode via file rather than env var.
Engineering impact: A second, silently-diverging implementation of a "single source of truth" that 4+ other files explicitly coordinate around.
Is behavior currently used? YES — both hooks are installed and run on every prompt/stop event in a standard install.
Recommended action: SIMPLIFY — make stop-enforce.py and status-bar.py use the same try/resolve_enforce_mode()/except-fallback-to-"smart" pattern already used by auto-route.py, enforce-route.py, and doctor.py.
Proposed target: One resolution path (enforce_config.resolve_enforce_mode) used by all 5 consumers.
Behavioral compatibility risk: LOW — aligns two outliers with the documented/intended behavior of the other three; only changes behavior for installs that rely on session/repo/global config without an env var, where it fixes a divergence rather than creating one.
Security risk: None directly; enforcement-mode confusion has second-order safety relevance (this is the mode that gates whether Claude is blocked from acting directly).
Performance impact: Negligible (one extra import/try per hook invocation, same cost the other 3 hooks already pay).
Estimated complexity removed: Small, but removes a genuine "two knobs, one behavior" instance.
Validation required: A test that sets enforce mode via repo/global YAML only (no env var) and asserts status-bar.py and stop-enforce.py report/behave the same as auto-route.py/enforce-route.py/doctor.py for that mode.
Dependencies on other findings: None.

ID: CFG-002
Category: Dead/stale code, config-key conflicting defaults
Severity: MEDIUM-HIGH
Confidence: HIGH
Location: Files: .claude/hooks/auto-route.py (whole file), src/llm_router/hooks/auto-route.py, hooks/auto-route.py, src/llm_router/model_discovery.py, src/llm_router/commands/doctor.py
Symbols: n/a (module-level defaults for LLM_ROUTER_CONFIDENCE_THRESHOLD, LLM_ROUTER_OLLAMA_MODEL, LLM_ROUTER_OLLAMA_TIMEOUT, LLM_ROUTER_OLLAMA_URL)
Lines: .claude/hooks/auto-route.py:61-64; src/llm_router/hooks/auto-route.py:259,291,230,323; src/llm_router/model_discovery.py:82; src/llm_router/commands/doctor.py:1310
Observation: `.claude/hooks/auto-route.py` is a git-tracked file (last touched 2026-04-13, `git log -1`) that is this repository's OWN dev-time installed Claude Code hook — i.e. what runs when working IN this repo. It is 1212 lines versus 4870 lines in the canonical `src/llm_router/hooks/auto-route.py` (last touched 2026-09-24, same day as this audit) — 5+ months and ~3650 lines of drift. Its env-var defaults disagree with the canonical file's: LLM_ROUTER_CONFIDENCE_THRESHOLD defaults to "4" there vs "2" canonically; LLM_ROUTER_OLLAMA_MODEL defaults to "gemma4:latest" there (not a real Ollama model tag) vs "" canonically; LLM_ROUTER_OLLAMA_TIMEOUT defaults to "5" there vs "45" canonically (and "4" in a third file, commands/doctor.py).
Evidence: `git log -1 --format="%H %ad %s" -- .claude/hooks/auto-route.py` → 2026-04-13; `git log -1 ... -- src/llm_router/hooks/auto-route.py` → 2026-09-24; `wc -l` 1212 vs 4870; grep for each var's `os.environ.get(...)` default in each file (see table in §2).
Why this exists, if discoverable: `.claude/hooks/` is the destination `install_hooks.py` copies INTO on end-user machines (`_HOOKS_SRC = _PACKAGE_DIR / "hooks"`, i.e. `src/llm_router/hooks`); this repo's own `.claude/hooks/auto-route.py` is presumably a leftover from an early self-install that was never re-synced (unlike the top-level `hooks/` directory, which IS actively kept byte-identical to `src/llm_router/hooks/` for the 14 files it has in common — confirmed via `diff -q`, both updated the same days in September).
Why this matters: Every Claude Code session run inside this repository itself is routed by a 5-month-stale hook with different defaults than what ships to users — including this very audit session (LLM_ROUTER_BASH_INTERCEPT, which fired live during this audit, is one of the vars this file also touches).
User-visible impact: None for end users (this file is never distributed — it is excluded from the sdist per pyproject.toml). Affects only this repo's own contributors/maintainers doing self-hosted development.
Engineering impact: A maintainer debugging "why did routing behave differently in this repo vs. in my test project" could easily lose time to this without knowing this file exists or is stale.
Is behavior currently used? YES, for any Claude Code session opened at this repo's root.
Recommended action: DELETE the stale `.claude/hooks/auto-route.py` (and its two siblings `usage-refresh.py`, `version-guard.py` — not independently verified for staleness here) and let `llm-router update`/`install_hooks.check_and_update_hooks()` regenerate it from the canonical source, OR add a CI check that fails if `.claude/hooks/*.py` drifts from `src/llm_router/hooks/*.py` by more than a version bump.
Proposed target: `.claude/hooks/` either removed from version control (regenerated locally, like a build artifact) or synced by the same mechanism that keeps top-level `hooks/` in sync with `src/llm_router/hooks/`.
Behavioral compatibility risk: LOW (dev-environment-only file, not shipped).
Security risk: None identified.
Performance impact: None.
Estimated complexity removed: One less silently-diverging copy of a 4870-line file.
Validation required: Confirm `.claude/hooks/*.py` are excluded from the sdist (pyproject.toml already suggests this) before deleting, then confirm `llm-router update` regenerates them correctly for a contributor.
Dependencies on other findings: None.

ID: CFG-003
Category: API surface / naming — env var collision
Severity: MEDIUM
Confidence: HIGH
Location: Files: src/llm_router/config.py:325-341, src/llm_router/repo_config.py:34-56,150-168, src/llm_router/hooks/auto-route.py:682-689, .env.example:24
Symbols: RouterConfig.llm_router_profile, RepoConfig.effective_profile, _is_enterprise_profile
Lines: config.py:325-341; repo_config.py:34-56,150-168; auto-route.py:682-689; .env.example:24
Observation: `LLM_ROUTER_PROFILE` is a legacy, ambiguous env var name that means two unrelated things depending on which subsystem reads it: (a) the routing cost tier (budget/balanced/premium/...), now canonically `LLM_ROUTER_COST_PROFILE`, read as a fallback in config.py/repo_config.py; (b) the deployment/identity profile (developer/enterprise), now canonically `LLM_ROUTER_DEPLOYMENT_PROFILE`, read as a fallback in hooks/auto-route.py/identity.py/server.py. Both readers are aware of the collision (comments cite GH#65 and GH#69 by number) and defend against misinterpretation with a value-domain filter — RoutingProfile's 6 values (budget/balanced/premium/reasoning/quota_balanced/subscription_local) never overlap with the identity axis's 3 values (enterprise/prod/production), so no observed runtime misfire is possible today. `.env.example` — the only onboarding template shipped — still tells new users to set the legacy name (`LLM_ROUTER_PROFILE=balanced`).
Evidence: repo_config.py:34-56 docstring block titled "GH#65: LLM_ROUTER_PROFILE collision"; config.py:325-341 AliasChoices("LLM_ROUTER_COST_PROFILE","LLM_ROUTER_PROFILE") plus a one-shot fallback warning; auto-route.py:682-689 `_is_enterprise_profile()` reading LLM_ROUTER_DEPLOYMENT_PROFILE first, LLM_ROUTER_PROFILE as legacy fallback; docs/archive/AUDIT_2026-08-30.md:130-132 independently checked the sibling var LLM_ROUTER_TIER for the identical collision shape and found it clean, confirming this is a known, actively-monitored defect class in this project.
Why this exists, if discoverable: Fully documented in-repo: an operator hit exactly this collision (GH#65), renamed per the identity-side deprecation guidance, and broke routing silently because the routing-side reader didn't know the new name existed yet.
Why this matters: The env var name itself is a landmine independent of the current safe mitigation — any future change that (a) adds a routing profile value equal to "enterprise"/"prod"/"production", or (b) adds an identity-axis value that happens to be a valid routing tier name, reopens the collision the value-domain filter currently closes.
User-visible impact: None currently (mitigated). Onboarding friction: new users following .env.example immediately hit the one-shot deprecation warning path for a variable the template itself told them to set.
Engineering impact: Two independent deprecation windows (GH#65, GH#69) both keeping the same legacy name alive for different reasons; closing one does not close the other.
Is behavior currently used? YES — LLM_ROUTER_PROFILE has 70 test references and 20 doc references; it is actively read as a fallback in both subsystems.
Recommended action: DEPRECATE (continue) / update `.env.example` to write `LLM_ROUTER_COST_PROFILE=balanced` instead of the legacy name, closing the onboarding half of this now rather than waiting for the full removal window.
Proposed target: `.env.example` uses canonical names; the legacy `LLM_ROUTER_PROFILE` fallback removal stays on its existing GH#65/GH#69 deprecation timeline.
Behavioral compatibility risk: LOW for the .env.example fix (purely onboarding text); the underlying fallback removal (out of scope here) would need its own deprecation-window validation.
Security risk: None.
Performance impact: None.
Estimated complexity removed: Removes the onboarding-time trigger of the deprecation warning; does not remove the underlying dual-meaning env var (tracked separately).
Validation required: None beyond the .env.example text change; existing tests already cover the fallback behavior itself.
Dependencies on other findings: None.

ID: CFG-004
Category: Config precedence documentation vs. implementation
Severity: MEDIUM-HIGH
Confidence: HIGH
Location: Files: src/llm_router/safe_config.py:1-19, src/llm_router/config.py:587-589,807-831
Symbols: RouterConfig.model_config, RouterConfig.model_post_init, load_safe_config
Lines: safe_config.py:11-15 (docstring); config.py:587-589 (model_config env_file), config.py:807-831 (model_post_init)
Observation: `safe_config.py`'s module docstring states the config precedence as "1. .env file (project-level) 2. ~/.llm-router/config.yaml (user-level fallback) 3. Environment variables (system-wide) 4. Hardcoded defaults" — i.e. `.env` outranks real environment variables. The actual consumer, `RouterConfig(BaseSettings)` in config.py, uses pydantic-settings' default source order (env vars > dotenv file > field defaults — pydantic-settings' documented behavior, not overridden here via `settings_customise_sources`), and then `model_post_init` applies `~/.llm-router/config.yaml` values ONLY into fields that are still falsy/empty after that resolution (`if not current: setattr(...)`) — making the YAML file the LOWEST-priority source actually applied, not tier 2. The real order is: env vars ≥ .env file > config.yaml > pydantic field defaults — the opposite of what safe_config.py documents for the env-vs-.env relationship, and a different tier position for the yaml file.
Evidence: safe_config.py:11-15 (quoted above); config.py:587-589 `"env_file": (paths.state_path(".env"), ".env")` on a `pydantic_settings.BaseSettings` subclass (pydantic-settings' documented default source precedence is init > env > dotenv > file secrets > field defaults); config.py:807-831 `model_post_init` reading `load_safe_config()` and only `setattr`-ing when `if not current`.
Why this exists, if discoverable: safe_config.py's OWN code contains no environment-variable-vs-dotenv merge logic at all — it only implements the "load YAML" tier; the docstring appears to describe an aspirational/whole-system precedence that was never implemented as stated, rather than describing what safe_config.py itself does.
Why this matters: This is the one place in the codebase that tries to state a config precedence in plain English for a human (as opposed to the code-comment precedence notes in repo_config.py/enforce_config.py, which are locally accurate); it is wrong, in the exact domain (API keys / router settings) most likely to cause a confused bug report ("I set OPENAI_API_KEY but it's still using my .env value" or vice versa).
User-visible impact: An operator debugging a config value using this docstring as a guide will look in the wrong place first.
Engineering impact: Three different modules (repo_config.py, enforce_config.py, safe_config.py) each state a precedence for a different config domain, and none of the three orderings agree with each other structurally (different tier counts, different tier positions) — see §5 table.
Is behavior currently used? YES — RouterConfig is the config singleton (`get_config()`) used throughout provider-key resolution.
Recommended action: REWRITE the safe_config.py docstring to match the actual pydantic-settings + model_post_init behavior; consider consolidating the three precedence descriptions (repo_config.py, enforce_config.py, safe_config.py) into one place documenting per-domain precedence, since routing-policy, enforce-mode, and secrets each legitimately have different tiers but a reader needs to know that going in.
Proposed target: One "Configuration precedence" doc section (or a single well-linked docstring) enumerating all three domains and their distinct tier orders explicitly, rather than three independently-drifting docstrings.
Behavioral compatibility risk: NONE (doc-only fix; the code's actual behavior is not proposed to change here).
Security risk: None directly, though a mis-documented precedence for API-key resolution is exactly the kind of thing that could cause a stale/wrong key to be used silently by an operator who "thought" they'd overridden it in the higher-priority place per the docstring.
Performance impact: None.
Estimated complexity removed: Removes one confirmed contradiction between documentation and code.
Validation required: None beyond doc review; consider a test that pins the actual precedence order (env > .env > yaml-fills-empty-only > default) so future refactors can't silently invert it again.
Dependencies on other findings: None.

ID: CFG-005
Category: Import-time vs. call-time config reads
Severity: LOW-MEDIUM
Confidence: HIGH
Location: Files: src/llm_router/timeout_config.py:22-40
Symbols: get_timeout_config
Lines: timeout_config.py:22 (@lru_cache(maxsize=1))
Observation: `get_timeout_config()` reads 7 `LLM_ROUTER_*_TIMEOUT` env vars and is decorated `@lru_cache(maxsize=1)`, so the first call in a process's lifetime freezes all 7 values for the rest of that process — functionally equivalent to an import-time read for any long-lived process (gateway, server, broker, MCP stdio server) even though it is call-time syntactically.
Evidence: timeout_config.py:22 decorator; timeout_config.py:62 `value = int(os.environ.get(env_var, defaults[key]))` inside the cached function.
Why this exists, if discoverable: Deliberate perf optimization to avoid re-parsing env vars on every timeout lookup (module docstring calls this out as "to prevent DoS via hardcoded values" — the caching itself isn't discussed).
Why this matters: A user who changes LLM_ROUTER_REQUEST_TIMEOUT (or any of the other 6) via `export` and expects it to take effect on the next routed call within a long-lived process (gateway/server/broker) will not see the change without a restart. The module docstring elsewhere in the codebase mentions a `reset_timeout_cache()`-shaped need ("After modifying environment variables in tests, call this to ...", timeout_config.py:138) confirming the authors know the cache needs manual busting — but only for tests, not documented as a production caveat.
User-visible impact: Silent staleness for anyone tuning timeouts on a running gateway/server/broker without restarting it.
Engineering impact: Minor — the escape hatch already exists (a reset function) for tests; it is simply not surfaced as an operational concern.
Is behavior currently used? YES.
Recommended action: KEEP the cache (it's the right perf tradeoff for a short-lived CLI process) but document the process-restart requirement for long-lived server/gateway modes, or key the cache off a config-generation counter that CLI commands like `set-enforce`-style config writers could bump.
Proposed target: A one-line doc note in the module docstring and/or the `serve`/`gateway`/`broker` command help text.
Behavioral compatibility risk: NONE (doc-only recommendation; the caching behavior itself is reasonable to keep).
Security risk: None.
Performance impact: N/A (no change proposed to the caching itself).
Estimated complexity removed: N/A.
Validation required: None.
Dependencies on other findings: Related to CFG-007 (these same 7 vars are also invisible to ENV_REGISTRY because of the same indirection).

ID: CFG-006
Category: Onboarding / documentation accuracy
Severity: LOW
Confidence: HIGH
Location: Files: .env.example:24-26
Symbols: n/a
Lines: .env.example:24-26
Observation: `.env.example` (the only new-user onboarding template) sets `LLM_ROUTER_PROFILE=balanced` (legacy name, see CFG-003) instead of the canonical `LLM_ROUTER_COST_PROFILE`, and also documents `LLM_ROUTER_TIER=free` — a variable that is real (a pydantic `Settings` field `llm_router_tier: Tier = Tier.FREE`, config.py:365) but invisible to a literal-string grep because pydantic-settings binds it implicitly by field name rather than via any `os.environ.get("LLM_ROUTER_TIER")` call anywhere in the source.
Evidence: .env.example:24-26; config.py:365 `llm_router_tier: Tier = Tier.FREE`; no `os.environ.get("LLM_ROUTER_TIER"` / `getenv("LLM_ROUTER_TIER"` hits anywhere in src/ (confirmed by grep); docs/archive/AUDIT_2026-08-30.md:130-132 already verified this exact var for a different concern (collision) and found it "Clean" with "only one reader" — corroborating it is real, just implicitly bound.
Why this exists, if discoverable: Pydantic-settings' implicit field-name-to-env-var binding is a legitimate pattern, but it means a purely-literal-string mechanical inventory (including this audit's own first pass, and structurally, `env_registry.py`'s AST scanner) cannot see these reads at all — a distinct blind-spot class from CFG-007's indirect-variable reads.
Why this matters: Minor by itself; flagged because it demonstrates a THIRD way an env var can be invisible to grep-based and AST-based inventories (implicit pydantic binding), in addition to indirect-constant reads (CFG-007) and dynamically-constructed names (not concretely found in this codebase, but structurally possible via f-strings, e.g. the per-provider `LLM_ROUTER_BUDGET_<PROVIDER>` pattern was checked and found to use literal names, not f-strings — verified clean).
User-visible impact: None (LLM_ROUTER_TIER already works as documented).
Engineering impact: Confirms env_registry.py's stated scope (AST-literal scan) cannot be extended to full completeness without also handling pydantic Settings fields as a separate declared category.
Is behavior currently used? YES.
Recommended action: KEEP (informational) — combine with CFG-003's .env.example fix; also fix the LLM_ROUTER_PROFILE→LLM_ROUTER_COST_PROFILE line while editing this file.
Proposed target: n/a.
Behavioral compatibility risk: NONE.
Security risk: None.
Performance impact: None.
Estimated complexity removed: None; informational.
Validation required: None.
Dependencies on other findings: CFG-003, CFG-007.

ID: CFG-007
Category: One source of truth (§73) — registry completeness gap
Severity: MEDIUM
Confidence: HIGH
Location: Files: src/llm_router/env_registry.py, tests/test_env_registry.py, src/llm_router/timeout_config.py:62, src/llm_router/identity.py (multiple)
Symbols: ENV_REGISTRY, _INDIRECT_READS, _env_reads_in_source
Lines: env_registry.py (whole); test_env_registry.py:33-73,101-127; timeout_config.py:62; identity.py:47,184,250,369,380
Observation: `ENV_REGISTRY` is validated against an independently-implemented AST scanner that can only see `os.environ.get`/`os.getenv`/`os.environ[...]` calls whose argument is a literal string constant. The scanner's authors already know reads through a variable are invisible to it and maintain a `_INDIRECT_READS` frozenset (4 entries) specifically for known cases of this. At least 12 more real, actively-read variables are read the same indirect way but appear in NEITHER `ENV_REGISTRY` NOR `_INDIRECT_READS`: the 5 timeout vars documented in timeout_config.py's own module docstring (LLM_ROUTER_REQUEST_TIMEOUT, REASONING_TIMEOUT, MEDIA_REQUEST_TIMEOUT, CODEX_TIMEOUT, BENCHMARK_TIMEOUT — all read via a loop variable at timeout_config.py:62) and 6 identity vars (LLM_ROUTER_AGENT_ID, USER_ID, USER_EMAIL, ORG_ID, TEAM_ID, TENANT_ID — all read via `..._ENV`-suffixed module constants in identity.py), plus LLM_ROUTER_MONTHLY_BUDGET (read only as an implicit pydantic Settings field binding, not any os.environ call — a third, distinct blind-spot class, see CFG-006). Because the validating test can only assert on what its own AST scanner finds, none of this ever fails CI: `test_every_env_var_read_is_registered` is vacuously satisfied for reads its scanner structurally cannot see.
Evidence: test_env_registry.py:60-73 `_env_reads_in_source` only matches `ast.Call` nodes with a literal first-arg `ast.Constant`, or `ast.Subscript` with a literal-constant key; test_env_registry.py:101-127 `_INDIRECT_READS` frozenset (4 entries) and its own comment "Keep this list SHORT: every entry is a hole in the scan"; direct grep confirms `grep -c "\"LLM_ROUTER_REQUEST_TIMEOUT\":" env_registry.py` = 0 (and same for REASONING_TIMEOUT, MEDIA_REQUEST_TIMEOUT, CODEX_TIMEOUT, BENCHMARK_TIMEOUT, AGENT_ID, USER_ID, USER_EMAIL, ORG_ID, TEAM_ID, TENANT_ID, MONTHLY_BUDGET — each verified individually after an initial `comm`-based cross-check was found to be corrupted by a locale-sort mismatch and redone correctly, see Methodology).
Why this exists, if discoverable: The scanner's own docstring is honest about this limit ("net_bind.py does `for env in (A, B): os.environ.get(env)`. A scan that matches only literal arguments misses that, and so would any similar tool.") — the gap is a known, accepted limitation of the mechanism, just not a completely enumerated one; the `_INDIRECT_READS` list appears to have been populated only for the specific cases the authors happened to be looking at when they wrote it (net_bind.py, identity.py's TOKEN/AUDIT_DISABLED), not exhaustively for every indirect reader in the tree.
Why this matters: `env_registry.py`'s own header claims to cover "every environment variable **the package** reads" (bolded in the source specifically because an earlier, larger claim was found to be wrong — see its own T-28 note) — this remains not quite true for the indirect-read class, silently, because the self-check that would catch a gap here is structurally blind to it.
User-visible impact: None directly.
Engineering impact: A maintainer relying on `ENV_REGISTRY` (or `llm-router doctor`, which renders from it per install_hooks.py cross-references) as the complete list of config surface will miss the timeout family entirely and the whole identity/tenancy family entirely — exactly the vars an enterprise/multi-tenant deployment would most need documented.
Is behavior currently used? YES — all 12 vars are actively read in shipped code paths (timeouts on every HTTP/reasoning/media/codex/benchmark call; identity on every routed request that needs attribution).
Recommended action: SIMPLIFY / DEPRECATE the gap — either (a) add these 12 to `_INDIRECT_READS` (cheapest, matches the pattern already used for the other 4), or (b) teach the scanner to resolve simple same-module `NAME = "LITERAL"` aliases before checking `Call`/`Subscript` args, which would close most of this gap AND any future ones of the same shape without a per-var manual list.
Proposed target: `_INDIRECT_READS` grows from 4 to ~16 entries, OR the scanner gains one more AST pass (resolve `Name` args back to a module-level string assignment before falling back to "unresolvable").
Behavioral compatibility risk: NONE (test-only change).
Security risk: None directly — but a registry that silently omits the identity/tenancy env var family is a bad place to look for "what identity data can be influenced by environment" during a security review.
Performance impact: None.
Estimated complexity removed: N/A — this is a completeness fix, not a simplification.
Validation required: Extend test_env_registry.py's own test suite to include the 12 vars found here, verifying `test_every_env_var_read_is_registered` and `test_the_scanner_cannot_see_indirect_reads` both pass with the extended list.
Dependencies on other findings: CFG-005, CFG-006.

ID: CFG-008
Category: CLI consistency — machine-readable output
Severity: MEDIUM
Confidence: HIGH
Location: Files: src/llm_router/commands/*.py (44 files), src/llm_router/commands/audit.py:91
Symbols: n/a
Lines: audit.py:91 (`--json` `add_argument`)
Observation: Of 44 CLI subcommand modules, exactly one (`commands/audit.py`, the `misroute` subcommand) exposes a `--json`/machine-readable output flag. No other command — including the ones most likely to be scripted or dashboarded (`status`, `config`, `doctor`, `budget`, `stats`, `sessions`, `savings-report`) — offers structured output; all are human-formatted-text-only.
Evidence: `grep -l -- "--json" src/llm_router/commands/*.py` → only audit.py; `grep -n -- "--json" audit.py` → line 91, scoped to the `misroute` subparser only.
Why this exists, if discoverable: Not discoverable from comments; appears to be organic growth without a cross-cutting output-format convention.
Why this matters: A product whose core value proposition is measurable savings/routing behavior (per README/CHANGELOG) has essentially no scriptable interface to its own data outside one narrow debug subcommand — any external dashboard, CI gate, or automation has to screen-scrape human-formatted CLI output.
User-visible impact: Anyone trying to pipe `llm-router status`/`doctor`/`budget` into another tool must parse decorated terminal text (colors, box-drawing characters, emoji observed in this audit's own executions).
Engineering impact: No shared "render as text or JSON" helper exists to retrofit; each command would need one added individually.
Is behavior currently used? N/A (absence of a feature).
Recommended action: SIMPLIFY — introduce one shared `--json` convention (a decorator or helper function commands opt into) rather than continuing ad hoc; prioritize `status`, `doctor`, `budget`, `config` as the highest-value targets.
Proposed target: A shared `commands/_output.py`-style helper providing `emit(data, json_flag)`; migrate high-value commands first.
Behavioral compatibility risk: LOW (additive flag, no change to default text output).
Security risk: None.
Performance impact: None.
Estimated complexity removed: N/A (this is a feature gap, not simplification).
Validation required: N/A for the finding itself.
Dependencies on other findings: CFG-011 (parsing-style consistency would make this easier to retrofit uniformly).

ID: CFG-009
Category: CLI consistency — missing --help
Severity: MEDIUM-HIGH
Confidence: HIGH
Location: Files: src/llm_router/commands/budget.py, config.py, dashboard.py, demo.py, doctor.py, explain_dashboard.py, gain.py, onboard.py, probe.py, profile.py, routing.py, savings_report.py, set_enforce.py, setup.py, share.py, status.py, team.py, test.py, uninstall.py, update.py (20 files)
Symbols: cmd_status, cmd_config, cmd_doctor (via main), cmd_budget, cmd_team, and 15 others
Lines: n/a (absence of any `--help`/`-h`/`argparse` handling in these 20 files, confirmed by grep and by direct execution for 5 of them)
Observation: 20 of 44 CLI subcommand modules contain no `--help`/`-h` handling of any kind. Confirmed by direct execution against the baseline: `llm-router status --help`, `llm-router config --help`, `llm-router doctor --help`, `llm-router budget --help`, and `llm-router team --help` all silently execute the command's normal (in doctor's case, network/file-probing) behavior instead of printing help, because the flag is simply never inspected — it is either ignored entirely or, depending on the command's own positional-arg parsing, could be misinterpreted as a value. `llm-router doctor --help` additionally exits with code 1 (its normal "health check found problems" exit code) rather than 0.
Evidence: `for f in commands/*.py; do grep -q "argparse\|--help\|\"-h\"" $f || echo $f; done` → the 20 files listed; direct execution transcripts for status/config/doctor/budget/team (see §8 of this report) showing real command output and, for doctor, `[exit code: 1]`.
Why this exists, if discoverable: The 16 argparse-based commands get `--help` for free; the 28 hand-rolled-parsing commands only get it if the author manually added a check (8 of them did: admin_actions, dev_refresh, install, okf, semantic, sessions, sse, welcome).
Why this matters: `--help` is the standard, expected way to discover a CLI command's options; a command that silently runs instead of showing help is surprising at best (for `status`/`config`/`budget`/`team`, relatively low-stakes since they're read-only) and actively misleading for `doctor`, which returns a "something's broken" exit code for what looks like (and was intended as) a help request. Any script that probes command availability via `cmd --help; echo $?` will misdiagnose `doctor` as broken.
User-visible impact: Confusing/incorrect behavior for a very common CLI convention across nearly half the command surface.
Engineering impact: No shared arg-parsing base to retrofit from; each of the 20 hand-rolled-with-no-help files needs its own fix (or a shared thin wrapper).
Is behavior currently used? YES — these are all live, dispatched commands (status/config/doctor/budget in particular are high-traffic per the CLI's own docstring usage examples).
Recommended action: SIMPLIFY — add a minimal shared "does argv contain -h/--help? print the docstring, exit 0" guard applied uniformly at the dispatch layer in cli.py (one place) rather than 20 separate per-command fixes; this also gives every future hand-rolled command --help for free.
Proposed target: A single check in `cli.py:main()` before dispatching to any subcommand: if `-h`/`--help` in args, look up and print that subcommand's help text (could reuse the existing module docstring block, parsed by subcommand) and exit 0 — never reaching the command's own body.
Behavioral compatibility risk: LOW — for the 20 affected commands, --help currently means "run the command", which is very unlikely to be relied upon by anyone as intentional behavior.
Security risk: None.
Performance impact: None.
Estimated complexity removed: Consolidates 20 potential per-command fixes into 1 dispatch-layer fix.
Validation required: A parametrized test asserting `llm-router <cmd> --help` exits 0 and does not perform the command's side effects, for all 44 dispatched commands (would also have caught CFG-010).
Dependencies on other findings: CFG-011.

ID: CFG-010
Category: CLI exit-code correctness
Severity: HIGH
Confidence: HIGH (execution-confirmed for `last`; source-confirmed, not separately executed, for `retrospect`)
Location: Files: src/llm_router/cli.py (dispatch), src/llm_router/commands/last.py:130-159, src/llm_router/commands/retrospect.py:48-107, src/llm_router/commands/snapshot.py:202-232
Symbols: main() dispatch for "last"/"retrospect"/"snapshot"; commands/last.py:main, commands/retrospect.py:main, commands/snapshot.py:main
Lines: cli.py's "last"/"retrospect"/"snapshot" dispatch branches (no sys.exit wrapper); last.py:130-159 (return 1 at 155, return 0 at 159); retrospect.py:48-107 (return 1 at 94, return 0 at 107); snapshot.py:202-232 (no return 1 path currently, but same dispatch shape)
Observation: `commands/last.py:main()` and `commands/retrospect.py:main()` are both typed `-> int` with a documented "Returns: Exit code" contract and both have a genuine `return 1` failure path. `cli.py`'s dispatch calls each as a bare statement (`_last_main(args[1:])`, `_retrospect_main(args[1:])`) without `sys.exit(...)`, discarding the return value — so the OS-level process exit code is always 0 no matter what the function returns. Confirmed live: running `llm-router last` against a fresh, empty install prints "Error: .../.llm-router/usage.db not found. Run some routed calls first." (the exact `return 1` branch) and the shell reports `exit code 0`. `commands/snapshot.py:main()` shares the identical dispatch shape (typed `-> int`, called without `sys.exit`) but has no `return 1`/nonzero branch in its current source, so it is a latent instance of the same class rather than a currently-observable bug.
Evidence: Direct execution transcript (this audit): `sys.argv=["llm-router","last"]; main()` → prints the not-found error, no `SystemExit` raised, `$?` = 0 in the wrapping shell. `commands/last.py:150-159` source. cli.py's own comment on the adjacent "verify" dispatch branch (tagged `CHZ-PKG-005`): "propagate verify's exit code. main() returns 1 when any health check fails; discarding it made `llm_router verify` always exit 0 ... a CI/install gate keying on the exit code treated a broken install as healthy." — i.e. the exact same bug, already found and fixed once, for a sibling command.
Why this exists, if discoverable: `verify`'s fix (sys.exit wrapping) was applied to that one command specifically after the bug was found; the same audit pass evidently did not check the other commands sharing the identical `main() -> int` / bare-call dispatch shape.
Why this matters: `last` and `retrospect` are diagnostic/reporting commands exactly the kind a script or CI job would run and gate on (`llm-router last || alert`) — and both will report success unconditionally.
User-visible impact: A script or a person relying on the exit code of `llm-router last`/`llm-router retrospect` to detect failure gets a false "succeeded" signal.
Engineering impact: The fix pattern is already in the codebase (verify's `sys.exit(_verify_main(args[1:]))`) and just needs to be applied to 2 (or a lint that catches all `-> int` functions dispatched without sys.exit, covering `snapshot` too before it becomes live).
Is behavior currently used? YES for `last`/`retrospect` (confirmed dispatched, confirmed reachable failure path for `last`); `snapshot`'s failure path is currently unreachable (no `return 1` exists yet) so its exit-code bug is dormant.
Recommended action: SIMPLIFY — wrap `last`, `retrospect`, and (preemptively) `snapshot` in `sys.exit(...)` at the cli.py dispatch site, exactly matching the `verify` fix.
Proposed target: `sys.exit(_last_main(args[1:]))`, `sys.exit(_retrospect_main(args[1:]))`, `sys.exit(_snapshot_main(args[1:]))` (renaming imports as needed to match existing convention).
Behavioral compatibility risk: LOW — a script currently relying on `last`/`retrospect` always exiting 0 (unlikely, since that would mean relying on a bug) would need to handle the newly-correct exit code, which is the intended fix.
Security risk: None.
Performance impact: None.
Estimated complexity removed: N/A — this is a 1-line-per-command correctness fix, not a simplification, but it is the single most concrete, reproducible bug found in this domain.
Validation required: A regression test executing `llm-router last` and `llm-router retrospect` against a fresh/empty state and asserting `SystemExit(1)` is raised (the general "every `-> int` command's exit code is honored" test proposed under CFG-009 would also cover this).
Dependencies on other findings: CFG-009 (a shared dispatch-layer test would catch both).

ID: CFG-011
Category: CLI consistency — argument parsing style
Severity: MEDIUM
Confidence: HIGH
Location: Files: src/llm_router/commands/*.py (44 files)
Symbols: n/a
Lines: n/a
Observation: CLI argument parsing across the 44 command modules is split 16 (argparse) / 28 (hand-rolled `sys.argv` slicing/looping, e.g. `commands/status.py`'s style or the manual `while i < len(rest)` loop for `summary` inlined directly in cli.py). There is no shared parsing helper or convention document; each hand-rolled command reimplements flag detection, and correctness of `--help` (CFG-009), unknown-flag handling, and `--json` (CFG-008) varies file-by-file as a direct consequence.
Evidence: `grep -l "^import argparse\|^from argparse" commands/*.py` → 16 files; `grep -L` (inverse) → 28 files; the `summary` dispatch branch inlined in cli.py itself (lines ~1035-1060) hand-parses `--since-hours`/`--limit`/`--markdown`/`--watch`/`--watch-interval` with a manual `while` loop rather than delegating to argparse, as one example of the pattern repeating even inside cli.py.
Why this exists, if discoverable: Organic growth over many contributions/versions; no single PR appears to have set a parsing convention.
Why this matters: This is the root cause tying together CFG-008 (only argparse commands get --json "for free" in principle, though even they mostly don't use it) and CFG-009 (only argparse commands get --help for free); fixing either downstream finding without addressing this would mean re-solving the same problem 28 more times.
User-visible impact: Inconsistent flag syntax expectations and error messages across commands (some print usage on a bad flag, most silently ignore it per CFG-009's findings).
Engineering impact: No shared surface to build a cross-cutting feature (like --json or --help) on top of without touching every file individually.
Is behavior currently used? YES (all 44 commands are live and dispatched).
Recommended action: MERGE toward a single convention — either standardize all commands on argparse (heavier per-file but gets --help/error-handling for free) or introduce one small shared parsing helper for the common cases (flag detection, --help, --json) that hand-rolled commands can call into without adopting full argparse. Either closes CFG-008 and CFG-009 in one pass.
Proposed target: One documented parsing convention (argparse-based subparser-per-command, or a lightweight shared helper) applied incrementally, starting with the highest-traffic commands (status, config, doctor, budget per CFG-009's --help gap).
Behavioral compatibility risk: MEDIUM if migrating existing hand-rolled commands to argparse changes their exact flag syntax or error text — needs care per-command, not a blanket rewrite in one commit.
Security risk: None.
Performance impact: None.
Estimated complexity removed: Meaningful — removes 28 independent, unreviewed argument-parsing implementations.
Validation required: Existing per-command tests should continue to pass; add the --help/--json regression tests proposed under CFG-008/CFG-009 as the acceptance bar for any migrated command.
Dependencies on other findings: CFG-008, CFG-009.

ID: CFG-012
Category: One source of truth (§73) — CLI command list
Severity: LOW
Confidence: HIGH
Location: Files: src/llm_router/cli.py:833-889
Symbols: _KNOWN_SUBCOMMANDS
Lines: cli.py:833-836 (comment), 837-889 (frozenset)
Observation: `_KNOWN_SUBCOMMANDS` is a second, hand-maintained list of every dispatched subcommand name, used only to power a Levenshtein-style "did you mean" suggestion (via `difflib.get_close_matches`) when an unrecognized command is typed. Its own comment explicitly states it is "NOT a second source of truth for dispatch itself, so it can drift without breaking anything except the suggestion quality."
Evidence: cli.py:833-836 comment; cli.py:837-889 frozenset literal, kept separate from the `if/elif` dispatch chain it mirrors.
Why this exists, if discoverable: Added per the cited "GH#61" fix (preventing an unrecognized command from falling through to starting the MCP stdio server and hanging).
Why this matters: This is the kind of duplicate the brief's §73 asks to be flagged — but it is a well-scoped, self-aware one: its blast radius on drift is capped at "worse typo suggestions," not incorrect dispatch.
User-visible impact: If a new command is added to the dispatch chain but not to this set, the only user-visible effect is a missing/wrong "did you mean" hint for typos of that command — never a dispatch failure.
Engineering impact: One more place to remember to update when adding a command, but with no correctness consequence if forgotten.
Is behavior currently used? YES.
Recommended action: KEEP as-is (already correctly scoped and labeled), or, as a low-cost improvement, derive it via a small script/lint that extracts subcommand literals from the dispatch chain's `elif args and args[0] == "..."` pattern at test time and asserts it matches `_KNOWN_SUBCOMMANDS`, closing the drift risk at near-zero cost without merging the two lists.
Proposed target: n/a (already acceptable) or an added lint/test per above.
Behavioral compatibility risk: NONE.
Security risk: None.
Performance impact: None.
Estimated complexity removed: N/A.
Validation required: N/A for keeping as-is; a drift-check test if the low-cost improvement is taken.
Dependencies on other findings: None.

ID: CFG-013
Category: Boolean/mode explosion
Severity: MEDIUM
Confidence: MEDIUM (structure and defaults are HIGH-confidence; claim of untested/harmful interaction is UNCERTAIN — not exercised in this audit)
Location: Files: src/llm_router/enforce_config.py, src/llm_router/hooks/auto-route.py, src/llm_router/hooks/enforce-route.py, src/llm_router/tool_surface.py, src/llm_router/hooks/agent_writes.py, src/llm_router/hooks/session-start.py (via commands/demo.py etc. for CLAUDE_SUBSCRIPTION)
Symbols: LLM_ROUTER_ENFORCE, LLM_ROUTER_ZERO_CLAUDE, LLM_ROUTER_DELEGATE, LLM_ROUTER_DIRECT_EXECUTION, LLM_ROUTER_AGENT_WRITES, LLM_ROUTER_HISTORY_RELAY, LLM_ROUTER_CLAUDE_SUBSCRIPTION, LLM_ROUTER_SLIM
Lines: see §2/§6 tables for exact read sites
Observation: At least 8 independently-read, independently-defaulted knobs jointly determine whether/how a prompt is routed and how directly the system can act on the user's behalf: 1 eight-value enum (ENFORCE) and at least 4 clean booleans (ZERO_CLAUDE, DELEGATE default-on, DIRECT_EXECUTION default-on, CLAUDE_SUBSCRIPTION default-off), plus 3 more mode/boolean vars (AGENT_WRITES, HISTORY_RELAY, SLIM). No single type or enum ties these together; each is read and defaulted independently in its own module.
Evidence: read sites and defaults tabulated in §6; each var's default confirmed by direct grep of its `os.environ.get(...)` call.
Why this exists, if discoverable: Organic accretion — each var was plausibly added to solve one specific problem (zero-Claude direct replacement, direct tool execution safety, Claude-subscription cost mode, etc.) without a unifying "operating mode" concept.
Why this matters: 2⁴ × 8 = 128 nominally reachable states from just the 4 clean booleans and the enum, before AGENT_WRITES/HISTORY_RELAY/SLIM are added, and nothing in the codebase enumerates which combinations are meaningful, which are equivalent, and which are invalid/untested. This is exactly the "count reachable configuration states and invalid combinations" ask in brief §25.
User-visible impact: UNCERTAIN — not demonstrated to cause a concrete user-visible bug in this audit; flagged as a complexity/maintainability risk, not a proven defect.
Engineering impact: Hard to reason about which combination a bug report represents; hard to write exhaustive tests; each new boolean multiplies the untested state space.
Is behavior currently used? YES for each variable individually; the CROSS-PRODUCT of settings is UNCERTAIN — not verified as reachable/tested in combination.
Recommended action: SIMPLIFY (candidate, needs evidence) — consider whether ZERO_CLAUDE, DELEGATE, DIRECT_EXECUTION, and HISTORY_RELAY are truly 4 independent axes or whether some are actually sub-modes of ENFORCE that happen to be split into separate env vars for historical reasons; a single "operating profile" enum (of which "smart + direct execution on + history relay on" etc. are named presets) would reduce both the state space and the number of env vars a new deployment has to reason about.
Proposed target: Not designed here — flagged for the routing-engine/§13 and product-core/§7 auditors, since resolving this requires understanding routing semantics beyond this domain's scope.
Behavioral compatibility risk: N/A (no change proposed by this auditor; recommendation is to investigate, not to merge).
Security risk: UNCERTAIN — ZERO_CLAUDE and DIRECT_EXECUTION both have safety relevance (one replaces Claude's turn outright, the other governs tool execution); an untested interaction between them is worth the security auditor's attention.
Performance impact: None.
Estimated complexity removed: Potentially significant if consolidation is possible; NOT quantified here.
Validation required: An interaction-matrix test (or at minimum, documentation of which combinations are supported) before any consolidation is attempted.
Dependencies on other findings: None directly; relevant to routing-engine (§13) and security (§31) domains' findings if they exist.

ID: CFG-014
Category: Env var completeness — privacy-relevant, untested
Severity: LOW-MEDIUM
Confidence: HIGH (existence and lack of coverage); MEDIUM (severity, since exact privacy impact of a gap here wasn't traced end-to-end)
Location: Files: src/llm_router/hooks/auto-route.py:4189-4195
Symbols: n/a (inline env check)
Lines: auto-route.py:4189-4195
Observation: `LLM_ROUTER_HISTORY_RELAY` gates a privacy-relevant behavior per its own inline comment ("Privacy gate (audit P2): LLM_ROUTER_HISTORY_RELAY=off keeps direct..."), defaults to "on", and has zero test references and zero documentation references anywhere in the repository (confirmed in the §2 mechanical table and §3's "dark vars" list).
Evidence: auto-route.py:4189-4192 comment and `os.environ.get("LLM_ROUTER_HISTORY_RELAY", "on")` call; grep across tests/docs/guide/architecture/README/SECURITY/CHANGELOG for the literal string returns 0 hits.
Why this exists, if discoverable: References "audit P2" — a prior internal audit pass that presumably added the gate as a remediation but did not follow up with tests or docs.
Why this matters: A privacy control that is undocumented cannot be verified by an operator to be doing what they think, and untested means a future refactor could silently break it without any test failing.
User-visible impact: UNCERTAIN whether currently broken (not exercised); the risk is regression-without-detection, not a proven current failure.
Engineering impact: Same as any untested security/privacy-relevant branch — a silent regression risk.
Is behavior currently used? YES (default "on", read on every relevant hook invocation).
Recommended action: SIMPLIFY (documentation + test coverage, not a code change) — add this to the same doc surface that covers other privacy controls (README's privacy/security section, per brief §41's claim ledger ask), and add a regression test asserting `LLM_ROUTER_HISTORY_RELAY=off` actually suppresses whatever it is supposed to suppress.
Proposed target: n/a (out of this domain's scope to design the test — flagged for the privacy/§32 auditor to confirm intended behavior first).
Behavioral compatibility risk: NONE (doc/test-only recommendation).
Security risk: MEDIUM (privacy-adjacent, currently unverifiable) — flagged for cross-reference with the §32 privacy auditor.
Performance impact: None.
Estimated complexity removed: N/A.
Validation required: See §32 auditor's findings, if any, on this same variable.
Dependencies on other findings: None within this domain; likely relevant to a §32 (privacy) finding.
```

## 10. Top items for synthesis

1. **CFG-010** (HIGH) — `llm-router last`/`retrospect` always exit 0 even on documented
   failure paths; the exact bug class already fixed once for `verify` (comment
   `CHZ-PKG-005`), confirmed by live execution. Strong Top-10 correctness-risk candidate.
2. **CFG-001** (HIGH) — `LLM_ROUTER_ENFORCE` has two hook scripts (`stop-enforce.py`,
   `status-bar.py`) that bypass the codebase's own explicitly-labeled "single source of
   truth" resolver and hardcode a different default ("hard" vs "smart"). Strong
   correctness-risk candidate; also a config-precedence exemplar for §25.
3. **CFG-004** (MEDIUM-HIGH) — `safe_config.py`'s documented config precedence is
   backwards relative to what the pydantic-settings code it describes actually does.
   Strong doc-problem candidate for §66/§41-style "doc contradicts code" ledger.
4. **CFG-009** (MEDIUM-HIGH) — ~45% of CLI commands silently ignore `--help` and run
   real behavior instead (`doctor --help` even exits 1). Reproducible, broad-surface UX
   defect; good complexity/consolidation candidate (one dispatch-layer fix covers 20
   commands).
5. **CFG-002** (MEDIUM-HIGH) — a git-tracked, 5-month-stale copy of `auto-route.py`
   (`.claude/hooks/`) with drifted env-var defaults silently governs this repo's own
   dev sessions. Good deletion-ledger candidate (safe-now: not shipped, confirmed
   excluded from sdist).
6. **CFG-007** (MEDIUM) — the codebase's best-designed anti-drift mechanism
   (`env_registry.py` + independent AST-scan test) has a real, evidenced, structural
   blind spot (indirect reads) that hides 12+ real vars from its own "complete"
   inventory claim — a good exemplar for "even a self-defending guard needs a second
   check on its blind spots."
7. **CFG-003** (MEDIUM) — `LLM_ROUTER_PROFILE` env-var name collision across two
   unrelated config axes, well-mitigated but still live in `.env.example`'s onboarding
   text. Good do-not-change-the-mitigation-but-fix-the-onboarding item.
8. **CFG-013** (MEDIUM, needs evidence) — boolean/mode explosion across
   ENFORCE/ZERO_CLAUDE/DELEGATE/DIRECT_EXECUTION/AGENT_WRITES/HISTORY_RELAY/SLIM;
   128+ nominally reachable states with no enumerated valid/invalid matrix. Hand off
   to routing-engine (§13) and security (§31) auditors for interaction-risk follow-up.
9. **CFG-008/CFG-011** (MEDIUM) — only 1/44 commands support `--json`; CLI arg-parsing
   is split 16 argparse / 28 hand-rolled with no shared convention. Root-caused
   together; a single consolidation effort (shared parsing helper) would resolve both
   plus most of CFG-009.
10. **§7 do-not-change register candidates**: `env_registry.py`/`test_env_registry.py`'s
    "check the check" design; the `LLM_ROUTER_PROFILE` collision's value-domain-filter
    mitigation; `cli.py`'s doc-string-enforced-by-test usage block
    (`test_f38_every_command_is_documented.py`); `_KNOWN_SUBCOMMANDS`'s honest scoping
    as a non-authoritative typo-suggestion list.
