"""Tests for classifier prompt versioning and eval harness."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from llm_router.classifier import (
    CLASSIFIER_PROMPT_PATH,
    CLASSIFIER_PROMPT_VERSION,
    CLASSIFIER_SYSTEM_PROMPT,
)
from llm_router.types import ClassificationResult, Complexity, TaskType


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "eval_classifier.py"


def _load_eval_script():
    spec = importlib.util.spec_from_file_location("eval_classifier", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_classifier_prompt_is_loaded_from_versioned_file():
    assert CLASSIFIER_PROMPT_VERSION == "v1"
    assert CLASSIFIER_PROMPT_PATH.name == "classifier_v1.txt"
    assert CLASSIFIER_SYSTEM_PROMPT == CLASSIFIER_PROMPT_PATH.read_text(encoding="utf-8").strip()


def test_eval_script_contains_100_or_more_examples():
    module = _load_eval_script()
    assert len(module.GOLDEN_SET) >= 100


@pytest.mark.asyncio
async def test_eval_script_reports_accuracy():
    module = _load_eval_script()
    examples = [
        module.GoldenExample("What is 2+2?", "query", "simple", "math"),
        module.GoldenExample("Write a release note", "generate", "moderate", "writing"),
    ]
    results = {
        "What is 2+2?": ClassificationResult(
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            reasoning="fact",
            inferred_task_type=TaskType.QUERY,
            classifier_model="fake",
            classifier_cost_usd=0.0,
            classifier_latency_ms=1.0,
        ),
        "Write a release note": ClassificationResult(
            complexity=Complexity.MODERATE,
            confidence=0.9,
            reasoning="writing",
            inferred_task_type=TaskType.GENERATE,
            classifier_model="fake",
            classifier_cost_usd=0.0,
            classifier_latency_ms=1.0,
        ),
    }

    async def fake_classify(prompt: str):
        return results[prompt]

    report = await module.evaluate_examples(fake_classify, examples)
    assert report["prompt_version"] == "v1"
    assert report["accuracy"] == 1.0
    assert report["total"] == 2
    assert report["failures"] == []
