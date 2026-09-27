"""Regression: a "successful" completion whose only content is CLI status
chatter must be reported as a FAILED attempt, so the router falls back to
the next model in the chain.

Observed live 2026-09-26: ``llm(task="analyze", ...)`` routed to
``codex/gpt-5.5`` and returned, as the model's "answer", only the banner
Codex CLI prints while it is waiting on stdin::

    Reading additional input from stdin...

reported as a *successful* 205-token, $0, 22.9s completion (dashboard/router
side estimates tokens from ``len(content)``, so chatter masquerading as
content produces a plausible-looking token count).

``tests/test_a29_codex_stdout_is_not_the_answer.py`` (2026-09-23/24) already
fixed two adjacent defects -- a non-JSON stdout line no longer becomes
``text_chunks``, and an ``item.completed`` error item is no longer silently
dropped -- and *deliberately* kept the CLI chatter as a last-resort
diagnostic in ``content`` when nothing else arrived (see
``test_chatter_is_kept_as_a_last_resort`` /
``test_stderr_still_beats_chatter``). That was the right call for
diagnostics, but ``run_codex`` still returned ``exit_code=proc.returncode or
0`` on that path -- 0, a success -- so ``CodexResult.success`` was ``True``
and the router (``router.py:2903``, ``if not codex_result.success: raise
...``) never triggered its fallback. This file pins that ``exit_code``/
``success`` must flip to failure whenever no real ``item.completed`` text
ever arrived, while the chatter/stderr text stays visible in ``content`` for
debugging (unchanged, and re-asserted here so a future fix cannot regress
the diagnostic by going back to dropping it).
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
    return asyncio.run(codex_agent.run_codex("hi", model="gpt-5.5"))


BANNER = b"Reading additional input from stdin...\n"
THREAD = json.dumps({"type": "thread.started", "thread_id": "x"}).encode() + b"\n"
DONE = json.dumps({
    "type": "turn.completed",
    "usage": {"input_tokens": 0, "output_tokens": 0},
}).encode() + b"\n"
ANSWER = json.dumps({
    "type": "item.completed", "item": {"type": "agent_message", "text": "pong"},
}).encode() + b"\n"


def test_stdout_only_banner_is_a_failed_attempt(monkeypatch) -> None:
    """Only the stdin banner arrived on stdout -- no thread/turn events even.

    The content must still surface the banner for diagnostics (unchanged
    from the 2026-09-23/24 fix), but the completion itself is not a success:
    nothing the model said is in it.
    """
    res = _run(monkeypatch, [BANNER])
    assert "Reading additional input" in res.content, (
        "the diagnostic chatter must still be visible in content"
    )
    assert res.success is False, (
        f"a reply that is only CLI status chatter must not read as success "
        f"(got exit_code={res.exit_code}, content={res.content!r})"
    )
    assert res.exit_code != 0


def test_banner_plus_empty_turn_is_a_failed_attempt(monkeypatch) -> None:
    """The exact live shape: banner, thread.started, turn.started, an empty
    turn.completed -- no item.completed ever arrives."""
    turn_started = json.dumps({"type": "turn.started"}).encode() + b"\n"
    res = _run(monkeypatch, [BANNER, THREAD, turn_started, DONE])
    assert res.success is False, (
        f"an empty turn dressed as exit_code 0 must fall back, not complete "
        f"(got exit_code={res.exit_code}, content={res.content!r})"
    )


def test_stderr_only_is_also_a_failed_attempt(monkeypatch) -> None:
    """stderr chatter with no stdout answer at all: still a failure."""
    res = _run(monkeypatch, [], [b"codex: authentication failed\n"])
    assert "authentication failed" in res.content
    assert res.success is False


def test_a_real_answer_is_still_a_success(monkeypatch) -> None:
    """Anti-vacuity: a genuine item.completed text must still succeed even
    with chatter alongside it."""
    res = _run(monkeypatch, [BANNER, THREAD, ANSWER, DONE])
    assert res.content == "pong"
    assert res.success is True
    assert res.exit_code == 0
