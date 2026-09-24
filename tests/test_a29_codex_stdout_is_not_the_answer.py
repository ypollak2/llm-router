"""The Codex adapter returned the CLI's stdin banner as the model's answer.

Observed live 2026-09-23/24: two routed calls to `codex/gpt-4o-mini` each
returned, after ~20s, the single line::

    Reading additional input from stdin...

reported by the router as a successful 70-token completion. Reproduced exactly
with the adapter's own argv::

    $ codex exec --json -m gpt-4o-mini -c model_provider=openai \\
          --color never --skip-git-repo-check -C . "reply with pong" </dev/null
    Reading additional input from stdin...
    {"type":"thread.started", ...}
    {"type":"item.completed","item":{"type":"error",
        "message":"Model metadata for `gpt-4o-mini` not found. ..."}}
    {"type":"turn.completed","usage":{"input_tokens":0,"output_tokens":0}}

Three defects, two of them fixed here:

1. `except json.JSONDecodeError: text_chunks.append(line)` — ANY non-JSON line
   on stdout became model output. `codex exec --json` emits JSONL; a non-JSON
   line is the CLI talking.
2. An `item.completed` whose item is `{"type": "error"}` carries `message`, not
   `text`, so the old code read `""` and dropped it in silence. A model the CLI
   does not know looked like an empty success.
3. NOT fixed here: `codex/gpt-4o-mini` should not be in a routing chain at all —
   the CLI has no metadata for it and the turn used 0 tokens. That is the
   capability-filter gap (architecture/GAP_ANALYSIS.md §10), not an adapter bug.
"""

from __future__ import annotations

import asyncio
import json

from llm_router import codex_agent


class _FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self):
        async def _gen():
            for line in self._lines:
                yield line
        return _gen()


class _FakeProc:
    returncode = 0

    def __init__(self, stdout_lines: list[bytes], stderr_lines: list[bytes]) -> None:
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)

    async def wait(self):
        return 0

    def kill(self):  # pragma: no cover — only on timeout
        pass


def _run(monkeypatch, stdout_lines, stderr_lines=()):
    async def _fake_exec(*a, **k):
        return _FakeProc(list(stdout_lines), list(stderr_lines))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: "/usr/bin/codex")
    return asyncio.run(codex_agent.run_codex("hi", model="gpt-4o-mini"))


BANNER = b"Reading additional input from stdin...\n"
THREAD = json.dumps({"type": "thread.started", "thread_id": "x"}).encode() + b"\n"
ERROR = json.dumps({
    "type": "item.completed",
    "item": {"id": "item_0", "type": "error",
             "message": "Model metadata for `gpt-4o-mini` not found."},
}).encode() + b"\n"
DONE = json.dumps({
    "type": "turn.completed",
    "usage": {"input_tokens": 0, "output_tokens": 0},
}).encode() + b"\n"
ANSWER = json.dumps({
    "type": "item.completed", "item": {"type": "agent_message", "text": "pong"},
}).encode() + b"\n"


def test_the_stdin_banner_is_not_the_answer(monkeypatch) -> None:
    """The exact observed failure."""
    res = _run(monkeypatch, [BANNER, THREAD, ANSWER, DONE])
    assert res.content == "pong", (
        f"CLI chatter leaked into the answer: {res.content!r}"
    )
    assert "Reading additional input" not in res.content


def test_a_model_error_is_surfaced_not_swallowed(monkeypatch) -> None:
    """An error item carries `message`, not `text` — the old code read "" ."""
    res = _run(monkeypatch, [BANNER, THREAD, ERROR, DONE])
    assert "Model metadata" in res.content, (
        f"the CLI reported an error and the adapter returned {res.content!r}"
    )
    assert res.exit_code != 0, "a reported error must not read as a success"


def test_an_error_does_not_return_the_banner(monkeypatch) -> None:
    """The regression in full: error path AND chatter present together.

    Without both fixes this returns "Reading additional input from stdin...",
    which is what the router recorded as a 70-token completion.
    """
    res = _run(monkeypatch, [BANNER, THREAD, ERROR, DONE])
    assert "Reading additional input" not in res.content


def test_a_normal_answer_still_works(monkeypatch) -> None:
    """Anti-vacuity: an adapter that returns nothing passes every test above."""
    res = _run(monkeypatch, [THREAD, ANSWER, DONE])
    assert res.content == "pong"
    assert res.exit_code == 0


def test_chatter_is_kept_as_a_last_resort(monkeypatch) -> None:
    """Dropped from the ANSWER, not from the diagnostics.

    When the CLI produces nothing else, its chatter is the only evidence of
    what happened, and silence would be worse than noise.
    """
    res = _run(monkeypatch, [BANNER])
    assert "Reading additional input" in res.content


def test_stderr_still_beats_chatter(monkeypatch) -> None:
    """Order of fallbacks: real output > reported error > stderr > chatter."""
    res = _run(monkeypatch, [BANNER], [b"codex: authentication failed\n"])
    assert "authentication failed" in res.content
