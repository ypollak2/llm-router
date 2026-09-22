"""The accumulation pool: eligible tasks waiting to become Ground Truth.

This is NOT Ground Truth. It is the queue that a future Ground Truth v1 will be
sampled from, which is the whole point — sampling from raw telemetry is what
produced a corpus with no labels in it.

Lifecycle, with a reason on every transition:

    CAPTURED ─► ELIGIBLE ─► READY_FOR_REPLAY ─► VERIFIED ─► FROZEN_IN_GROUND_TRUTH
        │           │              │               │
        ▼           ▼              ▼               ▼
    INELIGIBLE  INELIGIBLE   REPLAY_FAILED   VERIFICATION_UNAVAILABLE / AMBIGUOUS

Transitions are checked, not assumed: `advance()` refuses a jump that skips a
state. A candidate cannot be marked VERIFIED without having been replay-ready,
because "we think it would probably work" is exactly the guess this phase
exists to eliminate.

Deduplication reuses the seed-v2 machinery (`extract_corpus.tokens`,
`exact_key`, `jaccard`) rather than a second implementation, and keeps funnel
attribution: a rejected task is recorded with its reason, never dropped
silently.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from groundtruth.eligibility import Eligibility  # noqa: E402
from groundtruth.extract_corpus import exact_key, jaccard, tokens  # noqa: E402

SCHEMA_VERSION = 1

# ── Lifecycle states ─────────────────────────────────────────────────────────
CAPTURED = "CAPTURED"
ELIGIBLE = "ELIGIBLE"
READY_FOR_REPLAY = "READY_FOR_REPLAY"
VERIFIED = "VERIFIED"
FROZEN = "FROZEN_IN_GROUND_TRUTH"

INELIGIBLE = "INELIGIBLE"
REPLAY_FAILED = "REPLAY_FAILED"
VERIFICATION_UNAVAILABLE = "VERIFICATION_UNAVAILABLE"
AMBIGUOUS = "AMBIGUOUS"

TERMINAL_FAILURES = frozenset(
    {INELIGIBLE, REPLAY_FAILED, VERIFICATION_UNAVAILABLE, AMBIGUOUS})

# Which states may follow which. A failure state is reachable from anywhere;
# forward progress is strictly one step at a time.
_ALLOWED: dict[str, frozenset[str]] = {
    CAPTURED: frozenset({ELIGIBLE, INELIGIBLE}),
    ELIGIBLE: frozenset({READY_FOR_REPLAY, INELIGIBLE, VERIFICATION_UNAVAILABLE}),
    READY_FOR_REPLAY: frozenset({VERIFIED, REPLAY_FAILED, AMBIGUOUS,
                                 VERIFICATION_UNAVAILABLE}),
    VERIFIED: frozenset({FROZEN, AMBIGUOUS}),
    FROZEN: frozenset(),
    INELIGIBLE: frozenset(),
    REPLAY_FAILED: frozenset({READY_FOR_REPLAY}),   # a fixed envelope may retry
    VERIFICATION_UNAVAILABLE: frozenset({READY_FOR_REPLAY}),
    AMBIGUOUS: frozenset({READY_FOR_REPLAY}),
}

# Dedup reasons, mirroring seed-v2's funnel vocabulary.
DUP_EXACT = "duplicate-exact"
DUP_NEAR = "duplicate-near"


@dataclass
class Transition:
    at: float
    frm: str
    to: str
    reason: str


@dataclass
class Candidate:
    task_id: str
    prompt_sha256: str
    route_id: str | None = None
    session_id: str | None = None
    task_type: str | None = None
    complexity: str | None = None
    captured_at: float = 0.0

    state: str = CAPTURED
    history: list[Transition] = field(default_factory=list)

    eligibility: dict = field(default_factory=dict)
    envelope: dict = field(default_factory=dict)

    replay_ready: bool = False
    verifier_class: str = "none-known"
    needs_reference_answer: bool = False
    required_state_complete: bool = False
    scrubbed: bool = True
    scrub_counts: dict = field(default_factory=dict)
    residual_flags: list[str] = field(default_factory=list)

    content_sha: str = ""
    duplicate_count: int = 1
    source: str = "prompt_capture"

    def advance(self, to: str, reason: str) -> bool:
        """Move to `to` if the transition is legal. Returns False otherwise.

        Refusing an illegal jump matters more than it looks: the only way a
        candidate reaches VERIFIED is by actually having been replayed, so a
        bug that skipped a state would silently manufacture confidence.
        """
        if to not in _ALLOWED.get(self.state, frozenset()):
            return False
        self.history.append(Transition(time.time(), self.state, to, reason))
        self.state = to
        return True

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Candidate":
        hist = [Transition(**t) for t in d.get("history", [])]
        known = {k: v for k, v in d.items()
                 if k in cls.__dataclass_fields__ and k != "history"}
        c = cls(**known)
        c.history = hist
        return c


def default_pool_path() -> Path:
    home = Path(os.environ.get("LLM_ROUTER_HOME", Path.home() / ".llm-router"))
    return home / "ground_truth_candidates.jsonl"


def default_funnel_path() -> Path:
    return default_pool_path().with_suffix(".funnel.jsonl")


class Pool:
    """Append-only candidate store plus a rejection funnel.

    Append-only because a candidate's history is evidence. Superseding rows win
    on read, so a state change is a new row rather than a rewrite, and the
    sequence of transitions survives.
    """

    def __init__(self, path: Path | None = None, funnel: Path | None = None):
        self.path = path or default_pool_path()
        self.funnel_path = funnel or default_funnel_path()
        self._index: dict[str, Candidate] = {}
        self._dedup_sets: list[tuple[str, frozenset[str]]] = []
        self.load()

    # ── persistence ──────────────────────────────────────────────────────────
    def load(self) -> None:
        self._index.clear()
        self._dedup_sets.clear()
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                c = Candidate.from_json(json.loads(line))
            except Exception:  # noqa: BLE001 — a bad row must not sink the pool
                continue
            self._index[c.task_id] = c
        for c in self._index.values():
            self._dedup_sets.append((c.task_id, frozenset(tokens(c.envelope.get("prompt") or ""))))

    def _lock(self):
        """H-07. Serialise the read-modify-append on the pool file.

        `duplicate_count += 1` reads a Candidate loaded into memory at
        construction, increments it, and appends the whole row; `load()` then
        takes last-wins per task_id. Two admits of the same duplicate therefore
        both read 1, both write 2, and one increment is lost. Measured: **19
        recorded where 21 occurred**. `accumulate.py` constructs a fresh `Pool()`
        per call, so the production path hits this every time.

        `file_lock.exclusive_lock` already exists for exactly this shape -- it
        was written for `session_store.record_event`, whose append-then-compact
        critical section lost 22 of 1200 writes under six-process load. Locking a
        SIBLING file, not the data file, so the JSONL inode stays swappable.
        """
        try:
            from llm_router.file_lock import exclusive_lock

            return exclusive_lock(self.path.with_suffix(self.path.suffix + ".lock"))
        except Exception:  # noqa: BLE001 — a locking problem must not stop accumulation
            import contextlib

            return contextlib.nullcontext(False)

    def _append(self, c: Candidate) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(c.to_json(), ensure_ascii=False, sort_keys=True) + "\n")

    def _reject(self, reason: str, detail: dict) -> None:
        self.funnel_path.parent.mkdir(parents=True, exist_ok=True)
        with self.funnel_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "reason": reason, **detail},
                                ensure_ascii=False, sort_keys=True) + "\n")

    # ── admission ────────────────────────────────────────────────────────────
    def find_duplicate(self, prompt: str, *, threshold: float = 0.9) -> tuple[str | None, str | None]:
        """(task_id, reason) of an existing near-identical candidate, or (None, None).

        Uses the same tokeniser and Jaccard threshold as the seed-v2 corpus so
        the pool and the historical set agree on what "the same task" means.
        """
        key = exact_key(prompt)
        for tid, c in self._index.items():
            if c.content_sha == key:
                return tid, DUP_EXACT
        tset = frozenset(tokens(prompt))
        if not tset:
            return None, None
        for tid, other in self._dedup_sets:
            if jaccard(tset, other) >= threshold:
                return tid, DUP_NEAR
        return None, None

    def admit(self, candidate: Candidate, prompt: str, *,
              dedup: bool = True, threshold: float = 0.9) -> tuple[bool, str]:
        """Add a candidate. Returns (admitted, reason).

        A rejected task is written to the funnel with its reason, so the pool
        can always answer "why are tasks not accumulating" rather than only
        "how many did".
        """
        # T-12 (audit 2026-09-22). The H-07 fix locked ONLY the
        # duplicate-increment branch. `find_duplicate` still ran outside the
        # lock, and so did both append branches — so two threads admitting the
        # SAME NEW prompt each saw no duplicate, each fell through, and each
        # appended a canonical row. Measured: 20 threads, one prompt, **2
        # canonical rows where 1 was correct**.
        #
        # Locking only the increment fixes the second arrival and leaves the
        # first-arrival race wide open, which is the subtler half: a duplicate
        # miscount is a wrong number, but two canonical rows are two different
        # tasks claiming to be the same one.
        #
        # The whole decision — look for a duplicate, decide, append — is now one
        # critical section over a freshly loaded index. `_reject` writes to the
        # funnel, a different file, and takes no lock, so calling it in here
        # cannot deadlock.
        with self._lock():
            # Re-read INSIDE the lock. The in-memory copy was loaded at
            # construction and may already be stale; deciding on it is what
            # loses the count and duplicates the row.
            self.load()

            if dedup:
                dup_id, dup_reason = self.find_duplicate(prompt, threshold=threshold)
                if dup_id:
                    existing = self._index.get(dup_id)
                    if existing is None:
                        # compacted away between load and lookup
                        return False, dup_reason
                    existing.duplicate_count += 1
                    self._append(existing)
                    self._reject(dup_reason, {"task_id": candidate.task_id,
                                              "duplicate_of": dup_id})
                    return False, dup_reason

            if not candidate.eligibility.get("ground_truth_candidate"):
                reasons = candidate.eligibility.get("ineligibility_reasons") or ["unknown"]
                candidate.advance(INELIGIBLE, f"gate: {reasons[0]}")
                self._append(candidate)
                self._index[candidate.task_id] = candidate
                self._reject(reasons[0], {"task_id": candidate.task_id})
                return False, reasons[0]

            candidate.advance(ELIGIBLE, "passed the eligibility gate")
            if candidate.required_state_complete:
                candidate.advance(READY_FOR_REPLAY, "replay envelope is complete")
                candidate.replay_ready = True
            self._index[candidate.task_id] = candidate
            self._dedup_sets.append((candidate.task_id, frozenset(tokens(prompt))))
            self._append(candidate)
            return True, candidate.state

    # ── queries ──────────────────────────────────────────────────────────────
    def all(self) -> list[Candidate]:
        return list(self._index.values())

    def in_state(self, *states: str) -> list[Candidate]:
        return [c for c in self._index.values() if c.state in states]

    def stats(self) -> dict:
        cands = self.all()
        by_state = Counter(c.state for c in cands)
        by_type = Counter(c.task_type or "unknown" for c in cands
                          if c.state not in TERMINAL_FAILURES)
        by_verifier = Counter(c.verifier_class for c in cands
                              if c.state not in TERMINAL_FAILURES)
        rejected: Counter[str] = Counter()
        if self.funnel_path.exists():
            for line in self.funnel_path.read_text(errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    rejected[json.loads(line).get("reason", "unknown")] += 1
                except Exception:  # noqa: BLE001
                    continue
        eligible = [c for c in cands if c.state not in TERMINAL_FAILURES
                    and c.state != CAPTURED]
        return {
            "captured": len(cands),
            "by_state": dict(by_state),
            "eligible": len(eligible),
            "replay_ready": len(self.in_state(READY_FOR_REPLAY, VERIFIED, FROZEN)),
            "verifier_candidate": sum(1 for c in eligible
                                      if c.verifier_class != "none-known"),
            # High-confidence excludes anything still waiting on a human: a
            # residual privacy flag, or a reference answer nobody has
            # established. Counting those would be counting intent as evidence.
            "high_confidence": sum(
                1 for c in eligible
                if c.required_state_complete and c.verifier_class != "none-known"
                and not c.residual_flags and not c.needs_reference_answer),
            "needs_human_review": sum(1 for c in eligible
                                      if c.residual_flags or c.needs_reference_answer),
            "by_task_type": dict(by_type),
            "by_verifier_class": dict(by_verifier),
            "rejected": dict(rejected),
        }


def make_candidate(
    *,
    task_id: str,
    prompt: str,
    prompt_sha256: str,
    eligibility: Eligibility,
    envelope: dict | None = None,
    route_id: str | None = None,
    session_id: str | None = None,
    task_type: str | None = None,
    complexity: str | None = None,
    scrub_counts: dict | None = None,
    residual_flags: list[str] | None = None,
) -> Candidate:
    env = envelope or {}
    return Candidate(
        task_id=task_id,
        prompt_sha256=prompt_sha256,
        route_id=route_id,
        session_id=session_id,
        task_type=task_type,
        complexity=complexity,
        captured_at=time.time(),
        eligibility=eligibility.to_json(),
        envelope=env,
        verifier_class=eligibility.verifier_class,
        needs_reference_answer=eligibility.needs_reference_answer,
        required_state_complete=bool(env.get("complete")),
        scrub_counts=dict(scrub_counts or {}),
        residual_flags=list(residual_flags or []),
        content_sha=exact_key(prompt),
    )
