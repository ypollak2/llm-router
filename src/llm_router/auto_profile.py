"""Auto-detect available services and generate token-wise routing profiles.

This module:
1. Detects available subscriptions, API keys, and local tools
2. Generates ~/.llm-router/profile.yaml with token-wise priority
3. Monitors quotas and adjusts routing dynamically
4. Allows manual overrides and customization
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from llm_router.config import get_config
from llm_router.codex_agent import is_codex_available
from llm_router.gemini_cli_agent import is_gemini_cli_available
from llm_router.claude_usage import get_claude_pressure
from llm_router.logging import get_logger

from llm_router import paths

log = get_logger("llm_router.auto_profile")

def _profile_path():
    return paths.state_path("profile.yaml")
def _profile_cache_path():
    return paths.state_path("profile_cache.json")
def _profile_hash_path():
    # Own file, written only by save_profile (P0.15-a). NOT profile_cache.json:
    # save_detection_cache rewrites that whole file on every rescan, so a hash
    # stored there would be erased at once and every file would look edited.
    return paths.state_path("profile_hash.json")
def _profile_generated_path():
    return paths.state_path("profile.generated.yaml")
SCAN_INTERVAL_SECONDS = 3600  # Rescan every hour


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
    xai_available: bool
    together_available: bool
    huggingface_available: bool


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
            from llm_router.gemini_cli_quota import get_gemini_quota_status_sync
            quota = get_gemini_quota_status_sync()
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
    if cfg.xai_api_key:
        detected["xai_available"] = True
    if cfg.together_api_key:
        detected["together_available"] = True
    if cfg.huggingface_api_key:
        detected["huggingface_available"] = True

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

    # Tier 1: Free local (prioritized by task type + speed)
    if detected.get("ollama_available"):
        # Only models actually observed (discovery cache) are written, so the
        # profile never names a model the user doesn't have.
        _discovered: list[str] = []
        try:
            from llm_router.discover import get_cached_ollama_models
            _discovered = get_cached_ollama_models()
        except Exception:
            _discovered = []
        if _discovered:
            free_local.extend(_discovered)
        # EMPTY-ENVIRONMENT-1 (P0.15-d): with an empty discovery cache no local
        # model name is written. A name no adapter observed is a ghost model.

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
        cheap_apis.append("deepseek/deepseek-v4-flash")   # was deepseek-chat; alias deprecates 2026-07-24
    if detected.get("groq_available"):
        cheap_apis.append("groq/llama-3.3-70b-versatile")

    # Tier 4: Balanced APIs ($0.01-0.03/1M)
    if detected.get("gemini_api_available"):
        balanced_apis.append("gemini/gemini-2.5-pro")
    if detected.get("openai_available"):
        balanced_apis.append("openai/gpt-4o-mini")
        balanced_apis.append("openai/gpt-4o")
        balanced_apis.append("openai/gpt-5.4")      # current production workhorse ($2.50/$15)
    if detected.get("deepseek_available"):
        balanced_apis.append("deepseek/deepseek-v4-pro")   # reasoning flagship; was deepseek-reasoner (deprecates 2026-07-24)
    if detected.get("mistral_available"):
        balanced_apis.append("mistral/mistral-large-latest")
    if detected.get("cohere_available"):
        balanced_apis.append("cohere/command-r-plus")
    if detected.get("together_available"):
        balanced_apis.append("together/meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo")

    # Tier 4.5: Premium APIs (xAI Grok — competitive with frontier models)
    if detected.get("xai_available"):
        expensive_apis.append("xai/grok-3")

    # Tier 5: Expensive APIs ($0.025+/1M)
    if detected.get("openai_available"):
        expensive_apis.append("openai/o3")
        expensive_apis.append("openai/gpt-5.5")     # current flagship ($5/$30)
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
    elif detected.get("ollama_available"):
        yaml += "    # free_local: Ollama is up but no installed model has been observed yet.\n"
        yaml += "    # Run `llm-router discover`; no model name is written until one is seen.\n\n"

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


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a same-directory temp file and ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _recorded_profile_hash() -> str | None:
    """sha256 of the bytes ``save_profile`` last wrote, or None (legacy / unreadable)."""
    try:
        value = json.loads(_profile_hash_path().read_text()).get("sha256")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def profile_state() -> str:
    """Who owns profile.yaml right now (P0.15-a, AUTO-PROFILE-OVERWRITE-1).

    ``absent``  no file: the router may create it.
    ``owned``   the file's whole-file sha256 equals the one ``save_profile`` recorded.
    ``edited``  anything else: a user edit, a legacy file with no recorded hash,
                CRLF re-saves, an empty or non-YAML file. The router never
                overwrites these; it cannot tell an edit from a legacy file.
    """
    path = _profile_path()
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        return "absent"
    return "owned" if _recorded_profile_hash() == _sha256(current) else "edited"


PROFILE_EDITED_NOTE = (
    "profile.yaml has local changes and is no longer auto-updated; "
    "run `llm-router profile auto` to regenerate, a backup is kept"
)


def profile_status_lines() -> list[str]:
    """Lines for `llm-router doctor` / `llm-router map`; empty when nothing to say."""
    if profile_state() != "edited":
        return []
    lines = [PROFILE_EDITED_NOTE]
    if _profile_generated_path().exists():
        lines.append(f"latest regeneration (informational): {_profile_generated_path()}")
    return lines


def save_profile(yaml_content: str) -> Path:
    """Save profile YAML to ~/.llm-router/profile.yaml and record its sha256.

    The only writer of ``profile_hash.json``: the router later overwrites
    profile.yaml only while the file still hashes to what is recorded here.

    Args:
        yaml_content: Generated YAML string

    Returns:
        Path to saved profile file
    """
    data = yaml_content.encode("utf-8")
    # P0.15-c: profile.yaml counts as prior state for the grandfather file, so the one-time
    # write must happen before this file exists on a fresh install.
    from llm_router.model_discovery import ensure_grandfather
    ensure_grandfather()
    _atomic_write_bytes(_profile_path(), data)
    _atomic_write_bytes(
        _profile_hash_path(),
        json.dumps({"sha256": _sha256(data), "written_at": time.time()}).encode("utf-8"),
    )
    log.info(f"Profile saved to {_profile_path()}")
    return _profile_path()


def _backup_profile() -> Path | None:
    """Copy the current profile.yaml to ``profile.yaml.<UTC timestamp>.bak``; never overwrite a .bak."""
    src = _profile_path()
    if not src.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for n in range(1000):
        dest = src.with_name(f"{src.name}.{stamp}{'' if n == 0 else f'.{n}'}.bak")
        try:
            with open(src, "rb") as fin, open(dest, "xb") as fout:
                shutil.copyfileobj(fin, fout)
            return dest
        except FileExistsError:
            continue
    raise OSError(f"could not find a free backup name next to {src}")


def auto_generate_profile() -> Path:
    """Auto-detect services and generate profile in one call (explicit command).

    An existing profile.yaml is backed up first, so the explicit command can
    never lose a user's edits.

    Returns:
        Path to generated profile
    """
    detected = detect_services()
    yaml = generate_profile_yaml(detected)
    backup = _backup_profile()
    if backup is not None:
        log.info(f"Previous profile kept at {backup}")
    return save_profile(yaml)


def should_rescan() -> bool:
    """Check if enough time has passed to rescan services.

    Returns:
        True if last scan was >1 hour ago, False otherwise
    """
    if not _profile_cache_path().exists():
        return True

    try:
        cache = json.loads(_profile_cache_path().read_text())
        last_scan = cache.get("last_scan", 0)
        now = time.time()
        return (now - last_scan) > SCAN_INTERVAL_SECONDS
    except Exception:
        return True


def save_detection_cache(detected: ServiceDetection) -> None:
    """Cache detection results with timestamp for later comparison.

    Args:
        detected: ServiceDetection from detect_services()
    """
    _profile_cache_path().parent.mkdir(parents=True, exist_ok=True)
    cache = {
        "last_scan": time.time(),
        "detected": dict(detected),
    }
    _profile_cache_path().write_text(json.dumps(cache, indent=2))


def detect_changes() -> tuple[bool, list[str]]:
    """Compare current detection with cached detection.

    Returns:
        (has_changes, list of change descriptions)
    """
    if not _profile_cache_path().exists():
        return False, []

    try:
        cache = json.loads(_profile_cache_path().read_text())
        old_detected = cache.get("detected", {})
    except Exception:
        return False, []

    new_detected = detect_services()
    changes = []

    # Check each service
    if old_detected.get("claude_subscription") != new_detected.get("claude_subscription"):
        status = "enabled" if new_detected.get("claude_subscription") else "disabled"
        changes.append(f"Claude subscription: {status}")

    if old_detected.get("ollama_available") != new_detected.get("ollama_available"):
        status = "available" if new_detected.get("ollama_available") else "unavailable"
        changes.append(f"Ollama: {status}")

    if old_detected.get("codex_available") != new_detected.get("codex_available"):
        status = "available" if new_detected.get("codex_available") else "unavailable"
        changes.append(f"Codex CLI: {status}")

    if old_detected.get("gemini_cli_available") != new_detected.get("gemini_cli_available"):
        status = "available" if new_detected.get("gemini_cli_available") else "unavailable"
        changes.append(f"Gemini CLI: {status}")

    # Check API keys (show only count, not actual keys)
    old_apis = sum(1 for k in old_detected if k.endswith("_available") and old_detected[k])
    new_apis = sum(1 for k in new_detected if k.endswith("_available") and new_detected[k])
    if old_apis != new_apis:
        changes.append(f"API keys: {old_apis} → {new_apis} configured")

    # Check quotas
    old_claude_quota = old_detected.get("claude_quota_remaining", 0)
    new_claude_quota = new_detected.get("claude_quota_remaining", 0)
    quota_changed = abs(old_claude_quota - new_claude_quota) > 5  # >5% change
    if quota_changed:
        changes.append(f"Claude quota: {old_claude_quota:.0f}% → {new_claude_quota:.0f}%")

    old_gemini_quota = old_detected.get("gemini_cli_quota", {}).get("count", 0)
    new_gemini_quota = new_detected.get("gemini_cli_quota", {}).get("count", 0)
    if old_gemini_quota != new_gemini_quota:
        changes.append(f"Gemini CLI quota: {old_gemini_quota} → {new_gemini_quota} used today")

    return len(changes) > 0, changes


_last_rescan_note: str | None = None


def last_rescan_note() -> str | None:
    """The note the most recent ``rescan_and_update`` in this process wants shown, or None."""
    return _last_rescan_note


def rescan_and_update() -> tuple[bool, list[str]]:
    """Rescan services and update profile if changes detected.

    profile.yaml is rewritten only when the router owns it (absent, or hashing to
    what ``save_profile`` recorded). Otherwise the regeneration goes to
    ``profile.generated.yaml`` and a note is produced once per change of that
    content (AUTO-PROFILE-OVERWRITE-1).

    Returns:
        (profile_updated, list of changes)
    """
    global _last_rescan_note
    _last_rescan_note = None
    has_changes, changes = detect_changes()

    if has_changes:
        log.info(f"Service configuration changed: {', '.join(changes)}")
        detected = detect_services()
        yaml = generate_profile_yaml(detected)
        if profile_state() == "edited":
            data = yaml.encode("utf-8")
            try:
                previous = _profile_generated_path().read_bytes()
            except FileNotFoundError:
                previous = None
            if previous != data:
                _atomic_write_bytes(_profile_generated_path(), data)
                _last_rescan_note = PROFILE_EDITED_NOTE
                log.warning(PROFILE_EDITED_NOTE)
            save_detection_cache(detected)
            return False, changes
        save_profile(yaml)
        save_detection_cache(detected)
        return True, changes

    save_detection_cache(detect_services())
    return False, []


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
        output += "  ✓ Ollama — Qwen3.5 (primary), Kimi-K2.6 (code expert, 256K context)\n"
        output += "           Qwen3-Coder (code), Qwen2.5 (fallback), Gemma4 (lightweight)\n"
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
    if detected.get("xai_available"):
        output += "  ✓ xAI (Grok-3)\n"
    if detected.get("together_available"):
        output += "  ✓ Together AI\n"
    if detected.get("huggingface_available"):
        output += "  ✓ HuggingFace\n"
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
