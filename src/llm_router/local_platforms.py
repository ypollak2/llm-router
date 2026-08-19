"""Auto-discovery of local LLM inference platforms.

On startup, probes well-known ports for running local inference servers and
displays detected platforms + available models to the user. Detected platforms
are also injected into routing when no explicit openai_compat config is set.

Tier 1 — OpenAI-compatible (zero adapter needed):
  Ollama, LM Studio, Jan, llama.cpp, vLLM, llamafile, LocalAI, Msty, MLX,
  Cortex, text-generation-webui (with OpenAI extension)

Tier 2 — Needs light adapter:
  GPT4All (partial OpenAI-compat), Kobold.cpp (custom API)

Port override via env vars:
  LLM_ROUTER_LOCAL_LMSTUDIO_PORT, LLM_ROUTER_LOCAL_JAN_PORT, etc.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Platform registry
# ---------------------------------------------------------------------------

@dataclass
class LocalPlatform:
    name: str                          # Display name
    provider_key: str                  # Snake-case key for env/config
    default_port: int
    health_path: str                   # Path that returns 200 when running
    models_path: str                   # Path to list available models
    api_type: str                      # "openai", "ollama", "gpt4all", "koboldai"
    tier: int                          # 1 = drop-in; 2 = light adapter needed
    notes: str = ""
    port_env: str = ""                 # Derived in __post_init__

    def __post_init__(self) -> None:
        self.port_env = f"LLM_ROUTER_LOCAL_{self.provider_key.upper()}_PORT"

    @property
    def effective_port(self) -> int:
        env_val = os.environ.get(self.port_env, "")
        if env_val.isdigit():
            return int(env_val)
        return self.default_port

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self.effective_port}"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}{self.health_path}"

    @property
    def models_url(self) -> str:
        return f"{self.base_url}{self.models_path}"


# Ordered by likelihood of being installed / user population size
PLATFORMS: list[LocalPlatform] = [
    # ── Tier 1: pure OpenAI-compat ──────────────────────────────────────────
    LocalPlatform(
        name="Ollama",
        provider_key="ollama",
        default_port=11434,
        health_path="/api/tags",
        models_path="/api/tags",
        api_type="ollama",
        tier=1,
        notes="Most popular local runner. Auto-detected by existing llm_router Ollama support.",
    ),
    LocalPlatform(
        name="LM Studio",
        provider_key="lmstudio",
        default_port=1234,
        health_path="/v1/models",
        models_path="/v1/models",
        api_type="openai",
        tier=1,
        notes="GUI app for macOS/Windows. Enable local server in app settings.",
    ),
    LocalPlatform(
        name="Jan",
        provider_key="jan",
        default_port=1337,
        health_path="/v1/models",
        models_path="/v1/models",
        api_type="openai",
        tier=1,
        notes="Open-source desktop app. Start server from Jan's settings panel.",
    ),
    LocalPlatform(
        name="vLLM",
        provider_key="vllm",
        default_port=8000,
        health_path="/v1/models",
        models_path="/v1/models",
        api_type="openai",
        tier=1,
        notes="High-throughput GPU server. Best for powerful local hardware.",
    ),
    LocalPlatform(
        name="llama.cpp server",
        provider_key="llamacpp",
        default_port=8080,
        health_path="/v1/models",
        models_path="/v1/models",
        api_type="openai",
        tier=1,
        notes="Run with: llama-server -m model.gguf --port 8080",
    ),
    LocalPlatform(
        name="llamafile",
        provider_key="llamafile",
        default_port=8080,
        health_path="/v1/models",
        models_path="/v1/models",
        api_type="openai",
        tier=1,
        notes="Single-binary distribution. Run: ./model.llamafile",
    ),
    LocalPlatform(
        name="LocalAI",
        provider_key="localai",
        default_port=8080,
        health_path="/v1/models",
        models_path="/v1/models",
        api_type="openai",
        tier=1,
        notes="Docker-friendly multi-backend server. Supports many model formats.",
    ),
    LocalPlatform(
        name="Msty",
        provider_key="msty",
        default_port=10000,
        health_path="/v1/models",
        models_path="/v1/models",
        api_type="openai",
        tier=1,
        notes="macOS GUI app with local server mode.",
    ),
    # ── Tier 1: OpenAI-compat via extension/flag ─────────────────────────────
    LocalPlatform(
        name="MLX (Apple Silicon)",
        provider_key="mlx",
        default_port=8080,
        health_path="/v1/models",
        models_path="/v1/models",
        api_type="openai",
        tier=1,
        notes="Run with: mlx_lm.server --model <model>. Fastest on Apple M-series.",
    ),
    LocalPlatform(
        name="Cortex",
        provider_key="cortex",
        default_port=39281,
        health_path="/v1/models",
        models_path="/v1/models",
        api_type="openai",
        tier=1,
        notes="Jan's new CLI engine. Run: cortex start",
    ),
    LocalPlatform(
        name="text-generation-webui",
        provider_key="tgwebui",
        default_port=5000,
        health_path="/v1/models",
        models_path="/v1/models",
        api_type="openai",
        tier=1,
        notes="Start with --api flag to enable OpenAI-compat extension.",
    ),
    # ── Tier 2: light adapter needed ─────────────────────────────────────────
    LocalPlatform(
        name="GPT4All",
        provider_key="gpt4all",
        default_port=4891,
        health_path="/v1/models",
        models_path="/v1/models",
        api_type="gpt4all",
        tier=2,
        notes="Partial OpenAI-compat. Enable API server in GPT4All settings.",
    ),
    LocalPlatform(
        name="Kobold.cpp",
        provider_key="koboldcpp",
        default_port=5001,
        health_path="/api/v1/model",
        models_path="/api/v1/model",
        api_type="koboldai",
        tier=2,
        notes="Custom KoboldAI API. Popular for creative writing / roleplay.",
    ),
]

# Map from provider_key → platform for fast lookup
PLATFORM_MAP: dict[str, LocalPlatform] = {p.provider_key: p for p in PLATFORMS}

# ---------------------------------------------------------------------------
# Probe cache
# ---------------------------------------------------------------------------

@dataclass
class DetectedPlatform:
    platform: LocalPlatform
    models: list[str] = field(default_factory=list)
    detected_at: float = field(default_factory=time.monotonic)


_probe_cache: list[DetectedPlatform] | None = None
_probe_cache_time: float = 0.0
_PROBE_TTL = 120.0  # seconds — re-probe at most once every 2 min


# ---------------------------------------------------------------------------
# HTTP probing
# ---------------------------------------------------------------------------

def _probe_platform(p: LocalPlatform, timeout: float = 0.8) -> bool:
    """Return True if the platform responds to its health endpoint."""
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False  # never make real network calls in tests
    try:
        r = requests.get(p.health_url, timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def _list_models(p: LocalPlatform, timeout: float = 1.5) -> list[str]:
    """Return model names available on a detected platform."""
    try:
        r = requests.get(p.models_url, timeout=timeout)
        if r.status_code >= 400:
            return []
        data: Any = r.json()

        if p.api_type == "ollama":
            # {"models": [{"name": "llama3:8b"}, ...]}
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]

        if p.api_type in ("openai", "gpt4all"):
            # {"data": [{"id": "llama-3.2-8b"}, ...]}
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]

        if p.api_type == "koboldai":
            # {"result": "model-name"}
            model = data.get("result", "")
            return [model] if model else []

    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def probe_all(force: bool = False) -> list[DetectedPlatform]:
    """Probe all known platforms and return those that are running.

    Results are cached for _PROBE_TTL seconds. Pass force=True to bypass cache.
    """
    global _probe_cache, _probe_cache_time
    now = time.monotonic()
    if not force and _probe_cache is not None and (now - _probe_cache_time) < _PROBE_TTL:
        return _probe_cache

    detected: list[DetectedPlatform] = []
    seen_ports: set[int] = set()

    for platform in PLATFORMS:
        port = platform.effective_port
        # Skip duplicate ports (llamafile/llama.cpp/LocalAI/MLX all default to 8080)
        if port in seen_ports:
            continue
        if _probe_platform(platform):
            models = _list_models(platform)
            detected.append(DetectedPlatform(platform=platform, models=models))
            seen_ports.add(port)

    _probe_cache = detected
    _probe_cache_time = now
    return detected


def get_first_openai_compat() -> tuple[str, list[str]] | None:
    """Return (base_url, models) for the first detected non-Ollama OpenAI-compat platform.

    Used to auto-populate openai_compat_base_url when the user hasn't configured it.
    """
    for d in probe_all():
        if d.platform.api_type == "openai" and d.models:
            return d.platform.base_url + "/v1", d.models
    return None


def print_startup_summary(detected: list[DetectedPlatform] | None = None) -> None:
    """Print a concise summary of detected local platforms to stdout."""
    if detected is None:
        detected = probe_all()

    if not detected:
        return  # silent when nothing found — don't alarm users with "nothing detected"

    print("\n🖥️  Local LLM platforms detected:")
    for d in detected:
        tier_tag = "" if d.platform.tier == 1 else " [tier 2]"
        if d.models:
            model_list = ", ".join(d.models[:3])
            suffix = f" (+{len(d.models) - 3} more)" if len(d.models) > 3 else ""
            print(f"   ✓ {d.platform.name}{tier_tag}  →  {d.platform.base_url}")
            print(f"     models: {model_list}{suffix}")
        else:
            print(f"   ✓ {d.platform.name}{tier_tag}  →  {d.platform.base_url}  (no models listed)")
    print()
