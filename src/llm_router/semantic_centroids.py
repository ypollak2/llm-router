# SPDX-License-Identifier: MIT
"""Offline builder for the ``semantic_classify`` centroid artifact.

Takes a Firewall-v2-clean labeled corpus, embeds it, and produces the prototype
artifact that ``semantic_classify.classify_semantic`` loads at runtime. Pure and
injectable — the embedding function is passed in, so the whole pipeline is
unit-testable without a network or a model.

Pipeline (see clean plan P1/P4):
  1. Group the labeled corpus by class, per head (``task_type``, ``subject``).
  2. Embed each prompt; reduce each class to ``k`` prototypes via light k-means
     (``k=1`` ⇒ a single normalized mean, the common case).
  3. Calibrate the softmax ``temperature`` (grid search maximizing held-out
     argmax accuracy) and pick the ``confidence_floor`` at a target precision.
  4. Emit the artifact dict — same schema ``semantic_classify._parse_centroids``
     consumes, with a ``provenance`` block pointing at the audit report.

The contamination audit (``contamination_audit.audit``) is the caller's
responsibility to run *before* invoking this — a corpus that fails the audit
must never reach ``build_artifact``. The CLI (``scripts/build_semantic_centroids.py``)
enforces that ordering.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from llm_router.logging import get_logger
from llm_router.semantic_classify import _norm, _score_head, build_prototype

log = get_logger("llm_router.semantic_centroids")

# Temperatures swept during calibration. Spans "soft" (well-separated classes
# still yield moderate confidence) to "sharp" (near-argmax confidences).
_TEMPERATURE_GRID = (4.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 28.0)

# Default target precision the confidence floor is chosen to hit on held-out
# data: among predictions with conf ≥ floor, ≥ this fraction must be correct.
_DEFAULT_TARGET_PRECISION = 0.85


@dataclass(frozen=True)
class LabeledPrompt:
    prompt: str
    task_type: str
    subject: str | None = None


# ── prototype reduction ───────────────────────────────────────────────────────


def _kmeans(vectors: list[list[float]], k: int, *, iters: int = 25) -> list[list[float]]:
    """Tiny cosine k-means → up to ``k`` normalized prototypes.

    Deterministic seeding (evenly-spaced picks, not random) so a rebuild from the
    same corpus yields the same artifact — reproducibility is a compliance
    requirement (clean plan §5.5). Falls back to a single mean when there are
    fewer vectors than clusters.
    """
    vecs = [_norm(v) for v in vectors]
    if k <= 1 or len(vecs) <= k:
        if len(vecs) <= k and k > 1:
            return vecs  # each point is its own prototype
        return [build_prototype(vecs)]

    # Deterministic seeds: evenly spaced across the (input-order) corpus.
    step = len(vecs) / k
    centroids = [vecs[int(i * step)] for i in range(k)]

    for _ in range(iters):
        buckets: list[list[list[float]]] = [[] for _ in range(k)]
        for v in vecs:
            best = max(range(k), key=lambda c: sum(x * y for x, y in zip(v, centroids[c])))
            buckets[best].append(v)
        new_centroids = []
        for c in range(k):
            new_centroids.append(build_prototype(buckets[c]) if buckets[c] else centroids[c])
        if all(
            sum(x * y for x, y in zip(a, b)) > 0.9999
            for a, b in zip(centroids, new_centroids)
        ):
            centroids = new_centroids
            break
        centroids = new_centroids
    return centroids


def _build_head(
    grouped: dict[str, list[list[float]]], k: int
) -> dict[str, list[list[float]]]:
    return {label: _kmeans(vs, k) for label, vs in grouped.items() if vs}


# ── calibration ───────────────────────────────────────────────────────────────


def _accuracy_at(
    holdout: list[tuple[list[float], str]],
    protos: dict[str, list[list[float]]],
    temperature: float,
) -> float:
    if not holdout:
        return 0.0
    correct = sum(
        1 for vec, gold in holdout if _score_head(vec, protos, temperature)[0] == gold
    )
    return correct / len(holdout)


def calibrate_temperature(
    holdout: list[tuple[list[float], str]],
    protos: dict[str, list[list[float]]],
    *,
    default: float = 12.0,
) -> float:
    """Grid-search the softmax temperature that maximizes held-out accuracy.

    Argmax is temperature-invariant, so accuracy is identical across the grid —
    we therefore break ties toward the temperature whose *confidence* is best
    calibrated (mean top-prob closest to the observed accuracy), which is what
    makes the downstream floor meaningful.
    """
    if not holdout:
        return default
    best_t, best_key = default, -1.0
    acc_by_t = {t: _accuracy_at(holdout, protos, t) for t in _TEMPERATURE_GRID}
    top_acc = max(acc_by_t.values())
    for t in _TEMPERATURE_GRID:
        if acc_by_t[t] < top_acc - 1e-9:
            continue
        confs = [max(_score_head(v, protos, t)[2].values()) for v, _ in holdout]
        mean_conf = sum(confs) / len(confs)
        # Prefer the temperature whose mean confidence matches accuracy (well
        # calibrated ⇒ |mean_conf − acc| small). Higher key = better.
        key = -abs(mean_conf - acc_by_t[t])
        if key > best_key:
            best_key, best_t = key, t
    return best_t


def calibrate_floor(
    holdout: list[tuple[list[float], str]],
    protos: dict[str, list[list[float]]],
    temperature: float,
    *,
    target_precision: float = _DEFAULT_TARGET_PRECISION,
    default: float = 0.55,
) -> float:
    """Pick the lowest confidence floor whose held-out precision ≥ target.

    Sorts predictions by confidence and walks down, accepting the floor at the
    point where cumulative precision above it still clears ``target_precision``.
    Returns ``default`` when no floor achieves the target (corpus too weak —
    caller should collect more data rather than ship an unsafe floor).
    """
    if not holdout:
        return default
    scored = []
    for vec, gold in holdout:
        label, conf, _ = _score_head(vec, protos, temperature)
        scored.append((conf, label == gold))
    scored.sort(key=lambda x: x[0], reverse=True)

    correct = 0
    best_floor = None
    for i, (conf, ok) in enumerate(scored, start=1):
        correct += 1 if ok else 0
        precision = correct / i
        if precision >= target_precision:
            best_floor = conf  # lowest conf still meeting target so far
    return best_floor if best_floor is not None else default


# ── artifact assembly ─────────────────────────────────────────────────────────


def build_artifact(
    labeled: list[LabeledPrompt],
    embed_fn,
    *,
    embedding_model: str,
    k: int = 1,
    holdout: list[LabeledPrompt] | None = None,
    audit_report: dict | None = None,
    provenance_sources: list[str] | None = None,
) -> dict:
    """Build the centroid artifact dict from a clean labeled corpus.

    Args:
        labeled: Training examples (prompt + task_type [+ subject]).
        embed_fn: ``str -> list[float]`` embedding function (injected; the CLI
            passes ``semantic_classify._embed`` bound to the chosen backend).
        embedding_model: Recorded in the artifact + enforced at inference.
        k: Prototypes per class (k-means). 1 = single mean (default).
        holdout: Optional held-out examples for temperature/floor calibration.
            Falls back to defaults when absent.
        audit_report: The ``contamination_audit`` report dict (embedded in
            provenance — the artifact carries proof of its own cleanliness).
        provenance_sources: Human-readable source tags (e.g.
            ``["synthetic:v1", "mmlu-train:audited"]``).

    Returns:
        Artifact dict ready to ``json.dump`` to ``data/semantic_centroids.json``.
    """
    task_groups: dict[str, list[list[float]]] = defaultdict(list)
    subj_groups: dict[str, list[list[float]]] = defaultdict(list)
    dim: int | None = None

    for ex in labeled:
        vec = embed_fn(ex.prompt)
        if not vec:
            log.warning("embed_fn returned empty for a prompt — skipped")
            continue
        if dim is None:
            dim = len(vec)
        elif len(vec) != dim:
            raise ValueError(f"inconsistent embedding dim: {len(vec)} != {dim}")
        task_groups[ex.task_type].append(vec)
        if ex.subject:
            subj_groups[ex.subject].append(vec)

    if dim is None:
        raise ValueError("no usable training embeddings produced")

    task_protos = _build_head(task_groups, k)
    subj_protos = _build_head(subj_groups, k)

    # Calibrate against the task head (the load-bearing one).
    temperature, floor = 12.0, 0.55
    if holdout:
        hv = [(embed_fn(ex.prompt), ex.task_type) for ex in holdout]
        hv = [(_norm(v), g) for v, g in hv if v and len(v) == dim]
        if hv:
            temperature = calibrate_temperature(hv, task_protos)
            floor = calibrate_floor(hv, task_protos, temperature)

    return {
        "version": "1",
        "embedding_model": embedding_model,
        "dim": dim,
        "temperature": round(temperature, 4),
        "confidence_floor": round(floor, 4),
        "task_type": task_protos,
        "subject": subj_protos,
        "provenance": {
            "sources": provenance_sources or [],
            "k": k,
            "n_train": len(labeled),
            "n_holdout": len(holdout or []),
            "audit": audit_report or {"mode": "unaudited"},
        },
    }
