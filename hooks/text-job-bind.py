#!/usr/bin/env python3
# llm_router-hook-version: 1
"""PreToolUse[Bash] hook: bind commit messages and PR bodies to the local text job (D-360).

The model writes ``git commit -m "${LLM_TEXT:?}"`` or ``gh pr create --title "<short>" --body
"${LLM_TEXT:?}"``. This hook builds the job input from the real repository, runs the router's
own ``llm_text_job`` (local model only, same checks), writes the checked text to a private file
and rewrites the command to read it (``-F <file>`` / ``--body-file <file>``) through
``hookSpecificOutput.updatedInput``. No ``permissionDecision`` is returned: the rewritten command
goes through the normal permission flow. If the job does not produce a checked text the command
is DENIED with a short reason (the model then writes the text itself). A placeholder command is
never allowed through unchanged.

The placeholder is a shell variable: ``${LLM_TEXT:?}`` makes the shell abort the whole command
when nothing replaced it, so a hook that is not registered, was killed at its timeout or runs on a
host that ignores ``updatedInput`` cannot cause the placeholder itself to be committed or posted.

stdout is the host's decision channel and must hold exactly one JSON object or nothing. As a
script this hook keeps a private duplicate of file descriptor 1 for the decision and points
descriptor 1 and ``sys.stdout`` at stderr for everything else, so no library, print or child
process can put a line in front of, or behind, the JSON (the failure that voided every Codex
handoff of the BIND outcome audit: BIND-1, PR #818). The decision is written once, by
``_Channel.send``.

Two guards keep the host waiting for nothing. A deadline guard answers with a deny shortly
before the host's own (silent) kill, so the model is told why instead of the command going
ahead on a timeout. After the decision is out, a watchdog ends the process hard if a library
thread keeps the interpreter from exiting.

Commands that are not ``git commit`` or ``gh pr create`` with the placeholder cost one JSON read
and no import of the router beyond the latency recorder. Hand-written commit and PR commands leave
one text-free ledger row (``text_job_binds.jsonl``) so adoption can be measured.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading

# -- KPI G1: record how long this invocation ran (llm_router.hook_latency) -----
# The clock starts BEFORE the first llm_router import, so the package import is
# inside the measurement. Armed only when run as a script: a test that imports
# this file must not register an exit-time write. Fail-open: no llm_router on
# the path means no row for this run; any other error is reported on stderr
# (never stdout, which the host parses) and the hook carries on.
import time as _hl_time

_HOOK_T0 = _hl_time.monotonic()
if __name__ == "__main__":
    try:
        from llm_router import hook_latency as _hook_latency

        _hook_latency.begin("text-job-bind", "PreToolUse", _HOOK_T0)
    except ImportError:
        pass  # llm_router is not importable on this host: no recorder, no row
    except Exception as _hl_exc:  # noqa: BLE001 -- timing must never break the hook
        import sys as _hl_sys

        print(f"llm-router: hook latency not recorded ({type(_hl_exc).__name__})", file=_hl_sys.stderr)

_TOKEN = re.compile(r"\$\{?LLM_TEXT")
_COMMIT_OR_PR = re.compile(r"commit|\bpr\b.{0,12}create")

# Literal fallbacks: the same numbers as llm_router.text_job_bind.limits, used when the package
# cannot be imported (a plugin-only install). A test pins them together.
_HOOK_TIMEOUT_SEC = 130
_DEADLINE_SEC = _HOOK_TIMEOUT_SEC - 15
_EXIT_GRACE_SEC = 2.0

_NOT_IMPORTABLE = (
    "llm-router's text-job hook could not load the router package, so the placeholder was not "
    "replaced. Nothing was run. Write the text yourself and run the command again without the placeholder."
)
_HOOK_FAILED = (
    "llm-router's text-job hook failed, so the placeholder was not replaced. Nothing was run. "
    "Write the text yourself and run the command again without the placeholder."
)
_TOO_SLOW = (
    "llm-router's text-job hook ran out of time, so the placeholder was not replaced. Nothing was run. "
    "Write the text yourself and run the command again without the placeholder."
)


class _Channel:
    """The decision channel: writes at most one JSON object, to the stream it was given."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self.sent = False

    def send(self, obj: dict) -> bool:
        """Write the decision; ``False`` when one was already written (a second object would make the host drop both)."""
        with self._lock:
            if self.sent:
                return False
            self.sent = True
            self._stream.write(json.dumps(obj))
            self._stream.flush()
            return True

    def deny(self, reason: str) -> bool:
        # Both shapes, as every hook in this repo: the current field and the legacy one.
        return self.send({
            "decision": "block",
            "reason": reason,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        })

    def rewrite(self, tool_input: dict, note: str | None) -> None:
        out: dict = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": tool_input}}
        if note:
            out["systemMessage"] = note
        self.send(out)


def _private_stdout():
    """A private duplicate of fd 1 for the decision; fd 1 itself is pointed at stderr."""
    try:
        real = os.fdopen(os.dup(1), "w", encoding="utf-8")
        os.dup2(2, 1)
        return real
    except OSError:
        return sys.stdout


def _daemon_timer(seconds: float, fn) -> threading.Timer:
    t = threading.Timer(max(0.0, seconds), fn)
    t.daemon = True
    t.start()
    return t


def _arm_deadline(chan: _Channel, payload: dict) -> None:
    """Answer with a deny, then end the process, shortly before the host's own silent kill.

    ``os._exit`` skips the exit handlers, so what a timeout inside the engine leaves behind (a ledger row, the
    cooldown, the model wait as its latency phase, the latency row itself) is written here first, and only
    when this deny is the one that went out."""
    def expire() -> None:
        try:
            if chan.deny(_TOO_SLOW):
                try:
                    from llm_router.hook_latency import finish_now
                    from llm_router.text_job_bind import engine

                    finish_now(engine.record_deadline(payload))
                except Exception as exc:  # noqa: BLE001 -- the deny is out; bookkeeping must not stop the exit
                    print(f"llm-router: deadline bookkeeping failed ({type(exc).__name__})", file=sys.stderr)
            sys.stderr.flush()
        finally:
            os._exit(0)

    _daemon_timer(_DEADLINE_SEC - (_hl_time.monotonic() - _HOOK_T0), expire)


def _set_session(payload: dict) -> None:
    try:
        from llm_router.hook_latency import set_session

        set_session(payload.get("session_id"))
    except Exception:  # noqa: BLE001 -- timing metadata never changes the answer
        return


def _run(raw: str, chan: _Channel, *, script: bool) -> int:
    token_present = bool(_TOKEN.search(raw))
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    _set_session(payload)
    event = payload.get("hook_event_name")
    if event not in (None, "PreToolUse"):
        return 0
    if not (token_present or _COMMIT_OR_PR.search(raw)):
        return 0  # not a commit or PR command: nothing to bind, nothing to count
    try:
        from llm_router.text_job_bind import engine
    except Exception:  # noqa: BLE001 -- no package: still never let a placeholder through
        if token_present:
            chan.deny(_NOT_IMPORTABLE)
        return 0
    if token_present and script:
        _arm_deadline(chan, payload)
    decision = engine.decide(payload, started=_HOOK_T0)
    if decision.kind == "rewrite" and decision.tool_input is not None:
        chan.rewrite(decision.tool_input, decision.note)
    elif decision.kind == "deny":
        chan.deny(decision.reason or _HOOK_FAILED)
    return 0


def main(raw: str | None = None, *, out=None, fd_guard: bool = False) -> int:
    """Run the hook. ``raw`` is the stdin text (read from stdin when ``None``); ``out`` is where the
    decision goes (the private stdout when ``None``). ``fd_guard`` is for the script entry only: it
    repoints file descriptor 1 for the rest of the process and arms the deadline guard. In-process
    callers get their stdout back."""
    saved = sys.stdout
    chan = _Channel(out if out is not None else (_private_stdout() if fd_guard else saved))
    sys.stdout = sys.stderr
    try:
        text = raw if raw is not None else sys.stdin.read()
        try:
            return _run(text, chan, script=fd_guard)
        except Exception:  # noqa: BLE001 -- a failure must still deny a placeholder command
            if _TOKEN.search(text or ""):
                chan.deny(_HOOK_FAILED)
            return 0
    finally:
        if not fd_guard:
            sys.stdout = saved


def entry() -> None:
    """The script entry: descriptor guard on, decision written, then a normal exit (so the latency
    recorder's exit write runs) that a watchdog turns into a hard one if a library thread lingers."""
    os.environ.update(LITELLM_LOG="ERROR")
    code = main(fd_guard=True)
    sys.stderr.flush()
    _daemon_timer(_EXIT_GRACE_SEC, lambda: os._exit(0))
    sys.exit(code)


if __name__ == "__main__":
    entry()
