"""Shared pieces of the thin ``base_url`` framework adapters (AGT A.6).

Every adapter points one framework's own OpenAI- or Anthropic-compatible client at the
llm-router gateway (``llm-router gateway``). Nothing here talks to a provider or reads a
provider key: the gateway owns model choice, keys, budget and the ledger.

Light on purpose: stdlib only at import. The framework package and ``llm_router.gateway``
(a FastAPI app) are imported inside the functions that need them.
"""
from __future__ import annotations

import importlib
import importlib.util
from typing import Any

#: The gateway's own "you pick" sentinel (``route_server.is_auto_model``): the router chooses.
AUTO_MODEL = "auto"

#: Sent as the API key when the gateway has no token configured. The gateway ignores the
#: bearer in that case, but every OpenAI/Anthropic client refuses an empty key.
PLACEHOLDER_KEY = "llm-router-gateway"


def module_present(name: str) -> bool:
    """True when top-level module ``name`` can be imported (checked without importing it)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def gateway_root() -> str:
    """The gateway origin, no path: ``LLM_ROUTER_GATEWAY_URL`` or the active preset's."""
    from llm_router import presets

    url = presets.gateway_url().rstrip("/")
    return url[: -len("/v1")] if url.endswith("/v1") else url


def openai_base_url() -> str:
    """``base_url`` for an OpenAI-compatible client (chat.completions, responses)."""
    return gateway_root() + "/v1"


def anthropic_base_url() -> str:
    """``base_url`` for an Anthropic client (it appends ``/v1/messages`` itself)."""
    return gateway_root()


def gateway_api_key() -> str:
    """The gateway bearer token when one is configured, else a placeholder."""
    from llm_router.gateway import gateway_token

    return gateway_token() or PLACEHOLDER_KEY


def model_name(framework_model: Any) -> str:
    """The model id to send: a str as given, a framework model object's own ``model_name`` /
    ``model`` attribute, or ``auto`` for None. Anything else is a TypeError, not a guess."""
    if framework_model is None:
        return AUTO_MODEL
    if isinstance(framework_model, str):
        return framework_model
    for attr in ("model_name", "model"):
        value = getattr(framework_model, attr, None)
        if isinstance(value, str) and value:
            return value
    raise TypeError(
        f"cannot read a model name from {type(framework_model).__name__}: pass a str "
        f"(e.g. 'auto') or an object with a string .model_name / .model"
    )


def require(module: str, extra: str) -> Any:
    """Import framework ``module`` or raise an ImportError that names what to install."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ImportError(f"{module} is not installed: pip install {extra}") from exc
