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

    def test_fix_bug_with_intermediate_words(self):
        """Regression: 'fix the authentication bug' must match CODE_PATTERNS."""
        out = run_hook("fix the authentication bug in the login flow")
        assert out is not None
        hint = _extract_hint(out)
        assert "[ROUTE: code/" in hint
        assert "llm_code" in hint

    def test_code_modify_prompt(self):
        out = run_hook("modify the authentication middleware to support OAuth")
        assert out is not None
        hint = _extract_hint(out)
        assert "[ROUTE: code/" in hint

    def test_code_enhance_prompt(self):
        out = run_hook("enhance the caching layer for better throughput")
        assert out is not None
        hint = _extract_hint(out)
        # "enhance" is ambiguous — classifiers may route to code, analyze, or generate
        assert "[ROUTE: code/" in hint or "[ROUTE: analyze/" in hint or "[ROUTE: generate/" in hint

    def test_ollama_or_api_fallback_for_unmatched_long_prompt(self):
        """Prompts with no strong heuristic match fall to Ollama/API classification."""
        out = run_hook("I need help with something interesting today")
        assert out is not None
        hint = _extract_hint(out)
        # Should be classified by Ollama or API (not auto fallback)
        assert "[ROUTE:" in hint
        assert "via " in hint


class TestAutoRouteSkips:
    """Only truly mechanical shell/git/filesystem operations should be skipped."""

    def test_git_command(self):
        assert run_hook("git push origin main") is None

    def test_short_greeting(self):
        assert run_hook("hello") is None

    def test_empty_prompt(self):
        assert run_hook("") is None

    def test_read_file(self):
        assert run_hook("read the config.py file") is None

    def test_slash_command(self):
        assert run_hook("/help") is None

    def test_npm_command(self):
        assert run_hook("npm install express") is None

    def test_pip_command(self):
        assert run_hook("pip install requests") is None

    def test_commit_command(self):
        assert run_hook("commit these changes") is None

    def test_short_ambiguous(self):
        """Short prompts (<10 chars) are skipped."""
        assert run_hook("ok cool") is None


class TestAutoRouteNowRoutes:
    """Tasks that the OLD hook skipped but the NEW hook correctly routes."""

    def test_fix_bug_routes(self):
        """'fix the bug in server.py' is a code task, not a local skip."""
        out = run_hook("fix the bug in server.py")
        assert out is not None
        hint = _extract_hint(out)
        assert "[ROUTE: code/" in hint

    def test_update_logic_routes(self):
        out = run_hook("update the authentication logic to use JWT tokens")
        assert out is not None
        hint = _extract_hint(out)
        assert "[ROUTE: code/" in hint

    def test_improve_performance_routes(self):
        out = run_hook("improve the database query performance")
        assert out is not None
        hint = _extract_hint(out)
        # Could be code or analyze — both valid for "improve performance"
        assert "[ROUTE: code/" in hint or "[ROUTE: analyze/" in hint
