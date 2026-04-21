"""Auto-detect available services and generate token-wise routing profiles.

This module:
1. Detects available subscriptions, API keys, and local tools
2. Generates ~/.llm-router/profile.yaml with token-wise priority
3. Monitors quotas and adjusts routing dynamically
4. Allows manual overrides and customization
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from llm_router.config import get_config
from llm_router.codex_agent import is_codex_available
from llm_router.gemini_cli_agent import is_gemini_cli_available
from llm_router.claude_usage import get_claude_pressure
from llm_router.logging import get_logger

log = get_logger("llm_router.auto_profile")

PROFILE_PATH = Path.home() / ".llm-router" / "profile.yaml"


class ServiceDetection(TypedDict, total=False):
    """Detected services and their quota status."""
    claude_subscription: bool
    claude_quota_remaining: float  # percentage
    ollama_available: bool
    codex_available: bool
    gemini_cli_available: bool
    gemini_cli_quota: dict  # {count, daily_limit, pressure}
    openai_available: bool
    gemini_api_available: bool
    perplexity_available: bool
    groq_available: bool
    deepseek_available: bool
    mistral_available: bool
    cohere_available: bool


def detect_services() -> ServiceDetection:
    """Auto-detect all available services and their quota status.

    Returns:
        Dictionary with detected services and their current quota state.
    """
    detected: ServiceDetection = {}
    cfg = get_config()

    # ── Subscriptions & quotas ──────────────────────────────────────────────

    # Claude subscription
    if cfg.llm_router_claude_subscription:
        detected["claude_subscription"] = True
        try:
            pressure = get_claude_pressure()
            detected["claude_quota_remaining"] = (1.0 - pressure) * 100
        except Exception:
            detected["claude_quota_remaining"] = 0.0

    # Ollama (local, unlimited)
    if cfg.ollama_base_url:
        detected["ollama_available"] = True

    # Codex (free if OpenAI subscription)
    if is_codex_available():
        detected["codex_available"] = True

    # Gemini CLI (free tier: Google One AI Pro)
    if is_gemini_cli_available():
        detected["gemini_cli_available"] = True
        try:
            from llm_router.gemini_cli_quota import get_gemini_quota_status
            quota = get_gemini_quota_status()
            detected["gemini_cli_quota"] = quota
        except Exception:
            pass

    # ── API Keys ────────────────────────────────────────────────────────────

    if cfg.openai_api_key:
        detected["openai_available"] = True
    if cfg.gemini_api_key:
        detected["gemini_api_available"] = True
    if cfg.perplexity_api_key:
        detected["perplexity_available"] = True
    if cfg.groq_api_key:
        detected["groq_available"] = True
    if cfg.deepseek_api_key:
        detected["deepseek_available"] = True
    if cfg.mistral_api_key:
        detected["mistral_available"] = True
    if cfg.cohere_api_key:
        detected["cohere_available"] = True

    return detected


def generate_profile_yaml(detected: ServiceDetection) -> str:
    """Generate YAML profile based on detected services with token-wise priority.

    Priority order:
    1. Free local (Ollama - unlimited tokens)
    2. Free subscriptions (Claude Pro, Codex, Gemini CLI - limited daily)
    3. Cheap APIs (Gemini Flash, DeepSeek, Groq - <$0.01/1M)
    4. Mid-tier APIs (Gemini Pro, GPT-4o - $0.01-0.03/1M)
    5. Expensive APIs (o3 - $0.025+/1M)

    Args:
        detected: ServiceDetection from detect_services()

    Returns:
        YAML string for ~/.llm-router/profile.yaml
    """

    # Build service tiers
    free_local = []
    free_subscriptions = []
    cheap_apis = []
    balanced_apis = []
    expensive_apis = []

    # Tier 1: Free local
    if detected.get("ollama_available"):
        free_local.append("ollama/qwen3.5:32b")
        free_local.append("ollama/gemma4:latest")

    # Tier 2: Free subscriptions
    if detected.get("claude_subscription"):
        free_subscriptions.append("claude-sonnet-4-6")
        free_subscriptions.append("claude-haiku-4-5-20251001")
    if detected.get("codex_available"):
        free_subscriptions.append("codex/gpt-5.4")
        free_subscriptions.append("codex/o3")
    if detected.get("gemini_cli_available"):
        free_subscriptions.append("gemini_cli/gemini-2.5-flash")
        free_subscriptions.append("gemini_cli/gemini-2.0-flash")

    # Tier 3: Cheap APIs (<$0.01/1M)
    if detected.get("gemini_api_available"):
        cheap_apis.append("gemini/gemini-2.5-flash")
    if detected.get("deepseek_available"):
        cheap_apis.append("deepseek/deepseek-chat")
        cheap_apis.append("deepseek/deepseek-reasoner")
    if detected.get("groq_available"):
        cheap_apis.append("groq/llama-3.3-70b-versatile")

    # Tier 4: Balanced APIs ($0.01-0.03/1M)
    if detected.get("gemini_api_available"):
        balanced_apis.append("gemini/gemini-2.5-pro")
    if detected.get("openai_available"):
        balanced_apis.append("openai/gpt-4o-mini")
        balanced_apis.append("openai/gpt-4o")
    if detected.get("mistral_available"):
        balanced_apis.append("mistral/mistral-large-latest")
    if detected.get("cohere_available"):
        balanced_apis.append("cohere/command-r-plus")

    # Tier 5: Expensive APIs ($0.025+/1M)
    if detected.get("openai_available"):
        expensive_apis.append("openai/o3")
    if detected.get("perplexity_available"):
        expensive_apis.append("perplexity/sonar-pro")
        expensive_apis.append("perplexity/sonar")

    # Generate YAML
    yaml = "# Auto-detected LLM Router Profile\n"
    yaml += "# Generated by: llm-router profile auto\n"
    yaml += "# Edit this file to customize routing priority\n\n"

    yaml += "routing:\n"
    yaml += "  # Token-wise priority: exploit quotas of free services first,\n"
    yaml += "  # then cheap APIs, then expensive ones. Minimum waste.\n"
    yaml += "  model_priority:\n"

    if free_local:
        yaml += "    - tier: free_local\n"
        yaml += "      description: Local + unlimited\n"
        yaml += f"      models: {free_local}\n\n"

    if free_subscriptions:
        yaml += "    - tier: free_subscriptions\n"
        yaml += "      description: Claude Pro, Codex, Gemini CLI (limited daily)\n"
        yaml += f"      models: {free_subscriptions}\n\n"

    if cheap_apis:
        yaml += "    - tier: cheap_apis\n"
        yaml += "      description: Cheap APIs (<$0.01/1M)\n"
        yaml += f"      models: {cheap_apis}\n\n"

    if balanced_apis:
        yaml += "    - tier: balanced_apis\n"
        yaml += "      description: Balanced quality/cost ($0.01-0.03/1M)\n"
        yaml += f"      models: {balanced_apis}\n\n"

    if expensive_apis:
        yaml += "    - tier: expensive_apis\n"
        yaml += "      description: Premium models ($0.025+/1M, last resort)\n"
        yaml += f"      models: {expensive_apis}\n\n"

    # Add quota constraints
    yaml += "quotas:\n"
    if detected.get("claude_subscription"):
        remaining = detected.get("claude_quota_remaining", 0)
        yaml += "  claude_subscription:\n"
        yaml += f"    remaining_percent: {remaining:.0f}%\n"
        yaml += "    cap: null  # unlimited under Pro\n\n"

    if detected.get("gemini_cli_available"):
        quota_info = detected.get("gemini_cli_quota", {})
        count = quota_info.get("count", 0)
        limit = quota_info.get("daily_limit", 1500)
        yaml += "  gemini_cli:\n"
        yaml += f"    used_today: {count}/{limit}\n"
        yaml += f"    daily_limit: {limit}\n\n"

    yaml += "budget:\n"
    yaml += "  monthly_cap_usd: null  # Set this to limit API spend\n"
    yaml += "  enforcement: warn      # warn|soft|hard\n\n"

    yaml += "overrides:\n"
    yaml += "  # Override specific task routing if needed\n"
    yaml += "  # Example:\n"
    yaml += "  # code:\n"
    yaml += "  #   force_quality: true  # Always use Claude for code\n"

    return yaml


def save_profile(yaml_content: str) -> Path:
    """Save profile YAML to ~/.llm-router/profile.yaml.

    Args:
        yaml_content: Generated YAML string

    Returns:
        Path to saved profile file
    """
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(yaml_content)
    log.info(f"Profile saved to {PROFILE_PATH}")
    return PROFILE_PATH


def auto_generate_profile() -> Path:
    """Auto-detect services and generate profile in one call.

    Returns:
        Path to generated profile
    """
    detected = detect_services()
    yaml = generate_profile_yaml(detected)
    return save_profile(yaml)


def display_detected_services(detected: ServiceDetection) -> str:
    """Format detected services for display.

    Args:
        detected: ServiceDetection from detect_services()

    Returns:
        Formatted string for console output
    """
    output = "\n📊 Auto-Detected Services\n"
    output += "─" * 50 + "\n\n"

    output += "🆓 FREE LOCAL (Unlimited)\n"
    if detected.get("ollama_available"):
        output += "  ✓ Ollama (local models)\n"
    else:
        output += "  ✗ Ollama (not running on localhost:11434)\n"

    output += "\n💳 FREE SUBSCRIPTIONS (Limited Daily/Weekly)\n"
    if detected.get("claude_subscription"):
        remaining = detected.get("claude_quota_remaining", 0)
        output += f"  ✓ Claude Pro ({remaining:.0f}% quota remaining)\n"
    else:
        output += "  ✗ Claude Pro (not configured)\n"

    if detected.get("codex_available"):
        output += "  ✓ Codex CLI (OpenAI subscription)\n"
    else:
        output += "  ✗ Codex CLI (not installed)\n"

    if detected.get("gemini_cli_available"):
        quota = detected.get("gemini_cli_quota", {})
        count = quota.get("count", 0)
        limit = quota.get("daily_limit", 1500)
        output += f"  ✓ Gemini CLI ({count}/{limit} requests used today)\n"
    else:
        output += "  ✗ Gemini CLI (not installed)\n"

    output += "\n💰 PAID APIs (Cost-Optimized Order)\n"
    if detected.get("gemini_api_available"):
        output += "  ✓ Gemini API (cheapest: $0.01/1M)\n"
    if detected.get("deepseek_available"):
        output += "  ✓ DeepSeek ($0.0007/1K = $0.7/1M)\n"
    if detected.get("groq_available"):
        output += "  ✓ Groq (free tier available)\n"
    if detected.get("openai_available"):
        output += "  ✓ OpenAI (gpt-4o: $0.03/1M)\n"
    if detected.get("mistral_available"):
        output += "  ✓ Mistral\n"
    if detected.get("cohere_available"):
        output += "  ✓ Cohere\n"
    if detected.get("perplexity_available"):
        output += "  ✓ Perplexity (web search)\n"

    if not any([
        detected.get("gemini_api_available"),
        detected.get("deepseek_available"),
        detected.get("groq_available"),
        detected.get("openai_available"),
    ]):
        output += "  (None configured)\n"

    output += "\n"
    return output
