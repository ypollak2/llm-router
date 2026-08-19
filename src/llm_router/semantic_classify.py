# SPDX-License-Identifier: MIT
"""Embedding-space prompt classifier — the discriminative successor to the
regex ``_SIGNALS`` engine in ``classify.py``.

Why this exists
---------------
``classify.py`` classifies a prompt's ``task_type`` with hand-written regex
(``intent×3 + topic×2 + format×1``). That approach has two known failure modes:

1. **Brittleness.** Real prompts phrase the same intent a thousand ways; a
   finite regex table under-covers the tail and mis-fires on the overlap
   between categories (a "review this code" analyze/code boundary, say).
2. **Benchmark-recognition drift.** The RouterArena lineage was rejected
   precisely because those regexes drifted toward matching RA *harness
   templates* — fixed answer-format and header wrappers injected by the
   benchmark builder. A pattern keyed to such a wrapper is contamination,
   not classification. (This module names no such literal; the CI template
   guard asserts none appears here.)

This module replaces the *decision* with embedding geometry: a prompt is
embedded once, compared to per-class **prototype** vectors, and assigned the
nearest class with a **calibrated** confidence. Prototypes are learned offline
from Firewall-v2-clean data (self-generated synthetic + hash-audited benchmark
*train* splits — see ``Docs/archive/ROUTERARENA_CLEAN_075_PLAN.md`` §2). Because the
classifier operates purely on embedding distance, it *structurally cannot*
template-match RA: there is no literal string in this file to key on. A CI
guard (§5.3 of the plan) asserts no RA-template literal ever appears here.

Two heads, one forward pass — mirrors semantic-router's multi-task ModernBERT,
minus its banned MMLU-Pro fine-tune:

* ``task_type`` — QUERY / RESEARCH / GENERATE / ANALYZE / CODE. Feeds the same
  ``COMPLEXITY_TO_PROFILE → get_model_chain`` machinery the regex path feeds.
* ``subject``   — the MMLU-style domain axis (``types.Subject``: MATH, LAW,
  BUSINESS, MEDICAL, …). Feeds specialist routing (``policies`` ``specialists.*``).

Design invariants
-----------------
* **No hardcoded ``provider/model`` literals** (house rule
  ``llm_router-no-hardcoding-opensource``). This module decides *category*, never a
  model. Selection stays in the registry, exactly as ``classify.py`` documents.
* **Abstain-safe.** With no centroid artifact on disk, no reachable embedding
  backend, or a below-floor confidence, ``classify_semantic`` returns ``None``
  and the caller falls back to the existing regex ``classify_signals``. Absent
  the artifact this module is a pure no-op — wiring it in cannot change current
  routing behaviour until a clean artifact ships.
* **Complexity is not decided here.** Length/keyword complexity stays in
  ``classify._complexity`` (regression-locked cost curve). This head only
  sharpens *what kind* of task it is, not *how hard*.

Embedding backend
-----------------
Reuses LLM Router's existing embedding path — ``nomic-embed-text`` via the local
Ollama ``/api/embeddings`` endpoint (the same model ``semantic_cache.py`` uses,
768-dim, free, no torch in the hot path). Optionally, set
``LLM_ROUTER_SEMANTIC_CLASSIFIER_BACKEND=st`` to use a local
``sentence-transformers`` model (the opt-in ``tokenizers`` extra) when Ollama is
not running. Either way the artifact records which model produced its
prototypes, and inference refuses to mix embedding spaces.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from llm_router.logging import get_logger
from llm_router.types import Complexity, Subject, TaskType

log = get_logger("llm_router.semantic_classify")

# ── Artifact location ─────────────────────────────────────────────────────────
# The centroid artifact is built offline by the calibration generator (P1/P4 of
# the clean plan) and committed under ``data/``. Override for tests / A-B via
# ``LLM_ROUTER_SEMANTIC_CENTROIDS``.
_DEFAULT_CENTROIDS_PATH = Path(__file__).parent / "data" / "semantic_centroids.json"

# Embedding model used by the default (Ollama) backend. Must match the model the
# artifact was built with — inference refuses to run if they differ.
_OLLAMA_EMBED_MODEL = "nomic-embed-text"

# Confidence floor. Below this the classifier abstains and the caller falls back
# to the regex signal engine — a wrong-but-confident category is worse than a
# graceful fallback. Calibrated jointly with the softmax temperature; this is
# the conservative default before an artifact-specific value is loaded.
_DEFAULT_CONFIDENCE_FLOOR = 0.55

# Softmax temperature over cosine similarities. Higher = sharper distribution.
# This is the *only* free scalar in the confidence readout and is the analogue
# of the clean plan's τ: it is fit on clean calibration data and frozen into the
# artifact. The default is used only when the artifact omits one.
_DEFAULT_TEMPERATURE = 12.0

_EMBED_TIMEOUT_S = 3.0


# ── Embedding backends ────────────────────────────────────────────────────────


def _embed_ollama(text: str, base_url: str) -> list[float] | None:
    """Embed ``text`` via Ollama's ``/api/embeddings`` (same path as the cache).

    Synchronous ``urllib`` with a short timeout, no extra deps. Returns ``None``
    on any error so the caller treats it as "backend unavailable" and abstains.
    """
    try:
        payload = json.dumps({"model": _OLLAMA_EMBED_MODEL, "prompt": text}).encode()
        req = urllib.request.Request(
            f"{base_url}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_EMBED_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
            emb = data.get("embedding")
            return emb if isinstance(emb, list) and emb else None
    except Exception as exc:  # noqa: BLE001 — any failure ⇒ abstain
        log.debug("Ollama embedding failed: %s", exc)
        return None


@lru_cache(maxsize=1)
def _st_model(model_name: str):
    """Lazily load a sentence-transformers model (opt-in ``tokenizers`` extra).

    Cached so the (expensive) load happens once per process. Returns ``None`` if
    the extra is not installed, so the caller can degrade to Ollama / abstain.
    """
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log.debug("sentence-transformers unavailable: %s", exc)
        return None
    try:
        return SentenceTransformer(model_name)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load sentence-transformers model %r: %s", model_name, exc)
        return None


def _embed_st(text: str, model_name: str) -> list[float] | None:
    model = _st_model(model_name)
    if model is None:
        return None
    try:
        vec = model.encode(text, normalize_embeddings=False)
        return [float(x) for x in vec]
    except Exception as exc:  # noqa: BLE001
        log.debug("sentence-transformers encode failed: %s", exc)
        return None


def _embed(text: str, expected_model: str) -> list[float] | None:
    """Embed ``text`` with the backend that matches the artifact's model.

    ``LLM_ROUTER_SEMANTIC_CLASSIFIER_BACKEND`` selects the backend:
      * unset / ``ollama`` → Ollama ``nomic-embed-text`` (default).
      * ``st`` / ``sentence-transformers`` → local ST model named
        ``LLM_ROUTER_SEMANTIC_ST_MODEL`` (default = ``expected_model``).

    Returns ``None`` (⇒ abstain) if the backend is unreachable OR would produce
    vectors from a different embedding space than the artifact was built with.
    """
    backend = os.getenv("LLM_ROUTER_SEMANTIC_CLASSIFIER_BACKEND", "ollama").strip().lower()

    if backend in ("st", "sentence-transformers"):
        st_model = os.getenv("LLM_ROUTER_SEMANTIC_ST_MODEL", expected_model)
        if st_model != expected_model:
            log.warning(
                "ST model %r != artifact model %r — abstaining to avoid mixing "
                "embedding spaces", st_model, expected_model,
            )
            return None
        return _embed_st(text, st_model)

    # Default: Ollama. Guard the embedding space.
    if expected_model != _OLLAMA_EMBED_MODEL:
        log.warning(
            "Artifact built with %r but Ollama backend serves %r — abstaining",
            expected_model, _OLLAMA_EMBED_MODEL,
        )
        return None
    from llm_router.config import get_config

    base_url = get_config().ollama_base_url
    if not base_url:
        return None
    return _embed_ollama(text, base_url)


# ── Vector math (pure-python, optional numpy fast path) ───────────────────────


def _norm(v: list[float]) -> list[float]:
    """Return the L2-normalized copy of ``v`` (zero vector passes through)."""
    mag = math.sqrt(sum(x * x for x in v))
    if mag == 0.0:
        return list(v)
    return [x / mag for x in v]


def _cos_prenorm(a: list[float], b: list[float]) -> float:
    """Cosine of two vectors assumed already L2-normalized ⇒ just the dot."""
    return sum(x * y for x, y in zip(a, b))


def build_prototype(embeddings: list[list[float]]) -> list[float]:
    """Mean-then-normalize a set of same-class embeddings into one prototype.

    Exposed for the offline centroid generator (and tests) so the averaging
    convention lives in exactly one place — the generator must build prototypes
    the same way inference scores them. A class may have several prototypes
    (e.g. sub-clusters of "code"); each is built by one call over its members.
    """
    if not embeddings:
        raise ValueError("cannot build a prototype from zero embeddings")
    dim = len(embeddings[0])
    acc = [0.0] * dim
    for e in embeddings:
        if len(e) != dim:
            raise ValueError("inconsistent embedding dimensions in prototype build")
        for i, x in enumerate(e):
            acc[i] += x
    mean = [x / len(embeddings) for x in acc]
    return _norm(mean)


# ── Centroid artifact ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Centroids:
    """Loaded prototype artifact — the classifier's learned parameters.

    ``task`` / ``subject`` map a class label to a list of L2-normalized
    prototype vectors (multiple per class for multi-modal coverage). All vectors
    share ``dim`` and were produced by ``embedding_model``.
    """

    embedding_model: str
    dim: int
    temperature: float
    confidence_floor: float
    task: dict[str, list[list[float]]]
    subject: dict[str, list[list[float]]]
    provenance: dict


def _parse_centroids(raw: dict) -> Centroids:
    """Validate + normalize a raw artifact dict into a ``Centroids``.

    Prototypes are re-normalized on load so the artifact can be authored with
    plain means and inference can rely on ``_cos_prenorm``. Unknown class labels
    (not in the ``TaskType`` / ``Subject`` enums) are dropped with a warning
    rather than raising — a forward-compatible artifact must not brick routing.
    """
    embedding_model = str(raw["embedding_model"])
    dim = int(raw["dim"])
    temperature = float(raw.get("temperature", _DEFAULT_TEMPERATURE))
    floor = float(raw.get("confidence_floor", _DEFAULT_CONFIDENCE_FLOOR))

    def _clean(head: dict, valid: set[str], head_name: str) -> dict[str, list[list[float]]]:
        out: dict[str, list[list[float]]] = {}
        for label, protos in (head or {}).items():
            if label not in valid:
                log.warning("centroids: dropping unknown %s label %r", head_name, label)
                continue
            vecs = []
            for p in protos:
                if len(p) != dim:
                    log.warning("centroids: %s/%s prototype has wrong dim, skipped", head_name, label)
                    continue
                vecs.append(_norm([float(x) for x in p]))
            if vecs:
                out[label] = vecs
        return out

    valid_tasks = {t.value for t in TaskType}
    valid_subjects = {s.value for s in Subject}
    task = _clean(raw.get("task_type", {}), valid_tasks, "task_type")
    subject = _clean(raw.get("subject", {}), valid_subjects, "subject")
    return Centroids(
        embedding_model=embedding_model,
        dim=dim,
        temperature=temperature,
        confidence_floor=floor,
        task=task,
        subject=subject,
        provenance=raw.get("provenance", {}),
    )


@lru_cache(maxsize=1)
def _load_centroids(path_str: str) -> Centroids | None:
    """Load + cache the centroid artifact. ``None`` if missing/unparseable.

    Cached by path so repeated calls are free; tests override the path (which
    keys the cache) or call ``_load_centroids.cache_clear()``.
    """
    path = Path(path_str)
    if not path.is_file():
        log.debug("no centroid artifact at %s — semantic classifier will abstain", path)
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _parse_centroids(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to load centroid artifact %s: %s", path, exc)
        return None


def _centroids_path() -> str:
    return os.getenv("LLM_ROUTER_SEMANTIC_CENTROIDS", str(_DEFAULT_CENTROIDS_PATH))


# ── Confidence readout ────────────────────────────────────────────────────────


def _score_head(
    query_vec: list[float],
    protos_by_label: dict[str, list[list[float]]],
    temperature: float,
) -> tuple[str, float, dict[str, float]]:
    """Score one head: nearest-prototype similarity → temperature-softmax.

    For each label the score is the **max** cosine over that label's prototypes
    (nearest sub-cluster wins), then a softmax over labels yields a calibrated
    distribution. Returns ``(best_label, confidence, full_distribution)`` where
    ``confidence`` is the winning label's softmax mass.
    """
    per_label_max: dict[str, float] = {}
    for label, protos in protos_by_label.items():
        per_label_max[label] = max(_cos_prenorm(query_vec, p) for p in protos)

    # Numerically-stable softmax over the max-cosine scores.
    scaled = {k: v * temperature for k, v in per_label_max.items()}
    hi = max(scaled.values())
    exps = {k: math.exp(v - hi) for k, v in scaled.items()}
    total = sum(exps.values()) or 1.0
    dist = {k: v / total for k, v in exps.items()}
    best = max(dist, key=lambda k: dist[k])
    return best, dist[best], dist


# ── Public result type ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SemanticClassifyResult:
    """A confident embedding-space classification.

    ``task_confidence`` / ``subject_confidence`` are calibrated softmax masses.
    Only returned when ``task_confidence`` clears the artifact's floor — callers
    receive ``None`` otherwise and fall back to the regex engine.
    """

    task_type: TaskType
    subject: Subject
    task_confidence: float
    subject_confidence: float
    embedding_model: str
    method: str = "embedding"

    def to_signal(self, complexity: Complexity) -> "object":
        """Bridge to ``classify.ClassifySignal`` so this drops into the existing
        pipeline. Imported lazily to avoid a module import cycle.

        The signal is marked ``confident=True`` (we only build a result above
        floor) with ``method="embedding"`` and ``score`` carrying the confidence
        as an integer percent, so downstream logging/telemetry can distinguish
        embedding decisions from regex ones.
        """
        from llm_router.classify import ClassifySignal

        return ClassifySignal(
            task_type=self.task_type,
            complexity=complexity,
            score=int(round(self.task_confidence * 100)),
            confident=True,
            method="embedding",
        )


# ── Public API ────────────────────────────────────────────────────────────────


async def classify_semantic(
    prompt: str,
    *,
    confidence_floor: float | None = None,
) -> SemanticClassifyResult | None:
    """Classify ``prompt`` into (task_type, subject) via embedding prototypes.

    Returns a ``SemanticClassifyResult`` only when a centroid artifact is loaded,
    an embedding backend is reachable, the embedding space matches, and the
    task-head confidence clears the floor. In every other case it returns
    ``None`` — the explicit, safe "let the caller fall back to regex" signal.

    This is intentionally the *only* async surface: embedding is I/O. The sync
    hot path (``classify.classify_signals``) stays regex; this runs on the
    ambiguous tail in place of the per-call LLM classifier, removing that
    call's latency and cost while producing a sharper, calibrated decision.

    Args:
        prompt: The user's prompt text.
        confidence_floor: Override the artifact's floor (mainly for tests / A-B).

    Returns:
        A confident classification, or ``None`` to abstain.
    """
    centroids = _load_centroids(_centroids_path())
    if centroids is None or not centroids.task:
        return None

    raw_vec = _embed(prompt, centroids.embedding_model)
    if raw_vec is None or len(raw_vec) != centroids.dim:
        if raw_vec is not None:
            log.debug(
                "embedding dim %d != artifact dim %d — abstaining",
                len(raw_vec), centroids.dim,
            )
        return None
    query_vec = _norm(raw_vec)

    floor = confidence_floor if confidence_floor is not None else centroids.confidence_floor

    task_label, task_conf, _ = _score_head(query_vec, centroids.task, centroids.temperature)
    if task_conf < floor:
        log.debug(
            "semantic_classify: abstain (task=%s conf=%.3f < floor=%.2f)",
            task_label, task_conf, floor,
        )
        return None

    # Subject head is best-effort: a low-confidence subject just defaults to
    # GENERAL (specialist routing is an optional refinement, not load-bearing).
    if centroids.subject:
        subj_label, subj_conf, _ = _score_head(
            query_vec, centroids.subject, centroids.temperature
        )
    else:
        subj_label, subj_conf = Subject.GENERAL.value, 0.0

    try:
        task_type = TaskType(task_label)
    except ValueError:
        return None
    try:
        subject = Subject(subj_label) if subj_conf >= floor else Subject.GENERAL
    except ValueError:
        subject = Subject.GENERAL

    log.info(
        "semantic_classify: %s (%.0f%%) / subject=%s (%.0f%%) via %s",
        task_type.value, task_conf * 100, subject.value, subj_conf * 100,
        centroids.embedding_model,
    )
    return SemanticClassifyResult(
        task_type=task_type,
        subject=subject,
        task_confidence=task_conf,
        subject_confidence=subj_conf,
        embedding_model=centroids.embedding_model,
    )


def is_available() -> bool:
    """True iff a centroid artifact is loaded (an embedding backend may still be
    down at call time). Cheap check for callers deciding whether to even try."""
    c = _load_centroids(_centroids_path())
    return c is not None and bool(c.task)
