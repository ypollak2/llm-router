"""Signal layer — classify prompts via independent, configurable detectors.

Each Signal returns a float score in [0.0, 1.0]. Signals are pure functions
of the prompt + (optional) conversation context; they never mutate state.
Composition into routing decisions happens in llm_router.decisions.engine.

Built-in signal types (v0):
    - KeywordSignal: bm25 / ngram / fuzzy keyword matching
    - EmbeddingSignal: cosine similarity to exemplar phrases (MiniLM)
    - PiiSignal: detect API keys, secrets, PII — forces local-only routing
    - ComplexitySignal: heuristic complexity estimation (ported from llm_router.classifier)

Future signal types (v0.2+):
    - ReaskSignal: detect dissatisfied repeated prompts
    - FactCheckSignal: detect verifiable factual claims
    - JailbreakSignal: known prompt-injection patterns
"""
from llm_router.signals.base import Signal, SignalScore
from llm_router.signals.keyword import KeywordSignal
from llm_router.signals.pii import PiiSignal

__all__ = ["Signal", "SignalScore", "KeywordSignal", "PiiSignal"]
