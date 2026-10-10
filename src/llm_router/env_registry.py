"""Central registry of every environment variable **the package** reads.

SCOPE, stated because it was over-claimed (T-28, audit 2026-09-22). This covers
`src/llm_router/`. It does NOT cover `scripts/`, where an independent AST scan
found 18 further variables. The previous first line said "every environment
variable this codebase reads", which is a larger claim than the registry keeps —
and a registry that over-states its own coverage is worse than one that does
not exist, because it stops people looking.

Extending the scan to `scripts/` is a real option; declaring the boundary is the
minimum. `tests/test_env_registry.py` enforces the boundary as written here.

RED8-10. 195 distinct variables are read across 313 sites, and nothing declared
them. A config surface nobody has enumerated cannot be documented, cannot be
validated, and drifts silently -- the audit counted 186; by the time it was
measured here it was 195, and no one had noticed the difference.

WHY THIS IS A CHECKED-IN LITERAL AND NOT GENERATED
--------------------------------------------------
The obvious implementation is to walk the AST at import time and build this dict
from what the code actually reads. That would be worthless. The test that
validates the registry ALSO walks the AST, so a generated registry validates
against itself and passes unconditionally -- exactly the trap this audit has
already found twice:

  * ``tool_surface.unregistered()`` checked tier constants against ``_TIERS``,
    which IS the tier constants. A bogus tool name passed lint and 106 tests.
  * ``lint_tool_surface.py`` checks emitters against emitters, and reports clean
    under the same mutation.

Both LOOKED like validation. So this literal is the DECLARATION and the AST scan
is INDEPENDENT ground truth; the test compares them. Adding a new
``os.environ.get("X")`` fails that test until someone declares X here, which is
the entire point -- the friction is the feature.

CATEGORIES
----------
``llm_router``              this project's own configuration
``provider_credential`` third-party API keys and tokens -- never log these
``external_tool``       config for tools we shell out to or integrate with
``platform``            OS/terminal conventions (HOME, NO_COLOR, ...)
``test_only``           set by the test runner; must not affect production paths
"""

from __future__ import annotations

__all__ = ["ENV_REGISTRY", "CATEGORIES", "registered_names", "category_of"]

CATEGORIES = frozenset(
    {"llm_router", "provider_credential", "external_tool", "platform", "test_only"}
)

#: name -> (category, first_module_that_reads_it, module_count_at_registration)
ENV_REGISTRY: dict[str, tuple[str, str, int]] = {
    # ── llm_router  (153) ──
    "LLM_ROUTER_ADMIN_ACTIONS_PATH": ("llm_router", "admin_actions.py", 1),
    "LLM_ROUTER_AGENTIC_MODEL": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_AGENTS_CONFIG": ("llm_router", "tools/agents.py", 1),
    "LLM_ROUTER_AGENT_POLICY_MODE": ("llm_router", "router.py", 1),
    "LLM_ROUTER_AGENT_ROUTE_ALLOW": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_AGENT_WORKERS": ("llm_router", "agent_exec.py", 1),
    "LLM_ROUTER_AGENT_SLOT_TTL_S": ("llm_router", "hooks/agent-route.py", 2),
    "LLM_ROUTER_AGENT_ROUTE_CODEX": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_AGENT_ROUTE_CODEX_DAILY_BUDGET": ("llm_router", "hooks/agent-route.py", 1),
    # 2026-10-03: the Codex agent model used for subagent delegation (gpt-6-astra
    # by default — see codex_agent/REPORT.txt: 13/17 vs Claude's 13/17 on
    # identical real tasks, Wilson95 CIs overlapping). Previously hardcoded via
    # omission (run_codex's own default, gpt-5.5, which the evidence never
    # measured because that arm hit the account's quota on task 1).
    "LLM_ROUTER_CODEX_AGENT_MODEL": ("llm_router", "hooks/agent-route.py", 1),
    # 2026-10-03: delegations per rolling 5h Codex window (default 15, below the
    # ~17 a ChatGPT Plus account managed before its usage limit). 0 blocks all
    # Codex delegation (kill switch); it does not remove the cap.
    "LLM_ROUTER_CODEX_WINDOW_BUDGET": ("llm_router", "codex_window.py", 1),
    # 2026-09-28: opt-in override that keeps agent-route active (Codex/DIRECT
    # routing) in a headless (CLAUDE_CODE_ENTRYPOINT=sdk-*) session. Default
    # off — see the headless-guard note in hooks/agent-route.py.
    "LLM_ROUTER_AGENT_ROUTE_HEADLESS": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_ALERT_WEBHOOK": ("llm_router", "alerts.py", 1),
    # T-09: the quality/cost exchange rate in the bandit's reward — what one
    # correct answer is worth, in dollars. 0 makes the bandit rank purely by
    # cheapness, which is what the old unbounded ratio effectively did.
    "LLM_ROUTER_ANSWER_VALUE_USD": ("llm_router", "telemetry.py", 1),
    # T-15: explicit override for the HOST config dir (~/.claude). Distinct from
    # LLM_ROUTER_HOME, which covers llm-router's own state.
    "LLM_ROUTER_CLAUDE_DIR": ("llm_router", "install_hooks.py", 1),
    "LLM_ROUTER_ALLOWED_HOSTS": ("llm_router", "route_server.py", 1),
    "LLM_ROUTER_ALLOW_STUBS": ("llm_router", "cost.py", 2),
    "LLM_ROUTER_ALLOW_SUBAGENTS": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_ANOMALY_THRESHOLD": ("llm_router", "session_spend.py", 1),
    "LLM_ROUTER_AUDIT_DISABLED": ("llm_router", "misroute_audit.py", 1),
    "LLM_ROUTER_AUDIT_PATH": ("llm_router", "enterprise/audit.py", 1),
    # PR2 follow-up (2026-09-24): opt-in gate for the background benchmark
    # fetch, shared by benchmarks.py's benchmark_auto_fetch_enabled() and
    # hooks/session-start.py's ImportError fallback of the same check — off
    # by default (North Star #5, local-first).
    "LLM_ROUTER_AUTO_BENCHMARK_FETCH": ("llm_router", "benchmarks.py", 2),
    "LLM_ROUTER_BANDIT": ("llm_router", "router.py", 1),
    "LLM_ROUTER_BASH_COMPRESS": ("llm_router", "hooks/bash-compress.py", 1),
    "LLM_ROUTER_BENCHMARK_TTL_DAYS": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_BLOCK_PROVIDERS": ("llm_router", "router.py", 1),
    "LLM_ROUTER_BOUNDED_OPERATIONAL": ("llm_router", "bounded_operational.py", 1),
    "LLM_ROUTER_BROKER_CONCURRENCY": ("llm_router", "session_broker.py", 1),
    "LLM_ROUTER_BROKER_SECRET_FILE": ("llm_router", "session_broker.py", 1),
    "LLM_ROUTER_BROKER_SOCK": ("llm_router", "session_broker.py", 1),
    "LLM_ROUTER_BUDGETS_DB_PATH": ("llm_router", "budget_backend.py", 1),
    "LLM_ROUTER_BUDGET_BACKEND": ("llm_router", "budget_backend.py", 1),
    "LLM_ROUTER_BUDGET_FORECAST_HORIZON_SECONDS": ("llm_router", "budget_backend.py", 1),
    "LLM_ROUTER_BUDGET_FORECAST_MODE": ("llm_router", "budget_backend.py", 1),
    "LLM_ROUTER_BUDGET_FORECAST_WINDOW_SECONDS": ("llm_router", "budget_backend.py", 1),
    "LLM_ROUTER_BUDGET_POSTGRES_DSN": ("llm_router", "budget_backend_postgres.py", 1),
    "LLM_ROUTER_CAPABILITY_ROUTING": ("llm_router", "capabilities.py", 1),
    "LLM_ROUTER_CLASSIFY_LOCAL_ONLY": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_CLAUDE_SUBSCRIPTION": ("llm_router", "commands/demo.py", 6),
    "LLM_ROUTER_CLAUDE_TIMEOUT": ("llm_router", "claude_agent.py", 1),
    "LLM_ROUTER_CODEX_BASELINE": ("llm_router", "cost.py", 1),
    "LLM_ROUTER_CODEX_MODELS": ("llm_router", "codex_agent.py", 1),
    "LLM_ROUTER_COMPLEXITY_KNN": ("llm_router", "complexity_knn.py", 1),
    "LLM_ROUTER_COMPLEXITY_KNN_ARTIFACT": ("llm_router", "complexity_knn.py", 1),
    "LLM_ROUTER_COMPLEXITY_KNN_THRESHOLD": ("llm_router", "complexity_knn.py", 1),
    "LLM_ROUTER_COMPRESS_RESPONSE": ("llm_router", "tools/text.py", 1),
    # 2026-10-02: opt-in gate for llm_text_job (commit messages, PR
    # descriptions, long-output summaries on a local model with a checked
    # fallback). Off by default — see tools/text.py's _local_text_jobs_enabled.
    "LLM_ROUTER_LOCAL_TEXT_JOBS": ("llm_router", "tools/text.py", 1),
    "LLM_ROUTER_CONFIDENCE_THRESHOLD": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_CONTEXT_OPTIMIZER": ("llm_router", "context.py", 1),
    "LLM_ROUTER_COST_PROFILE": ("llm_router", "repo_config.py", 1),
    "LLM_ROUTER_CP_AUDIT_PATH": ("llm_router", "control_plane/audit.py", 1),
    "LLM_ROUTER_CP_POSTGRES_DSN": ("llm_router", "control_plane/store_postgres.py", 1),
    "LLM_ROUTER_CP_STORE_PATH": ("llm_router", "commands/cp.py", 2),
    "LLM_ROUTER_DB_PATH": ("llm_router", "agentic/telemetry.py", 2),
    "LLM_ROUTER_DELEGATE": ("llm_router", "hooks/enforce-route.py", 1),
    "LLM_ROUTER_DEPLOYMENT_PROFILE": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_DEV_SRC": ("llm_router", "commands/dev_refresh.py", 1),
    "LLM_ROUTER_DIRECT_EXECUTION": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_DRAFT_REVERT_AFTER": ("llm_router", "hooks/draft_usage.py", 1),
    "LLM_ROUTER_DRAFT_TASKS": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_ENFORCE_CONTEXT": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_QA_ROUTING": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_DISABLE_CONTINUATION_BYPASS": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS": ("llm_router", "router.py", 1),
    "LLM_ROUTER_DYNAMIC_LEADERBOARD_ORDERING": ("llm_router", "dynamic_routing.py", 1),
    "LLM_ROUTER_ENFORCE": ("llm_router", "commands/doctor.py", 9),
    "LLM_ROUTER_ENSEMBLE": ("llm_router", "ensemble.py", 1),
    "LLM_ROUTER_ENSEMBLE_PRIMARY": ("llm_router", "ensemble.py", 1),
    "LLM_ROUTER_ENSEMBLE_SECONDARY": ("llm_router", "ensemble.py", 1),
    "LLM_ROUTER_ENSEMBLE_TIMEOUT": ("llm_router", "ensemble.py", 1),
    "LLM_ROUTER_ESCALATE_DEADLINE_S": ("llm_router", "router.py", 1),
    "LLM_ROUTER_ESCALATE_ON_QUALITY": ("llm_router", "router.py", 1),
    "LLM_ROUTER_ESCALATE_THRESHOLD": ("llm_router", "router.py", 1),
    "LLM_ROUTER_EXECUTION_LEDGER_DB": ("llm_router", "execution_ledger.py", 1),
    "LLM_ROUTER_EXPLAIN": ("llm_router", "tools/routing.py", 2),
    "LLM_ROUTER_FORCE_COLOR": ("llm_router", "surface_status.py", 1),
    "LLM_ROUTER_FREE_TIER_DRAFTS": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_GATES": ("llm_router", "gates.py", 1),
    "LLM_ROUTER_GROUNDING_CHECK": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_LOCAL_AGENT_LOOP": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_AGENT_WRITES": ("llm_router", "hooks/agent_writes.py", 1),
    "LLM_ROUTER_AGENT_COMMANDS": ("llm_router", "hooks/agent_writes.py", 1),
    "LLM_ROUTER_AGENT_LOOP_BUDGET_S": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_OKF_AUTOINDEX": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_OKF_AUTOINDEX_TTL_H": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_HOOK_BUDGET_S": ("llm_router", "hooks/auto-route.py", 1),
    # P0.7-c: the only switch for the hook's LLM classifier layers (default off).
    "LLM_ROUTER_HOOK_LLM_LAYER": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_CONSTRAINED_TOOLS": ("llm_router", "hooks/agent_loop.py", 1),
    "LLM_ROUTER_AGENT_NUM_CTX": ("llm_router", "hooks/agent_loop.py", 1),
    "LLM_ROUTER_LOCAL_NUM_CTX": ("llm_router", "hooks/agent_loop.py", 1),
    "LLM_ROUTER_AGENT_TEMPERATURE": ("llm_router", "hooks/agent_loop.py", 1),
    "LLM_ROUTER_AGENT_WINDOW": ("llm_router", "hooks/context_budget.py", 1),
    "LLM_ROUTER_MAX_TOOL_RESULT_CHARS": ("llm_router", "hooks/context_budget.py", 1),
    "LLM_ROUTER_LOCAL_VISION": ("llm_router", "vision_registry.py", 1),
    "LLM_ROUTER_VISION_MODEL": ("llm_router", "vision_registry.py", 1),
    "LLM_ROUTER_COMPRESS_EMIT": ("llm_router", "hooks/bash-compress.py", 1),
    "LLM_ROUTER_IMAGE_INTERCEPT": ("llm_router", "hooks/tool_intercept.py", 1),
    "LLM_ROUTER_TOOLLAYER": ("llm_router", "toolkit/sandbox.py", 1),
    "LLM_ROUTER_TRACE": ("llm_router", "trace.py", 1),
    "LLM_ROUTER_DISCOVERY_TTL_HOURS": ("llm_router", "model_discovery.py", 1),
    "LLM_ROUTER_CONTEXT_INJECTION": ("llm_router", "context_injection.py", 1),
    "LLM_ROUTER_TRACE_FILE": ("llm_router", "trace.py", 1),
    "LLM_ROUTER_BASH_INTERCEPT": ("llm_router", "hooks/tool_intercept.py", 1),
    "LLM_ROUTER_SYNTHETIC": ("llm_router", "routing_quality.py", 2),
    "BENCH_SANDBOX": ("test_only", "routing_quality.py", 1),
    "LLM_ROUTER_GATEWAY_TOKEN": ("provider_credential", "gateway.py", 1),
    "LLM_ROUTER_HARNESS": ("llm_router", "scripts/routerarena/apply_divert_router.py", 1),
    "LLM_ROUTER_HOME": ("llm_router", "hooks/agent_writes.py", 2),
    # NS1 (2026-09-27): override for Claude Code's transcript directory, read by
    # northstar.py so a test (or a future multi-host build) can point the North
    # Star metric at a fixture tree instead of the operator's real ~/.claude.
    # Same override name scripts/groundtruth/sources.py already reads (out of
    # this registry's declared scope, hence declared here where it FIRST
    # enters src/llm_router/).
    "CLAUDE_PROJECTS_DIR": ("llm_router", "northstar.py", 1),
    "LLM_ROUTER_SYMBOL_GROUNDING": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_GATEWAY_HOST": ("llm_router", "presets.py", 1),
    "LLM_ROUTER_GATEWAY_PORT": ("llm_router", "presets.py", 1),
    "LLM_ROUTER_GATEWAY_URL": ("llm_router", "presets.py", 1),
    "LLM_ROUTER_PROXY_STEPS": ("llm_router", "proxy/server.py", 1),
    "LLM_ROUTER_PROXY_HEDGE_S": ("llm_router", "proxy/server.py", 1),
    "LLM_ROUTER_PROXY_STEP_BUDGET_S": ("llm_router", "proxy/server.py", 1),
    "LLM_ROUTER_PROXY_MODEL": ("llm_router", "proxy/server.py", 1),
    "LLM_ROUTER_PROXY_TRIM": ("llm_router", "proxy/server.py", 1),
    "LLM_ROUTER_PROXY_NUM_CTX": ("llm_router", "proxy/server.py", 1),
    "LLM_ROUTER_PROXY_UPSTREAM": ("llm_router", "proxy/server.py", 1),
    "LLM_ROUTER_PROXY_PORT": ("llm_router", "proxy/server.py", 1),
    "LLM_ROUTER_PROXY_UPSTREAM_PORT": ("llm_router", "proxy/failopen_shim.py", 1),
    "LLM_ROUTER_PROXY_LOOP_MAX_CONSECUTIVE": ("llm_router", "proxy/server.py", 1),
    "LLM_ROUTER_PROXY_LOOP_REPEAT_WINDOW": ("llm_router", "proxy/server.py", 1),
    # Backend-health breaker (proxy/backend_health.py): consecutive empty or
    # sub-second invalid replies before local serving pauses, and the pause.
    "LLM_ROUTER_PROXY_BACKEND_FAIL_N": ("llm_router", "proxy/server.py", 1),
    "LLM_ROUTER_PROXY_BACKEND_COOLDOWN_S": ("llm_router", "proxy/server.py", 1),
    # Opt-in serve mode (proxy/local_mode.py): off (default) or local-agent.
    "LLM_ROUTER_PROXY_LOCAL_AGENT_MODE": ("llm_router", "proxy/server.py", 1),
    # Opt-in SHADOW mode (proxy/local_shadow.py): off (default) or on.
    "LLM_ROUTER_PROXY_LOCAL_SHADOW": ("llm_router", "proxy/server.py", 1),
    "LLM_ROUTER_PROXY_TIERS": ("llm_router", "proxy/server.py", 1),
    "LLM_ROUTER_PROXY_TIER_POLICY": ("llm_router", "proxy/server.py", 1),
    # P0.11 (PLAN v16, D-20): the proxy's Haiku guard inputs (proxy/haiku_guard.py).
    "LLM_ROUTER_HAIKU_GUARD_KINDS": ("llm_router", "proxy/haiku_guard.py", 1),
    "LLM_ROUTER_HAIKU_GUARD_AUDIT_DIR": ("llm_router", "proxy/haiku_guard.py", 1),
    "LLM_ROUTER_HAIKU_GUARD_SHADOW_VERDICTS": ("llm_router", "proxy/haiku_guard.py", 1),
    # GE4 (PLAN v16, OD-4): the Frontier shadow switch, off unless on/1/true/yes (shadow_frontier.py).
    "LLM_ROUTER_SHADOW_FRONTIER": ("llm_router", "shadow_frontier.py", 1),
    # Kill switch for the tier decision's quota-pressure step (off/0/false/no).
    "LLM_ROUTER_PROXY_QUOTA_PRESSURE": ("llm_router", "proxy/quota_pressure.py", 1),
    # Escalation signals (proxy/escalation.py): the correction-signal
    # tool-failure streak, and the long/multi-part first-prompt safety floor.
    "LLM_ROUTER_PROXY_ESCALATION_TOOL_FAIL_N": ("llm_router", "proxy/escalation.py", 1),
    "LLM_ROUTER_PROXY_LONG_PROMPT_WORDS": ("llm_router", "proxy/escalation.py", 1),
    "LLM_ROUTER_PROXY_LONG_PROMPT_PARTS": ("llm_router", "proxy/escalation.py", 1),
    "LLM_ROUTER_LOCAL_AGENT": ("llm_router", "local_agent/__init__.py", 1),
    "LLM_ROUTER_LOCAL_AGENT_TOP_K": ("llm_router", "local_agent/__init__.py", 1),
    "LLM_ROUTER_LOCAL_AGENT_PROMPT_BUDGET": ("llm_router", "local_agent/__init__.py", 1),
    "LLM_ROUTER_LOCAL_AGENT_KEEP_RESULTS": ("llm_router", "local_agent/__init__.py", 1),
    "LLM_ROUTER_LOCAL_AGENT_TOOL_DESC_CHARS": ("llm_router", "local_agent/__init__.py", 1),
    "LLM_ROUTER_LOCAL_AGENT_EMBED_MODEL": ("llm_router", "local_agent/__init__.py", 1),
    "LLM_ROUTER_LOCAL_AGENT_EDIT": ("llm_router", "local_agent/__init__.py", 1),
    "LLM_ROUTER_GEMINI_BASELINE": ("llm_router", "cost.py", 1),
    "LLM_ROUTER_GEMINI_SUBSCRIPTION": ("llm_router", "commands/demo.py", 3),
    "LLM_ROUTER_GEMINI_TIMEOUT": ("llm_router", "gemini_cli_agent.py", 1),
    "LLM_ROUTER_HEALTH_SNAPSHOT": ("llm_router", "health.py", 1),
    "LLM_ROUTER_PROVIDER_RESET_PATH": ("llm_router", "provider_reset.py", 1),
    "LLM_ROUTER_HISTORY_RELAY": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_HOOK_SLOW_SECONDS": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_HTTP_TIMEOUT": ("llm_router", "hooks/session-end.py", 2),
    "LLM_ROUTER_IDEMPOTENCY_PATH": ("llm_router", "idempotency.py", 1),
    "LLM_ROUTER_IDENTITY_PATH": ("llm_router", "enterprise/identity.py", 1),
    "LLM_ROUTER_INDICATOR": ("llm_router", "surface_status.py", 1),
    "LLM_ROUTER_INVOICE_DISCREPANCY_PCT": ("llm_router", "invoice_reconciliation/__init__.py", 1),
    "LLM_ROUTER_JUDGE_AUTODRAIN": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_JUDGE_CASCADE_SAMPLE_RATE": ("llm_router", "judge_cascade.py", 1),
    "LLM_ROUTER_JUDGE_CASCADE_THRESHOLD": ("llm_router", "judge_cascade.py", 1),
    "LLM_ROUTER_JUDGE_MODEL": ("llm_router", "judge_cascade.py", 1),
    "LLM_ROUTER_JUDGE_QUEUE_MAX_ENTRIES": ("llm_router", "judge.py", 1),
    "LLM_ROUTER_JUDGE_SAMPLE_RATE": ("llm_router", "judge.py", 1),
    "LLM_ROUTER_BREAKER_LOCK_WAIT_S": ("llm_router", "hooks/agent-route.py", 3),
    "LLM_ROUTER_LIBRARIAN_MODEL": ("llm_router", "library/sealer.py", 1),
    "LLM_ROUTER_LOG_JSON": ("llm_router", "logging.py", 1),
    "LLM_ROUTER_LOG_LEVEL": ("llm_router", "logging.py", 1),
    "LLM_ROUTER_MAX_AGENT_DEPTH": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_MAX_CONCURRENT_AGENTS": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_METRICS_INCLUDE_PRESSURE": ("llm_router", "admin_api.py", 1),
    "LLM_ROUTER_MINI_SUMMARY_EVERY": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_OIDC_AUDIENCE": ("llm_router", "enterprise/oidc.py", 1),
    "LLM_ROUTER_OIDC_DEFAULT_ORG": ("llm_router", "server.py", 2),
    "LLM_ROUTER_OIDC_DEFAULT_TEAM": ("llm_router", "server.py", 2),
    "LLM_ROUTER_OIDC_EMAIL_CLAIM": ("llm_router", "enterprise/oidc.py", 1),
    "LLM_ROUTER_OIDC_GROUPS_CLAIM": ("llm_router", "enterprise/oidc.py", 1),
    "LLM_ROUTER_OIDC_ISSUER": ("llm_router", "enterprise/oidc.py", 1),
    "LLM_ROUTER_OIDC_JWKS_URI": ("llm_router", "enterprise/oidc.py", 1),
    "LLM_ROUTER_OIDC_ROLE_MAP": ("llm_router", "enterprise/oidc.py", 1),
    "LLM_ROUTER_OKF": ("llm_router", "okf.py", 1),
    "LLM_ROUTER_OKF_MIN_SCORE": ("llm_router", "okf.py", 1),
    "LLM_ROUTER_OLLAMA_MODEL": ("llm_router", "model_discovery.py", 1),
    "LLM_ROUTER_OLLAMA_NUM_CTX": ("llm_router", "providers.py", 1),
    "LLM_ROUTER_OLLAMA_THINK": ("llm_router", "providers.py", 1),
    "LLM_ROUTER_OLLAMA_TIMEOUT": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_OLLAMA_URL": ("llm_router", "hooks/agent_loop.py", 3),
    "LLM_ROUTER_OLLAMA_WARMUP": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_OLLAMA_WATCHDOG": ("llm_router", "ollama_watchdog.py", 1),
    "LLM_ROUTER_OLLAMA_WATCHDOG_FAIL_N": ("llm_router", "ollama_watchdog.py", 1),
    "LLM_ROUTER_OLLAMA_WATCHDOG_MIN_INTERVAL_S": ("llm_router", "ollama_watchdog.py", 1),
    "LLM_ROUTER_OLLAMA_WATCHDOG_RESTART": ("llm_router", "ollama_watchdog.py", 1),
    "LLM_ROUTER_OLLAMA_WATCHDOG_TIMEOUT_S": ("llm_router", "ollama_watchdog.py", 1),
    "LLM_ROUTER_OLLAMA_WARMUP_MODEL": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_PLAYWRIGHT_COMPRESS": ("llm_router", "hooks/playwright-compress.py", 1),
    "LLM_ROUTER_POLICY": ("llm_router", "cli_init_policy.py", 2),
    "LLM_ROUTER_POLICY_PATH": ("llm_router", "control_plane/migration.py", 1),
    "LLM_ROUTER_PREMIUM_MAX_PRESSURE": ("llm_router", "router.py", 1),
    "LLM_ROUTER_PRESET": ("llm_router", "presets.py", 1),
    "LLM_ROUTER_PROFILE": ("llm_router", "repo_config.py", 2),
    "LLM_ROUTER_PROJECT_DIR": ("llm_router", "semantic/scope.py", 1),
    "LLM_ROUTER_PROVIDER_REGISTRY_PATH": ("llm_router", "provider_registry.py", 1),
    "LLM_ROUTER_PXPIPE_ENABLED": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_PXPIPE_HEAVY_MODELS": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_PXPIPE_URL": ("llm_router", "hooks/session-start.py", 1),
    # NS4 quality breaker — a class (lever, task_type[, model]) whose routed
    # answers keep failing (northstar.units() redo/discarded) is un-routed
    # automatically. See quality_breaker.py for the state machine.
    "LLM_ROUTER_QUALITY_BREAKER_COOLDOWN_S": ("llm_router", "quality_breaker.py", 1),
    "LLM_ROUTER_QUALITY_BREAKER_LOOKBACK_DAYS": ("llm_router", "quality_breaker.py", 1),
    "LLM_ROUTER_QUALITY_BREAKER_MIN_N": ("llm_router", "quality_breaker.py", 1),
    "LLM_ROUTER_QUALITY_BREAKER_PATH": ("llm_router", "quality_breaker.py", 1),
    "LLM_ROUTER_QUALITY_BREAKER_PROBE_SIZE": ("llm_router", "quality_breaker.py", 1),
    "LLM_ROUTER_QUALITY_BREAKER_THRESHOLD": ("llm_router", "quality_breaker.py", 1),
    "LLM_ROUTER_QUALITY_BREAKER_WINDOW": ("llm_router", "quality_breaker.py", 1),
    "LLM_ROUTER_QUALITY_MIN_CALLS": ("llm_router", "quality_feedback.py", 1),
    "LLM_ROUTER_QUALITY_SKIP": ("llm_router", "quality_feedback.py", 1),
    "LLM_ROUTER_QUALITY_SKIP_THRESHOLD": ("llm_router", "quality_feedback.py", 1),
    "LLM_ROUTER_QUOTAS_PATH": ("llm_router", "enterprise/quotas.py", 1),
    "LLM_ROUTER_QUOTA_DELAY": ("llm_router", "quota_tracker.py", 1),
    "LLM_ROUTER_QUOTA_RETRY": ("llm_router", "quota_tracker.py", 1),
    "LLM_ROUTER_QUOTA_MAX_AGE": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_QUOTA_TTL": ("llm_router", "hooks/auto-route.py", 2),
    # Verifier PR C (SHADOW): kill switch for patch capture + worker spawn, and the per-unit
    # verify budget (default 120 s, capped at 300 s).
    "LLM_ROUTER_VERIFY": ("llm_router", "verify_queue.py", 1),
    "LLM_ROUTER_VERIFY_BUDGET_S": ("llm_router", "verify_worker.py", 1),
    "LLM_ROUTER_RENDER_MODE": ("llm_router", "hooks/response_formatter.py", 1),
    "LLM_ROUTER_RESPONSE_ROUTER": ("llm_router", "commands/doctor.py", 3),
    "LLM_ROUTER_ROUTE_BANNER": ("llm_router", "hooks/agent-route.py", 2),
    "LLM_ROUTER_ROUTING_LEDGER": ("llm_router", "routing_quality.py", 1),
    "LLM_ROUTER_SECRETS_BACKEND": ("llm_router", "secrets_vault.py", 1),
    "LLM_ROUTER_SEMANTIC_ARM": ("llm_router", "semantic/modes.py", 2),
    "LLM_ROUTER_SEMANTIC_AUTOINDEX": ("llm_router", "semantic/autoindex.py", 1),
    "LLM_ROUTER_SEMANTIC_AUTOINDEX_COOLDOWN_S": ("llm_router", "semantic/autoindex.py", 1),
    "LLM_ROUTER_SEMANTIC_AUTOINDEX_MAX_FILES": ("llm_router", "semantic/autoindex.py", 1),
    "LLM_ROUTER_SEMANTIC_AUTOINDEX_SCRATCH_PREFIXES": ("llm_router", "semantic/autoindex.py", 1),
    "LLM_ROUTER_SEMANTIC_CACHE": ("llm_router", "semantic_cache.py", 2),
    "LLM_ROUTER_SEMANTIC_CACHE_THRESHOLD": ("llm_router", "semantic_cache.py", 1),
    "LLM_ROUTER_SEMANTIC_CENTROIDS": ("llm_router", "semantic_classify.py", 1),
    "LLM_ROUTER_SEMANTIC_CLASSIFIER_BACKEND": ("llm_router", "semantic_classify.py", 1),
    "LLM_ROUTER_SEMANTIC_HISTORY": ("llm_router", "semantic/modes.py", 1),
    "LLM_ROUTER_SEMANTIC_INTERVENTION": ("llm_router", "semantic/modes.py", 1),
    "LLM_ROUTER_SEMANTIC_SOURCE": ("llm_router", "semantic/modes.py", 1),
    "LLM_ROUTER_SEMANTIC_ST_MODEL": ("llm_router", "semantic_classify.py", 1),
    "LLM_ROUTER_SERVICE_PORT": ("llm_router", "hook_client.py", 3),
    "LLM_ROUTER_SESSIONS_PATH": ("llm_router", "agents/session.py", 1),
    "LLM_ROUTER_SESSION_BUDGET": ("llm_router", "hooks/enforce-route.py", 1),
    "LLM_ROUTER_SESSION_CONTEXT": ("llm_router", "session_store.py", 1),
    "LLM_ROUTER_SESSION_RESCUE": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_SESSION_CONTEXT_DRAFT_BUDGET": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_SESSION_ID": ("llm_router", "hooks/auto-route.py", 2),
    "LLM_ROUTER_SESSION_KIND": ("llm_router", "session_kind.py", 1),
    "LLM_ROUTER_KPI_BENCHMARK_PATH": ("llm_router", "commands/kpi.py", 1),
    "LLM_ROUTER_HOOK_LATENCY": ("llm_router", "hook_latency.py", 1),
    "LLM_ROUTER_HOOK_LATENCY_MAX_BYTES": ("llm_router", "hook_latency.py", 1),
    "LLM_ROUTER_PI_BIN": ("llm_router", "commands/pi.py", 1),
    "LLM_ROUTER_PI_CONTEXT": ("llm_router", "commands/pi.py", 1),
    "LLM_ROUTER_PI_MODEL": ("llm_router", "commands/pi.py", 1),
    "LLM_ROUTER_PI_PROFILE_DIR": ("llm_router", "commands/pi.py", 1),
    "LLM_ROUTER_SESSION_PAID_CAP": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_SESSION_START_USAGE_COOLDOWN_S": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_SESSION_START_USAGE_FRESH_S": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_SIDECAR_PREFETCH": ("llm_router", "commands/doctor.py", 2),
    "LLM_ROUTER_SLIM": ("llm_router", "tool_surface.py", 1),
    "LLM_ROUTER_STALE_PRESSURE_FLOOR": ("llm_router", "budget.py", 1),
    "LLM_ROUTER_STATE_DIR": ("llm_router", "surface_status.py", 1),
    # Read by the shell script, not by Python: the AST scan cannot see it, so it
    # is listed in tests/test_env_registry.py's _INDIRECT_READS. "fast" = the
    # debug fast line only; "both" = the full line then the fast line on a second
    # row; anything else (the default, "full") = the full status line only.
    "LLM_ROUTER_STATUSLINE": ("llm_router", "hooks/statusline-command.sh", 1),
    "LLM_ROUTER_STATUSLINE_TIMING": ("llm_router", "hooks/statusline-command.sh", 1),
    "LLM_ROUTER_STATUSLINE_REFRESH_CMD": ("llm_router", "statusline_tick.py", 1),
    "LLM_ROUTER_STATUSLINE_SLOW_MS": ("llm_router", "statusline_refresh.py", 1),
    "LLM_ROUTER_STATUS_EVERY": ("llm_router", "hooks/status-bar-clawcode.py", 2),
    "LLM_ROUTER_STATUS_MODE": ("llm_router", "hooks/status-bar.py", 1),
    "LLM_ROUTER_STREAMING_JUDGE": ("llm_router", "streaming_judge.py", 1),
    "LLM_ROUTER_SUBAGENT_CLI_DELEGATION": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_SUBAGENT_CLI_TIMEOUT": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_SUBAGENT_DIRECT": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_SUBAGENT_DIRECT_MAX_COMPLEXITY": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_SUBAGENT_GOVERNANCE": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_SUBAGENT_MODEL_PIN": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_STOP_HOOK": ("llm_router", "hooks/codex-stop.py", 2),
    "LLM_ROUTER_SUBPROCESS_TIMEOUT": ("llm_router", "hooks/session-end.py", 2),
    "LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH": ("llm_router", "quota_savings.py", 1),
    "LLM_ROUTER_SUPPRESS_PRICING_STALENESS": ("llm_router", "pricing.py", 1),
    "LLM_ROUTER_URL": ("llm_router", "commands/doctor.py", 1),
    "LLM_ROUTER_USAGE_DB_PATH": ("llm_router", "quota_savings.py", 1),
    "LLM_ROUTER_USAGE_TTL_SEC": ("llm_router", "statusline_tick.py", 1),
    "LLM_ROUTER_USAGE_PATH": ("llm_router", "commands/invoice.py", 1),
    "LLM_ROUTER_WEEKLY_QUOTA_USD": ("llm_router", "quota_savings.py", 1),
    "LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV": ("llm_router", "quota_savings.py", 1),
    "LLM_ROUTER_ZERO_CLAUDE": ("llm_router", "hooks/auto-route.py", 2),
    "LLM_ROUTER_ZERO_CLAUDE_SCOPE": ("llm_router", "zero_claude_edit.py", 1),
    # Plan 3.3: gate for the pre-write lint check (ruff) on scoped zero-Claude
    # edits. Defaults ON — see local_agent/verify.py's _DEFAULT_ENABLED.
    "LLM_ROUTER_ZERO_CLAUDE_VERIFY": ("llm_router", "local_agent/verify.py", 1),
    # Plan 3.7 (warm.py): keep the zero-Claude edit model resident, fail fast cold.
    "LLM_ROUTER_LOCAL_KEEP_ALIVE": ("llm_router", "warm.py", 1),
    # P2 local-usage plan: shadow-only would-be "local" tier (log only, never routes).
    "LLM_ROUTER_LOCAL_TIER": ("llm_router", "local_tier.py", 1),
    # P1.6 engine core: decide() cache mode sqlite|memory|off (default sqlite), and the L3 / calibration
    # artifact path overrides (default $LLM_ROUTER_HOME/engine_lexical.json, engine_calibration.json).
    "LLM_ROUTER_DECIDE_CACHE": ("llm_router", "engine.py", 1),
    "LLM_ROUTER_ENGINE_LEXICAL": ("llm_router", "engine.py", 1),
    "LLM_ROUTER_ENGINE_CALIBRATION": ("llm_router", "engine.py", 1),
    # LOCAL-TIMEOUT-1: seconds a local model that just timed out stays at the back of the chain (0 = off).
    "LLM_ROUTER_LOCAL_TIMEOUT_COOLDOWN_S": ("llm_router", "router.py", 1),
    # Local Ollama classifier (one v6 verdict per human turn): off|shadow|on, default off.
    "LLM_ROUTER_LOCAL_CLASSIFIER": ("llm_router", "local_classifier.py", 1),
    "LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS": ("llm_router", "local_classifier.py", 1),
    # The classifier's own Ollama alias (M1.4): other callers load qwen3.5 at a different
    # num_ctx, and a shared name would reload the runner back and forth.
    "LLM_ROUTER_CLASSIFIER_MODEL": ("llm_router", "local_classifier.py", 1),
    "LLM_ROUTER_CLASSIFIER_KEEP_ALIVE": ("llm_router", "local_classifier.py", 1),
    # M1.8 round 3: decision-model backend (Ollama /v1/systemone, nimble). Default OFF
    # (backend "chat" = the v6 /api/chat classifier); see decision_classifier.py.
    "LLM_ROUTER_CLASSIFIER_BACKEND": ("llm_router", "local_classifier.py", 1),
    "LLM_ROUTER_DECISION_MODEL": ("llm_router", "decision_classifier.py", 1),
    "LLM_ROUTER_DECISION_ABSTAIN_BELOW": ("llm_router", "decision_classifier.py", 1),
    # P1.7: truth labels (JSONL text_sha/session_id/truth) for kpi's "classifier shadow vs rules" line.
    "LLM_ROUTER_SHADOW_LABELS": ("llm_router", "commands/kpi.py", 1),
    # P1.7-d: opt-in sample of shadow turns whose TEXT is kept in shadow_text.jsonl (0600) for the labeller.
    "LLM_ROUTER_SHADOW_TEXT_SAMPLE": ("llm_router", "proxy/shadow_text.py", 1),
    "LLM_ROUTER_ZCE_COLD_BUDGET_S": ("llm_router", "warm.py", 1),
    "LLM_ROUTER_ZCE_WARMUP": ("llm_router", "warm.py", 1),
    # ── indirect reads: DECLARED BY HAND, invisible to the AST scan ──
    # These are read through a VARIABLE, not a string literal:
    #     for env in (ALLOW_PUBLIC_ENV, LEGACY_SSE_ALLOW_PUBLIC_ENV):
    #         os.environ.get(env)
    # The scanner matches os.environ.get("LITERAL") only, so it cannot see them —
    # and neither can any similar tool. Stated here rather than quietly omitted,
    # because a registry that silently under-reports its own surface is the same
    # class of defect as the guards this audit keeps finding: it looks complete.
    "LLM_ROUTER_ALLOW_PUBLIC_BIND": ("llm_router", "net_bind.py", 1),
    "LLM_ROUTER_SSE_ALLOW_PUBLIC": ("llm_router", "net_bind.py", 1),

    # ── provider_credential  (20) ──
    "ANTHROPIC_ADMIN_KEY": ("provider_credential", "invoice_reconciliation/anthropic.py", 1),
    "LLM_ROUTER_CP_ED25519_PRIVATE_KEY": ("provider_credential", "control_plane/signing.py", 1),
    "LLM_ROUTER_CP_SIDECAR_TOKEN": ("provider_credential", "control_plane/api.py", 1),
    "LLM_ROUTER_ESCALATE_MIN_PROMPT_TOKENS": ("provider_credential", "router.py", 1),
    "LLM_ROUTER_HF_TOKENIZERS": ("provider_credential", "token_budget.py", 1),
    "LLM_ROUTER_PROJECT_ID": ("provider_credential", "session_store.py", 1),
    "LLM_ROUTER_PROJECT_ALLOWLIST": ("llm_router", "gateway.py", 1),
    "LLM_ROUTER_PROJECT_ROOT": ("llm_router", "semantic/scope.py", 1),
    "LLM_ROUTER_RESPONSE_ROUTER_TOKEN_THRESHOLD": ("provider_credential", "response_router.py", 1),
    "LLM_ROUTER_SCIM_TOKEN": ("provider_credential", "admin_api.py", 2),
    "LLM_ROUTER_SEATS_AUTO": ("llm_router", "subscription_local_routing.py", 1),
    "LLM_ROUTER_SUBSCRIPTION_PROVIDER": ("llm_router", "subscription_local_routing.py", 2),
    "LLM_ROUTER_TOKEN": ("provider_credential", "identity.py", 1),
    "DEEPSEEK_API_KEY": ("provider_credential", "commands/doctor.py", 1),
    "GEMINI_ACCESS_TOKEN": ("provider_credential", "invoice_reconciliation/gemini.py", 1),
    "GEMINI_API_KEY": ("provider_credential", "commands/demo.py", 7),
    "GEMINI_PROJECT_ID": ("provider_credential", "invoice_reconciliation/gemini.py", 1),
    "GOOGLE_API_KEY": ("provider_credential", "commands/demo.py", 3),
    "HELICONE_API_KEY": ("provider_credential", "integrations/helicone.py", 1),
    "OPENAI_ADMIN_KEY": ("provider_credential", "invoice_reconciliation/openai.py", 1),
    "OPENAI_API_KEY": ("provider_credential", "commands/demo.py", 7),
    "OPENROUTER_API_KEY": ("provider_credential", "commands/doctor.py", 1),
    "PERPLEXITY_API_KEY": ("provider_credential", "commands/demo.py", 1),
    "VAULT_TOKEN": ("provider_credential", "org_policy.py", 1),
    # ── external_tool  (24) ──
    "CLAUDE_CODE_PATH": ("external_tool", "claude_agent.py", 1),
    # P0.13: the project root llm_act may write in, when the MCP client sends no roots.
    "CLAUDE_PROJECT_DIR": ("external_tool", "tools/agentic.py", 1),
    "CLAUDE_CODE_SESSION_ID": ("external_tool", "hooks/agent-depth-release.py", 3),
    # P0.14-d: which CLI ran a hook (hook_latency.detect_host). Each host exports its own
    # plugin root to plugin hooks; CLAUDECODE=1 is set by Claude Code for its children.
    "CLAUDE_PLUGIN_ROOT": ("external_tool", "hook_latency.py", 1),
    "CODEX_PLUGIN_ROOT": ("external_tool", "hook_latency.py", 1),
    "CLAUDECODE": ("external_tool", "hook_latency.py", 1),
    # 2026-09-28: Claude Code's own headless-vs-interactive signal ("cli" for an
    # interactive session, "sdk-cli"/"sdk-py" for `-p`/SDK callers). Verified
    # empirically against a real `claude -p ... --output-format json` run
    # (see hooks/agent-route.py's headless-guard note) — not read from the
    # PreToolUse hook payload itself, which carries no entrypoint field.
    "CLAUDE_CODE_ENTRYPOINT": ("external_tool", "hooks/agent-route.py", 1),
    "CLAUDE_SESSION_ID": ("external_tool", "hooks/context-capture.py", 6),
    "CODEX_PATH": ("external_tool", "codex_agent.py", 1),
    "GEMINI_CLI_PATH": ("external_tool", "gemini_cli_agent.py", 1),
    "GEMINI_CLI_TIER": ("external_tool", "gemini_cli_quota.py", 1),
    "HOST": ("external_tool", "server.py", 1),
    "LOCALAPPDATA": ("external_tool", "install_hooks.py", 1),
    "OLLAMA_BASE_URL": ("external_tool", "agentic/react.py", 8),
    "OLLAMA_BUDGET_MODELS": ("external_tool", "model_discovery.py", 1),
    # Ollama server's own tuning knob (operator-set, not ours to default):
    # the window `local_context_guard.effective_window` falls back to when no
    # explicit `num_ctx` and no `/api/ps` reading are available.
    "OLLAMA_CONTEXT_LENGTH": ("external_tool", "local_context_guard.py", 1),
    "OLLAMA_MODELS": ("external_tool", "model_discovery.py", 1),
    "OLLAMA_HOST": ("external_tool", "hooks/playwright-compress.py", 1),
    "OLLAMA_URL": ("external_tool", "commands/doctor.py", 2),
    "OTEL_EXPORTER_OTLP_ENDPOINT": ("external_tool", "observability.py", 2),
    "OTEL_EXPORTER_OTLP_INSECURE": ("external_tool", "tracing.py", 1),
    "OTEL_SERVICE_NAME": ("external_tool", "observability.py", 2),
    "PORT": ("external_tool", "server.py", 1),
    "RESPONSE": ("external_tool", "hooks/response-router.py", 1),
    "VAULT_ADDR": ("external_tool", "org_policy.py", 1),
    "ANTHROPIC_BASE_URL": ("external_tool", "hooks/session-start.py", 1),
    "_SESSION_BUDGET_WARNING": ("external_tool", "hooks/enforce-route.py", 1),
    # ── platform  (4) ──
    "APPDATA": ("platform", "commands/doctor.py", 4),
    "NO_COLOR": ("platform", "commands/budget.py", 16),
    "PATH": ("platform", "statusline_tick.py", 1),
    "XDG_CONFIG_HOME": ("platform", "install_hooks.py", 1),
    # ── test_only  (1) ──
    "PYTEST_CURRENT_TEST": ("test_only", "config.py", 4),
}


def registered_names() -> frozenset[str]:
    return frozenset(ENV_REGISTRY)


def category_of(name: str) -> str | None:
    entry = ENV_REGISTRY.get(name)
    return entry[0] if entry else None
