"""LangGraph through the llm-router gateway: streaming plus one tool call.

    pip install -e . -r examples/langgraph/requirements.txt
    python examples/langgraph/main.py

The agent's chat model comes from ``LangGraphAdapter`` (``ChatOpenAI`` with base_url set to the
gateway). ``stream_mode="messages"`` streams, the agent calls ``get_weather`` once, and the final
answer is streamed. The gateway runs on the offline fake in examples/fake_backend.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fake_backend as fb  # noqa: E402

fb.install()
fb.start_gateway()

from langchain_core.tools import tool  # noqa: E402
from langgraph.prebuilt import create_react_agent  # noqa: E402

from llm_router.frameworks.langgraph import LangGraphAdapter  # noqa: E402

tool_calls: list[str] = []


@tool
def get_weather(city: str) -> str:
    """Get the weather for a city."""
    tool_calls.append(city)
    return fb.RESULT


agent = create_react_agent(LangGraphAdapter().wrap_model(fb.MODEL), [get_weather])
text_chunks: list[str] = []
for chunk, _meta in agent.stream({"messages": [("user", fb.PROMPT)]}, stream_mode="messages"):
    if type(chunk).__name__ == "AIMessageChunk" and isinstance(chunk.content, str) and chunk.content:
        text_chunks.append(chunk.content)
answer = "".join(text_chunks)
print(answer)
fb.expect(answer == fb.FINAL, f"unexpected answer: {answer!r}")
fb.expect(tool_calls == ["Lisbon"], f"tool calls: {tool_calls}")
fb.expect(len(text_chunks) > 1, f"the answer was not streamed (chunks: {len(text_chunks)})")
rows = fb.usage_rows(expected=2)
fb.expect(len(rows) == 2 and all(r["success"] == 1 for r in rows), f"ledger rows: {rows}")
fb.finish()
