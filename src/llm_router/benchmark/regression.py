"""Regression detector — Plan 07 Category G.3.

Stores benchmark scores per (version, policy, benchmark, split) tuple and
detects release-over-release drops larger than a configurable threshold.

Storage model: a single SQLite table ``benchmark_results`` (defined in
:mod:`llm_router.cost`) that's append-only. Versions are arbitrary strings
(typically git tags) so the detector orders runs chronologically by
``timestamp`` rather than parsing semver — we want the comparison to track
"when this score was recorded" even if a release skipped a version bump
or used an unusual tag scheme.

The plan's spec calls out a 0.005 threshold for the sub_10 Arena split:
"sub_10 Arena Score must not drop > 0.005". :data:`DEFAULT_DROP_THRESHOLD`
mirrors that. Callers running stricter or looser comparisons pass their
own value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

from llm_router.cost import _get_db

__all__ = [
    "DEFAULT_DROP_THRESHOLD",
    "BenchmarkRunRecord",
    "RegressionEntry",
    "RegressionReport",
    "store_result",
    "load_history",
    "detect_regressions",
]


# Mirrors plan 07's "must not drop > 0.005" gating threshold.
DEFAULT_DROP_THRESHOLD = 0.005


# ── Records ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BenchmarkRunRecord:
    """One historical row from ``benchmark_results``."""

    version: str
    policy: str
    benchmark: str
    split: str
    score: float
    n_samples: int
    timestamp: str
    per_subject: dict[str, float]


@dataclass(frozen=True)
class RegressionEntry:
    """A single detected regression between two adjacent versions.

    ``delta`` is signed (``current.score - previous.score``); the detector
    only flags entries where ``delta < -threshold``, so it's always negative
    for items in :attr:`RegressionReport.regressions`. Improvements show up
    in :attr:`RegressionReport.history` instead.
    """

    previous_version: str
    current_version: str
    previous_score: float
    current_score: float
    delta: float
    subject_breakdown: dict[str, tuple[float, float]]  # subject → (prev, curr)


@dataclass(frozen=True)
class RegressionReport:
    """End-to-end output: history + detected regressions."""

    policy: str
    benchmark: str
    split: str
    history: list[BenchmarkRunRecord]
    regressions: list[RegressionEntry]
    threshold: float

    @property
    def has_regressions(self) -> bool:
        return bool(self.regressions)


# ── Storage ─────────────────────────────────────────────────────────────────


async def store_result(
    *,
    version: str,
    policy: str,
    benchmark: str,
    split: str,
    score: float,
    n_samples: int,
    per_subject: dict[str, float] | None = None,
) -> None:
    """Append one benchmark run to ``benchmark_results``.

    Multiple runs of the same (version, policy, benchmark, split) tuple are
    allowed and stored as separate rows; :func:`load_history` returns them
    in insertion order so the most recent re-run wins for downstream diffing.
    """
    db = await _get_db()
    try:
        await db.execute(
            """INSERT INTO benchmark_results
                   (version, policy, benchmark, split, score, n_samples, per_subject_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                version,
                policy,
                benchmark,
                split,
                float(score),
                int(n_samples),
                json.dumps(per_subject or {}, sort_keys=True),
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def load_history(
    *,
    policy: str,
    benchmark: str,
    split: str | None = None,
    since_version: str | None = None,
) -> list[BenchmarkRunRecord]:
    """Return chronologically-ordered records for the given (policy, benchmark).

    ``since_version`` filters to entries at-or-after the first occurrence of
    that version. Versions appearing multiple times use the *earliest*
    occurrence as the cutoff so a re-run of the baseline doesn't truncate
    history.
    """
    where = ["policy = ?", "benchmark = ?"]
    params: list[object] = [policy, benchmark]
    if split is not None:
        where.append("split = ?")
        params.append(split)
    sql = f"""
        SELECT version, policy, benchmark, split, score, n_samples,
               timestamp, per_subject_json
          FROM benchmark_results
         WHERE {' AND '.join(where)}
         ORDER BY timestamp ASC, id ASC
    """

    try:
        db = await _get_db()
    except Exception:
        return []

    try:
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        # Pre-migration DBs lack the table. Returning empty matches the
        # semantics callers want: "nothing recorded yet, no regressions
        # to detect" — exactly what a fresh install should report.
        return []
    finally:
        await db.close()

    records = [
        BenchmarkRunRecord(
            version=row[0],
            policy=row[1],
            benchmark=row[2],
            split=row[3],
            score=float(row[4]),
            n_samples=int(row[5]),
            timestamp=row[6],
            per_subject=_parse_per_subject(row[7]),
        )
        for row in rows
    ]

    if since_version:
        for idx, rec in enumerate(records):
            if rec.version == since_version:
                return records[idx:]
        return []  # since_version never seen → nothing to report
    return records


def _parse_per_subject(raw: str | None) -> dict[str, float]:
    """Coerce the stored JSON into a typed dict, tolerating malformed rows."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}


# ── Detection ───────────────────────────────────────────────────────────────


def detect_regressions(
    history: list[BenchmarkRunRecord],
    *,
    threshold: float = DEFAULT_DROP_THRESHOLD,
) -> list[RegressionEntry]:
    """Walk ``history`` pairwise and emit entries where the score drops > threshold.

    Pairwise (each entry vs the immediately preceding entry) rather than
    against a fixed baseline because consecutive drops compound: a benchmark
    sliding from 0.70 → 0.695 → 0.690 → 0.685 → 0.68 wouldn't be caught by
    "below 0.70" gating, but each step would trip the pairwise check.
    """
    if len(history) < 2:
        return []
    out: list[RegressionEntry] = []
    for prev, curr in zip(history, history[1:]):
        delta = curr.score - prev.score
        if delta < -threshold:
            # Build a subject-level breakdown so the report can pinpoint
            # which classifier subject lost ground when the headline dropped.
            subjects = set(prev.per_subject) | set(curr.per_subject)
            breakdown = {
                s: (prev.per_subject.get(s, 0.0), curr.per_subject.get(s, 0.0))
                for s in sorted(subjects)
            }
            out.append(
                RegressionEntry(
                    previous_version=prev.version,
                    current_version=curr.version,
                    previous_score=prev.score,
                    current_score=curr.score,
                    delta=delta,
                    subject_breakdown=breakdown,
                )
            )
    return out


async def build_report(
    *,
    policy: str,
    benchmark: str,
    split: str | None = None,
    since_version: str | None = None,
    threshold: float = DEFAULT_DROP_THRESHOLD,
) -> RegressionReport:
    """Convenience entry point: load history + detect → :class:`RegressionReport`."""
    history = await load_history(
        policy=policy,
        benchmark=benchmark,
        split=split,
        since_version=since_version,
    )
    regressions = detect_regressions(history, threshold=threshold)
    return RegressionReport(
        policy=policy,
        benchmark=benchmark,
        split=split or "<any>",
        history=history,
        regressions=regressions,
        threshold=threshold,
    )


def format_report(report: RegressionReport) -> str:
    """Render a :class:`RegressionReport` as a CLI-friendly multi-line string."""
    header = (
        f"Regression check: {report.policy}/{report.benchmark} (split={report.split})\n"
        f"Threshold: drop > {report.threshold:.4f}\n"
    )
    if not report.history:
        return header + "No history recorded yet — run `llm-router benchmark run` first."

    lines = [header, "History:"]
    prev_score: float | None = None
    for rec in report.history:
        if prev_score is None:
            marker = ""
        else:
            delta = rec.score - prev_score
            marker = f" ({delta:+.4f} {'✗' if delta < -report.threshold else '✓'})"
        lines.append(
            f"  {rec.version:<12} score={rec.score:.4f} "
            f"n={rec.n_samples} ts={rec.timestamp}{marker}"
        )
        prev_score = rec.score

    if report.regressions:
        lines.append("")
        lines.append("Regressions:")
        for r in report.regressions:
            lines.append(
                f"  {r.previous_version} → {r.current_version}  "
                f"{r.previous_score:.4f} → {r.current_score:.4f}  "
                f"(Δ={r.delta:+.4f})"
            )
            for subj, (prev, curr) in r.subject_breakdown.items():
                if abs(curr - prev) >= report.threshold:
                    arrow = "↘" if curr < prev else "↗"
                    lines.append(f"    {arrow} {subj}: {prev:.3f} → {curr:.3f}")
    else:
        lines.append("")
        lines.append("No regressions detected.")
    return "\n".join(lines)
