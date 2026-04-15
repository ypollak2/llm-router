"""HTTP client for hooks to communicate with sidecar service.

Thin wrapper that handles classification requests with timeouts and fallback.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("llm-router.hook-client")

SERVICE_PORT = int(os.environ.get("LLM_ROUTER_SERVICE_PORT", "7337"))
SERVICE_URL = f"http://127.0.0.1:{SERVICE_PORT}"


def classify_prompt(prompt: str, session_id: str = "", context: dict | None = None) -> dict | None:
    """Classify a prompt via sidecar service.
    
    Returns classification response dict or None on error/timeout.
    On any error, returns None (hook will allow unconditionally).
    """
    if context is None:
        context = {}
    
    try:
        payload = json.dumps({
            "prompt": prompt,
            "session_id": session_id,
            "context": context,
        }).encode("utf-8")
        
        req = urllib.request.Request(
            f"{SERVICE_URL}/classify",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        
        with urllib.request.urlopen(req, timeout=0.5) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        # Service unavailable or timeout — graceful degradation
        logger.warning(f"Service unavailable ({type(e).__name__}): allowing unconditionally")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    
    # Graceful degradation: allow everything if service unavailable
    return None


def score_models(models: list[str], task_type: str, complexity: str) -> list[str]:
    """Score and rank models via sidecar service.
    
    Requests the /score endpoint to compute composite quality scores for each model,
    then returns a ranked list (best-first). Falls back gracefully to original order
    if service unavailable or request fails.
    
    Args:
        models: List of LiteLLM model IDs to score
        task_type: Task type (query, code, research, generate, analyze)
        complexity: Complexity level (simple, moderate, complex)
    
    Returns:
        Ranked list of model IDs (best-first), or original list on error.
    """
    if not models:
        return []
    
    try:
        payload = json.dumps({
            "task_type": task_type,
            "complexity": complexity,
            "models": models,
        }).encode("utf-8")
        
        req = urllib.request.Request(
            f"{SERVICE_URL}/score",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                ranked = data.get("ranked_models", models)
                return ranked if isinstance(ranked, list) else models
    
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        # Service unavailable or timeout — graceful degradation
        logger.warning(f"Score service unavailable ({type(e).__name__}): using original order")
    except Exception as e:
        logger.error(f"Unexpected error in score_models: {e}", exc_info=True)
    
    # Graceful degradation: return original order
    return models
