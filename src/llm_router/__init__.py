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
def _resolve_version() -> str:
    # Read on first access to ``__version__`` rather than at import: ``importlib.metadata``
    # alone is ~9 ms of ``python -X importtime`` and every per-prompt hook paid it
    # to resolve a string it never prints (PG4 / P2-G-3).
    try:
        import tomllib
        from pathlib import Path

        pp = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
        if pp.is_file():
            with pp.open("rb") as fh:
                data = tomllib.load(fh)
            if data.get("project", {}).get("name") in {"llm-routing", "llm_routing"}:
                return data["project"]["version"]
    except Exception:  # noqa: BLE001 -- fall through to the installed metadata
        pass
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("llm-routing")
    except PackageNotFoundError:
        return "0.0.0+unknown"


# Public re-exports, resolved on first access (PEP 562).
#
# ``import llm_router.<anything>`` runs this file first. It used to import
# ``response_router`` and ``sdk`` eagerly, which pulled ``llm_router.types`` (and
# the SDK's own imports) into every process that touched any submodule -- including
# the per-prompt hooks, where that was ~14 ms of the ``import`` phase measured with
# ``python -X importtime`` (PG4 / P2-G-3). ``from llm_router import route`` and
# ``llm_router.route`` still work; the import just happens when the name is used.

__all__ = ["route", "RouteResult", "RoutingError", "route_response_explanations"]

_LAZY_EXPORTS = {
    "route": ("llm_router.sdk", "route"),
    "RouteResult": ("llm_router.sdk", "RouteResult"),
    "RoutingError": ("llm_router.sdk", "RoutingError"),
    "route_response_explanations": ("llm_router.response_router", "route_response"),
}


def __getattr__(name: str):
    if name == "__version__":
        globals()["__version__"] = value = _resolve_version()
        return value
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value  # later lookups skip this function
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS) | {"__version__"})
