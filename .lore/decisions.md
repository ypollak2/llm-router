# Decision Log

| Decision | Affects | Risk | ADR |
|----------|---------|------|-----|
| Introduce Tool Slim Mode, Real-Time Session Spend  | src/llm_router/, tests/, .claude-plugin/ | high | [→](decisions/introduce-tool-slim-mode-real-time-session-spend-meter-and-c.md) |
| Add VS Code and Cursor IDE install support | src/llm_router/cli.py, src/llm_router/ru | medium | [→](decisions/add-vs-code-and-cursor-ide-install-support.md) |
| Introduce multi-agent CLI compatibility and new pl | .claude-plugin/, .codex-plugin/, .factor | high | [→](decisions/introduce-multi-agent-cli-compatibility-and-new-plugin-for-f.md) |
| Agent-context chain reordering for LLM selection | src/llm_router/router.py, src/llm_router | medium | [→](decisions/agent-context-chain-reordering-for-llm-selection.md) |
| Ensure JSONL savings are flushed before cumulative | src/llm_router/hooks/session-end.py, sav | medium | |
| Introduce Codex CLI plugin support and marketplace | .codex-plugin/, .claude-plugin/, CHANGEL | high | [→](decisions/introduce-codex-cli-plugin-support-and-marketplace-integrati.md) |
| Introduce Policy Engine, Savings Digest, and Commu | src/llm_router/policy.py, src/llm_router | high | [→](decisions/introduce-policy-engine-savings-digest-and-community-benchma.md) |
| Add multi-host adapter support for Codex CLI, Clau | docs/multi-host-research.md, src/llm_rou | medium | [→](decisions/add-multi-host-adapter-support-for-codex-cli-claude-desktop-.md) |
| Introduce llm_auto tool for host-agnostic savings  | src/llm_router/tools/admin.py, src/llm_r | medium | [→](decisions/introduce-llm-auto-tool-for-host-agnostic-savings-tracking-a.md) |
| Block Glob/Read/Grep/LS for Q&A tasks in hard enfo | src/llm_router/hooks/enforce-route.py, t | medium | |
| Always inject Ollama models when configured | src/llm_router/config.py, src/llm_router | medium | |
| Introduce Team Dashboard with Multi-Channel Push N | src/llm_router/team.py, src/llm_router/c | high | [→](decisions/introduce-team-dashboard-with-multi-channel-push-notificatio.md) |
| Always inject Ollama into routing chain when confi | src/llm_router/router.py | medium | |
| Enhance savings projection accuracy and persistenc | src/llm_router/cost.py, src/llm_router/h | medium | [→](decisions/enhance-savings-projection-accuracy-and-persistence-add-host.md) |
| Introduce Latency-Aware and Personalized Routing w | src/llm_router/benchmarks.py, src/llm_ro | high | [→](decisions/introduce-latency-aware-and-personalized-routing-with-smart-.md) |
| Context-aware routing for short continuation promp | src/llm_router/hooks/auto-route.py | medium | |
| Introduce an interactive onboarding wizard to conf | src/llm_router/cli.py, src/llm_router/ho | medium | [→](decisions/introduce-an-interactive-onboarding-wizard-to-configure-llm-.md) |
| Add cumulative savings section to session-end hook | src/llm_router/hooks/session-end.py | low | |
| Introduce repository-aware YAML configuration, con | src/llm_router/cli.py, src/llm_router/ho | medium | [→](decisions/introduce-repository-aware-yaml-configuration-config-linting.md) |
| Ensure Claude OAuth usage is always refreshed and  | src/llm_router/hooks/session-end.py, src | medium | [→](decisions/ensure-claude-oauth-usage-is-always-refreshed-and-snapshot-u.md) |
| Introduce MCP-aware routing and enforce-route bloc | src/llm_router/hooks/auto-route.py, src/ | high | [→](decisions/introduce-mcp-aware-routing-and-enforce-route-blocklist-in-a.md) |
