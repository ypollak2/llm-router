"""Pure-Python lexical tier model for the one classifier engine (PLAN v16 P1.6 L3).

Multinomial logistic regression over hashed word uni/bi-grams. No embedding
model, no network call, no third-party import: the engine's sync path must
never wait on either (D-19 A, D-13). Training is deterministic (fixed seed,
``zlib.crc32`` hashing, never the per-process ``hash()``), so a model fitted
twice on the same rows is byte-identical.

The model is an artifact, not code: ``fit`` builds it from labelled rows
(GT-500 tune + E2-cal + the example store, per the plan), ``save``/``load``
move it as JSON. With no artifact the engine has no L3 layer and says so.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import zlib
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "ml_lexical/1"
DEFAULT_DIM = 1 << 16
_WORD = re.compile(r"[a-z0-9_]+")


def features(text: str, dim: int = DEFAULT_DIM) -> dict[int, float]:
    """Hashed, L2-normalised bag of unigrams + bigrams + two length buckets."""
    words = _WORD.findall((text or "").lower())
    toks = list(words)
    toks += [a + " " + b for a, b in zip(words, words[1:])]
    n = len(text or "")
    toks.append("__len_%d" % min(5, n // 200))
    if "```" in (text or ""):
        toks.append("__fence")
    counts: dict[int, float] = {}
    for t in toks:
        i = zlib.crc32(t.encode()) % dim
        counts[i] = counts.get(i, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in counts.values())) or 1.0
    return {i: v / norm for i, v in counts.items()}


@dataclass(frozen=True)
class LexicalModel:
    classes: tuple[str, ...]
    dim: int
    weights: tuple[dict[int, float], ...]  # one sparse vector per class
    bias: tuple[float, ...]
    version: str  # sha256[:8] of the serialised model

    def predict_proba(self, text: str) -> dict[str, float]:
        f = features(text, self.dim)
        z = [b + sum(w.get(i, 0.0) * v for i, v in f.items()) for w, b in zip(self.weights, self.bias)]
        m = max(z)
        e = [math.exp(x - m) for x in z]
        s = sum(e)
        return {c: x / s for c, x in zip(self.classes, e)}

    def predict(self, text: str) -> tuple[str, float]:
        """``(class, probability)``; ties break to the class listed first."""
        p = self.predict_proba(text)
        best = max(self.classes, key=lambda c: (p[c], -self.classes.index(c)))
        return best, p[best]

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA, "classes": list(self.classes), "dim": self.dim, "bias": list(self.bias),
            "weights": [{str(i): round(v, 6) for i, v in sorted(w.items()) if v} for w in self.weights],
        }


def _version(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:8]


def _from_json(raw: dict) -> LexicalModel:
    if raw.get("schema") != SCHEMA:
        raise ValueError("not an %s artifact" % SCHEMA)
    classes = tuple(str(c) for c in raw["classes"])
    weights = tuple({int(i): float(v) for i, v in w.items()} for w in raw["weights"])
    bias = tuple(float(b) for b in raw["bias"])
    if len(classes) < 2 or not (len(classes) == len(weights) == len(bias)):
        raise ValueError("ml_lexical artifact: class/weight/bias length mismatch")
    return LexicalModel(classes, int(raw["dim"]), weights, bias, _version(raw))


def fit(texts, labels, *, dim: int = DEFAULT_DIM, epochs: int = 30, lr: float = 0.5,
        l2: float = 1e-4, seed: int = 0) -> LexicalModel:
    """Deterministic SGD fit. ``labels`` are class names; >= 2 distinct required."""
    texts, labels = list(texts), list(labels)
    if len(texts) != len(labels) or not texts:
        raise ValueError("fit needs equal-length, non-empty texts and labels")
    classes = tuple(sorted(set(labels)))
    if len(classes) < 2:
        raise ValueError("fit needs at least two distinct labels")
    idx = {c: k for k, c in enumerate(classes)}
    feats = [features(t, dim) for t in texts]
    ys = [idx[y] for y in labels]
    w: list[dict[int, float]] = [{} for _ in classes]
    b = [0.0] * len(classes)
    rng = random.Random(seed)
    order = list(range(len(texts)))
    for _ in range(epochs):
        rng.shuffle(order)
        for n in order:
            f = feats[n]
            z = [b[k] + sum(w[k].get(i, 0.0) * v for i, v in f.items()) for k in range(len(classes))]
            m = max(z)
            e = [math.exp(x - m) for x in z]
            s = sum(e)
            for k in range(len(classes)):
                g = e[k] / s - (1.0 if k == ys[n] else 0.0)
                b[k] -= lr * g
                wk = w[k]
                for i, v in f.items():
                    wk[i] = wk.get(i, 0.0) * (1.0 - lr * l2) - lr * g * v
    payload = LexicalModel(classes, dim, tuple(w), tuple(b), "").to_json()
    return _from_json(payload)


def save(model: LexicalModel, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(model.to_json(), sort_keys=True))
    return p


_cache: dict[tuple[str, int], LexicalModel | None] = {}


def load(path: str | Path) -> LexicalModel | None:
    """The artifact at ``path``, or None when absent or invalid (no L3 layer)."""
    p = Path(path)
    try:
        key = (str(p), p.stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _cache:
        try:
            _cache[key] = _from_json(json.loads(p.read_text()))
        except (OSError, ValueError, KeyError, TypeError):
            _cache[key] = None
    return _cache[key]
