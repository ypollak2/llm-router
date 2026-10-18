"""OpenAI Agents SDK through the llm-router gateway: streaming plus one tool call.

    pip install -e . -r examples/openai_agents/requirements.txt
    python examples/openai_agents/main.py

The agent's model comes from ``OpenAIAgentsAdapter`` (an ``AsyncOpenAI`` whose base_url is the
gateway). ``Runner.run_streamed`` streams, the agent calls ``get_weather`` once, and the final answer
is streamed. The gateway runs on the offline fake in examples/fake_backend.py.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fake_backend as fb  # noqa: E402

fb.install()
fb.start_gateway()

from agents import Agent, Runner, function_tool, set_tracing_disabled  # noqa: E402

from llm_router.frameworks.openai_agents import OpenAIAgentsAdapter  # noqa: E402

set_tracing_disabled(True)
tool_calls: list[str] = []


@function_tool
def get_weather(city: str) -> str:
    """Get the weather for a city."""
    tool_calls.append(city)
    return fb.RESULT


async def main() -> tuple[str, int]:
    agent = Agent(name="weather", instructions="Answer weather questions with the tool.",
                  tools=[get_weather], model=OpenAIAgentsAdapter().wrap_model(fb.MODEL))
    run = Runner.run_streamed(agent, fb.PROMPT)
    deltas = 0
    async for event in run.stream_events():
        if event.type == "raw_response_event" and event.data.type == "response.output_text.delta":
            deltas += 1
    return str(run.final_output), deltas


answer, deltas = asyncio.run(main())
print(answer)
fb.expect(answer == fb.FINAL, f"unexpected answer: {answer!r}")
fb.expect(tool_calls == ["Lisbon"], f"tool calls: {tool_calls}")
fb.expect(deltas > 1, f"the answer was not streamed (text deltas: {deltas})")
rows = fb.usage_rows(expected=2)
fb.expect(len(rows) == 2 and all(r["success"] == 1 for r in rows), f"ledger rows: {rows}")
fb.finish()
