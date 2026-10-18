"""Pydantic AI through the llm-router gateway: streaming plus one tool call.

    pip install -e . -r examples/pydantic_ai/requirements.txt
    python examples/pydantic_ai/main.py

The agent's model comes from ``PydanticAiAdapter`` (``OpenAIChatModel`` over a provider whose
base_url is the gateway). The gateway runs on the offline fake in examples/fake_backend.py.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fake_backend as fb  # noqa: E402

fb.install()
fb.start_gateway()

from pydantic_ai import Agent  # noqa: E402

from llm_router.frameworks.pydantic_ai import PydanticAiAdapter  # noqa: E402

tool_calls: list[str] = []
agent = Agent(PydanticAiAdapter().wrap_model(fb.MODEL))


@agent.tool_plain
def get_weather(city: str) -> str:
    """Get the weather for a city."""
    tool_calls.append(city)
    return fb.RESULT


async def main() -> list[str]:
    async with agent.run_stream(fb.PROMPT) as run:
        return [piece async for piece in run.stream_text(delta=True, debounce_by=None)]


pieces = asyncio.run(main())
answer = "".join(pieces)
print(answer)
fb.expect(answer == fb.FINAL, f"unexpected answer: {answer!r}")
fb.expect(tool_calls == ["Lisbon"], f"tool calls: {tool_calls}")
fb.expect(len(pieces) > 1, f"the answer was not streamed (pieces: {len(pieces)})")
rows = fb.usage_rows(expected=2)
fb.expect(len(rows) == 2 and all(r["success"] == 1 for r in rows), f"ledger rows: {rows}")
fb.finish()
