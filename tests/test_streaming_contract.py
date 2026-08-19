"""Contract tests: Codex and Gemini CLI must emit streaming events via on_event callback."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


def _load_expectations() -> dict:
    local = Path(__file__).parent / "fixtures" / "routing_expectations.local.yaml"
    default = Path(__file__).parent / "fixtures" / "routing_expectations.example.yaml"
    path = local if local.exists() else default
    with open(path) as f:
        return yaml.safe_load(f)


EXPECTATIONS = _load_expectations()
STREAMING = EXPECTATIONS["streaming"]


# ── Codex streaming ───────────────────────────────────────────────────────────

class TestCodexStreaming:
    @pytest.mark.asyncio
    async def test_codex_calls_on_event_for_item_completed(self) -> None:
        """run_codex must call on_event('item.completed', text) for each JSONL item."""
        import json

        from llm_router.codex_agent import run_codex

        jsonl_events = [
            json.dumps({"type": "thread.started", "thread_id": "abc"}).encode() + b"\n",
            json.dumps({"type": "turn.started"}).encode() + b"\n",
            json.dumps({
                "type": "item.completed",
                "item": {"id": "i0", "type": "agent_message", "text": "pong"},
            }).encode() + b"\n",
            json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 1},
            }).encode() + b"\n",
        ]

        received_events: list[tuple[str, str]] = []

        async def _on_event(ev_type: str, text: str) -> None:
            received_events.append((ev_type, text))

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        async def _fake_stdout():
            for chunk in jsonl_events:
                yield chunk

        async def _fake_stderr():
            return
            yield  # make it an async generator

        mock_proc.stdout = _fake_stdout()
        mock_proc.stderr = _fake_stderr()

        async def _fake_wait():
            pass

        mock_proc.wait = _fake_wait

        with (
            patch("llm_router.codex_agent.find_codex_binary", return_value="/usr/bin/codex"),
            patch("llm_router.codex_agent.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("llm_router.safe_subprocess.get_safe_env", return_value={}),
        ):
            result = await run_codex("say pong", on_event=_on_event)

        assert result.content == "pong"
        event_types = [e[0] for e in received_events]
        assert "item.completed" in event_types
        # item text was forwarded
        item_texts = [t for et, t in received_events if et == "item.completed"]
        assert any("pong" in t for t in item_texts)

    @pytest.mark.asyncio
    async def test_codex_run_succeeds_without_on_event(self) -> None:
        """on_event is optional — run_codex must work without it."""
        import json

        from llm_router.codex_agent import run_codex

        jsonl_events = [
            json.dumps({"type": "item.completed", "item": {"text": "hello"}}).encode() + b"\n",
        ]

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        async def _stdout():
            for c in jsonl_events:
                yield c

        async def _stderr():
            return
            yield

        mock_proc.stdout = _stdout()
        mock_proc.stderr = _stderr()

        async def _wait():
            pass

        mock_proc.wait = _wait

        with (
            patch("llm_router.codex_agent.find_codex_binary", return_value="/usr/bin/codex"),
            patch("llm_router.codex_agent.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("llm_router.safe_subprocess.get_safe_env", return_value={}),
        ):
            result = await run_codex("say hello")  # no on_event

        assert result.content == "hello"
        assert result.exit_code == 0


# ── Gemini CLI streaming ───────────────────────────────────────────────────────

class TestGeminiCLIStreaming:
    @pytest.mark.asyncio
    async def test_gemini_calls_on_event_for_each_line(self) -> None:
        """run_gemini_cli must call on_event('line', text) for each output line."""
        from llm_router.gemini_cli_agent import run_gemini_cli

        lines = [b"Paris is the capital\n", b"of France.\n"]

        received: list[tuple[str, str]] = []

        async def _on_event(ev_type: str, text: str) -> None:
            received.append((ev_type, text))

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        async def _stdout():
            for line in lines:
                yield line

        async def _stderr():
            return
            yield

        mock_proc.stdout = _stdout()
        mock_proc.stderr = _stderr()

        async def _wait():
            pass

        mock_proc.wait = _wait

        with (
            patch("llm_router.gemini_cli_agent.find_gemini_binary", return_value="/usr/bin/gemini"),
            patch("llm_router.gemini_cli_agent.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("llm_router.safe_subprocess.get_safe_env", return_value={}),
        ):
            result = await run_gemini_cli("capital of France?", on_event=_on_event)

        assert "Paris" in result.content
        assert all(et == "line" for et, _ in received)
        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_gemini_run_succeeds_without_on_event(self) -> None:
        """on_event is optional — run_gemini_cli must work without it."""
        from llm_router.gemini_cli_agent import run_gemini_cli

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        async def _stdout():
            yield b"42\n"

        async def _stderr():
            return
            yield

        mock_proc.stdout = _stdout()
        mock_proc.stderr = _stderr()

        async def _wait():
            pass

        mock_proc.wait = _wait

        with (
            patch("llm_router.gemini_cli_agent.find_gemini_binary", return_value="/usr/bin/gemini"),
            patch("llm_router.gemini_cli_agent.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("llm_router.safe_subprocess.get_safe_env", return_value={}),
        ):
            result = await run_gemini_cli("what is 6x7?")

        assert result.content == "42"
        assert result.exit_code == 0
