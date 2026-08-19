"""The routing directive must reach stdout BEFORE the ledger write, not after.

WHY THIS EXISTS
---------------
`llm_router-auto-route.py` was measured taking 36.9 seconds, of which 36.26s was 8 sqlite
`execute` calls and 31.19s was inside `execution_ledger.record_event`. Claude Code's
UserPromptSubmit budget is 30s, so the host reported:

    UserPromptSubmit hook timed out after 30s — output discarded

"Output discarded" is the whole problem. The routing directive was fully computed —
classification done, model chosen, savings estimated — and then thrown away because an
ACCOUNTING write ran first and blocked. LLM Router silently stopped routing on exactly the
turns when something else held the database, and the only visible symptom was a timeout
message that points at the hook rather than at the write.

The cause of that particular slow window was never established. Nine hypotheses were
refuted by measurement (gateway lock, gateway existence, WAL size, checkpoint starvation,
pytest load, Ollama model, Ollama generally, import cost, external classifier), and a 2x2
controlled reproduction could not reproduce it. This ordering does not depend on knowing
the cause: it bounds the blast radius of ANY slow write from "the routing decision is
lost" to "the accounting row may be late", which is the recoverable one.

WHAT THIS TEST PINS
-------------------
The hook emits its JSON on stdout even when `record_event` is catastrophically slow.
Under the old ordering this test fails by timing out with empty stdout — which is exactly
the production symptom, reproduced deliberately.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"

#: Far longer than the hook could ever legitimately take, and far longer than the test's
#: own timeout, so a hook that waits on it cannot possibly finish in time.
STALL_SECONDS = 120


def _shim_dir(tmp: Path) -> Path:
    """A PYTHONPATH entry that stalls `record_event` and nothing else.

    The hook runs as a subprocess, so in-process monkeypatching cannot reach it.

    A FIRST ATTEMPT shadowed the `llm_router` package on PYTHONPATH and rewrote its
    ``__path__``. It did not take effect — the real, fast ``record_event`` was imported,
    the stall never happened, and the test passed against the DEFECTIVE ordering. That is
    a can't-fail test, caught only by reverting the fix and finding the test still green.

    This version pre-seeds ``sys.modules`` from ``sitecustomize``, which the interpreter
    imports at startup before the hook runs. ``from llm_router.execution_ledger import ...``
    consults ``sys.modules`` first, so the stub wins without touching the real package.
    """
    shim = tmp / "shim"
    shim.mkdir(parents=True)
    (shim / "sitecustomize.py").write_text(
        textwrap.dedent(
            f"""
            import sys, time, types

            _m = types.ModuleType("llm_router.execution_ledger")

            class LedgerEvent:
                def __init__(self, **kw):
                    self.__dict__.update(kw)

            def record_event(event, **kw):
                time.sleep({STALL_SECONDS})   # never returns within the test's lifetime
                return True

            _m.LedgerEvent = LedgerEvent
            _m.record_event = record_event
            sys.modules["llm_router.execution_ledger"] = _m
            """
        )
    )
    return shim


def _run(env_extra: dict[str, str], tmp_home: Path, timeout: float):
    payload = json.dumps({"prompt": "refactor the parser", "session_id": "emit-order"})
    env = os.environ.copy()
    env["HOME"] = str(tmp_home)
    env["LLM_ROUTER_DISABLE_LLM_CLASSIFIERS"] = "1"
    env["LLM_ROUTER_DIRECT_EXECUTION"] = "0"
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        env[k] = ""
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=payload, capture_output=True, text=True, env=env, timeout=timeout,
    )


def test_the_directive_is_emitted_even_when_the_ledger_write_hangs():
    """The regression test. Fails by timeout with empty stdout under the old ordering.

    Deliberately NOT asserting on how long the hook takes overall — it still has to be
    killed, because the stalled write holds the process open. What matters is that the
    directive was already on stdout when that happened, so the host has something to use.
    """
    with tempfile.TemporaryDirectory(prefix="llm_router-emit-order-") as td:
        tmp = Path(td)
        shim = _shim_dir(tmp)
        env = {"PYTHONPATH": str(shim) + os.pathsep + os.environ.get("PYTHONPATH", "")}

        try:
            result = _run(env, tmp, timeout=25)
            stdout = result.stdout
        except subprocess.TimeoutExpired as exc:
            # The hook is still blocked on the stalled write — expected. What we are
            # testing is whether stdout was flushed BEFORE that block.
            stdout = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) \
                else (exc.stdout or "")

        assert stdout.strip(), (
            "stdout was EMPTY while the ledger write hung — the routing directive was "
            "computed and then discarded. This is the production failure: "
            "'UserPromptSubmit hook timed out after 30s — output discarded'."
        )
        payload = json.loads(stdout)
        assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_the_hook_is_fast_when_the_ledger_is_not_stalled():
    """Control: without the shim the hook completes normally and emits the same shape.

    Without this, the test above would also pass against a hook that emitted a hardcoded
    payload and never routed at all.
    """
    with tempfile.TemporaryDirectory(prefix="llm_router-emit-order-ok-") as td:
        tmp = Path(td)
        result = _run({}, tmp, timeout=60)
        assert result.stdout.strip(), "control run produced no stdout at all"
        payload = json.loads(result.stdout)
        assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert payload["hookSpecificOutput"]["additionalContext"].strip(), (
            "the directive body is empty — the hook emitted a shell with no routing hint"
        )
