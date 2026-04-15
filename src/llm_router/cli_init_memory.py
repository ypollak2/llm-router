"""llm-router init-claude-memory — Auto-discover setup and generate Claude Code memory files."""

from llm_router.config import get_config
from llm_router.safe_config import write_safe_config_template, safe_config_path


def run_init_claude_memory() -> None:
    """Auto-discover current LLM configuration and generate Claude Code memory + safe config.

    This command:
    1. Detects which API keys and Ollama are configured
    2. Generates ~/.claude/projects/*/memory/user_environment_setup.md
    3. Creates ~/.llm-router/config.yaml template for enterprise users

    Enterprise use case: When security teams block .env at project level, users can:
    - Fill in ~/.llm-router/config.yaml with their keys
    - The router will read it as a fallback
    - Project-level .env is still preferred if readable
    """
    config = get_config()

    # ── Auto-discover current configuration ──────────────────────────────────
    discovered = {
        "openai_api_key": config.openai_api_key[:10] + "***" if config.openai_api_key else "",
        "gemini_api_key": config.gemini_api_key[:10] + "***" if config.gemini_api_key else "",
        "anthropic_api_key": config.anthropic_api_key[:10] + "***"
        if config.anthropic_api_key
        else "",
        "mistral_api_key": config.mistral_api_key[:10] + "***" if config.mistral_api_key else "",
        "deepseek_api_key": config.deepseek_api_key[:10] + "***" if config.deepseek_api_key else "",
        "groq_api_key": config.groq_api_key[:10] + "***" if config.groq_api_key else "",
        "perplexity_api_key": config.perplexity_api_key[:10] + "***"
        if config.perplexity_api_key
        else "",
        "fal_key": config.fal_key[:10] + "***" if config.fal_key else "",
        "stability_api_key": config.stability_api_key[:10] + "***"
        if config.stability_api_key
        else "",
        "elevenlabs_api_key": config.elevenlabs_api_key[:10] + "***"
        if config.elevenlabs_api_key
        else "",
        "ollama_base_url": config.ollama_base_url,
        "ollama_budget_models": config.ollama_budget_models,
        "llm_router_profile": config.llm_router_profile.value,
        "llm_router_claude_subscription": config.llm_router_claude_subscription,
    }

    # ── Generate safe config template ──────────────────────────────────────
    write_safe_config_template(discovered)
    safe_path = safe_config_path()

    print("\n✅ LLM Router Initialization Complete\n")
    print("=" * 60)
    print("  Configuration Discovery Results")
    print("=" * 60)
    print()
    print("Text LLM Providers:")
    print(f"  OpenAI:      {('✅ Configured' if config.openai_api_key else '❌ Not configured')}")
    print(f"  Gemini:      {('✅ Configured' if config.gemini_api_key else '❌ Not configured')}")
    print(f"  Anthropic:   {('✅ Configured' if config.anthropic_api_key else '❌ Not configured')}")
    print(f"  Mistral:     {('✅ Configured' if config.mistral_api_key else '❌ Not configured')}")
    print(f"  Deepseek:    {('✅ Configured' if config.deepseek_api_key else '❌ Not configured')}")
    print(f"  Groq:        {('✅ Configured' if config.groq_api_key else '❌ Not configured')}")
    print(f"  Perplexity:  {('✅ Configured' if config.perplexity_api_key else '❌ Not configured')}")
    print()
    print("Local Inference:")
    print(
        f"  Ollama:      {('✅ ' + config.ollama_base_url if config.ollama_base_url else '❌ Not configured')}"
    )
    if config.ollama_budget_models:
        models = config.ollama_budget_models.split(",")
        for model in models:
            print(f"               📦 {model.strip()}")
    print()
    print("Router Settings:")
    print(f"  Profile:     {config.llm_router_profile.value}")
    print(
        f"  Mode:        {'Claude Subscription (no API key)' if config.llm_router_claude_subscription else 'API Key Mode'}"
    )
    print()
    print("=" * 60)
    print()

    # ── Show where the safe config was written ──────────────────────────────
    if safe_path.exists():
        print(f"📝 Safe Config Template:\n   {safe_path}\n")
        print("   Use this file when your security team blocks .env at the project level.")
        print("   Fill in the API keys and the router will read them as a fallback.\n")
        print("   Permissions set to 600 (readable by you only)\n")

    # ── Show next steps ────────────────────────────────────────────────────
    print("Next Steps:")
    print("  1. If using Ollama only: Nothing to do — you're all set! ✅")
    print("  2. If .env is blocked: Edit ~/.llm-router/config.yaml and add your API keys")
    print("  3. Run 'llm-router status' to verify the setup\n")
