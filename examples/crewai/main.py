"""CrewAI through the llm-router gateway: one agent, one task.

    pip install -e . -r examples/crewai/requirements.txt
    python examples/crewai/main.py

The agent's ``LLM`` comes from ``CrewAIAdapter`` (``crewai.LLM`` with base_url set to the gateway).
The gateway runs on the offline fake in examples/fake_backend.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fake_backend as fb  # noqa: E402

fb.install()
fb.start_gateway()

from crewai import Agent, Crew, Process, Task  # noqa: E402

from llm_router.frameworks.crewai import CrewAIAdapter  # noqa: E402

agent = Agent(role="Weather reporter", goal="Report the weather", backstory="Answers briefly.",
              llm=CrewAIAdapter().wrap_model(fb.MODEL), verbose=False)
task = Task(description=fb.PROMPT, expected_output="one sentence", agent=agent)
answer = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False).kickoff().raw
print(answer)
fb.expect(fb.FINAL in answer, f"unexpected answer: {answer!r}")
rows = fb.usage_rows()
fb.expect(rows and all(r["success"] == 1 for r in rows), f"ledger rows: {rows}")
fb.finish()
