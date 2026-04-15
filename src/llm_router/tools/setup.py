"""API setup and feedback tools — llm_setup, llm_rate."""

from __future__ import annotations

from llm_router import providers
from llm_router.config import get_config
from llm_router.cost import rate_routing_decision


# ── Provider registry ──────────────────────────────────────────────────────────
# The authoritative catalog of all supported providers.
# Each entry stores metadata used by ``llm_setup`` actions: signup URL,
# environment variable name, free-tier availability, capability tags,
# pricing summary, and whether the provider is recommended for new users.
# This dict is read-only at runtime; it drives the status, guide, discover,
# add, test, and provider-detail sub-commands.
_PROVIDER_REGISTRY: dict[str, dict] = {
    "gemini": {
        "name": "Google Gemini",
        "signup_url": "https://aistudio.google.com/apikey",
        "env_var": "GEMINI_API_KEY",
        "free_tier": "Yes — generous free tier (1500 req/day for Flash)",
        "capabilities": ["text", "code", "images (Imagen 3)", "video (Veo 2)"],
        "pricing": "Free tier available, then pay-as-you-go",
        "recommended": True,
    },
    "groq": {
        "name": "Groq",
        "signup_url": "https://console.groq.com/keys",
        "env_var": "GROQ_API_KEY",
        "free_tier": "Yes — fast inference, free tier available",
        "capabilities": ["text", "code"],
        "pricing": "Free tier with rate limits, then pay-as-you-go",
        "recommended": True,
    },
    "openai": {
        "name": "OpenAI",
        "signup_url": "https://platform.openai.com/api-keys",
        "env_var": "OPENAI_API_KEY",
        "free_tier": "No — $5 free credit for new accounts",
        "capabilities": ["text", "code", "images (DALL-E)", "audio (TTS/Whisper)"],
        "pricing": "Pay-as-you-go, starts at ~$0.15/1M tokens (GPT-4o-mini)",
        "recommended": True,
    },
    "deepseek": {
        "name": "DeepSeek",
        "signup_url": "https://platform.deepseek.com/api_keys",
        "env_var": "DEEPSEEK_API_KEY",
        "free_tier": "No — but extremely cheap ($0.14/1M input tokens)",
        "capabilities": ["text", "code", "reasoning"],
        "pricing": "Pay-as-you-go, cheapest for quality ratio",
        "recommended": True,
    },
    "perplexity": {
        "name": "Perplexity",
        "signup_url": "https://www.perplexity.ai/settings/api",
        "env_var": "PERPLEXITY_API_KEY",
        "free_tier": "No — API requires credits",
        "capabilities": ["search-augmented research"],
        "pricing": "Pay-as-you-go, ~$1/1000 searches (Sonar)",
        "recommended": False,
    },
    "anthropic": {
        "name": "Anthropic",
        "signup_url": "https://console.anthropic.com/settings/keys",
        "env_var": "ANTHROPIC_API_KEY",
        "free_tier": "No — $5 free credit for new accounts",
        "capabilities": ["text", "code", "analysis"],
        "pricing": "Pay-as-you-go, ~$3/1M tokens (Sonnet)",
        "recommended": False,
    },
    "mistral": {
        "name": "Mistral AI",
        "signup_url": "https://console.mistral.ai/api-keys",
        "env_var": "MISTRAL_API_KEY",
        "free_tier": "No — pay-as-you-go",
        "capabilities": ["text", "code"],
        "pricing": "Pay-as-you-go, ~$0.15/1M tokens (Small)",
        "recommended": False,
    },
    "together": {
        "name": "Together AI",
        "signup_url": "https://api.together.xyz/settings/api-keys",
        "env_var": "TOGETHER_API_KEY",
        "free_tier": "Yes — $5 free credit",
        "capabilities": ["text", "code", "open-source models"],
        "pricing": "Pay-as-you-go, cheap open-source hosting",
        "recommended": False,
    },
    "xai": {
        "name": "xAI (Grok)",
        "signup_url": "https://console.x.ai/",
        "env_var": "XAI_API_KEY",
        "free_tier": "No",
        "capabilities": ["text", "code"],
        "pricing": "Pay-as-you-go",
        "recommended": False,
    },
    "cohere": {
        "name": "Cohere",
        "signup_url": "https://dashboard.cohere.com/api-keys",
        "env_var": "COHERE_API_KEY",
        "free_tier": "Yes — free trial tier",
        "capabilities": ["text", "RAG"],
        "pricing": "Free trial, then pay-as-you-go",
        "recommended": False,
    },
    "fal": {
        "name": "fal.ai",
        "signup_url": "https://fal.ai/dashboard/keys",
        "env_var": "FAL_KEY",
        "free_tier": "Yes — limited free credits",
        "capabilities": ["images (Flux)", "video (Kling, minimax)"],
        "pricing": "Pay-per-generation, ~$0.01-0.10/image",
        "recommended": False,
    },
    "stability": {
        "name": "Stability AI",
        "signup_url": "https://platform.stability.ai/account/keys",
        "env_var": "STABILITY_API_KEY",
        "free_tier": "Yes — 25 free credits",
        "capabilities": ["images (Stable Diffusion 3)"],
        "pricing": "Credit-based, ~$0.02-0.06/image",
        "recommended": False,
    },
    "elevenlabs": {
        "name": "ElevenLabs",
        "signup_url": "https://elevenlabs.io/app/settings/api-keys",
        "env_var": "ELEVENLABS_API_KEY",
        "free_tier": "Yes — 10k characters/month free",
        "capabilities": ["voice synthesis", "voice cloning"],
        "pricing": "Free tier, then $5/mo+",
        "recommended": False,
    },
    "runway": {
        "name": "Runway",
        "signup_url": "https://dev.runwayml.com/",
        "env_var": "RUNWAY_API_KEY",
        "free_tier": "No — credit-based",
        "capabilities": ["video generation (Gen-3)"],
        "pricing": "Credit-based, ~$0.05/sec of video",
        "recommended": False,
    },
    "replicate": {
        "name": "Replicate",
        "signup_url": "https://replicate.com/account/api-tokens",
        "env_var": "REPLICATE_API_TOKEN",
        "free_tier": "No — pay-per-prediction",
        "capabilities": ["various open-source models"],
        "pricing": "Pay-per-prediction, varies by model",
        "recommended": False,
    },
}

# Cheapest/fastest model per provider, used exclusively for API key validation.
# Each test call sends a trivial 2-token prompt ("Reply with exactly: OK")
# with max_tokens=5, costing ~$0.0001 per provider.
_TEST_MODELS: dict[str, str] = {
    "openai": "openai/gpt-4o-mini",
    "gemini": "gemini/gemini-2.5-flash-lite",
    "groq": "groq/llama-3.3-70b-versatile",
    "deepseek": "deepseek/deepseek-chat",
    "mistral": "mistral/mistral-small-latest",
    "perplexity": "perplexity/sonar",
    "anthropic": "anthropic/claude-haiku-4-5-20251001",
    "together": "together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "xai": "xai/grok-2-latest",
    "cohere": "cohere/command-r",
}


# ── Tool functions ─────────────────────────────────────────────────────────────

async def llm_setup(
    action: str = "status",
    provider: str | None = None,
    api_key: str | None = None,
) -> str:
    """Set up and manage API providers, hooks, and routing enforcement.

    Actions:
    - "status": Show which providers are configured and which are missing
    - "guide": Step-by-step guide to add recommended free/cheap providers
    - "discover": Scan for existing API keys in environment (safe, read-only)
    - "add": Add an API key for a provider (writes to .env file securely)
    - "test": Validate API keys with a minimal call (tests configured or specific provider)
    - "provider": Show details about a specific provider
    - "install_hooks": Install auto-routing hooks globally (every Claude Code session)
    - "uninstall_hooks": Remove auto-routing hooks

    Args:
        action: What to do — "status", "guide", "discover", "add", "test", "provider", "install_hooks", or "uninstall_hooks".
        provider: Provider name (for "add", "test", and "provider" actions).
        api_key: API key value (for "add" action only). Key is validated before saving.
    """
    if action == "status":
        return _setup_status()
    elif action == "guide":
        return _setup_guide()
    elif action == "discover":
        return await _setup_discover()
    elif action == "test":
        return await _setup_test(provider)
    elif action == "add":
        if not provider:
            return "Specify a provider name. Run `llm_setup(action='status')` to see available providers."
        if not api_key:
            reg = _PROVIDER_REGISTRY.get(provider)
            if reg:
                return (
                    f"To add **{reg['name']}**:\n"
                    f"1. Sign up at: {reg['signup_url']}\n"
                    f"2. Copy your API key\n"
                    f"3. Run: `llm_setup(action='add', provider='{provider}', api_key='your-key-here')`\n\n"
                    f"Free tier: {reg['free_tier']}"
                )
            return f"Unknown provider: {provider}. Run `llm_setup(action='status')` for the list."
        return _setup_add(provider, api_key)
    elif action == "provider":
        return _setup_provider_detail(provider)
    elif action == "install_hooks":
        return _setup_install_hooks()
    elif action == "uninstall_hooks":
        return _setup_uninstall_hooks()
    else:
        return f"Unknown action: {action}. Use: status, guide, discover, add, test, provider, install_hooks, or uninstall_hooks."


async def llm_rate(good: bool, decision_id: int | None = None) -> str:
    """Rate the last (or a specific) routing decision as good or bad.

    Stores thumbs-up / thumbs-down feedback in the ``routing_decisions`` table.
    Over time this signal can be used to retrain the local classifier so routing
    choices improve based on your preferences.

    Args:
        good: True = routing was a good choice; False = bad choice.
        decision_id: Row ID to rate. Omit (or pass None) to rate the most recent
            routing decision.

    Returns:
        Confirmation string with the rated decision ID, or an error message.
    """
    rated_id = await rate_routing_decision(decision_id, good)
    if rated_id is None:
        return "No routing decision found to rate. Make a routed call first."
    label = "👍 Good" if good else "👎 Bad"
    return f"{label} — feedback recorded for routing decision #{rated_id}."


# ── Setup helper functions ─────────────────────────────────────────────────────

def _setup_status() -> str:
    """Build a markdown report of configured vs. missing providers."""
    config = get_config()
    configured = config.available_providers
    lines = ["# API Provider Status\n"]

    # Configured providers
    if configured:
        lines.append(f"## Configured ({len(configured)})")
        for name in sorted(configured):
            reg = _PROVIDER_REGISTRY.get(name, {})
            caps = ", ".join(reg.get("capabilities", ["unknown"]))
            lines.append(f"- **{name}**: {caps}")
        lines.append("")

    # Missing providers
    missing = set(_PROVIDER_REGISTRY.keys()) - configured
    if missing:
        recommended = [p for p in sorted(missing) if _PROVIDER_REGISTRY[p].get("recommended")]
        others = [p for p in sorted(missing) if not _PROVIDER_REGISTRY[p].get("recommended")]

        if recommended:
            lines.append("## Recommended to Add")
            for name in recommended:
                reg = _PROVIDER_REGISTRY[name]
                lines.append(f"- **{name}**: {reg['free_tier']} — {reg['signup_url']}")
            lines.append("")

        if others:
            lines.append(f"## Other Available ({len(others)})")
            for name in others:
                reg = _PROVIDER_REGISTRY[name]
                lines.append(f"- {name}: {reg['signup_url']}")
            lines.append("")

    lines.append(f"**Total: {len(configured)}/{len(_PROVIDER_REGISTRY)} providers configured**")
    lines.append("\nRun `llm_setup(action='guide')` for step-by-step setup.")
    return "\n".join(lines)


def _setup_guide() -> str:
    """Return a static markdown quick-start guide for new users."""
    return """# Quick Start Guide — Get Running in 5 Minutes

## Step 1: Gemini (FREE — best starting point)
1. Go to https://aistudio.google.com/apikey
2. Click "Create API Key" (Google account required)
3. Copy the key
4. Run: `llm_setup(action='add', provider='gemini', api_key='your-key')`
5. You now have: text, code, images (Imagen 3), and video (Veo 2)!

## Step 2: Groq (FREE — ultra-fast inference)
1. Go to https://console.groq.com/keys
2. Sign up and create an API key
3. Run: `llm_setup(action='add', provider='groq', api_key='your-key')`
4. Adds: blazing fast Llama 3.3 for classification and simple tasks

## Step 3: DeepSeek (CHEAP — best quality/price)
1. Go to https://platform.deepseek.com/api_keys
2. Sign up and add $5 credit (lasts weeks of heavy use)
3. Run: `llm_setup(action='add', provider='deepseek', api_key='your-key')`
4. Adds: excellent coding and reasoning at 1/20th the cost of GPT-4o

## Step 4 (Optional): OpenAI
1. Go to https://platform.openai.com/api-keys
2. Add billing ($5 minimum)
3. Run: `llm_setup(action='add', provider='openai', api_key='your-key')`
4. Adds: GPT-4o, o3, DALL-E 3, TTS, Whisper

## After Setup
- Run `llm_setup(action='status')` to see what's configured
- Run `llm_setup(action='discover')` to find keys already on your machine
- Use `llm_health()` to verify all providers are working

## Budget Protection
Set per-provider monthly limits:
```
LLM_ROUTER_BUDGET_OPENAI=10.00
LLM_ROUTER_BUDGET_GEMINI=5.00
LLM_ROUTER_MONTHLY_BUDGET=20.00
```

## Security Notes
- Keys are stored in `.env` (local only, never committed to git)
- `.env` should be in `.gitignore` (the router checks this)
- Keys are loaded into environment variables at runtime only
- No keys are ever logged or sent to third parties
"""


async def _setup_discover() -> str:
    """Scan environment variables and common .env file locations for API keys."""
    import os
    from pathlib import Path

    lines = ["# API Key Discovery\n"]
    lines.append("Scanning for existing API keys on your machine...\n")

    found: list[tuple[str, str, str]] = []  # (provider, source, masked_key)

    # 1. Check current environment variables
    for provider, reg in _PROVIDER_REGISTRY.items():
        env_var = reg["env_var"]
        val = os.environ.get(env_var, "")
        if val:
            masked = _mask_key(val)
            found.append((provider, f"env: ${env_var}", masked))

    # 2. Check common .env file locations (read-only, no writes)
    import asyncio

    env_paths = [
        Path.home() / ".env",
        Path.cwd() / ".env",
        Path.home() / ".config" / "llm-router" / ".env",
    ]
    for env_path in env_paths:
        # Offload synchronous Path.exists() to thread pool to avoid blocking event loop
        exists = await asyncio.to_thread(env_path.exists)
        if exists:
            try:
                content = await asyncio.to_thread(env_path.read_text)
                for provider, reg in _PROVIDER_REGISTRY.items():
                    env_var = reg["env_var"]
                    for line in content.splitlines():
                        stripped = line.strip()
                        if stripped.startswith(env_var + "="):
                            val = stripped.split("=", 1)[1].strip().strip("'\"")
                            if val:
                                masked = _mask_key(val)
                                source = f"file: {env_path}"
                                # Avoid duplicates
                                if not any(p == provider and s == source for p, s, _ in found):
                                    found.append((provider, source, masked))
            except PermissionError:
                pass

    if found:
        lines.append(f"## Found {len(found)} API Key(s)\n")
        for provider, source, masked in found:
            status = "configured" if provider in get_config().available_providers else "found but not loaded"
            lines.append(f"- **{provider}** ({status}): `{masked}` — {source}")
        lines.append("")
        lines.append("Keys marked 'found but not loaded' exist on your machine but aren't in the router's .env file.")
        lines.append("Run `llm_setup(action='add', provider='<name>', api_key='<key>')` to add them.")
    else:
        lines.append("No existing API keys found in environment or common config files.")
        lines.append("\nRun `llm_setup(action='guide')` for setup instructions.")

    lines.append("\n## Security")
    lines.append("- This scan only checked environment variables and .env files")
    lines.append("- No keys were transmitted — all checks are local")
    lines.append("- Keys are masked in this output for safety")

    return "\n".join(lines)


def _mask_key(key: str) -> str:
    """Mask an API key for safe display, preserving only edge characters."""
    if len(key) <= 12:
        return key[:3] + "***" + key[-2:]
    return key[:4] + "***" + key[-4:]


def _setup_add(provider: str, api_key: str) -> str:
    """Write an API key to the project .env file and reload config."""
    from pathlib import Path

    reg = _PROVIDER_REGISTRY.get(provider)
    if not reg:
        return f"Unknown provider: {provider}. Run `llm_setup(action='status')` for the list."

    env_var = reg["env_var"]
    api_key = api_key.strip()

    # Basic key format validation
    if len(api_key) < 10:
        return f"API key seems too short ({len(api_key)} chars). Please check and try again."
    if " " in api_key or "\n" in api_key:
        return "API key contains whitespace. Please check and try again."

    # Find the .env file
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        # Try project root — tools/setup.py is 4 levels deep from project root
        env_path = Path(__file__).parent.parent.parent.parent / ".env"

    if env_path.exists():
        content = env_path.read_text()
        # Check if key already exists
        new_lines = []
        replaced = False
        for line in content.splitlines():
            if line.strip().startswith(env_var + "="):
                new_lines.append(f"{env_var}={api_key}")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"{env_var}={api_key}")
        env_path.write_text("\n".join(new_lines) + "\n")
    else:
        env_path.write_text(f"{env_var}={api_key}\n")

    # Verify .gitignore protection
    gitignore_warning = ""
    gitignore_path = env_path.parent / ".gitignore"
    if gitignore_path.exists():
        gitignore_content = gitignore_path.read_text()
        if ".env" not in gitignore_content:
            gitignore_warning = "\n\n**WARNING**: `.env` is NOT in `.gitignore` — add it to prevent leaking keys!"
    else:
        gitignore_warning = "\n\n**WARNING**: No `.gitignore` found — create one with `.env` to prevent leaking keys!"

    # Reload config to pick up new key
    import os
    import llm_router.config as _cfg
    os.environ[env_var] = api_key
    _cfg._config = None  # Force reload on next get_config()

    masked = _mask_key(api_key)

    # Suggest setting a budget cap if none is already configured for this provider
    from llm_router.budget_store import get_cap as _get_stored_cap
    budget_nudge = ""
    if _get_stored_cap(provider) <= 0:
        budget_nudge = (
            f"\n\n💰 **Set a monthly budget cap** to protect against runaway costs:\n"
            f"   `llm-router budget set {provider} 20`\n"
            f"   (or any amount — the router will route away from this provider as you approach the cap)"
        )

    return (
        f"Added **{reg['name']}** (`{masked}`) to `{env_path}`\n\n"
        f"Run `llm_health()` to verify the key works."
        f"{gitignore_warning}"
        f"{budget_nudge}"
    )


async def _setup_test(provider: str | None) -> str:
    """Validate API key(s) by making a minimal LLM call (~$0.0001 each)."""
    config = get_config()
    configured = config.available_providers

    if provider:
        if provider not in _TEST_MODELS:
            return f"No test model configured for '{provider}'. Testable: {', '.join(sorted(_TEST_MODELS))}"
        if provider not in configured:
            return f"Provider '{provider}' is not configured. Add a key first: `llm_setup(action='add', provider='{provider}')`"
        providers_to_test = [provider]
    else:
        providers_to_test = [p for p in sorted(configured) if p in _TEST_MODELS]
        if not providers_to_test:
            return "No testable text providers configured. Run `llm_setup(action='status')` to see available providers."

    results: list[str] = ["## API Key Validation\n"]
    test_prompt = "Reply with exactly: OK"
    test_messages = [{"role": "user", "content": test_prompt}]

    for p in providers_to_test:
        model = _TEST_MODELS[p]
        try:
            resp = await providers.call_llm(
                model=model, messages=test_messages, temperature=0, max_tokens=5,
            )
            results.append(f"- **{p}**: Valid ({model}, ${resp.cost_usd:.6f}, {resp.latency_ms:.0f}ms)")
        except Exception as e:
            err_str = str(e)
            if "auth" in err_str.lower() or "api key" in err_str.lower() or "invalid" in err_str.lower():
                results.append(f"- **{p}**: INVALID KEY ({e})")
            elif "rate" in err_str.lower() or "429" in err_str:
                results.append(f"- **{p}**: Valid (rate-limited, key works but quota exceeded)")
            else:
                results.append(f"- **{p}**: ERROR ({e})")

    return "\n".join(results)


def _setup_provider_detail(provider: str | None) -> str:
    """Return detailed information about a single provider."""
    if not provider:
        return "Specify a provider name. Example: `llm_setup(action='provider', provider='gemini')`"

    reg = _PROVIDER_REGISTRY.get(provider)
    if not reg:
        available = ", ".join(sorted(_PROVIDER_REGISTRY.keys()))
        return f"Unknown provider: {provider}. Available: {available}"

    config = get_config()
    is_configured = provider in config.available_providers

    lines = [
        f"# {reg['name']}",
        "",
        f"**Status**: {'Configured' if is_configured else 'Not configured'}",
        f"**Free tier**: {reg['free_tier']}",
        f"**Pricing**: {reg['pricing']}",
        f"**Capabilities**: {', '.join(reg['capabilities'])}",
        f"**Sign up**: {reg['signup_url']}",
        f"**Env var**: `{reg['env_var']}`",
    ]

    if not is_configured:
        lines.extend([
            "",
            "## How to Add",
            f"1. Go to {reg['signup_url']}",
            "2. Create an account and generate an API key",
            f"3. Run: `llm_setup(action='add', provider='{provider}', api_key='your-key')`",
        ])

    return "\n".join(lines)


def _setup_install_hooks() -> str:
    """Install auto-routing hooks and rules globally into Claude Code."""
    from llm_router.install_hooks import install
    actions = install()
    lines = [
        "# LLM Router — Hooks Installed Globally",
        "",
        "The following actions were performed:",
        "",
    ]
    for a in actions:
        lines.append(f"- {a}")

    lines.extend([
        "",
        "**Restart Claude Code to activate.**",
        "",
        "Every prompt will now be evaluated by the auto-router.",
        "Work that skips a required route is blocked by default (`LLM_ROUTER_ENFORCE=hard`).",
        "The router classifies tasks and injects `⚡ MANDATORY ROUTE` directives that",
        "direct Claude to use the optimal `llm_*` tool.",
        "Set `LLM_ROUTER_ENFORCE=soft` or `off` to relax enforcement.",
        "",
        "To remove: `llm_setup(action='uninstall_hooks')`",
    ])
    return "\n".join(lines)


def _setup_uninstall_hooks() -> str:
    """Remove auto-routing hooks and rules from Claude Code."""
    from llm_router.install_hooks import uninstall
    actions = uninstall()
    lines = [
        "# LLM Router — Hooks Removed",
        "",
    ]
    for a in actions:
        lines.append(f"- {a}")

    lines.extend([
        "",
        "Restart Claude Code to apply changes.",
        "To reinstall: `llm_setup(action='install_hooks')`",
    ])
    return "\n".join(lines)


def register(mcp, should_register=None) -> None:
    """Register setup and feedback tools with the FastMCP instance."""
    gate = should_register or (lambda _: True)
    if gate("llm_setup"):
        mcp.tool()(llm_setup)
    if gate("llm_rate"):
        mcp.tool()(llm_rate)
