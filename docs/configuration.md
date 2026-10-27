# Configuration

llm-router needs no configuration to start: with no settings it routes using the
`balanced` profile and the `smart` enforcement mode. These are the only settings
documented for users. Everything else the code reads is internal, may change
without notice, and is deliberately not listed here.

The table is generated from `PUBLIC` in `src/llm_router/env_registry.py`
(`python scripts/gen_config_docs.py`; CI runs it with `--check`). Edit the registry,
not the table.

<!-- BEGIN GENERATED -->
| Setting | Type | Default | What it does |
|---|---|---|---|
| `LLM_ROUTER_COST_PROFILE` (alias `LLM_ROUTER_PROFILE`) | enum: budget \| balanced \| premium \| reasoning \| quota_balanced \| subscription_local | balanced | Cost/quality tier that picks the model chain. Unrecognised values fall back to balanced with one warning. |
| `LLM_ROUTER_MONTHLY_BUDGET` | float (USD) | 20.0 | Monthly spend cap in US dollars. |
| `LLM_ROUTER_ENFORCE` | enum: smart \| hard \| advise \| suggest \| soft \| shadow \| off | smart | How strongly the hooks hold tool calls until a prompt is routed; off, shadow, advise, suggest and soft never block. The environment variable wins; otherwise the session file, repo config (.llm_router.yml) or ~/.llm-router/routing.yaml decide. |
| `LLM_ROUTER_HOME` | path | ~/.llm-router | Directory for all llm-router state (usage database, traces, caches). |
| `LLM_ROUTER_BLOCK_PROVIDERS` | comma-separated provider names | empty (nothing blocked) | Providers removed from routing entirely, e.g. codex,gemini_cli,openai,anthropic,ollama. |
| `LLM_ROUTER_LOG_LEVEL` | logging level name | INFO | Log verbosity (DEBUG, INFO, WARNING, ERROR). |
| `LLM_ROUTER_CLAUDE_SUBSCRIPTION` | bool (1 \| true \| yes \| on) | off | Declares a flat-rate Claude subscription so savings are computed against it, not per-token prices. |
| `OLLAMA_BASE_URL` | URL | http://localhost:11434 | Ollama server used for local models. |
| Provider API keys: `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `PERPLEXITY_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`, `GROQ_API_KEY`, `TOGETHER_API_KEY`, `XAI_API_KEY`, `COHERE_API_KEY`, `OPENROUTER_API_KEY`, `FAL_KEY`, `STABILITY_API_KEY`, `ELEVENLABS_API_KEY`, `RUNWAY_API_KEY`, `REPLICATE_API_TOKEN` | secret strings | unset | Provider API keys (text and media providers). Set the ones you have; a provider with no key is skipped. |
<!-- END GENERATED -->

Set a variable in your shell, or in `$LLM_ROUTER_HOME/.env`. `llm-router-onboard`
walks through the provider keys interactively.
