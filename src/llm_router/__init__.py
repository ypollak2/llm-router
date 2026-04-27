"""LLM Router — Multi-LLM routing MCP server for Claude Code.

Provides intelligent routing across 15+ LLM providers (text, image, video, audio)
with complexity-based model selection, budget-aware downshifting, circuit-breaker
health tracking, and multi-step orchestration pipelines.

Also includes ResponseRouter for routing Claude's explanations through cheaper models
to reduce session quota consumption by 60-70%.

See README.md for full documentation.
"""

# Version is read dynamically from pyproject.toml to maintain single source of truth
import tomllib
from pathlib import Path

__version__ = "7.6.1"  # SYNCED FROM pyproject.toml — DO NOT EDIT MANUALLY

try:
    _pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
    _pyproject_data = tomllib.load(_pyproject_path.open("rb"))
    __version__ = _pyproject_data["project"]["version"]
except Exception:
    # Fallback to hardcoded version above if pyproject.toml is unavailable
    pass

# Export response router for easy access
from llm_router.response_router import route_response as route_response_explanations

__all__ = ["route_response_explanations"]
