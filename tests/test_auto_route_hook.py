"""Tests for the auto-route UserPromptSubmit hook."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"


def _hook_env(home_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["LLM_ROUTER_DISABLE_LLM_CLASSIFIERS"] = "1"
    env["OPENAI_API_KEY"] = ""
    env["GEMINI_API_KEY"] = ""
    env["GOOGLE_API_KEY"] = ""
    return env


def run_hook(prompt: str, session_id: str | None = None, home_dir: Path | None = None) -> dict | None:
    """Run the hook script with a prompt and return parsed output."""
    with tempfile.TemporaryDirectory(prefix="llm-router-hook-test-") as tmp_home:
        effective_home = home_dir or Path(tmp_home)
        payload = json.dumps({"prompt": prompt, "session_id": session_id or ""})
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=payload,
            capture_output=True,
            text=True,
            env=_hook_env(effective_home),
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout)


def run_hook_with_last_route(
    prompt: str,
    last_task_type: str,
    last_complexity: str = "moderate",
    last_tool: str = "llm_code",
) -> dict | None:
    """Run the hook with a pre-seeded last_route file to test context inheritance."""
    with tempfile.TemporaryDirectory(prefix="llm-router-hook-test-") as tmp_home:
        home_dir = Path(tmp_home)
        session_id = f"test-session-{int(time.time() * 1000)}"
        router_dir = home_dir / ".llm-router"
        router_dir.mkdir(parents=True, exist_ok=True)
        last_route_path = router_dir / f"last_route_{session_id}.json"
        try:
            last_route_path.write_text(json.dumps({
                "task_type": last_task_type,
                "complexity": last_complexity,
                "tool": last_tool,
                "saved_at": time.time(),
            }))
            return run_hook(prompt, session_id=session_id, home_dir=home_dir)
        finally:
            last_route_path.unlink(missing_ok=True)


def _extract_hint(output: dict) -> str:
    return output["hookSpecificOutput"]["contextForAgent"]


GOLDEN_ROUTE_CASES = [
    ("What does os.path.join do?", "query", "simple", "llm_query"),
    ("What is the quick definition of REST API?", "query", "simple", "llm_query"),
    ("Explain what a foreign key is in SQL.", "query", "simple", "llm_query"),
    ("Summarize how database indexes work", "query", "simple", "llm_query"),
    ("Write a hero section for our site", "generate", "moderate", "llm_generate"),
    ("Draft a launch email for our new pricing page", "generate", "moderate", "llm_generate"),
    ("Brainstorm three taglines for a coffee delivery app", "generate", "moderate", "llm_generate"),
    ("Create onboarding copy for the welcome modal", "generate", "moderate", "llm_generate"),
    ("Draft FAQ answers for our pricing page", "generate", "moderate", "llm_generate"),
    ("Analyze the pros and cons of microservices vs monolith", "analyze", "moderate", "llm_analyze"),
    ("Compare Redis Streams and RabbitMQ for background jobs", "analyze", "moderate", "llm_analyze"),
    ("Evaluate whether feature flags fit this rollout plan", "analyze", "moderate", "llm_analyze"),
    ("Compare Postgres logical replication and CDC tools", "analyze", "moderate", "llm_analyze"),
    ("Research latest AI funding rounds", "research", "moderate", "llm_research"),
    ("Research the latest SOC 2 automation vendors", "research", "moderate", "llm_research"),
    ("Find the latest OpenAI pricing changes", "research", "moderate", "llm_research"),
    ("Research today's changes in EU AI regulation", "research", "moderate", "llm_research"),
    ("Refactor the caching middleware to support TTLs", "code", "moderate", "llm_code"),
    ("Add OAuth login support to the dashboard", "code", "moderate", "llm_code"),
    ("Refactor the billing service into smaller modules", "code", "moderate", "llm_code"),
    ("Update the API client to retry on 429 responses", "code", "moderate", "llm_code"),
    (
        "Architect a distributed task queue with retries and dead letter handling",
        "code",
        "complex",
        "llm_code",
    ),
    ("Generate an image of a futuristic city at night", "image", "moderate", "llm_image"),
    ("Generate a logo mockup for a travel startup", "image", "moderate", "llm_image"),
]


@pytest.mark.parametrize(
    "prompt,expected_task,expected_complexity,expected_tool",
    GOLDEN_ROUTE_CASES,
)
def test_golden_prompt_routing_matrix(
    prompt: str,
    expected_task: str,
    expected_complexity: str,
    expected_tool: str,
):
    out = run_hook(prompt)
    assert out is not None
    hint = _extract_hint(out)
    # Check for the task/complexity pair (may be wrapped with sparkles ✨)
    assert f"{expected_task}/{expected_complexity}" in hint
    assert expected_tool in hint


class TestAutoRouteClassification:
    def test_research_prompt(self):
        out = run_hook("Research the latest AI trends for 2026")
        assert out is not None
        hint = _extract_hint(out)
        assert "research/" in hint
        assert "llm_research" in hint

    def test_generate_prompt(self):
        out = run_hook("Write a blog post about machine learning")
        assert out is not None
        hint = _extract_hint(out)
        assert "generate/" in hint
        assert "llm_generate" in hint

    def test_analyze_prompt(self):
        out = run_hook("Analyze the pros and cons of microservices vs monolith")
        assert out is not None
        hint = _extract_hint(out)
        assert "analyze/" in hint
        assert "llm_analyze" in hint

    def test_code_prompt(self):
        out = run_hook("Implement a binary search tree in Python")
        assert out is not None
        hint = _extract_hint(out)
        assert "code/" in hint
        assert "llm_code" in hint

    def test_image_prompt(self):
        out = run_hook("Generate an image of a futuristic city at night")
        assert out is not None
        hint = _extract_hint(out)
        assert "image/" in hint
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
        assert "code/" in hint
        assert "llm_code" in hint

    def test_code_modify_prompt(self):
        out = run_hook("modify the authentication middleware to support OAuth")
        assert out is not None
        hint = _extract_hint(out)
        assert "code/" in hint

    def test_code_enhance_prompt(self):
        out = run_hook("enhance the caching layer for better throughput")
        assert out is not None
        hint = _extract_hint(out)
        # "enhance" is ambiguous — classifiers may route to code, analyze, or generate
        assert "code/" in hint or "analyze/" in hint or "generate/" in hint

    def test_ollama_or_api_fallback_for_unmatched_long_prompt(self):
        """Prompts with no strong heuristic match fall to Ollama/API classification."""
        out = run_hook("I need help with something interesting today")
        assert out is not None
        hint = _extract_hint(out)
        # Should be classified by Ollama or API (not auto fallback)
        assert "ROUTE:" in hint
        assert "via " in hint


class TestAutoRouteSkips:
    """Only truly local system commands should be skipped (v7.5.0 aggressive routing)."""

    def test_git_command(self):
        # v7.5.0: git commands are now routed as coordination tasks for cheap classification
        out = run_hook("git push origin main")
        if out is not None:
            assert "coordination" in _extract_hint(out).lower() or "llm_" in _extract_hint(out)

    def test_short_greeting(self):
        assert run_hook("hello") is None

    def test_empty_prompt(self):
        assert run_hook("") is None

    def test_read_file(self):
        # "read the config.py file" is now routed (query) rather than skipped
        out = run_hook("read the config.py file")
        if out is not None:
            assert "llm_" in _extract_hint(out)

    def test_slash_command(self):
        assert run_hook("/help") is None

    def test_npm_command(self):
        # v7.5.0: npm commands are now routed as coordination tasks for cheap classification
        out = run_hook("npm install express")
        if out is not None:
            assert "llm_" in _extract_hint(out)

    def test_pip_command(self):
        # v7.5.0: pip commands are now routed as coordination tasks for cheap classification
        out = run_hook("pip install requests")
        if out is not None:
            assert "llm_" in _extract_hint(out)

    def test_commit_command(self):
        # "commit these changes" is now routed (code) rather than skipped
        out = run_hook("commit these changes")
        if out is not None:
            assert "llm_" in _extract_hint(out)

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
        assert "code/" in hint

    def test_update_logic_routes(self):
        out = run_hook("update the authentication logic to use JWT tokens")
        assert out is not None
        hint = _extract_hint(out)
        assert "code/" in hint

    def test_improve_performance_routes(self):
        out = run_hook("improve the database query performance")
        assert out is not None
        hint = _extract_hint(out)
        # Could be code or analyze — both valid for "improve performance"
        assert "code/" in hint or "analyze/" in hint


class TestShortCodeFollowup:
    """Short follow-ups after code tasks inherit code classification."""

    def test_short_followup_after_code_inherits_code(self):
        """6-15 word follow-up after a code task → code-context-inherit."""
        # 6 words — escapes _is_continuation (≤5 words) but within the 15-word threshold
        out = run_hook_with_last_route(
            "please go ahead and do the change now",
            last_task_type="code",
        )
        assert out is not None
        hint = _extract_hint(out)
        assert "llm_code" in hint
        assert "code-context-inherit" in hint

    def test_explain_why_followup_after_code_inherits_code(self):
        """Classic misclassification: short explain-why after editing code."""
        out = run_hook_with_last_route(
            "explain why the dashboard doesn't update",
            last_task_type="code",
        )
        assert out is not None
        hint = _extract_hint(out)
        assert "llm_code" in hint
        assert "code-context-inherit" in hint

    def test_short_followup_after_generate_does_not_inherit_code(self):
        """Short follow-ups after non-code tasks are NOT treated as code."""
        out = run_hook_with_last_route(
            "do the change",
            last_task_type="generate",
            last_tool="llm_generate",
        )
        # Should classify normally — not inherit as code
        if out is not None:
            hint = _extract_hint(out)
            assert "code-context-inherit" not in hint

    def test_long_followup_after_code_does_not_inherit(self):
        """Follow-ups >15 words go through the full classifier, not context-inherit."""
        long_prompt = (
            "can you explain in detail why the routing hook misclassifies "
            "short follow-up prompts as generate instead of code tasks"
        )
        assert len(long_prompt.split()) > 15
        out = run_hook_with_last_route(long_prompt, last_task_type="code")
        if out is not None:
            hint = _extract_hint(out)
            assert "code-context-inherit" not in hint

    def test_stale_last_route_not_inherited(self):
        """Expired last_route (>30 min) is not used for context inheritance."""
        with tempfile.TemporaryDirectory(prefix="llm-router-hook-test-") as tmp_home:
            home_dir = Path(tmp_home)
            session_id = f"test-stale-{int(time.time() * 1000)}"
            router_dir = home_dir / ".llm-router"
            router_dir.mkdir(parents=True, exist_ok=True)
            last_route_path = router_dir / f"last_route_{session_id}.json"
            try:
                last_route_path.write_text(json.dumps({
                    "task_type": "code",
                    "complexity": "moderate",
                    "tool": "llm_code",
                    "saved_at": time.time() - 3700,  # >30 min ago
                }))
                out = run_hook("do the change", session_id=session_id, home_dir=home_dir)
                if out is not None:
                    hint = _extract_hint(out)
                    assert "code-context-inherit" not in hint
            finally:
                last_route_path.unlink(missing_ok=True)
