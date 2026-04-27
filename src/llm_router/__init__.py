"""LLM Router — Multi-LLM routing MCP server for Claude Code.

Provides intelligent routing across 15+ LLM providers (text, image, video, audio)
with complexity-based model selection, budget-aware downshifting, circuit-breaker
health tracking, and multi-step orchestration pipelines.

Also includes ResponseRouter for routing Claude's explanations through cheaper models
to reduce session quota consumption by 60-70%.

See README.md for full documentation.
"""

# Version is read dynamically from pyproject.toml to maintain single source of truth
try:
    from importlib.metadata import version as _get_version
    __version__ = _get_version("llm-router")
except Exception:
    # Fallback for development installations or when package metadata is unavailable
    __version__ = "0.0.0.dev0"

# Export response router for easy access
from llm_router.response_router import route_response as route_response_explanations

__all__ = ["route_response_explanations"]
