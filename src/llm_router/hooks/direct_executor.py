"""Direct model execution — call LLMs via HTTP without Claude orchestration.

This module is used by auto-route.py to call models directly from the
UserPromptSubmit hook, returning responses via {"decision": "block"} so
Claude never sees the prompt (0 subscription tokens consumed).

Supports: Ollama (local), Gemini (API), OpenAI (API), Codex (local).
Each call function uses urllib.request (stdlib only — no dependencies).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    """A model in the routing chain."""
    provider: str      # ollama, gemini, openai, codex
    model: str         # e.g. "qwen3.5:latest", "gemini-2.5-flash"
    quota_cost: float = 0.0  # 0 for free/paid-API, >0 for subscription models


@dataclass(frozen=True)
class DirectResult:
    """Result of a direct model call."""
    text: str
    model: ModelSpec
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0


# ── System Prompts ────────────────────────────────────────────────────────────

DIRECT_SYSTEM_PROMPT = """\
You are an AI assistant operating within the llm-router system, providing a direct response to a user of Claude Code.
Your primary goal is to provide a helpful, accurate, and concise response to the user's request.

Guidelines:
1. Be concise and get straight to the point.
2. Use standard Markdown for formatting (code blocks, bold, lists).
3. Do not include unnecessary conversational filler or meta-commentary about being an AI.
4. Your response will be displayed directly in the user's terminal.
"""


# ── Provider HTTP calls ──────────────────────────────────────────────────────

def _get_ollama_url() -> str:
    """Get Ollama base URL, reading env at call time (after dotenv is loaded)."""
    url = os.environ.get("LLM_ROUTER_OLLAMA_URL") or \
          os.environ.get("OLLAMA_BASE_URL") or \
          "http://localhost:11434"
    return url


def call_ollama(prompt: str, model: str, timeout: int = 15) -> str | None:
    """Call Ollama's /api/chat endpoint. Returns response text or None."""
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": DIRECT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.3, "num_predict": 2048},
    }).encode()
    ollama_url = _get_ollama_url()
    req = urllib.request.Request(
        f"{ollama_url}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
            msg = result.get("message", {})
            content = msg.get("content", "")
            # Some models (qwen3.5) put response in thinking field when content is empty
            if not content.strip() and msg.get("thinking"):
                content = msg["thinking"]
            
            # Capture usage metrics if available
            usage = {
                "input_tokens": result.get("prompt_eval_count", 0),
                "output_tokens": result.get("eval_count", 0),
            }
            return content, usage
    except Exception:
        return None, {}


def call_gemini(prompt: str, model: str = "gemini-2.5-flash", timeout: int = 15) -> tuple[str | None, dict]:
    """Call Gemini API. Returns (response text, usage dict) or (None, {})."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None, {}
    # Gemini 1.5+ supports system_instruction
    body = json.dumps({
        "system_instruction": {"parts": [{"text": DIRECT_SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048},
    }).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
            content = result["candidates"][0]["content"]["parts"][0]["text"]
            usage = result.get("usageMetadata", {})
            return content, {
                "input_tokens": usage.get("promptTokenCount", 0),
                "output_tokens": usage.get("candidatesTokenCount", 0),
            }
    except Exception:
        return None, {}


def call_openai(prompt: str, model: str = "gpt-4o-mini", timeout: int = 15) -> tuple[str | None, dict]:
    """Call OpenAI chat completions API. Returns (response text, usage dict) or (None, {})."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None, {}
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": DIRECT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 2048,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"]
            usage = result.get("usage", {})
            return content, {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            }
    except Exception:
        return None, {}


# ── Quality Gate ─────────────────────────────────────────────────────────────

def quality_ok(response: str, task_type: str) -> bool:
    """Basic quality gate — reject garbage responses before returning to user."""
    if not response or len(response.strip()) < 10:
        return False
    # Model refused or is confused
    refusal_phrases = ("i cannot", "i can't", "as an ai", "i don't have")
    lower = response.lower()
    if sum(1 for p in refusal_phrases if p in lower) >= 2:
        return False
    return True


# ── Chain Executor ───────────────────────────────────────────────────────────

_PROVIDER_CALLS = {
    "ollama": lambda prompt, model, timeout: call_ollama(prompt, model, timeout),
    "gemini": lambda prompt, model, timeout: call_gemini(prompt, model, timeout),
    "openai": lambda prompt, model, timeout: call_openai(prompt, model, timeout),
}


def execute_chain(
    prompt: str,
    chain: list[ModelSpec],
    task_type: str,
    timeout: int = 15,
) -> DirectResult | None:
    """Try each model in the chain until one returns a quality response.

    Skips models whose provider is 'claude' - those cannot be called directly
    from the hook. The caller decides whether failure falls through or blocks.

    Returns DirectResult on success, None if all models failed or only Claude remains.
    """
    for model in chain:
        if model.provider == "claude":
            continue  # Can't call Claude from the hook — skip

        call_fn = _PROVIDER_CALLS.get(model.provider)
        if not call_fn:
            continue

        t0 = time.monotonic()
        try:
            response, usage = call_fn(prompt, model.model, timeout)
        except Exception:
            continue

        if response and quality_ok(response, task_type):
            latency_ms = int((time.monotonic() - t0) * 1000)
            return DirectResult(
                text=response,
                model=model,
                latency_ms=latency_ms,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )

    return None  # All non-Claude models failed; the caller selects failover policy.


# ── Agent Loop Execution (for file-op tasks) ─────────────────────────────────

def execute_agent(
    prompt: str,
    chain: list[ModelSpec],
    project_root: str | None = None,
    timeout: int = 60,
) -> DirectResult | None:
    """Run a tool-calling agent loop for tasks that need file operations.

    Unlike execute_chain (text-in/text-out), this gives the model access to
    read_file, edit_file, write_file, search_files, list_files, and run_command.

    Only Ollama models support tool calling from the hook. Other providers
    are skipped (they'd need their own tool-calling protocol).

    Returns DirectResult on success, None if all models failed.
    """
    from pathlib import Path as _Path

    try:
        from llm_router.hooks.agent_loop import run_agent_loop
    except ImportError:
        return None

    if project_root:
        root = _Path(project_root)
    else:
        root = _Path.cwd()

    for model in chain:
        if model.provider != "ollama":
            continue  # Only Ollama supports tool calling from the hook (for now)

        t0 = time.monotonic()
        # run_agent_loop might need to return usage as well
        # For now, we'll just capture the response
        response = run_agent_loop(
            prompt=prompt,
            model=model.model,
            project_root=root,
            timeout_per_call=timeout,
        )

        if response and quality_ok(response, "code"):
            latency_ms = int((time.monotonic() - t0) * 1000)
            return DirectResult(
                text=response,
                model=model,
                latency_ms=latency_ms,
            )

    return None
