# SPDX-License-Identifier: MIT
"""Learned complexity score — P(needs frontier) by kNN retrieval (Phase 2.1).

The analogue of Cursor's "Compass": a 0-1 score for how likely a prompt needs
a frontier-tier model, with ONE tunable threshold that turns it into a yes/no.

How it scores
-------------
Embed the prompt, retrieve the K most similar labelled prompts from an
artifact, and shrink their weighted label mean toward the index prior::

    score = (Σᵢ wᵢ·yᵢ + κ·prior) / (Σᵢ wᵢ + κ),   wᵢ = exp((simᵢ − sim_max) / T)

This is the formula in ``docs/ROUTERARENA.md`` ("How it routes"), the only
mechanism in this repository that has beaten a constant: K=120, κ=3.0, T=0.1.
The weights are NOT normalised, so ``Σ wᵢ`` is an effective neighbour count
and κ is measured in neighbours: a query far from everything falls back to the
prior instead of trusting one weak neighbour. ``evidence = Σw / (Σw + κ)`` is
the share of the score that came from neighbours rather than the prior.

Design invariants (same as ``semantic_classify``)
-------------------------------------------------
* **Artifact-gated.** No artifact on disk ⇒ ``complexity_score`` returns None
  without embedding anything. The artifact is built locally by
  ``scripts/build_complexity_knn.py`` into the state directory; it is never
  committed (it can hold private prompt embeddings).
* **Abstain-safe.** Unreachable embedder, a different embedding model, a
  dimension mismatch or a corrupt artifact ⇒ None, and the caller keeps its
  own complexity.
* **Off by default.** Having an artifact does not change routing; callers
  consult the score only when ``LLM_ROUTER_COMPLEXITY_KNN=on`` (ensemble) or
  ``complexity_knn: true`` in the proxy tier policy. It ships OFF because it
  has not been measured on the owner's own prompts (see the build script's
  report).
* **No model literals.** This decides "frontier or not", never a model.
"""
from __future__ import annotations

import asyncio
import base64
import heapq
import json
import math
import operator
import os
from array import array
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from llm_router.classify import _COMPLEXITY_RANK
from llm_router.logging import get_logger
from llm_router.types import Complexity

log = get_logger("llm_router.complexity_knn")

SCHEMA = "complexity_knn/1"

# Frozen hyperparameters: docs/ROUTERARENA.md "How it routes".
DEFAULT_K = 120
DEFAULT_KAPPA = 3.0
DEFAULT_TEMPERATURE = 0.1
DEFAULT_THRESHOLD = 0.5

_ARTIFACT_NAME = "complexity_knn.json"

_RANK = {c.value: r for c, r in _COMPLEXITY_RANK.items()}  # one ordering, keyed by value


def enabled() -> bool:
    """True when the ensemble should consult the score (default off)."""
    return os.environ.get("LLM_ROUTER_COMPLEXITY_KNN", "off").strip().lower() in ("1", "on", "true", "yes")


def artifact_path() -> Path:
    override = os.environ.get("LLM_ROUTER_COMPLEXITY_KNN_ARTIFACT", "").strip()
    if override:
        return Path(override).expanduser()
    from llm_router.paths import state_path

    return state_path(_ARTIFACT_NAME)


# ── artifact ──────────────────────────────────────────────────────────────────


def _unit(vec) -> array:
    mag = math.sqrt(sum(x * x for x in vec))
    return array("f", (x / mag for x in vec) if mag else vec)


@dataclass(frozen=True)
class Artifact:
    embedding_model: str
    dim: int
    k: int
    kappa: float
    temperature: float
    threshold: float
    prior: float
    rows: tuple  # tuple[array("f")], each L2-normalised
    labels: array  # array("f"), one soft label in [0, 1] per row
    provenance: dict

    @property
    def n(self) -> int:
        return len(self.rows)

    @classmethod
    def from_parts(cls, vectors, labels, *, embedding_model: str, k: int = DEFAULT_K,
                   kappa: float = DEFAULT_KAPPA, temperature: float = DEFAULT_TEMPERATURE,
                   threshold: float = DEFAULT_THRESHOLD, prior: float | None = None,
                   provenance: dict | None = None) -> "Artifact":
        if not vectors or len(vectors) != len(labels):
            raise ValueError("artifact needs one label per vector and at least one vector")
        dim = len(vectors[0])
        rows = tuple(_unit(v) for v in vectors)
        if any(len(r) != dim for r in rows):
            raise ValueError("inconsistent embedding dimensions")
        ys = array("f", (min(1.0, max(0.0, float(y))) for y in labels))
        p0 = float(prior) if prior is not None else sum(ys) / len(ys)
        return cls(embedding_model, dim, int(k), float(kappa), float(temperature),
                   float(threshold), p0, rows, ys, dict(provenance or {}))


def write_artifact(path: str | Path, vectors, labels, *, embedding_model: str, **kw) -> Path:
    """Serialise an artifact: JSON metadata + base64 little-endian float32 blobs
    (fast to load without numpy, about a third the size of a float list)."""
    art = Artifact.from_parts(vectors, labels, embedding_model=embedding_model, **kw)
    flat = array("f")
    for r in art.rows:
        flat.extend(r)
    labels_arr = array("f", art.labels)
    import sys

    if sys.byteorder != "little":  # pragma: no cover - every supported host is little-endian
        flat.byteswap()
        labels_arr.byteswap()
    doc = {
        "schema": SCHEMA, "embedding_model": art.embedding_model, "dim": art.dim, "n": art.n,
        "k": art.k, "kappa": art.kappa, "temperature": art.temperature,
        "threshold": art.threshold, "prior": art.prior, "provenance": art.provenance,
        "vectors_f32_b64": base64.b64encode(flat.tobytes()).decode("ascii"),
        "labels_f32_b64": base64.b64encode(labels_arr.tobytes()).decode("ascii"),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, target)
    return target


def _parse(raw: dict) -> Artifact:
    if raw.get("schema") != SCHEMA:
        raise ValueError(f"unknown schema {raw.get('schema')!r}")
    dim, n = int(raw["dim"]), int(raw["n"])
    flat = array("f")
    flat.frombytes(base64.b64decode(raw["vectors_f32_b64"]))
    ys = array("f")
    ys.frombytes(base64.b64decode(raw["labels_f32_b64"]))
    import sys

    if sys.byteorder != "little":  # pragma: no cover
        flat.byteswap()
        ys.byteswap()
    if len(flat) != dim * n or len(ys) != n or n == 0:
        raise ValueError("artifact blob sizes do not match dim/n")
    rows = tuple(flat[i * dim:(i + 1) * dim] for i in range(n))
    return Artifact(str(raw["embedding_model"]), dim, int(raw.get("k", DEFAULT_K)),
                    float(raw.get("kappa", DEFAULT_KAPPA)),
                    float(raw.get("temperature", DEFAULT_TEMPERATURE)),
                    float(raw.get("threshold", DEFAULT_THRESHOLD)), float(raw["prior"]),
                    rows, ys, dict(raw.get("provenance") or {}))


@lru_cache(maxsize=2)
def _load(path_str: str, mtime_ns: int) -> Artifact | None:
    try:
        return _parse(json.loads(Path(path_str).read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001 — a bad artifact must never break routing
        log.warning("complexity_knn: unusable artifact %s: %s", path_str, exc)
        return None


def load_artifact() -> Artifact | None:
    """The artifact, or None when absent or unusable. Reloaded when the file changes."""
    path = artifact_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    return _load(str(path), mtime)


def clear_cache() -> None:
    _load.cache_clear()


def is_available() -> bool:
    return load_artifact() is not None


def threshold(art: Artifact) -> float:
    """The one tunable: ``LLM_ROUTER_COMPLEXITY_KNN_THRESHOLD`` over the artifact's."""
    raw = os.environ.get("LLM_ROUTER_COMPLEXITY_KNN_THRESHOLD", "").strip()
    try:
        return float(raw) if raw else art.threshold
    except ValueError:
        log.warning("complexity_knn: ignoring non-numeric threshold %r", raw)
        return art.threshold


# ── scoring ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ComplexityScore:
    score: float  # P(needs frontier), 0-1
    needs_frontier: bool  # score >= threshold
    evidence: float  # Σw / (Σw + κ): neighbour share of the score, 0-1
    threshold: float


def shrunk_score(sims_labels, *, kappa: float, temperature: float, prior: float) -> tuple[float, float]:
    """``(score, evidence)`` from the top-K ``(similarity, label)`` pairs."""
    if not sims_labels:
        return prior, 0.0
    mx = max(s for s, _ in sims_labels)
    num = den = 0.0
    for s, y in sims_labels:
        w = math.exp((s - mx) / temperature)
        num += w * y
        den += w
    return (num + kappa * prior) / (den + kappa), den / (den + kappa)


def score_vector(vec, art: Artifact) -> ComplexityScore:
    """Score an embedding against the artifact (pure, synchronous)."""
    q = _unit(vec)
    mul = operator.mul
    sims = [sum(map(mul, q, r)) for r in art.rows]
    top = heapq.nlargest(min(art.k, art.n), range(art.n), key=sims.__getitem__)
    score, evidence = shrunk_score([(sims[i], art.labels[i]) for i in top], kappa=art.kappa,
                                   temperature=art.temperature, prior=art.prior)
    t = threshold(art)
    return ComplexityScore(score, score >= t, evidence, t)


def _embed(text: str, model: str) -> list[float] | None:
    from llm_router.semantic_classify import _embed as embed

    return embed(text, model)


async def complexity_score(prompt: str) -> ComplexityScore | None:
    """P(needs frontier) for ``prompt``, or None to abstain."""
    art = load_artifact()
    if art is None or not prompt or not prompt.strip():
        return None
    try:
        vec = await asyncio.to_thread(_embed, prompt, art.embedding_model)
        if vec is None or len(vec) != art.dim:
            return None
        return await asyncio.to_thread(score_vector, vec, art)
    except Exception as exc:  # noqa: BLE001 — scoring must never stall routing
        log.warning("complexity_knn: scoring failed: %s", exc)
        return None


def frontier_complexity(complexity, needs_frontier: bool):
    """Move ``complexity`` across the frontier boundary only (moderate | complex).

    Needs frontier and below ``complex`` → ``complex``; does not need it and at
    or above ``complex`` → ``moderate``. Anything already on the right side is
    returned unchanged. Accepts and returns a ``Complexity`` or its string value.
    """
    as_enum = isinstance(complexity, Complexity)
    value = complexity.value if as_enum else str(complexity)
    rank = _RANK.get(value)
    if rank is None:
        return complexity
    frontier = _RANK[Complexity.COMPLEX.value]
    if needs_frontier and rank < frontier:
        value = Complexity.COMPLEX.value
    elif not needs_frontier and rank >= frontier:
        value = Complexity.MODERATE.value
    return Complexity(value) if as_enum else value


# ── evaluation helper (used by the build script and its tests) ────────────────


def auc(scores, labels) -> float:
    """ROC AUC by the rank-sum formula, ties counted as one half."""
    pairs = sorted(zip(scores, labels), key=lambda p: p[0])
    pos = sum(1 for _, y in pairs if y)
    neg = len(pairs) - pos
    if not pos or not neg:
        return float("nan")
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        rank_sum += avg_rank * sum(1 for _, y in pairs[i:j + 1] if y)
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)
