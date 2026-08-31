"""LLM Router — Multi-LLM routing MCP server for Claude Code.

Provides intelligent routing across 15+ LLM providers (text, image, video, audio)
with complexity-based model selection, budget-aware downshifting, circuit-breaker
health tracking, and multi-step orchestration pipelines.

Also includes ResponseRouter for routing Claude's explanations through cheaper models
to reduce session quota consumption by 60-70%.

See README.md for full documentation.
"""

# Report the version of the code that is ACTUALLY RUNNING.
#
# Distribution metadata is only refreshed by `pip install`, so a source checkout
# reports whatever was last installed: this package ran from a 13.0.8 tree while
# `llm-router doctor` printed 13.0.4, which silently mislabels every bug report
# filed from a checkout. When a sibling pyproject.toml exists we are demonstrably
# running from source, and that file — not the stale metadata — is the truth.
# Wheels do not ship pyproject.toml, so they still fall through to the metadata.
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version


def _resolve_version() -> str:
    try:
        import tomllib
        from pathlib import Path

        _pp = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
        if _pp.is_file():
            data = tomllib.load(_pp.open("rb"))
            if data.get("project", {}).get("name") in {"llm-routing", "llm_routing"}:
                return data["project"]["version"]
    except Exception:
        pass
    try:
        return _pkg_version("llm-routing")
    except PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _resolve_version()

# Export response router for easy access
from llm_router.response_router import route_response as route_response_explanations
from llm_router.sdk import RouteResult, RoutingError, route

__all__ = ["route", "RouteResult", "RoutingError", "route_response_explanations"]
