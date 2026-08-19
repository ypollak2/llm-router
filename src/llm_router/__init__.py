"""LLM Router — Multi-LLM routing MCP server for Claude Code.

Provides intelligent routing across 15+ LLM providers (text, image, video, audio)
with complexity-based model selection, budget-aware downshifting, circuit-breaker
health tracking, and multi-step orchestration pipelines.

Also includes ResponseRouter for routing Claude's explanations through cheaper models
to reduce session quota consumption by 60-70%.

See README.md for full documentation.
"""

# Single source of truth = the installed distribution's metadata. This is
# correct for wheels (where pyproject.toml is NOT shipped); only fall back to
# reading pyproject.toml when running from an un-installed source checkout.
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("llm-routing")
except PackageNotFoundError:
    try:
        import tomllib
        from pathlib import Path

        _pp = Path(__file__).parent.parent.parent / "pyproject.toml"
        __version__ = tomllib.load(_pp.open("rb"))["project"]["version"]
    except Exception:
        __version__ = "0.0.0+unknown"

# Export response router for easy access
from llm_router.response_router import route_response as route_response_explanations
from llm_router.sdk import RouteResult, RoutingError, route

__all__ = ["route", "RouteResult", "RoutingError", "route_response_explanations"]
