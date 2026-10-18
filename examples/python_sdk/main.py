"""SDK door: call the router in-process, no server.

    python examples/python_sdk/main.py

One call through ``llm_router.Router``; one assertion on the answer; one on the ledger row the SDK
wrote. The model edge is the offline fake in examples/fake_backend.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fake_backend as fb  # noqa: E402

fb.install()

from llm_router import Router  # noqa: E402

result = Router().run(fb.PROMPT)                       # the whole integration: one call
print(result.text, "via", result.model)

fb.expect(result.text == fb.FINAL, f"unexpected answer: {result.text!r}")
rows = fb.usage_rows()
fb.expect(len(rows) == 1, f"expected one ledger row, got {len(rows)}")
row = rows[0]
fb.expect(row["success"] == 1, f"ledger row not a success: {row}")
fb.expect((row["input_tokens"], row["output_tokens"]) == fb.USAGE, f"ledger tokens: {row}")
fb.finish()
