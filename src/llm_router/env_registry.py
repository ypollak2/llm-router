"""Central registry of every environment variable this codebase reads.

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
    # ── llm_router  (152) ──
    "LLM_ROUTER_ADMIN_ACTIONS_PATH": ("llm_router", "admin_actions.py", 1),
    "LLM_ROUTER_AGENTIC_MODEL": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_AGENTS_CONFIG": ("llm_router", "tools/agents.py", 1),
    "LLM_ROUTER_AGENT_POLICY_MODE": ("llm_router", "router.py", 1),
    "LLM_ROUTER_AGENT_ROUTE_ALLOW": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_ALERT_WEBHOOK": ("llm_router", "alerts.py", 1),
    "LLM_ROUTER_ALLOWED_HOSTS": ("llm_router", "route_server.py", 1),
    "LLM_ROUTER_ALLOW_STUBS": ("llm_router", "cost.py", 2),
    "LLM_ROUTER_ALLOW_SUBAGENTS": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_ANOMALY_THRESHOLD": ("llm_router", "session_spend.py", 1),
    "LLM_ROUTER_AUDIT_DISABLED": ("llm_router", "misroute_audit.py", 1),
    "LLM_ROUTER_AUDIT_PATH": ("llm_router", "enterprise/audit.py", 1),
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
    "LLM_ROUTER_COMPRESS_RESPONSE": ("llm_router", "tools/text.py", 1),
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
    "LLM_ROUTER_DISABLE_CONTINUATION_BYPASS": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": ("llm_router", "hooks/auto-route.py", 1),
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
    "LLM_ROUTER_GATEWAY_HOST": ("llm_router", "presets.py", 1),
    "LLM_ROUTER_GATEWAY_PORT": ("llm_router", "presets.py", 1),
    "LLM_ROUTER_GATEWAY_URL": ("llm_router", "presets.py", 1),
    "LLM_ROUTER_GEMINI_BASELINE": ("llm_router", "cost.py", 1),
    "LLM_ROUTER_GEMINI_SUBSCRIPTION": ("llm_router", "commands/demo.py", 3),
    "LLM_ROUTER_GEMINI_TIMEOUT": ("llm_router", "gemini_cli_agent.py", 1),
    "LLM_ROUTER_HEALTH_SNAPSHOT": ("llm_router", "health.py", 1),
    "LLM_ROUTER_HISTORY_RELAY": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_HOOK_SLOW_SECONDS": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_HTTP_TIMEOUT": ("llm_router", "hooks/session-end.py", 2),
    "LLM_ROUTER_IDEMPOTENCY_PATH": ("llm_router", "idempotency.py", 1),
    "LLM_ROUTER_IDENTITY_PATH": ("llm_router", "enterprise/identity.py", 1),
    "LLM_ROUTER_INDICATOR": ("llm_router", "surface_status.py", 1),
    "LLM_ROUTER_INVOICE_DISCREPANCY_PCT": ("llm_router", "invoice_reconciliation/__init__.py", 1),
    "LLM_ROUTER_JUDGE_CASCADE_SAMPLE_RATE": ("llm_router", "judge_cascade.py", 1),
    "LLM_ROUTER_JUDGE_CASCADE_THRESHOLD": ("llm_router", "judge_cascade.py", 1),
    "LLM_ROUTER_JUDGE_MODEL": ("llm_router", "judge_cascade.py", 1),
    "LLM_ROUTER_JUDGE_SAMPLE_RATE": ("llm_router", "judge.py", 1),
    "LLM_ROUTER_LIBRARIAN_MODEL": ("llm_router", "library/sealer.py", 1),
    "LLM_ROUTER_LOG_JSON": ("llm_router", "logging.py", 1),
    "LLM_ROUTER_LOG_LEVEL": ("llm_router", "logging.py", 1),
    "LLM_ROUTER_MAX_AGENT_DEPTH": ("llm_router", "hooks/agent-route.py", 1),
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
    "LLM_ROUTER_OLLAMA_MODEL": ("llm_router", "hooks/auto-route.py", 3),
    "LLM_ROUTER_OLLAMA_NUM_CTX": ("llm_router", "providers.py", 1),
    "LLM_ROUTER_OLLAMA_TIMEOUT": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_OLLAMA_URL": ("llm_router", "hooks/agent_loop.py", 3),
    "LLM_ROUTER_OLLAMA_WARMUP": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_OLLAMA_WARMUP_MODEL": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_PLAYWRIGHT_COMPRESS": ("llm_router", "hooks/playwright-compress.py", 1),
    "LLM_ROUTER_POLICY": ("llm_router", "cli_init_policy.py", 2),
    "LLM_ROUTER_POLICY_PATH": ("llm_router", "control_plane/migration.py", 1),
    "LLM_ROUTER_POLICY_STORE_PATH": ("llm_router", "policy_versions.py", 1),
    "LLM_ROUTER_PREMIUM_MAX_PRESSURE": ("llm_router", "router.py", 1),
    "LLM_ROUTER_PRESET": ("llm_router", "presets.py", 1),
    "LLM_ROUTER_PROFILE": ("llm_router", "repo_config.py", 2),
    "LLM_ROUTER_PROJECT_DIR": ("llm_router", "semantic_cache.py", 1),
    "LLM_ROUTER_PROVIDER_REGISTRY_PATH": ("llm_router", "provider_registry.py", 1),
    "LLM_ROUTER_PXPIPE_ENABLED": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_PXPIPE_HEAVY_MODELS": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_PXPIPE_URL": ("llm_router", "hooks/session-start.py", 1),
    "LLM_ROUTER_QUALITY_MIN_CALLS": ("llm_router", "quality_feedback.py", 1),
    "LLM_ROUTER_QUALITY_SKIP": ("llm_router", "quality_feedback.py", 1),
    "LLM_ROUTER_QUALITY_SKIP_THRESHOLD": ("llm_router", "quality_feedback.py", 1),
    "LLM_ROUTER_QUOTAS_PATH": ("llm_router", "enterprise/quotas.py", 1),
    "LLM_ROUTER_QUOTA_DELAY": ("llm_router", "quota_tracker.py", 1),
    "LLM_ROUTER_QUOTA_RETRY": ("llm_router", "quota_tracker.py", 1),
    "LLM_ROUTER_QUOTA_TTL": ("llm_router", "hooks/auto-route.py", 2),
    "LLM_ROUTER_RENDER_MODE": ("llm_router", "hooks/response_formatter.py", 1),
    "LLM_ROUTER_RESPONSE_ROUTER": ("llm_router", "commands/doctor.py", 3),
    "LLM_ROUTER_ROUTE_BANNER": ("llm_router", "hooks/agent-route.py", 2),
    "LLM_ROUTER_ROUTING_LEDGER": ("llm_router", "routing_quality.py", 1),
    "LLM_ROUTER_SECRETS_BACKEND": ("llm_router", "secrets_vault.py", 1),
    "LLM_ROUTER_SEMANTIC_CACHE_THRESHOLD": ("llm_router", "semantic_cache.py", 1),
    "LLM_ROUTER_SEMANTIC_CENTROIDS": ("llm_router", "semantic_classify.py", 1),
    "LLM_ROUTER_SEMANTIC_CLASSIFIER_BACKEND": ("llm_router", "semantic_classify.py", 1),
    "LLM_ROUTER_SEMANTIC_ST_MODEL": ("llm_router", "semantic_classify.py", 1),
    "LLM_ROUTER_SERVICE_PORT": ("llm_router", "hook_client.py", 3),
    "LLM_ROUTER_SESSIONS_PATH": ("llm_router", "agents/session.py", 1),
    "LLM_ROUTER_SESSION_BUDGET": ("llm_router", "hooks/enforce-route.py", 1),
    "LLM_ROUTER_SESSION_CONTEXT": ("llm_router", "session_store.py", 1),
    "LLM_ROUTER_SESSION_ID": ("llm_router", "hooks/auto-route.py", 2),
    "LLM_ROUTER_SESSION_PAID_CAP": ("llm_router", "hooks/auto-route.py", 1),
    "LLM_ROUTER_SIDECAR_PREFETCH": ("llm_router", "commands/doctor.py", 2),
    "LLM_ROUTER_SLIM": ("llm_router", "tool_surface.py", 1),
    "LLM_ROUTER_STALE_PRESSURE_FLOOR": ("llm_router", "budget.py", 1),
    "LLM_ROUTER_STATE_DIR": ("llm_router", "surface_status.py", 1),
    "LLM_ROUTER_STATUS_EVERY": ("llm_router", "hooks/status-bar-clawcode.py", 2),
    "LLM_ROUTER_STATUS_MODE": ("llm_router", "hooks/status-bar.py", 1),
    "LLM_ROUTER_STREAMING_JUDGE": ("llm_router", "streaming_judge.py", 1),
    "LLM_ROUTER_SUBAGENT_CLI_DELEGATION": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_SUBAGENT_CLI_TIMEOUT": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_SUBAGENT_DIRECT": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_SUBAGENT_DIRECT_MAX_COMPLEXITY": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_SUBAGENT_GOVERNANCE": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_SUBAGENT_MODEL_PIN": ("llm_router", "hooks/agent-route.py", 1),
    "LLM_ROUTER_SUBPROCESS_TIMEOUT": ("llm_router", "hooks/session-end.py", 2),
    "LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH": ("llm_router", "quota_savings.py", 1),
    "LLM_ROUTER_SUPPRESS_PRICING_STALENESS": ("llm_router", "pricing.py", 1),
    "LLM_ROUTER_URL": ("llm_router", "commands/doctor.py", 1),
    "LLM_ROUTER_USAGE_DB_PATH": ("llm_router", "quota_savings.py", 1),
    "LLM_ROUTER_USAGE_PATH": ("llm_router", "commands/invoice.py", 1),
    "LLM_ROUTER_WEEKLY_QUOTA_USD": ("llm_router", "quota_savings.py", 1),
    "LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV": ("llm_router", "quota_savings.py", 1),
    "LLM_ROUTER_ZERO_CLAUDE": ("llm_router", "hooks/auto-route.py", 2),
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
    # ── external_tool  (19) ──
    "CLAUDE_CODE_PATH": ("external_tool", "claude_agent.py", 1),
    "CLAUDE_CODE_SESSION_ID": ("external_tool", "hooks/agent-depth-release.py", 3),
    "CLAUDE_SESSION_ID": ("external_tool", "hooks/context-capture.py", 6),
    "CODEX_PATH": ("external_tool", "codex_agent.py", 1),
    "GEMINI_CLI_PATH": ("external_tool", "gemini_cli_agent.py", 1),
    "GEMINI_CLI_TIER": ("external_tool", "gemini_cli_quota.py", 1),
    "HOST": ("external_tool", "server.py", 1),
    "LOCALAPPDATA": ("external_tool", "install_hooks.py", 1),
    "OLLAMA_BASE_URL": ("external_tool", "agentic/react.py", 8),
    "OLLAMA_BUDGET_MODELS": ("external_tool", "hooks/chain_builder.py", 1),
    "OLLAMA_HOST": ("external_tool", "hooks/playwright-compress.py", 1),
    "OLLAMA_URL": ("external_tool", "commands/doctor.py", 2),
    "OTEL_EXPORTER_OTLP_ENDPOINT": ("external_tool", "observability.py", 2),
    "OTEL_EXPORTER_OTLP_INSECURE": ("external_tool", "tracing.py", 1),
    "OTEL_SERVICE_NAME": ("external_tool", "observability.py", 2),
    "PORT": ("external_tool", "server.py", 1),
    "RESPONSE": ("external_tool", "hooks/response-router.py", 1),
    "VAULT_ADDR": ("external_tool", "org_policy.py", 1),
    "_SESSION_BUDGET_WARNING": ("external_tool", "hooks/enforce-route.py", 1),
    # ── platform  (3) ──
    "APPDATA": ("platform", "commands/doctor.py", 4),
    "NO_COLOR": ("platform", "commands/budget.py", 16),
    "XDG_CONFIG_HOME": ("platform", "install_hooks.py", 1),
    # ── test_only  (1) ──
    "PYTEST_CURRENT_TEST": ("test_only", "config.py", 4),
}


def registered_names() -> frozenset[str]:
    return frozenset(ENV_REGISTRY)


def category_of(name: str) -> str | None:
    entry = ENV_REGISTRY.get(name)
    return entry[0] if entry else None
