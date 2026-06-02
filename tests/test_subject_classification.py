"""Plan 07 Phase 3 (Category B) — Subject as third classification dimension.

Subject captures the topical axis orthogonal to (complexity, task_type).
The Subject enum is taxonomic — each policy decides which model handles
each subject. This test file covers the data-model changes only; wiring
the lookup into model selection is deferred (specialists in standard.yaml
is currently empty).
"""

from __future__ import annotations

import pytest


class TestSubjectEnum:
    """Subject is a string Enum like TaskType and Complexity."""

    def test_subject_general_exists(self) -> None:
        from llm_router.types import Subject

        assert Subject.GENERAL.value == "general"

    def test_all_15_categories_exist(self) -> None:
        from llm_router.types import Subject

        expected = {
            "general", "code", "medical", "math", "physics",
            "history", "law", "business", "narrative", "reasoning",
            "cloze", "trivia", "scientific", "creative", "meta",
        }
        assert {s.value for s in Subject} == expected

    def test_subject_is_string_enum(self) -> None:
        """Subject(str, Enum) so .value coerces to string seamlessly."""
        from llm_router.types import Subject

        assert isinstance(Subject.CODE, str)
        assert Subject.CODE == "code"

    def test_subject_constructable_from_string(self) -> None:
        from llm_router.types import Subject

        assert Subject("medical") is Subject.MEDICAL
        with pytest.raises(ValueError):
            Subject("not-a-subject")


class TestClassificationResultSubject:
    """ClassificationResult gains a `subject` field with safe default."""

    def test_subject_defaults_to_general_when_omitted(self) -> None:
        """Backwards compat — 20+ existing constructor calls must not break."""
        from llm_router.types import (
            ClassificationResult,
            Complexity,
            Subject,
            TaskType,
        )

        # Construct exactly the way pre-Phase-3 callers do (no subject kwarg).
        result = ClassificationResult(
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            reasoning="lookup",
            inferred_task_type=TaskType.QUERY,
            classifier_model="haiku",
            classifier_cost_usd=0.0,
            classifier_latency_ms=42.0,
        )
        assert result.subject == Subject.GENERAL

    def test_subject_can_be_set_explicitly(self) -> None:
        from llm_router.types import (
            ClassificationResult,
            Complexity,
            Subject,
            TaskType,
        )

        result = ClassificationResult(
            complexity=Complexity.MODERATE,
            confidence=0.85,
            reasoning="prescription analysis",
            inferred_task_type=TaskType.ANALYZE,
            classifier_model="haiku",
            classifier_cost_usd=0.0001,
            classifier_latency_ms=180.0,
            subject=Subject.MEDICAL,
        )
        assert result.subject == Subject.MEDICAL

    def test_result_remains_frozen_with_new_field(self) -> None:
        from dataclasses import FrozenInstanceError

        from llm_router.types import (
            ClassificationResult,
            Complexity,
            Subject,
            TaskType,
        )

        result = ClassificationResult(
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            reasoning="test",
            inferred_task_type=TaskType.QUERY,
            classifier_model="haiku",
            classifier_cost_usd=0.0,
            classifier_latency_ms=10.0,
            subject=Subject.CODE,
        )
        with pytest.raises(FrozenInstanceError):
            result.subject = Subject.MATH  # type: ignore[misc]


class TestClassifierJSONParsesSubject:
    """The v2 classifier prompt outputs subject; _parse_classification reads it."""

    def test_parse_picks_up_subject_field(self) -> None:
        from llm_router.classifier import _parse_classification

        raw = (
            '{"complexity":"moderate","task_type":"analyze",'
            '"subject":"medical","confidence":0.9,"reasoning":"diagnosis"}'
        )
        parsed = _parse_classification(raw)
        assert parsed.get("subject") == "medical"

    def test_parse_missing_subject_does_not_crash(self) -> None:
        """v1-era responses without `subject` must still parse cleanly."""
        from llm_router.classifier import _parse_classification

        raw = '{"complexity":"simple","task_type":"query","confidence":0.9}'
        parsed = _parse_classification(raw)
        # The classifier wraps this into ClassificationResult downstream;
        # at parse-time the absence is just a missing key, not an error.
        assert "subject" not in parsed or parsed.get("subject") in (None, "")


class TestCacheKeyVersioning:
    """Cache key includes the classifier version so v1 entries don't bleed
    into v2 lookups with stale subject=general."""

    def test_cache_key_changes_when_version_changes(self) -> None:
        from llm_router.cache import ClassificationCache

        key_v1 = ClassificationCache._hash_key("hello", "balanced", "haiku")
        # After the change, the hash incorporates the live classifier version.
        # The simplest invariant: providing the same prompt today must NOT match
        # a key generated under the old (unversioned) scheme. We assert that
        # the live key length is still SHA-256 hex (no schema break) and that
        # the version is folded into the input.
        assert len(key_v1) == 64  # SHA-256 hex length

        # Direct exercise: two different versions produce different keys.
        # We do this via the internal helper by passing a version sentinel —
        # if the function signature gains a `classifier_version` kwarg the
        # test passes; if not, this fails RED and we know to widen the helper.
        key_a = ClassificationCache._hash_key(
            "hello", "balanced", "haiku", classifier_version="v1"
        )
        key_b = ClassificationCache._hash_key(
            "hello", "balanced", "haiku", classifier_version="v2"
        )
        assert key_a != key_b
