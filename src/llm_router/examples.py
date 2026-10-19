# SPDX-License-Identifier: MIT
"""The classifier's private learning store, ``examples.jsonl`` (PLAN v16 P3.6, R-CLS-5).

One row per thing the user taught the router. It is the only input of the L3 kNN
learning step (``classifier_learning``). Under D-121 the local LLM classifier is
killed, so there is no L2 few-shot consumer: the rows feed the L3 kNN index only.

What a row holds (privacy: never more than the kNN index already holds)
-----------------------------------------------------------------------
``text_sha`` (16 hex chars of the normalised prompt, ``prompt_key``), ``source``, ``label``
(P(needs frontier), 0 or 1), ``ts``, and, only when the kNN index is switched on and
an artifact exists, ``vec`` (the L2-normalised embedding, base64 float32) with ``model``.
The kNN artifact stores exactly those embeddings, so this adds no information about
the prompt. Prompt text is never written. A source that has no prompt text at hand
(a key press on a past turn) writes a row without ``vec``: it is evidence, and it
cannot join the index.

Sources: ``prefix`` (a forced ``local:`` / ``claude:`` ..., the legacy row shape
``forced``/``decided``/``door`` is kept), ``redo`` (the person pressed ``r`` on a routed
turn: a human-validated redo, label 1), ``rule`` (an "always local" user rule; its own
text is the example, label 0).

Retention: see ``docs/RETENTION.md`` (``examples.jsonl``). Mode 0600, directory 0700.
"""
from __future__ import annotations

import base64
import json
import math
import os
import tempfile
import time
from array import array
from pathlib import Path

from llm_router import paths, prompt_key

FILENAME = "examples.jsonl"
SOURCES = ("prefix", "redo", "rule")
MAX_BYTES = 2 * 1024 * 1024  # past this the OLDEST half is dropped
MAX_AGE_S = 180 * 86400  # rows older than this are ignored by every reader


def default_path() -> Path:
    return paths.llm_router_home() / FILENAME


def prefix_label(forced: str) -> float | None:
    """P(needs frontier) a forced prefix implies: ``claude``/``opus`` 1, ``local``/``codex``/
    ``haiku``/``sonnet`` 0 (below the frontier boundary). Unknown names: no label."""
    if forced in ("claude", "native", "opus"):
        return 1.0
    if forced in ("local", "codex", "haiku", "sonnet"):
        return 0.0
    return None


def encode_vec(vec) -> str:
    mag = math.sqrt(sum(x * x for x in vec))
    arr = array("f", (x / mag for x in vec) if mag else vec)
    import sys

    if sys.byteorder != "little":  # pragma: no cover
        arr.byteswap()
    return base64.b64encode(arr.tobytes()).decode("ascii")


def decode_vec(blob: str) -> array:
    arr = array("f")
    arr.frombytes(base64.b64decode(blob))
    import sys

    if sys.byteorder != "little":  # pragma: no cover
        arr.byteswap()
    return arr


def embedding_fields(text: str) -> dict:
    """``{"vec", "model"}`` for ``text``, or ``{}`` unless the kNN index is on and has an
    artifact (the embedding model is the artifact's). Any failure gives ``{}``."""
    try:
        from llm_router import complexity_knn

        if not complexity_knn.enabled():
            return {}
        art = complexity_knn.load_artifact()
        if art is None or not text or not text.strip():
            return {}
        vec = complexity_knn._embed(text, art.embedding_model)
        if vec is None or len(vec) != art.dim:
            return {}
        return {"vec": encode_vec(vec), "model": art.embedding_model}
    except Exception:  # noqa: BLE001 -- learning evidence never stops routing
        return {}


def append_row(row: dict, *, path: Path | str | None = None, max_bytes: int = MAX_BYTES) -> None:
    """Append one row (0600, bounded: past ``max_bytes`` the oldest half is dropped
    first). Raises on a failed write; callers decide how to fail open."""
    target = Path(path) if path is not None else default_path()
    payload = (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > max_bytes:
        lines = target.read_bytes().splitlines(keepends=True)
        fd, temporary = tempfile.mkstemp(prefix=".examples-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.writelines(lines[len(lines) // 2:])
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as stream:
        stream.write(payload)


def record(text: str | None, source: str, label: float, *, text_sha: str | None = None,
           now: float | None = None, path: Path | str | None = None) -> bool:
    """Append a ``source`` example. ``text`` (when given) is hashed and, when the kNN index
    is on, embedded; it is never stored. Pass ``text_sha`` when only the hash is known."""
    from llm_router import failopen

    try:
        if source not in SOURCES or label not in (0.0, 1.0):
            raise ValueError("invalid example source or label")
        if text is None and not text_sha:
            raise ValueError("an example needs a prompt or its hash")
        row = {"text_sha": text_sha or prompt_key.key(text), "source": source,
               "label": float(label), "ts": time.time() if now is None else now}
        if text is not None:
            row.update(embedding_fields(text))
        append_row(row, path=path)
        return True
    except Exception as exc:  # noqa: BLE001 -- evidence is fail-open
        failopen.record("LR-FO-EXAMPLE", exc)
        return False


def read_examples(path: Path | str | None = None, *, now: float | None = None) -> list[dict]:
    """Normalised rows, oldest first, newer than ``MAX_AGE_S``: ``{text_sha, source, label,
    ts, vec (array | None), model}``. Legacy prefix rows get a source and a derived label; rows
    with no usable label, and unparseable lines, are skipped."""
    target = Path(path) if path is not None else default_path()
    cutoff = (time.time() if now is None else now) - MAX_AGE_S
    out: list[dict] = []
    try:
        with target.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                row = _normalise(raw)
                if row is not None and row["ts"] >= cutoff:
                    out.append(row)
    except OSError:
        return []
    return out


def _normalise(raw) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("text_sha"), str):
        return None
    ts = raw.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    source = raw.get("source") or ("prefix" if "forced" in raw else None)
    label = raw.get("label")
    if label is None and source == "prefix" and isinstance(raw.get("forced"), str):
        label = prefix_label(raw["forced"])
    if source not in SOURCES or label not in (0, 1, 0.0, 1.0) or isinstance(label, bool):
        return None
    vec = None
    if isinstance(raw.get("vec"), str):
        try:
            vec = decode_vec(raw["vec"])
        except Exception:  # noqa: BLE001
            vec = None
    return {"text_sha": raw["text_sha"], "source": source, "label": float(label), "ts": float(ts),
            "vec": vec, "model": raw.get("model") if isinstance(raw.get("model"), str) else None}


def record_redo(msg_id: str, *, now: float | None = None, path: Path | str | None = None,
                ledger_path: Path | str | None = None) -> bool:
    """A human ``redone`` press on route ``msg_id`` becomes a label-1 example. The turn's
    prompt text is gone, so only the proxy ledger's ``text_sha`` for that ``msg_id`` is
    written (no ``vec``). False when the ledger has no such turn: nothing is invented."""
    from llm_router import failopen
    from llm_router.proxy import ledger

    try:
        rows = ledger.read_rows(Path(ledger_path) if ledger_path else None, days=30)
        sha = next((r["text_sha"] for r in reversed(rows)
                    if r.get("msg_id") == msg_id and isinstance(r.get("text_sha"), str)), None)
        if sha is None:
            return False
        return record(None, "redo", 1.0, text_sha=sha, now=now, path=path)
    except Exception as exc:  # noqa: BLE001 -- evidence is fail-open
        failopen.record("LR-FO-EXAMPLE", exc)
        return False
