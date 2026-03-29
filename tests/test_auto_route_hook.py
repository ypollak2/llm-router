"""Tests for the auto-route UserPromptSubmit hook."""

import json
import subprocess
import sys

HOOK_PATH = ".claude/hooks/auto-route.py"


def run_hook(prompt: str) -> dict | None:
    """Run the hook script with a prompt and return parsed output."""
    payload = json.dumps({"prompt": prompt})
    result = subprocess.run(
        [sys.executable, HOOK_PATH],
        input=payload,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def _extract_hint(output: dict) -> str:
    return output["hookSpecificOutput"]["contextForAgent"]


class TestAutoRouteClassification:
    def test_research_prompt(self):
        out = run_hook("Research the latest AI trends for 2026")
        assert out is not None
        hint = _extract_hint(out)
        assert "[ROUTE: research/" in hint
        assert "llm_research" in hint

    def test_generate_prompt(self):
        out = run_hook("Write a blog post about machine learning")
        assert out is not None
        hint = _extract_hint(out)
        assert "[ROUTE: generate/" in hint
        assert "llm_generate" in hint

    def test_analyze_prompt(self):
        out = run_hook("Analyze the pros and cons of microservices vs monolith")
        assert out is not None
        hint = _extract_hint(out)
        assert "[ROUTE: analyze/" in hint
        assert "llm_analyze" in hint

    def test_code_prompt(self):
        out = run_hook("Implement a binary search tree in Python")
        assert out is not None
        hint = _extract_hint(out)
        assert "[ROUTE: code/" in hint
        assert "llm_code" in hint

    def test_image_prompt(self):
        out = run_hook("Generate an image of a futuristic city at night")
        assert out is not None
        hint = _extract_hint(out)
        assert "[ROUTE: image/" in hint
        assert "llm_image" in hint

    def test_complex_code_prompt(self):
        out = run_hook(
            "Design a comprehensive distributed task queue system from scratch "
            "with Redis backend, worker pools, and dead letter handling"
        )
        assert out is not None
        hint = _extract_hint(out)
        assert "complex" in hint

    def test_simple_query(self):
        out = run_hook("What is the quick definition of REST API?")
        assert out is not None
        hint = _extract_hint(out)
        assert "simple" in hint


class TestAutoRouteSkips:
    def test_local_file_edit(self):
        assert run_hook("fix the bug in server.py") is None

    def test_git_command(self):
        assert run_hook("git push origin main") is None

    def test_short_greeting(self):
        assert run_hook("hello") is None

    def test_empty_prompt(self):
        assert run_hook("") is None

    def test_install_command(self):
        assert run_hook("install the new dependency") is None

    def test_run_tests(self):
        assert run_hook("run the tests for classifier") is None

    def test_read_file(self):
        assert run_hook("read the config.py file") is None
