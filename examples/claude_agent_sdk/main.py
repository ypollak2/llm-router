"""Claude Agent SDK through the llm-router gateway.

    pip install -e . -r examples/claude_agent_sdk/requirements.txt
    python examples/claude_agent_sdk/main.py

The SDK drives a Claude Code process; ``ClaudeAgentSdkAdapter`` sets that process's
ANTHROPIC_BASE_URL to the gateway's ``/v1/messages``. The gateway runs on the offline fake in
examples/fake_backend.py, so the process never reaches Anthropic.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fake_backend as fb  # noqa: E402

fb.install()
fb.start_gateway()

from claude_agent_sdk import AssistantMessage, TextBlock, query  # noqa: E402

from llm_router.frameworks.claude_agent_sdk import ClaudeAgentSdkAdapter  # noqa: E402


async def main() -> str:
    options = ClaudeAgentSdkAdapter().wrap_model("claude-haiku-4-5")
    options.allowed_tools = []
    options.max_turns = 1
    text = ""
    async for message in query(prompt=fb.PROMPT, options=options):
        if isinstance(message, AssistantMessage):
            text += "".join(b.text for b in message.content if isinstance(b, TextBlock))
    return text


answer = asyncio.run(main())
print(answer)
fb.expect(fb.FINAL in answer, f"unexpected answer: {answer!r}")
rows = fb.usage_rows()
fb.expect(rows and all(r["success"] == 1 for r in rows), f"ledger rows: {rows}")
fb.finish()
