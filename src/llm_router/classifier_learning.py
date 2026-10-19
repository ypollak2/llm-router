# SPDX-License-Identifier: MIT
"""The learning step and its update gate (PLAN v16 P3.6-a, R-CLS-5).

``examples.jsonl`` (``examples``) holds what the person taught the router. This module turns
the rows that carry an embedding into a CANDIDATE L3 kNN index (the current artifact plus the
new examples) and lets it replace the live artifact only if it passes the gate:

    each update is re-scored on a held-out replay set; it is rejected when exact-match or
    under-route worsens by more than 2 points.

D-121: the local LLM classifier is killed, so there is no L2 few-shot path. The examples feed
the L3 kNN only; nothing here calls a model.

The replay set is a JSONL file named by ``LLM_ROUTER_LEARNING_REPLAY`` (the owner's private
GT-500 in production; a synthetic fixture in tests). One row per held-out prompt::

    {"vec": "<base64 float32 embedding>" | [floats], "needs_frontier": true|false,
     "text_sha": "<optional 16 hex>"}

(``truth`` = a complexity name may replace ``needs_frontier``; ``complex`` means frontier.)
Rows are embedded in advance with the artifact's embedding model, so the gate makes no model
call. Held-out stays held out: an example whose ``text_sha`` is in the replay set is never
added to the index, so ``text_sha`` is REQUIRED on every replay row: a replay file with a row
that lacks it is rejected whole (an unhashed row could be trained on and scored on).

One ``learn`` at a time: a lock file (``complexity_knn.json.lock``, ``file_lock``, bounded wait) serialises
runs, the base artifact is re-checked before the swap and ``.prev`` is written only for a base
that is still the one this run read, so a concurrent run's ``applied_shas`` are never dropped.

Fail closed: no replay set, an unreadable one, or fewer than ``LLM_ROUTER_LEARNING_MIN_REPLAY``
(default 100) usable rows, and the update is rejected. An empty set passes everything, so it
passes nothing. Without a base artifact there is no baseline to compare with, so nothing is
learned. Every decision is one row (counts and rates, no text) in ``classifier_learning.jsonl``.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from llm_router import capped_log, complexity_knn, examples, paths

REPLAY_ENV = "LLM_ROUTER_LEARNING_REPLAY"
MIN_REPLAY_ENV = "LLM_ROUTER_LEARNING_MIN_REPLAY"
DEFAULT_MIN_REPLAY = 100
MAX_WORSENING = 0.02  # 2 points, strict: a drop of exactly 2.0 passes, anything more is rejected
DECISIONS_FILE = "classifier_learning.jsonl"
DECISIONS_MAX_BYTES = 256 * 1024

ACCEPTED = "accepted"
REJECTED = "rejected"
NOTHING_NEW = "nothing_new"


def replay_path() -> Path | None:
    raw = os.environ.get("LLM_ROUTER_LEARNING_REPLAY", "").strip()
    return Path(raw).expanduser() if raw else None


def min_replay() -> int:
    try:
        return max(1, int(os.environ.get("LLM_ROUTER_LEARNING_MIN_REPLAY", "").strip() or DEFAULT_MIN_REPLAY))
    except ValueError:
        return DEFAULT_MIN_REPLAY


@dataclass(frozen=True)
class Metrics:
    n: int
    exact: float  # share of replay rows whose frontier/not-frontier call equals the truth
    under_route: float  # share of replay rows that needed frontier and were not sent there


@dataclass(frozen=True)
class Verdict:
    status: str  # accepted | rejected | nothing_new
    reason: str
    base: Metrics | None = None
    candidate: Metrics | None = None
    added: int = 0
    detail: dict = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.status == ACCEPTED


class ReplayError(ValueError):
    """The replay file cannot be trusted (a row without ``text_sha``)."""


def load_replay(path: Path | str | None) -> list[tuple]:
    """``[(vector, needs_frontier, text_sha)]``; unparseable rows are skipped, but a parseable
    row without a ``text_sha`` raises ``ReplayError``: the held-out guard needs every hash."""
    if path is None:
        return []
    out: list[tuple] = []
    missing = 0
    try:
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    raw = json.loads(line)
                    vec = raw["vec"]
                    vec = examples.decode_vec(vec) if isinstance(vec, str) else [float(x) for x in vec]
                    if "needs_frontier" in raw:
                        truth = bool(raw["needs_frontier"])
                    else:
                        truth = str(raw["truth"]) == "complex"
                except (ValueError, KeyError, TypeError):
                    continue
                sha = raw.get("text_sha")
                if not isinstance(sha, str) or not sha:
                    missing += 1
                    continue
                out.append((vec, truth, sha))
    except OSError:
        return []
    if missing:
        raise ReplayError(f"{missing} replay row(s) have no text_sha")
    return out


def evaluate(art: complexity_knn.Artifact, replay: list[tuple]) -> Metrics:
    exact = under = 0
    n = 0
    for vec, truth, _ in replay:
        if len(vec) != art.dim:
            continue
        n += 1
        pred = complexity_knn.score_vector(vec, art).needs_frontier
        exact += pred == truth
        under += truth and not pred
    return Metrics(n, exact / n, under / n) if n else Metrics(0, 0.0, 0.0)


def candidate_rows(art: complexity_knn.Artifact, rows: list[dict], replay_shas: set[str]):
    """``(vectors, labels, shas)`` of examples that may join ``art``: they carry a vector of the
    artifact's model and dimension, are not in the replay set and were not applied before.
    One row per ``text_sha``; the newest wins."""
    applied = set((art.provenance.get("learning") or {}).get("applied_shas") or ())
    newest: dict[str, dict] = {}
    for row in rows:
        if row["vec"] is not None and len(row["vec"]) == art.dim and row["model"] == art.embedding_model:
            newest[row["text_sha"]] = row
    vecs, labels, shas = [], [], []
    for sha, row in newest.items():
        if sha in replay_shas or sha in applied:
            continue
        vecs.append(row["vec"])
        labels.append(row["label"])
        shas.append(sha)
    return vecs, labels, shas


def build_candidate(art, vecs, labels, shas, *, now: float) -> complexity_knn.Artifact:
    prov = dict(art.provenance)
    prev = prov.get("learning") or {}
    applied = list(prev.get("applied_shas") or ()) + shas
    prov["learning"] = {"applied_shas": applied[-5000:], "updated_at": now,
                        "examples_added_total": int(prev.get("examples_added_total") or 0) + len(shas)}
    return complexity_knn.Artifact.from_parts(
        [*art.rows, *vecs], [*art.labels, *labels], embedding_model=art.embedding_model, k=art.k,
        kappa=art.kappa, temperature=art.temperature, threshold=art.threshold, provenance=prov)


def gate(base: complexity_knn.Artifact, candidate: complexity_knn.Artifact, replay: list[tuple],
         *, min_n: int | None = None) -> Verdict:
    need = min_replay() if min_n is None else min_n
    usable = [r for r in replay if len(r[0]) == base.dim]
    if len(usable) < need:
        return Verdict(REJECTED, f"replay_too_small: {len(usable)} usable rows < {need}")
    b, c = evaluate(base, usable), evaluate(candidate, usable)
    worse = []
    if b.exact - c.exact > MAX_WORSENING + 1e-12:
        worse.append(f"exact {b.exact:.3f} -> {c.exact:.3f}")
    if c.under_route - b.under_route > MAX_WORSENING + 1e-12:
        worse.append(f"under_route {b.under_route:.3f} -> {c.under_route:.3f}")
    if worse:
        return Verdict(REJECTED, "worse_by_more_than_2_pts: " + "; ".join(worse), b, c)
    return Verdict(ACCEPTED, "within_2_pts_on_replay", b, c)


def decisions_path() -> Path:
    return paths.state_path(DECISIONS_FILE)


def _log(verdict: Verdict, *, dry_run: bool, now: float) -> None:
    row = {"ts": now, "status": verdict.status, "reason": verdict.reason, "added": verdict.added,
           "dry_run": dry_run}
    for name, m in (("base", verdict.base), ("candidate", verdict.candidate)):
        if m is not None:
            row[name] = {"n": m.n, "exact": round(m.exact, 4), "under_route": round(m.under_route, 4)}
    try:
        capped_log.append(decisions_path(), (json.dumps(row, separators=(",", ":")) + "\n").encode(),
                          DECISIONS_MAX_BYTES)
    except OSError:
        pass  # the decision itself stands; a failed log row is not a reason to learn or not


def _file_id(path: Path) -> str | None:
    """Content hash of a file, or None when it does not exist."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


LOCK_TIMEOUT_S = 60.0  # how long a second `learn` waits for the first before giving up


class LockTimeout(RuntimeError):
    """Another ``learn`` (or the lock primitive) did not release ``<artifact>.lock`` in time."""


@contextlib.contextmanager
def _lock(target: Path, timeout: float | None = None):
    """Exclusive lock on ``<artifact>.lock`` for the whole run. Waits at most ``timeout``
    (default ``LOCK_TIMEOUT_S``), then raises ``LockTimeout``. Uses ``file_lock``
    (``fcntl.flock``; ``msvcrt.locking`` on Windows), so ``learn`` runs there too."""
    from llm_router import file_lock

    status: dict = {}
    wait = LOCK_TIMEOUT_S if timeout is None else timeout
    with file_lock.exclusive_lock(Path(str(target) + ".lock"), wait, status) as got:
        if not got:
            raise LockTimeout(
                f"another 'llm-router learn' holds {target.name}.lock (waited {wait:g}s, lock {status.get('state')}); "
                "try again when it finishes")
        yield


def _write_prev(target: Path) -> None:
    """Copy the live artifact to ``<artifact>.prev`` atomically (unique temp name)."""
    prev = target.with_suffix(target.suffix + ".prev")
    fd, tmp = tempfile.mkstemp(prefix=prev.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(target.read_bytes())
        os.replace(tmp, prev)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _install(target: Path, cand: complexity_knn.Artifact, base_id: str | None) -> None:
    """Swap ``cand`` in. Raises ``BaseChanged`` when the artifact is no longer the base this run read."""
    if _file_id(target) != base_id:
        raise BaseChanged()
    if target.exists():
        _write_prev(target)
    complexity_knn.write_artifact(
        target, [list(r) for r in cand.rows], list(cand.labels), embedding_model=cand.embedding_model,
        k=cand.k, kappa=cand.kappa, temperature=cand.temperature, threshold=cand.threshold,
        provenance=cand.provenance)
    complexity_knn.clear_cache()


class BaseChanged(Exception):
    pass


def apply(*, dry_run: bool = False, replay: Path | str | None = None,
          examples_path: Path | str | None = None, now: float | None = None) -> Verdict:
    """Build the candidate from new examples, gate it, and (unless ``dry_run``) install it.
    Serialised by a lock file; also deletes example rows older than 180 days."""
    with _lock(complexity_knn.artifact_path()):
        return _apply_locked(dry_run=dry_run, replay=replay, examples_path=examples_path, now=now)


def _apply_locked(*, dry_run, replay, examples_path, now) -> Verdict:
    stamp = time.time() if now is None else now
    try:
        examples.prune(examples_path, now=stamp)
    except OSError:
        pass  # retention housekeeping must not stop a learning run
    complexity_knn.clear_cache()
    target = complexity_knn.artifact_path()
    base_id = _file_id(target)
    art = complexity_knn.load_artifact()
    if art is None:
        v = Verdict(REJECTED, "no_base_artifact: nothing to compare an update with")
        _log(v, dry_run=dry_run, now=stamp)
        return v
    rows = examples.read_examples(examples_path, now=stamp)
    try:
        replay_rows = load_replay(Path(replay) if replay else replay_path())
    except ReplayError as exc:
        v = Verdict(REJECTED, f"replay_missing_text_sha: {exc}")
        _log(v, dry_run=dry_run, now=stamp)
        return v
    vecs, labels, shas = candidate_rows(art, rows, {r[2] for r in replay_rows if r[2]})
    if not shas:
        return Verdict(NOTHING_NEW, "no new examples with an embedding",
                       detail={"examples_total": len(rows), "without_vector": sum(r["vec"] is None for r in rows)})
    cand = build_candidate(art, vecs, labels, shas, now=stamp)
    v = gate(art, cand, replay_rows)
    v = Verdict(v.status, v.reason, v.base, v.candidate, len(shas))
    if v.accepted and not dry_run:
        try:
            _install(target, cand, base_id)
        except BaseChanged:
            v = Verdict(REJECTED, "base_changed_during_run: the artifact was replaced by another run; nothing written",
                        v.base, v.candidate, len(shas))
        except OSError as exc:
            v = Verdict(REJECTED, f"write_failed: {type(exc).__name__}", v.base, v.candidate, len(shas))
    _log(v, dry_run=dry_run, now=stamp)
    return v


def purge_user_examples() -> Verdict:
    """Rebuild the artifact without the user-derived rows ``learn`` appended.

    ``learn`` only ever appends, so those rows are the LAST ``examples_added_total`` rows of the
    artifact (``build_complexity_knn.py`` writes a fresh artifact with no ``learning`` provenance).
    ``.prev`` is replaced by a copy of the purged artifact (the pre-purge one holds the user
    embeddings, so it is not kept). ``applied_shas`` is cleared, so a later
    ``learn`` would apply the still-stored examples again: delete ``examples.jsonl`` too."""
    stamp = time.time()
    with _lock(complexity_knn.artifact_path()):
        complexity_knn.clear_cache()
        target = complexity_knn.artifact_path()
        base_id = _file_id(target)
        art = complexity_knn.load_artifact()
        if art is None:
            return Verdict(REJECTED, "no_base_artifact: nothing to purge")
        n = (art.provenance.get("learning") or {}).get("examples_added_total")
        if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
            return Verdict(NOTHING_NEW, "no user-derived rows in the artifact")
        if n >= art.n:
            return Verdict(REJECTED, "purge_would_empty_the_artifact: restore the shipped one instead")
        prov = {k: v for k, v in art.provenance.items() if k != "learning"}
        prov["learning_purged"] = {"at": stamp, "rows": n}
        cand = complexity_knn.Artifact.from_parts(
            list(art.rows[:-n]), list(art.labels[:-n]), embedding_model=art.embedding_model, k=art.k,
            kappa=art.kappa, temperature=art.temperature, threshold=art.threshold, provenance=prov)
        try:
            _install(target, cand, base_id)
        except BaseChanged:
            return Verdict(REJECTED, "base_changed_during_run: nothing written")
        except OSError as exc:
            return Verdict(REJECTED, f"write_failed: {type(exc).__name__}")
        # _install left the pre-purge artifact as .prev: it still holds every user-derived
        # embedding. Replace it with a copy of the purged artifact (or remove it).
        prev = target.with_suffix(target.suffix + ".prev")
        try:
            _write_prev(target)
        except OSError:
            try:
                prev.unlink(missing_ok=True)
            except OSError as exc:
                return Verdict(REJECTED, f"purged_but_prev_not_removed: {type(exc).__name__}; delete {prev}",
                               added=-n)
        v = Verdict(ACCEPTED, "user_derived_rows_removed", added=-n)
        _log(v, dry_run=False, now=stamp)
        return v
