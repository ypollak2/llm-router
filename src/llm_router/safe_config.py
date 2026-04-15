"""Security-friendly fallback configuration when .env is blocked.

Enterprise security teams often restrict reading `.env` files at the project
level. This module provides an alternative: ~/.llm-router/config.yaml in the
user's home directory, which is:

- Readable by the user alone (user can set permissions)
- Portable across projects
- Outside the project (thus not subject to repo-level security blocks)
- YAML format (human-readable, easy to edit)

Priority order for configuration:
  1. .env file (project-level, if readable)
  2. ~/.llm-router/config.yaml (user-level fallback)
  3. Environment variables (system-wide)
  4. Hardcoded defaults

This module also provides auto-discovery: when init-claude-memory is run,
it detects which providers are currently configured (from any source) and
generates a YAML template for the user to fill in.
"""

from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None


def safe_config_path() -> Path:
    """Return path to the user-level safe config file.

    Located in ~/.llm-router/ to avoid project-level security restrictions.
    """
    return Path.home() / ".llm-router" / "config.yaml"


def load_safe_config() -> dict[str, Any]:
    """Load configuration from ~/.llm-router/config.yaml if it exists.

    Returns:
        Dict of config keys and values. Empty dict if file doesn't exist
        or YAML parsing fails.
    """
    if not yaml:
        return {}

    path = safe_config_path()
    if not path.exists():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_safe_config_template(discovered: dict[str, Any]) -> None:
    """Write a template config.yaml with discovered settings.

    Used by `llm-router init-claude-memory` to generate a starting point.
    The user fills in the placeholders and the router reads them.

    Args:
        discovered: Dict of auto-detected settings (e.g. Ollama URL, configured providers).
    """
    if not yaml:
        return

    path = safe_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    template = f"""# LLM Router Configuration (Security-Friendly)
#
# Use this file when your security team blocks .env at the project level.
# Place this at: ~/.llm-router/config.yaml
# Permissions: chmod 600 ~/.llm-router/config.yaml (readable by you only)
#
# Priority: .env (project) → config.yaml (user home) → env vars → defaults
# If both .env and config.yaml exist, .env takes precedence.

# Text LLM API Keys (leave empty to disable)
openai_api_key: "{discovered.get('openai_api_key', '')}"
gemini_api_key: "{discovered.get('gemini_api_key', '')}"
anthropic_api_key: "{discovered.get('anthropic_api_key', '')}"
mistral_api_key: "{discovered.get('mistral_api_key', '')}"
deepseek_api_key: "{discovered.get('deepseek_api_key', '')}"
groq_api_key: "{discovered.get('groq_api_key', '')}"
perplexity_api_key: "{discovered.get('perplexity_api_key', '')}"

# Ollama (local inference — free, no API key needed)
ollama_base_url: "{discovered.get('ollama_base_url', 'http://localhost:11434')}"
ollama_budget_models: "{discovered.get('ollama_budget_models', 'gemma4:latest,qwen3.5:latest')}"

# Media providers
fal_key: "{discovered.get('fal_key', '')}"
stability_api_key: "{discovered.get('stability_api_key', '')}"
elevenlabs_api_key: "{discovered.get('elevenlabs_api_key', '')}"

# Router settings
llm_router_profile: "{discovered.get('llm_router_profile', 'balanced')}"
llm_router_claude_subscription: {str(discovered.get('llm_router_claude_subscription', False)).lower()}

# (More options available — see llm-router/src/llm_router/config.py for full list)
"""

    if not path.exists():
        with open(path, "w", encoding="utf-8") as f:
            f.write(template)
        path.chmod(0o600)  # Read/write by user only
