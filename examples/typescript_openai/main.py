"""TypeScript (official ``openai`` npm client) through the llm-router gateway.

    python examples/typescript_openai/main.py        # needs node >= 22.18 and `npm install` here

Serves the gateway on the offline fake (examples/fake_backend.py), runs ``index.ts`` with
OPENAI_BASE_URL pointed at it, then asserts the client's exit status and the ledger rows.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import fake_backend as fb  # noqa: E402

fb.install()
url = fb.start_gateway()
done = subprocess.run(["node", "index.ts"], cwd=HERE, env=fb.child_env({"OPENAI_BASE_URL": url + "/v1"}),
                      capture_output=True, text=True, timeout=120)
print(done.stdout, done.stderr, sep="")
fb.expect(done.returncode == 0, f"index.ts failed with {done.returncode}")
fb.expect(fb.FINAL in done.stdout, f"unexpected answer: {done.stdout!r}")
rows = fb.usage_rows(expected=2)
fb.expect(len(rows) == 2 and all(r["success"] == 1 for r in rows), f"ledger rows: {rows}")
fb.finish()
