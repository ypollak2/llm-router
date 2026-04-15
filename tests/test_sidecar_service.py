"""Tests for llm-router sidecar service."""

import pytest
import json
import subprocess
import time
from pathlib import Path


def test_service_modules_exist():
    """Verify service modules are importable."""
    from llm_router.service import app, ClassifyRequest, ClassifyResponse
    from llm_router.service_manager import start_service, stop_service
    from llm_router.hook_client import classify_prompt
    
    assert app is not None
    assert ClassifyRequest is not None
    assert ClassifyResponse is not None
    assert callable(start_service)
    assert callable(stop_service)
    assert callable(classify_prompt)


def test_heuristic_classifier():
    """Test heuristic classification without service."""
    from llm_router.service import _heuristic_classify, _score_confidence
    
    # Query detection
    task_type, score = _heuristic_classify("what does os.path.join do?")
    assert task_type == "query"
    
    # Code detection
    task_type, score = _heuristic_classify("write a function to parse JSON")
    assert task_type == "code"
    
    # Research detection
    task_type, score = _heuristic_classify("latest AI research in 2026")
    assert task_type == "research"


def test_infrastructure_detection():
    """Test that infrastructure tools are detected."""
    from llm_router.service import _is_infrastructure
    
    # Should skip routing
    assert _is_infrastructure("mcp__plugin_serena_serena__read_file")
    assert _is_infrastructure("mcp__llm-router__llm_query")
    assert _is_infrastructure("Read")
    assert _is_infrastructure("Bash")
    
    # Should not skip
    assert not _is_infrastructure("normal_prompt")


def test_confidence_scoring():
    """Test confidence scoring."""
    from llm_router.service import _score_confidence
    
    # High confidence (heuristic score 8+)
    assert _score_confidence("query", "simple", 8) == "high"
    assert _score_confidence("query", "simple", 9) == "high"
    
    # Medium confidence
    assert _score_confidence("query", "simple", 5) == "medium"
    assert _score_confidence("query", "simple", 6) == "medium"
    
    # Low confidence
    assert _score_confidence("query", "simple", 1) == "low"
    assert _score_confidence("query", "simple", 3) == "low"
