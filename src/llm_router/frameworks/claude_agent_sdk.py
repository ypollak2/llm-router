"""Claude Agent SDK adapter — stub.

Anthropic's Claude Agent SDK (https://docs.claude.com/en/api/agent-sdk)
is the official agent framework for Claude models. The integration path:
    1. Wraps the anthropic.Anthropic client with llm_router routing.
    2. Tags lineage with the subagent name when one is active.
    3. Honors Claude's tool_use streaming format.

v0.0.2 ships this stub; concrete impl in v0.0.3+.
"""
from __future__ import annotations

from typing import Any


CLAUDE_AGENT_SDK_AVAILABLE = False


class ClaudeAgentSdkAdapter:
    name: str = "claude-agent-sdk"

    def wrap_model(self, framework_model: Any, agent_id: str | None = None):
        raise NotImplementedError("Claude Agent SDK adapter lands in v0.0.3+.")

    def detect_agent_id(self, framework_runtime: Any) -> str | None:
        return None

    @classmethod
    def is_available(cls) -> bool:
        return CLAUDE_AGENT_SDK_AVAILABLE
