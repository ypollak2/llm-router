"""Async sidecar routing service for llm-router.

Runs as independent FastAPI process on localhost:7337 (configurable).
Hooks communicate with service via HTTP, never blocking.
Service handles all classification and routing logic asynchronously.

Endpoints:
  POST /classify — classify a prompt, return routing decision (high/medium/low confidence)
  GET /health — service health check
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

# Import the existing classifier (we'll extend it)

# ────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(
            Path.home() / ".llm-router" / "service.log", mode="a"
        ),
    ],
)
logger = logging.getLogger("llm-router.service")

app = FastAPI(title="llm-router-service", version="5.3.0")

# ────────────────────────────────────────────────────────────────────────────
# Models

class ClassifyRequest(BaseModel):
    """Classification request from hook."""
    prompt: str
    session_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class ClassifyResponse(BaseModel):
    """Classification response with routing decision."""
    task_type: str  # query, code, research, generate, analyze
    complexity: str  # simple, moderate, complex
    confidence: str  # high, medium, low
    route_to: str  # llm_query, llm_code, etc.
    reasoning: str
    should_block: bool = False  # enforce-route should block (never set to True in v5.3)
    skip_routing: bool = False  # skip routing entirely for this prompt


# ────────────────────────────────────────────────────────────────────────────
# Infrastructure Detection

INFRASTRUCTURE_PATTERNS = {
    # MCP plugin tools (Serena, Obsidian, etc.)
    r"^mcp__plugin_\w+__",
    # llm-router's own tools
    r"^mcp__llm-router__",
    # System operations (never route these)
    r"^(Bash|Read|Edit|Write|MultiEdit|NotebookEdit|Glob|Grep|LS|Agent|ToolSearch)$",
}

def _is_infrastructure(tool_name: str) -> bool:
    """Check if this is an infrastructure tool that should skip routing."""
    for pattern in INFRASTRUCTURE_PATTERNS:
        if re.match(pattern, tool_name):
            return True
    return False


# ────────────────────────────────────────────────────────────────────────────
# Heuristic Classifier (fast, high-confidence)

HEURISTIC_RULES = {
    # Query/API lookups
    "query": [
        r"what (is|does|are)\s+\w+[.:]?\s*\?",
        r"how (do|does|to)\s+\w+",
        r"explain\s+\w+",
        r"documentation|api|reference",
        r"what.*mean|what.*stand for",
        r"^(when|where|why|which)\s+",
    ],
    # Code generation/implementation
    "code": [
        r"write.*code|implement|function|method",
        r"refactor|optimize|improve.*performance",
        r"fix.*bug|debug|error",
        r"add.*feature|implement.*feature",
        r"create.*class|struct|interface",
        r"^(def|class|async def|fn|func)\s+",
    ],
    # Research/current events
    "research": [
        r"latest|recent|2024|2025|2026",
        r"news|trend|market|analysis",
        r"research|study|paper|publication",
        r"current state of",
    ],
    # Writing/content generation
    "generate": [
        r"write|draft|compose|create.*article",
        r"generate|make.*copy|content",
        r"brainstorm|ideate",
        r"describe|summarize|outline",
    ],
    # Analysis/deep thinking
    "analyze": [
        r"analyze|analyze|evaluate|compare",
        r"pros and cons|trade-off|advantage",
        r"performance|efficiency|scalability",
        r"architecture|design.*decision",
    ],
}


def _heuristic_classify(prompt: str) -> tuple[str, int]:
    """Fast heuristic classification.
    
    Returns (task_type, confidence_score).
    Confidence: 1-10 scale.
    """
    prompt_lower = prompt.lower()
    
    best_match = None
    best_score = 0
    
    for task_type, patterns in HEURISTIC_RULES.items():
        for pattern in patterns:
            if re.search(pattern, prompt_lower):
                score = 8  # Heuristic match = high confidence
                if score > best_score:
                    best_score = score
                    best_match = task_type
    
    # Default to query for ambiguous
    if best_match is None:
        best_match = "query"
        best_score = 3  # Low confidence
    
    return best_match, best_score


def _complexity_from_heuristic(prompt: str, task_type: str) -> str:
    """Estimate complexity from prompt length and vocabulary."""
    words = len(prompt.split())
    
    if words < 10:
        return "simple"
    elif words < 30:
        return "moderate"
    else:
        return "complex"


# ────────────────────────────────────────────────────────────────────────────
# Confidence Scoring

def _score_confidence(task_type: str, complexity: str, heuristic_score: int) -> str:
    """Map confidence to buckets: high (>7), medium (4-7), low (<4)."""
    if heuristic_score >= 8:
        return "high"
    elif heuristic_score >= 4:
        return "medium"
    else:
        return "low"


# ────────────────────────────────────────────────────────────────────────────
# Routing Decision

def _route_for_task(task_type: str) -> str:
    """Pick the appropriate MCP tool for the task type."""
    task_to_tool = {
        "query": "llm_query",
        "code": "llm_code",
        "research": "llm_research",
        "generate": "llm_generate",
        "analyze": "llm_analyze",
    }
    return task_to_tool.get(task_type, "llm_route")


# ────────────────────────────────────────────────────────────────────────────
# Endpoints

@app.post("/classify")
async def classify_endpoint(req: ClassifyRequest) -> ClassifyResponse:
    """Classify a prompt and return routing decision.
    
    This endpoint is called by hooks via HTTP. It never blocks.
    Classification runs async; if slow, returns medium/low confidence.
    """
    try:
        # Infrastructure check (skip routing entirely)
        if _is_infrastructure(req.prompt):
            return ClassifyResponse(
                task_type="infrastructure",
                complexity="n/a",
                confidence="high",
                route_to="none",
                reasoning="Infrastructure operation — routing skipped",
                should_block=False,
                skip_routing=True,
            )
        
        # Fast heuristic classification
        task_type, heuristic_score = _heuristic_classify(req.prompt)
        complexity = _complexity_from_heuristic(req.prompt, task_type)
        confidence = _score_confidence(task_type, complexity, heuristic_score)
        route_to = _route_for_task(task_type)
        
        reasoning = f"Heuristic match ({heuristic_score}/10): {task_type}/{complexity}"
        
        return ClassifyResponse(
            task_type=task_type,
            complexity=complexity,
            confidence=confidence,
            route_to=route_to,
            reasoning=reasoning,
            should_block=False,
            skip_routing=False,
        )
    
    except Exception as e:
        logger.error(f"Classification error: {e}", exc_info=True)
        # Graceful degradation: allow everything on error
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "ok",
        "version": "5.3.0",
        "pid": os.getpid(),
    }


# ────────────────────────────────────────────────────────────────────────────
# Service Launch

def start_service(host: str = "127.0.0.1", port: int = 7337, log_level: str = "warning"):
    """Start the sidecar service."""
    logger.info(f"Starting llm-router service on {host}:{port}")
    
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        access_log=False,
    )


if __name__ == "__main__":
    port = int(os.environ.get("LLM_ROUTER_SERVICE_PORT", "7337"))
    start_service(port=port, log_level="info")
