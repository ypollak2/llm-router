"""Claude Agent SDK adapter: ``ClaudeAgentOptions`` whose Claude Code process talks to the gateway.

The Claude Agent SDK (https://docs.claude.com/en/api/agent-sdk) drives a Claude Code process, which
reads ``ANTHROPIC_BASE_URL``. ``wrap_model`` returns ``ClaudeAgentOptions`` with that variable (and
the key) set in the process env to the gateway's Anthropic-compatible ``/v1/messages``, so each model
call the agent makes is routed, gated and ledgered by llm-router. Nothing is subclassed.

    from llm_router.frameworks.claude_agent_sdk import ClaudeAgentSdkAdapter
    options = ClaudeAgentSdkAdapter().wrap_model("auto")     # query(prompt=..., options=options)

Install the framework yourself (it is never a llm-router dependency): pip install claude-agent-sdk
Runnable, pinned: examples/claude_agent_sdk/.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from llm_router.frameworks import _gateway

CLAUDE_AGENT_SDK_AVAILABLE = _gateway.module_present("claude_agent_sdk")


class ClaudeAgentSdkAdapter:
    name: str = "claude-agent-sdk"

    def wrap_model(self, framework_model: Any = None, agent_id: str | None = None):
        """``ClaudeAgentOptions`` aimed at the gateway. ``framework_model`` is a model name,
        None for ``auto``, or an existing ``ClaudeAgentOptions`` whose other fields are kept
        (its ``env`` is extended, its ``model`` reused).

        ``agent_id`` is accepted for the FrameworkAdapter protocol; the gateway has no
        per-agent header, so it is not sent."""
        options_cls = _gateway.require("claude_agent_sdk", "claude-agent-sdk").ClaudeAgentOptions
        gateway_env = {
            "ANTHROPIC_BASE_URL": _gateway.anthropic_base_url(),
            "ANTHROPIC_API_KEY": _gateway.gateway_api_key(),
        }
        if isinstance(framework_model, options_cls):
            return dataclasses.replace(
                framework_model,
                model=framework_model.model or _gateway.AUTO_MODEL,
                env={**framework_model.env, **gateway_env})
        return options_cls(model=_gateway.model_name(framework_model), env=gateway_env)

    def detect_agent_id(self, framework_runtime: Any) -> str | None:
        return None

    @classmethod
    def is_available(cls) -> bool:
        return CLAUDE_AGENT_SDK_AVAILABLE
