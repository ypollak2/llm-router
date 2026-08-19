"""Hermes framework adapter — skeleton.

"Hermes" in this context refers to a function-calling / tool-use protocol
for agent loops. Multiple projects use the name (Hermes-2 / Hermes-3 from
Nous Research, the Hermes function-calling format adopted by several
open-weight chat templates, and some closed implementations).

v0.0.2 ships this skeleton with the FrameworkAdapter shape so users can
contribute the concrete integration without first negotiating the
protocol contract. The concrete implementation lands in v0.0.3 once we
confirm which Hermes flavour the user wants to target.

Decision deferred:
    - Tool-use format: Nous-Hermes Vicuna-style tool tags vs JSON function
      call dicts vs <tool_call>...</tool_call> blocks.
    - Streaming: most Hermes-style flows want streamed tool calls; LLM Router's
      MCP server currently returns full responses. v0.0.3 adds streaming.
"""
from __future__ import annotations

from typing import Any


HERMES_AVAILABLE = False  # flip to True when concrete impl lands


class HermesAdapter:
    """FrameworkAdapter skeleton for Hermes-style tool-use protocols."""

    name: str = "hermes"

    def wrap_model(self, framework_model: Any, agent_id: str | None = None):
        raise NotImplementedError(
            "Hermes adapter ships in v0.0.3 once the protocol target is "
            "confirmed. Track: https://github.com/ypollak2/llm_router/issues"
        )

    def detect_agent_id(self, framework_runtime: Any) -> str | None:
        return None

    @classmethod
    def is_available(cls) -> bool:
        return HERMES_AVAILABLE
