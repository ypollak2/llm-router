"""Counts-only redo census and frozen validation draw (P3.6-c).

Prompt and answer text is read only in memory; the pool contains metadata only.
The local frozen.json stores the salt so a later labeller can re-derive pair
identities from transcripts. Frozen metadata records whether untagged sessions
were admitted, exclusion provenance and, when enabled, per-kind populations and
draw sizes beside the combined strata. With --include-untagged (D-275) a session's kind is
resolved from ALL evidence by resolve_with_all_evidence: the owner override wins; else any
non-organic kind from the tag file, backfill sidecar or proxy ledger (the whole ledger,
streamed) excludes the session; else organic evidence makes it organic; else it is untagged.
Disagreeing sources exclude the session (excluded_by_evidence). An unreadable or corrupt
overrides file, tag file, sidecar or ledger refuses the run with counts only. The census
then reports the ledger and backfill sidecar states and untagged counts by cause (tag,
ledger and sidecar facets); with the flag off it prints none of those include-untagged
keys. Run directories are 0700 and files are 0600.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from llm_router import (northstar, offload_share, paths, redo_signal, session_kind,
                        session_kind_backfill)

MIN_FLAGGED = 19
DEFAULT_SEED = 360
DEFAULT_TUNE_SEED = 361         # a tune set has its own seed: never the test seed
TUNE_PREFIX = "tune-"
VOID_FILE = "voided.json"       # in a tune run dir: the run is voided (counts-only record, kept)
_VOID_REASON = re.compile(r"[A-Za-z0-9 _.,:;()/#-]{1,200}")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
EMPTY_EXCLUSION = "M0.9 exclusion list EMPTY: spent M0.9 pairs are NOT excluded"
LEDGER_PRESENT, LEDGER_MISSING, LEDGER_UNREADABLE = "present", "missing", "unreadable"
LEDGER_CORRUPT = "corrupt"      # non-blank lines, none of which parsed to a row
LEDGER_MISSING_WARNING = (
    "WARNING: proxy ledger missing: sessions are resolved from tag files and the backfill "
    "sidecar only")
Resolver = Callable[[str], session_kind.Resolution]
ServedLoader = Callable[[str], list[tuple[float, str]]]
_EXCLUSION_KEY = re.compile(r"[0-9a-f]{16}")


class InvalidExclusionFile(ValueError):
    """Lines that are neither blank, comments nor 16 lowercase hex characters; carries the count only."""

    def __init__(self, count: int) -> None:
        super().__init__(f"{count} invalid exclusion line(s)")
        self.count = count


class EvidenceUnreadable(RuntimeError):
    """An evidence source (overrides, tag files, sidecar, ledger) is unreadable or corrupt (D-275).

    Carries counts only: ``overrides``, ``tag_files``, ``sidecar`` and ``ledger``."""

    def __init__(self, overrides: int, tag_files: int, sidecar: int, ledger: int) -> None:
        self.counts = dict(overrides=overrides, tag_files=tag_files, sidecar=sidecar,
                           ledger=ledger)
        self.total = overrides + tag_files + sidecar + ledger
        super().__init__(f"{self.total} unreadable evidence source(s)")

    def message(self) -> str:
        detail = " ".join(f"{k}={v}" for k, v in self.counts.items())
        return f"Refused: {self.total} unreadable evidence source(s): {detail}"


class PriorRunUnreadable(RuntimeError):
    """A run directory under redo_validation/ cannot be read as a run: disjointness cannot be proven.

    Carries run directory names only, never paths, and their count."""

    def __init__(self, runs: list[str]) -> None:
        self.runs = sorted(runs)
        self.count = len(self.runs)
        super().__init__(f"{self.count} unreadable prior run directory(ies)")


@dataclass(frozen=True)
class PriorRun:
    """The salted pair ids of an earlier run (``purpose`` is ``tune`` or ``test``; legacy runs are test)."""
    purpose: str
    salt: str
    pair_ids: frozenset[str]


def tune_dir_name(run: str) -> str:
    """The run-directory name of a tune id: ``tune-<id>``; an id that already has the prefix is kept."""
    return run if run.startswith(TUNE_PREFIX) else TUNE_PREFIX + run


def load_prior_runs(purposes: set[str]) -> list[PriorRun]:
    """Every run directory under ``redo_validation/`` whose purpose is in ``purposes``.

    A directory (not dot-prefixed) whose manifest or frozen.json is missing or invalid raises
    ``PriorRunUnreadable``: an unreadable run cannot be proven disjoint."""
    root = paths.state_path("redo_validation")
    if not root.is_dir():
        return []
    runs: list[PriorRun] = []
    bad_runs: list[str] = []
    for run_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        try:
            frozen = json.loads((run_dir / "frozen.json").read_text(encoding="utf-8"))
            rows = [json.loads(line) for line in
                    (run_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
                    if line.strip()]
            salt = frozen["salt"]
            ids = frozenset(row["pair_id"] for row in rows)
            if not isinstance(salt, str) or not all(isinstance(i, str) for i in ids):
                raise ValueError("invalid run")
        except (OSError, ValueError, KeyError, TypeError):
            bad_runs.append(run_dir.name)
            continue
        purpose = "tune" if frozen.get("purpose") == "tune" else "test"
        if purpose in purposes:
            runs.append(PriorRun(purpose, salt, ids))
    if bad_runs:
        raise PriorRunUnreadable(bad_runs)
    return runs


def tune_run_states() -> tuple[list[str], list[dict]]:
    """``(live tune run names, voided tune run records)`` under ``redo_validation/``.

    One P3.6-c study has ONE canonical tune set: a second ``--tune`` refuses while a live tune run exists,
    and a non-baseline freeze compares only on the live one. A voided run keeps its directory (its pairs
    stay excluded from later draws) and its ``voided.json`` record ``{run, reason, voided_utc}``. Raises
    ``ValueError`` on an unreadable frozen.json or void record."""
    root = paths.state_path("redo_validation")
    live: list[str] = []
    voided: list[dict] = []
    if not root.is_dir():
        return live, voided
    for run_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        frozen = json.loads((run_dir / "frozen.json").read_text(encoding="utf-8"))
        if not isinstance(frozen, dict) or frozen.get("purpose") != "tune":
            continue
        if not os.path.lexists(run_dir / VOID_FILE):
            live.append(run_dir.name)
            continue
        record = json.loads((run_dir / VOID_FILE).read_text(encoding="utf-8"))
        if not isinstance(record, dict) or not all(
                isinstance(record.get(key), str) for key in ("run", "reason", "voided_utc")):
            raise ValueError("invalid void record")
        voided.append({key: record[key] for key in ("run", "reason", "voided_utc")})
    return live, voided


def _void_tune(run: str, reason: str | None) -> int:
    if reason is None or not _VOID_REASON.fullmatch(reason):
        print("Refused: --void-tune needs --reason of 1-200 letters, digits, spaces or .,:;()/#_- "
              "(a procedure note, never prompt text).")
        return 2
    run_id = tune_dir_name(run)
    run_dir = paths.state_path("redo_validation", run_id)
    try:
        frozen = json.loads((run_dir / "frozen.json").read_text(encoding="utf-8"))
        purpose = frozen.get("purpose") if isinstance(frozen, dict) else None
    except (OSError, ValueError):
        print("Refused: no readable tune run with this id.")
        return 2
    if purpose != "tune":
        print("Refused: run purpose is not tune.")
        return 2
    try:
        _write_private(run_dir / VOID_FILE, json.dumps(dict(
            run=run_id, reason=reason, voided_utc=datetime.now(timezone.utc).isoformat()),
            sort_keys=True) + "\n")
    except FileExistsError:
        print("Refused: tune run is already voided.")
        return 2
    print(f"voided tune run: {run_id}")
    return 0


def resolve_with_all_evidence(*, override: str | None, tag: str | None, sidecar: str | None,
                              ledger: Iterable[str] = ()) -> session_kind.Resolution:
    """D-275: one pure rule over every source's kind (``None``, unknown or empty = no evidence).

    The owner override wins. Otherwise the distinct valid kinds of tag, sidecar and ledger:
    none -> untagged (``Resolution(None, None)``); several -> ``SOURCE_CONFLICT`` (excluded);
    exactly one -> that kind, with the first source that supplied it (a non-organic kind is
    excluded by the caller). The live-tag-wins precedence of ``KindIndex.resolve`` does not apply."""
    if override in session_kind.VALID_KINDS:
        return session_kind.Resolution(override, session_kind.SOURCE_OVERRIDE)
    supplied = [(session_kind.SOURCE_TAG, {tag}), (session_kind.SOURCE_BACKFILL, {sidecar}),
                (session_kind.SOURCE_LEDGER, set(ledger))]
    supplied = [(src, {k for k in kinds if k in session_kind.VALID_KINDS}) for src, kinds in supplied]
    distinct = set().union(*(kinds for _, kinds in supplied))
    if not distinct:
        return session_kind.Resolution(None, None)
    if len(distinct) > 1:
        return session_kind.Resolution(None, session_kind.SOURCE_CONFLICT)
    (only,) = distinct
    return session_kind.Resolution(only, next(src for src, kinds in supplied if only in kinds))


def _sidecar_warning(state: str) -> str:
    return (f"WARNING: backfill sidecar {state}: sessions it would resolve are counted "
            "as untagged")


def _feed_ledger(index: session_kind.KindIndex,
                 rows: Iterable[dict] | None = None) -> tuple[str, int]:
    """Stream the whole proxy ledger; only session_id -> kinds is kept in ``index``.

    A session with pairs in the window may have been stamped only before it, so the whole
    ledger is read. A ledger with non-blank lines but no dict row is ``corrupt``. Memory is bounded by streaming, not by time. Return the ledger state
    and the number of dict rows read. ``rows`` replaces the file in tests.
    """
    kept = 0
    nonblank = 0

    def take(row: object) -> None:
        nonlocal kept
        if not isinstance(row, dict):
            return
        kept += 1
        index.add(row.get("session_id"), row.get("session_kind"))

    if rows is not None:
        for row in rows:
            take(row)
        return LEDGER_PRESENT, kept
    from llm_router.proxy import ledger as proxy_ledger
    target = proxy_ledger.ledger_path()
    if not os.path.lexists(target):
        return LEDGER_MISSING, 0
    try:
        with open(target, "rb") as stream:
            for raw in stream:
                nonblank += bool(raw.strip())
                try:
                    row = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                take(row)
    except OSError:
        return LEDGER_UNREADABLE, kept
    if nonblank and not kept:
        return LEDGER_CORRUPT, kept
    return LEDGER_PRESENT, kept


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pair_key(session_id: str, turn_index: int) -> str:
    return _sha(f"{session_id}|{turn_index}")[:16]


def load_excluded(extra: set[str] | None = None, source: Path | None = None) -> set[str]:
    """Load unsalted M0.9 hashes (from ``source``, default the shipped file), plus optional extra exclusions."""
    source = source or Path(__file__).parent / "data" / "redo_m09_excluded.txt"
    # utf-8-sig: a byte-order mark on the first line is not part of the first key.
    keys = [line.strip() for line in source.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    invalid = sum(not _EXCLUSION_KEY.fullmatch(key) for key in keys)
    if invalid:
        raise InvalidExclusionFile(invalid)
    return set(keys) | set(extra or ())


@dataclass(frozen=True)
class Candidate:
    session_id: str
    turn_index: int
    ts: float
    signals: tuple[str, ...]
    screened: bool
    kind: str
    kind_source: str | None
    prev_served_class: str | None = None

    @property
    def key(self) -> str:
        return pair_key(self.session_id, self.turn_index)

    @property
    def stratum(self) -> str:
        return "F" if self.signals else "S" if self.screened else "R"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _window(days: float, now: float | None) -> tuple[float, float]:
    if not math.isfinite(days) or days <= 0:
        raise ValueError("days must be finite and positive")
    until = datetime.now(timezone.utc).timestamp() if now is None else now
    return until - days * 86400, until


def _served_loader(since: float, until: float) -> ServedLoader:
    cache: dict[str, list[tuple[float, str]]] | None = None
    failure: Exception | None = None

    def load(sid: str) -> list[tuple[float, str]]:
        nonlocal cache, failure
        if failure is not None:
            raise failure
        if cache is None:
            try:
                built: dict[str, list[tuple[float, str]]] = {}
                for action in offload_share.action_units(since, until)["actions"]:
                    built.setdefault(action["session_id"], []).append(
                        (action["ts"], action["served_class"]))
                cache = built
            except Exception as exc:  # ledger failure means unmeasured
                failure = exc
                raise
        return cache.get(sid, [])

    return load


def _interrupts(entries: list[dict], prompts: list[redo_signal.HumanPrompt]) -> set[int]:
    """Locate interruption records between consecutive human prompts in one pass."""
    positions = {p.entry_index: k for k, p in enumerate(prompts)}
    interrupted: set[int] = set()
    pending = False
    for i, rec in enumerate(entries):
        if i in positions:
            if pending and positions[i] >= 1:
                interrupted.add(positions[i])
            pending = False
        elif (rec.get("type") == "user"
              and redo_signal._content_text(
                  redo_signal._message(rec).get("content")).lstrip().startswith(
                      "[Request interrupted")):
            pending = True
    return interrupted


def _screen(prompt: str, previous: str, interrupted: bool) -> bool:
    similarity = redo_signal.five_gram_jaccard(prompt, previous)
    return bool(interrupted or (similarity is not None and similarity >= 0.5)
                or redo_signal._FAILURE_RE.search(prompt.lower().replace("’", "'"))
                or len(redo_signal._words(prompt)) <= 12)


def _spread(pool: list[Candidate]) -> dict:
    sizes = Counter(p.session_id for p in pool)
    flagged = Counter(p.session_id for p in pool if p.signals)
    return {
        "n_sessions": len(sizes),
        "max_pairs_per_session": max(sizes.values(), default=0),
        "largest_session_share": max(sizes.values(), default=0) / len(pool) if pool else 0,
        "median_pairs_per_session": median(sizes.values()) if sizes else 0,
        "n_sessions_F": len(flagged),
        "largest_session_share_F": (
            max(flagged.values()) / sum(flagged.values()) if flagged else 0),
    }


def collect(*, projects_dir: Path | None = None, days: float = 30,
            now: float | None = None, resolve: Resolver | None = None,
            served_loader: ServedLoader | None = None,
            excluded: set[str] | None = None, include_untagged: bool = False,
            exclusion_source: str = "shipped",
            ledger_rows: Iterable[dict] | None = None,
            variant: str = redo_signal.BASELINE_VARIANT,
            prior_runs: list[PriorRun] | None = None) -> tuple[list[Candidate], dict]:
    """Build a text-free pool and counts; injected loaders never access live ledgers."""
    since, until = _window(days, now)
    sidecar_state, sidecar = session_kind_backfill.read_sidecar()    # once per run
    ledger_state: str | None = None
    ledger_rows_read: int | None = None
    root = projects_dir if projects_dir is not None else northstar.claude_projects_dir()
    if resolve is None and include_untagged:
        index = session_kind.KindIndex(backfill=False)   # holds the ledger stamps only
        ledger_state, ledger_rows_read = _feed_ledger(index, ledger_rows)
        overrides_state, owner = session_kind.read_overrides_strict()
        resolved: dict[str, session_kind.Resolution] = {}
        bad_tags = 0
        for path in sorted(Path(root).glob("*/*.jsonl")):
            sid = path.stem
            if sid in resolved:
                continue
            tag_state, tag_kind = session_kind.tag_evidence(sid)
            bad_tags += tag_state == "unreadable"
            resolved[sid] = resolve_with_all_evidence(
                override=owner.get(sid), tag=tag_kind, sidecar=sidecar.get(sid),
                ledger=index.ledger_kinds(sid))
        refused = EvidenceUnreadable(
            overrides=int(overrides_state == "unreadable"), tag_files=bad_tags,
            sidecar=int(sidecar_state in (session_kind_backfill.SIDECAR_UNREADABLE,
                                          session_kind_backfill.SIDECAR_CORRUPT)),
            ledger=int(ledger_state in (LEDGER_UNREADABLE, LEDGER_CORRUPT)))
        if refused.total:
            raise refused
        resolve = resolved.__getitem__
    elif resolve is None:
        resolve = session_kind.KindIndex(backfill=True, sidecar=sidecar).resolve
    causes: dict[str, Counter[str]] = {
        "tag": Counter(dict.fromkeys(("no_tag_file", "corrupt_tag"), 0)),
        "ledger": Counter(dict.fromkeys(("no_ledger_stamp",), 0)),
        "sidecar": Counter(dict.fromkeys(("sidecar_missing", "sidecar_unreadable", "sidecar_corrupt",
                                          "sidecar_no_entry", "sidecar_unknown"), 0)),
    }
    ledger_conflict_excluded = 0
    served_loader = served_loader or _served_loader(since, until)
    excluded_keys = load_excluded(excluded)
    kinds: Counter[str] = Counter()
    counts = dict(sessions_total=0, organic_pairs_eligible=0, untagged_eligible_pairs=0, no_ts=0,
                  excluded_m09=0, flagged_ineligible=0,

                  exclusion_list_size=len(excluded_keys),
                  exclusion_list_sha256=_sha("\n".join(sorted(excluded_keys))),
                  exclusion_source=exclusion_source)
    eligible_by_kind: Counter[str] = Counter()
    pool: list[Candidate] = []
    measured = True
    any_actions = False
    seen: set[str] = set()
    for path in sorted(Path(root).glob("*/*.jsonl")):
        sid = path.stem
        if sid in seen:
            continue
        seen.add(sid)
        resolution = resolve(sid)
        counts["sessions_total"] += 1
        if include_untagged and resolution.source == session_kind.SOURCE_CONFLICT:
            # Conflicting ledger stamps are evidence of a kind, not of none: never untagged.
            ledger_conflict_excluded += 1
            continue
        kind_name = resolution.kind or "untagged"
        kinds[kind_name] += 1
        if include_untagged and kind_name == "untagged":   # one count per facet
            causes["tag"][session_kind.tag_file_state(sid)] += 1
            causes["ledger"]["no_ledger_stamp"] += 1
            if sidecar_state != session_kind_backfill.SIDECAR_PRESENT:
                causes["sidecar"]["sidecar_" + sidecar_state] += 1
            elif sid not in sidecar:
                causes["sidecar"]["sidecar_no_entry"] += 1
            else:   # "unknown", or anything that is not a kind
                causes["sidecar"]["sidecar_unknown"] += 1
        if kind_name != session_kind.KIND_ORGANIC and not (
                include_untagged and resolution.kind in (None, "untagged")):
            if kind_name == "untagged":
                # Count only (no pool, no text kept): the pairs this session would have admitted
                # as organic. The tagging_gap verdict compares these with organic_pairs_eligible,
                # so zero-prompt headless runs cannot outvote the organic supply.
                counts["untagged_eligible_pairs"] += sum(
                    pair.ts is not None and since <= pair.ts <= until
                    for pair in redo_signal.turn_pairs(redo_signal.read_transcript(path)))
            continue
        entries = redo_signal.read_transcript(path)
        prompts = redo_signal.human_prompts(entries)
        interrupted = _interrupts(entries, prompts)
        pairs = redo_signal.turn_pairs(entries)
        eligible = {p.turn_index for p in pairs}
        flags: dict[int, list[str]] = {}
        for ts, k, signal in redo_signal.redo_flag_times(entries, variant):
            flags.setdefault(k, []).append(signal)
            if k not in eligible and ts is not None and since <= ts <= until:
                counts["flagged_ineligible"] += 1
        try:
            actions = sorted(served_loader(sid), key=lambda item: item[0])
        except Exception:  # injected and default loader failures have identical semantics
            measured = False
            actions = []
        any_actions = any_actions or bool(actions)
        for pair in pairs:
            if pair.ts is None:
                counts["no_ts"] += 1
                continue
            if not since <= pair.ts <= until:
                continue
            eligible_by_kind[kind_name] += 1
            if kind_name == session_kind.KIND_ORGANIC:
                counts["organic_pairs_eligible"] += 1
            elif kind_name == "untagged":
                counts["untagged_eligible_pairs"] += 1
            if pair_key(sid, pair.turn_index) in excluded_keys:
                counts["excluded_m09"] += 1
                continue
            prior = next((r for r in prior_runs or ()
                          if _sha(f"{sid}|{pair.turn_index}|{r.salt}")[:16] in r.pair_ids), None)
            if prior is not None:
                counts[f"excluded_prior_{prior.purpose}"] = counts.get(
                    f"excluded_prior_{prior.purpose}", 0) + 1
                continue
            k = pair.turn_index
            signals = tuple(flags.get(k, ()))
            previous_class = next(
                (cls for ts, cls in reversed(actions) if ts <= pair.ts), None)
            pool.append(Candidate(
                sid, k, pair.ts, signals,
                not signals and _screen(prompts[k].text, prompts[k - 1].text, k in interrupted),
                kind_name, resolution.source, previous_class))
    measured = measured and any_actions     # an empty ledger measures nothing: n=0 is unmeasured, not 0
    if not measured:
        # Partial ledger coverage must not look like a complete measurement.
        pool = [Candidate(p.session_id, p.turn_index, p.ts, p.signals, p.screened,
                          p.kind, p.kind_source) for p in pool]
    strata = Counter(p.stratum for p in pool)
    signals = Counter(signal for p in pool for signal in p.signals)
    counts.update(
        sessions_by_kind={k: kinds[k] for k in sorted(
            set(kinds) | {"organic", "untagged"})},
        eligible_after_exclusion=len(pool),
        N_F=strata["F"], N_S=strata["S"], N_R=strata["R"],
        flagged_per_signal={s: signals[s] for s in ("reask", "contradiction", "repeat")},
        cheap_served=(sum(p.prev_served_class in offload_share.OFFLOADED_CLASSES
                          for p in pool) if measured else None),
        **_spread(pool),
    )
    if include_untagged:
        counts["backfill_sidecar"] = sidecar_state
        # Each facet sums to sessions_by_kind["untagged"]; conflicts are excluded, not untagged.
        counts["untagged_causes"] = {facet: dict(sorted(c.items()))
                                     for facet, c in sorted(causes.items())}
        counts["ledger_conflict_excluded"] = ledger_conflict_excluded
        # sessions_total == sum(sessions_by_kind.values()) + excluded_by_evidence
        counts["excluded_by_evidence"] = ledger_conflict_excluded
        if ledger_state is not None:    # None: an injected resolver, the ledger was not read
            counts["proxy_ledger"] = ledger_state
            counts["proxy_ledger_rows"] = ledger_rows_read
        counts["include_untagged"] = True
        counts["by_kind"] = {}
        for kind in ("organic", "untagged"):
            sub_pool = [p for p in pool if p.kind == kind]
            sub_strata = Counter(p.stratum for p in sub_pool)
            sub_signals = Counter(signal for p in sub_pool for signal in p.signals)
            counts["by_kind"][kind] = dict(
                eligible=eligible_by_kind[kind],
                eligible_after_exclusion=len(sub_pool),
                N_F=sub_strata["F"], N_S=sub_strata["S"], N_R=sub_strata["R"],
                flagged_per_signal={s: sub_signals[s]
                                    for s in ("reask", "contradiction", "repeat")},
                cheap_served=(sum(p.prev_served_class in offload_share.OFFLOADED_CLASSES
                                  for p in sub_pool) if measured else None),
                **_spread(sub_pool))
    return pool, counts


def census(**kwargs) -> dict:
    """Return counts only, without printing or writing."""
    return collect(**kwargs)[1]


def draw(pool: Iterable[Candidate], seed: int, salt: str) -> list[dict]:
    """Pure deterministic stratified draw; input order does not affect the result."""
    groups = {h: [] for h in ("F", "S", "R")}
    for pair in pool:
        groups[pair.stratum].append(pair)
    rng = random.Random(seed)
    for candidates in groups.values():
        candidates.sort(key=lambda p: p.key)
        rng.shuffle(candidates)
    flagged = groups["F"]
    sizes = Counter(p.session_id for p in flagged)
    concentrated = bool(flagged and max(sizes.values()) / len(flagged) > 0.6)
    chosen: dict[str, list[Candidate]] = {"F": [], "S": groups["S"][:30]}
    per_session: Counter[str] = Counter()
    for p in flagged:
        if len(chosen["F"]) >= 60:
            break
        if concentrated and per_session[p.session_id] >= 15:
            continue
        chosen["F"].append(p)
        per_session[p.session_id] += 1
    chosen["R"] = groups["R"][:150 - len(chosen["F"]) - len(chosen["S"])]
    rows: list[dict] = []
    for h, selected in chosen.items():
        for p in selected:
            rows.append(dict(
                pair_id=_sha(f"{p.session_id}|{p.turn_index}|{salt}")[:16],
                session_hash=_sha(f"{salt}|{p.session_id}")[:16],
                turn_index=p.turn_index, day=_iso(p.ts)[:10], stratum=h,
                signals=list(p.signals), detector_version=redo_signal.PATTERN_VERSION,
                kind=p.kind, kind_source=p.kind_source,
                prev_served_class=p.prev_served_class,
                weight_N_h_combined=len(groups[h]), n_h_combined=len(selected)))
    return rows


def _tagging_gap(counts: dict) -> bool:
    """Untagged sessions hold more eligible pairs than organic ones (session counts do not
    decide: headless runs with no human prompt are untagged but contribute no pair)."""
    return counts["untagged_eligible_pairs"] > counts["organic_pairs_eligible"]


def _no_go_reason(counts: dict, include_untagged: bool = False) -> str | None:
    if counts["N_F"] >= MIN_FLAGGED:
        return None
    if not include_untagged and _tagging_gap(counts):
        return "tagging_gap"
    return "insufficient_flagged_supply"


def _gate(counts: dict, include_untagged: bool = False) -> str:
    n, kinds = counts["N_F"], counts["sessions_by_kind"]
    reason = _no_go_reason(counts, include_untagged)
    if reason is None:
        verdict = "GO"
    elif reason == "tagging_gap":
        verdict = ("NO-GO (tagging_gap) -- next step: fix session tagging, then "
                   "re-census; do not tune the detector on this result")
    else:
        verdict = ("NO-GO (insufficient_flagged_supply) -- next step: detector tune to v3 "
                   "(P3.6-c PR 3); re-census after the window slides")
    organic = kinds["organic"]      # collect() always emits both keys
    untagged = kinds["untagged"]
    other_kind = sum(v for k, v in kinds.items() if k not in {"organic", "untagged"})
    return (f"N_F={n} vs threshold {MIN_FLAGGED}: {verdict}; "
            f"sessions: organic={organic} untagged={untagged} other_kind={other_kind}"
            + ("; include_untagged=on" if include_untagged else ""))


def _gate_lines(counts: dict, include_untagged: bool = False) -> list[str]:
    lines = [_gate(counts, include_untagged)]
    if _no_go_reason(counts, include_untagged) == "tagging_gap":
        lines.append(
            "WARNING: untagged sessions hold more eligible pairs than organic ones: N_F "
            "shortfall may be a tagging gap, not detector supply -- do not tune the detector "
            "on this result")
    return lines


def _untagged_warnings(counts: dict) -> list[str]:
    """Warnings for an --include-untagged run: missing ledger, sidecar, conflicts."""
    lines = []
    if counts.get("proxy_ledger") == LEDGER_MISSING:
        lines.append(LEDGER_MISSING_WARNING)
    if counts["backfill_sidecar"] in (session_kind_backfill.SIDECAR_UNREADABLE,
                                      session_kind_backfill.SIDECAR_CORRUPT):
        lines.append(_sidecar_warning(counts["backfill_sidecar"]))
    if counts["ledger_conflict_excluded"] > 0:
        lines.append(f"WARNING: {counts['ledger_conflict_excluded']} session(s) with "
                     "conflicting kind stamps were excluded")
    return lines


def _write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        os.chmod(path, 0o600)
        stream.write(content)


def cmd_redo_sample(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="llm-router redo-sample")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--census", action="store_true")
    mode.add_argument("--freeze", action="store_true")
    mode.add_argument("--tune", action="store_true",
                      help="draw THE tune set of the study (own seed, own tune-<id> run dir) disjoint "
                           "from the M0.9 list, every test run and every earlier tune set; refuses "
                           "while a live (not voided) tune run exists")
    mode.add_argument("--void-tune", action="store_true",
                      help="void tune run --run with --reason (recorded, shown in later frozen.json); "
                           "its tune rows are never compared again and its widenings stay spent")
    parser.add_argument("--seed", type=int, default=None,
                        help=f"default {DEFAULT_SEED}; {DEFAULT_TUNE_SEED} with --tune")
    parser.add_argument("--run-id", "--run", dest="run_id")
    parser.add_argument("--detector", choices=sorted(redo_signal.VARIANTS),
                        default=redo_signal.BASELINE_VARIANT,
                        help="detector variant for --census/--freeze; non-baseline freeze requires "
                             "the logged argmax-F1 candidate")
    parser.add_argument("--allow-below-threshold", action="store_true")
    parser.add_argument("--allow-empty-exclusion", action="store_true")
    parser.add_argument("--days", type=float, default=30)
    parser.add_argument("--projects-dir", type=Path)
    parser.add_argument("--allow-nondefault-projects-dir", action="store_true",
                        help="let --tune read a projects dir other than the census default (recorded; "
                             "a curated tune pool is a procedure violation)")
    parser.add_argument("--reason", help="with --void-tune: why the tune run is voided")
    parser.add_argument("--include-untagged", action="store_true",
                        help="include untagged sessions as a disclosed stratum (default: off)")
    parser.add_argument("--exclusion-file", type=Path,
                        help="local M0.9 exclusion hashes (default: state dir redo_m09_excluded.txt)")
    options = parser.parse_args(args)
    if options.reason is not None and not options.void_tune:
        parser.error("--reason needs --void-tune")
    if options.void_tune:
        if options.run_id is None:
            parser.error("--void-tune needs --run")
        if not _RUN_ID.fullmatch(options.run_id):
            parser.error("run-id must contain only letters, digits, underscores and hyphens")
        return _void_tune(options.run_id, options.reason)
    if options.tune and options.detector != redo_signal.BASELINE_VARIANT:
        parser.error("--detector is not allowed with --tune: tune sets are drawn with the v2 baseline")
    seed = options.seed if options.seed is not None else (
        DEFAULT_TUNE_SEED if options.tune else DEFAULT_SEED)
    local_keys: set[str] | None = None
    exclusion_source = "shipped"
    label = "shipped"       # which file a refusal names: never its path
    try:
        load_excluded()     # the shipped list is validated like a local one
        label = "local"
        local_path = options.exclusion_file
        if local_path is None:
            default_path = paths.state_path("redo_m09_excluded.txt")
            if default_path.is_file():
                local_path = default_path
        if local_path is not None:
            if not local_path.is_file():
                raise OSError("exclusion path is not a file")
            local_keys = load_excluded(source=local_path)
            exclusion_source = "local-file"
    except InvalidExclusionFile as exc:     # before UnicodeError: both are ValueErrors
        print(f"Refused: {label} exclusion file has {exc.count} invalid line(s)")
        return 2
    except (OSError, UnicodeError):
        print(f"Refused: {label} exclusion file not found or unreadable.")
        return 2
    if local_path is not None:
        try:
            readable = local_path.stat().st_mode & 0o044
        except OSError:
            print("WARNING: local exclusion file mode could not be checked")
        else:
            if readable:
                print("WARNING: local exclusion file is group- or world-readable; "
                      "recommended mode 0600")
    now = datetime.now(timezone.utc).timestamp()
    try:
        since, until = _window(options.days, now)
    except ValueError as exc:
        parser.error(str(exc))
    run_id = options.run_id or datetime.fromtimestamp(now, timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")
    if not _RUN_ID.fullmatch(run_id):
        parser.error("run-id must contain only letters, digits, underscores and hyphens")
    if options.tune:
        run_id = tune_dir_name(run_id)
        if not _RUN_ID.fullmatch(run_id):
            parser.error("run-id too long")
    run_dir = paths.state_path("redo_validation", run_id)
    if (options.freeze or options.tune) and os.path.lexists(run_dir):
        print("Refused: run directory already exists.")
        return 2
    # redo_score imports redo_sample; defer this import until both modules are loaded.
    from llm_router.redo_score import TUNE_PROVENANCE_KEYS, TuneFreezeRefused, freeze_tune_provenance
    tune_provenance = dict.fromkeys(TUNE_PROVENANCE_KEYS)
    try:
        detector_sha = hashlib.sha256(Path(redo_signal.__file__).read_bytes()).hexdigest()
        if options.freeze and options.detector != redo_signal.BASELINE_VARIANT:
            tune_provenance = freeze_tune_provenance(
                paths.state_path("redo_validation", "tune_log.jsonl"), options.detector, detector_sha)
    except TuneFreezeRefused as refused:
        print(refused.message)
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print("Refused: non-baseline freeze requires a current logged strict argmax-F1 candidate "
              "and a readable valid tune log.")
        return 2
    root = (options.projects_dir if options.projects_dir is not None
            else northstar.claude_projects_dir())
    root = Path(root)
    projects_default = os.path.realpath(root) == os.path.realpath(northstar.claude_projects_dir())
    if options.tune and not projects_default and not options.allow_nondefault_projects_dir:
        print("Refused: a tune pool is drawn from the census population (the default projects dir); "
              "a non-default --projects-dir needs --allow-nondefault-projects-dir, which is recorded.")
        return 2
    if not root.is_dir() or not any(p.is_file() for p in root.glob("*/*.jsonl")):
        print(f"WARNING: projects dir missing or contains no transcripts: {root.name}; "
              "refusing to report a census", file=sys.stderr)
        return 4
    try:
        prior_runs = load_prior_runs({"tune", "test"} if options.tune else {"tune"})
    except PriorRunUnreadable as refused:
        run_names = ", ".join(refused.runs[:5])
        if len(refused.runs) > 5:
            run_names += f" and {len(refused.runs) - 5} more"
        print(f"Refused: {refused.count} unreadable prior run directory(ies): {run_names}; "
              "disjointness cannot be proven; a run dir under redo_validation/ is incomplete; "
              "move it aside or remove it.")
        return 2
    try:
        live_tune, voided_tune = tune_run_states()
    except (OSError, ValueError):
        print("Refused: a tune run's frozen.json or void record is unreadable.")
        return 2
    if options.tune and live_tune:
        print(f"Refused: a live tune run already exists ({', '.join(live_tune)}); one tune set per "
              "P3.6-c study: void it first with redo-sample --void-tune --run <id> --reason <text>.")
        return 2
    try:
        pool, counts = collect(
            projects_dir=root, days=options.days, now=now, excluded=local_keys,
            include_untagged=options.include_untagged, exclusion_source=exclusion_source,
            variant=options.detector, prior_runs=prior_runs)
    except EvidenceUnreadable as refused:
        print(refused.message())
        return 2
    warnings = _untagged_warnings(counts) if options.include_untagged else []
    if options.census:
        print(f"window: {_iso(since)} to {_iso(until)}; "
              f"detector: {redo_signal.PATTERN_VERSION}; variant: {options.detector}")
        for key, value in counts.items():
            rendered = "unmeasured" if value is None else (
                json.dumps(value, sort_keys=True) if isinstance(value, dict) else value)
            print(f"{key}: {rendered}")
        if counts["exclusion_list_size"] == 0:
            print(EMPTY_EXCLUSION)
        for line in warnings:
            print(line)
        print("\n".join(_gate_lines(
            counts, options.include_untagged)))
        return 0
    for line in warnings:
        print(line)
    if not options.tune:    # a tune set is drawn from whatever supply there is: no flagged-supply gate
        print("\n".join(_gate_lines(
            counts, options.include_untagged)))
    below = counts["N_F"] < MIN_FLAGGED
    if below and not options.allow_below_threshold and not options.tune:
        print("Refused: --allow-below-threshold is required to freeze this supply.")
        if _no_go_reason(counts, options.include_untagged) == "tagging_gap":
            print("The cause is session tagging, not detector supply: fix session "
                  "tagging, then re-census. --allow-below-threshold freezes an "
                  "under-supplied sample and does not fix the cause.")
        return 3
    if counts["exclusion_list_size"] == 0 and not options.allow_empty_exclusion:
        print(EMPTY_EXCLUSION)
        print("Refused: --allow-empty-exclusion is required to freeze without an M0.9 "
              "exclusion list.")
        return 5
    salt = _sha(f"redo-sample-salt|{seed}|{run_id}")
    rows = draw(pool, seed, salt)
    for row in rows:
        row["exclusion_list_sha256"] = counts["exclusion_list_sha256"]
    manifest = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    drawn = Counter(row["stratum"] for row in rows)
    strata = {h: {"N": counts[f"N_{h}"], "n": drawn[h]} for h in ("F", "S", "R")}
    if options.include_untagged:
        for h in strata:
            strata[h]["by_kind"] = {
                kind: {
                    "N": sum(p.stratum == h and p.kind == kind for p in pool),
                    "n": sum(row["stratum"] == h and row["kind"] == kind for row in rows),
                } for kind in ("organic", "untagged")}
    frozen = dict(
        exclusion_list_size=counts["exclusion_list_size"],
        exclusion_list_sha256=counts["exclusion_list_sha256"],
        exclusion_source=counts["exclusion_source"],
        include_untagged=options.include_untagged,
        allow_empty_exclusion=options.allow_empty_exclusion,
        detector_version=redo_signal.PATTERN_VERSION,
        detector_sha256=detector_sha,
        manifest_sha256=hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
        seed=seed, run_id=run_id, salt=salt, min_flagged=MIN_FLAGGED,
        purpose="tune" if options.tune else "test", detector_variant=options.detector,
        excluded_prior_tune=counts.get("excluded_prior_tune", 0),
        excluded_prior_test=counts.get("excluded_prior_test", 0),
        n_prior_runs=len(prior_runs),
        n_flagged_pool=counts["N_F"], allow_below_threshold=options.allow_below_threshold,
        no_go=(None if options.tune else _no_go_reason(
            counts, options.include_untagged)),
        concentrated=counts["largest_session_share_F"] > 0.6,
        shortfall=150 - len(rows),
        window=dict(days=options.days, since_iso=_iso(since), until_iso=_iso(until)),
        counts=counts, strata=strata,
        projects_dir_sha256=_sha(os.path.realpath(root)), projects_dir_default=projects_default,
        allow_nondefault_projects_dir=options.allow_nondefault_projects_dir,
        projects_dir_transcripts=sum(p.is_file() for p in root.glob("*/*.jsonl")),
        voided_tune_runs=voided_tune, n_voided_tune_runs=len(voided_tune),
        weights_combined={h: s["N"] / s["n"] if s["n"] else None for h, s in strata.items()},
        created_utc=_iso(now))
    frozen.update(tune_provenance)
    if options.include_untagged:
        frozen["sizes_by_kind"] = counts["by_kind"]
        frozen["ledger_conflict_excluded"] = counts["ledger_conflict_excluded"]
        frozen["excluded_by_evidence"] = counts["excluded_by_evidence"]
    parent = run_dir.parent
    try:
        os.makedirs(parent, mode=0o700)
    except FileExistsError:
        pass
    else:
        os.chmod(parent, 0o700)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-" + run_id + "-", dir=parent))
    try:
        os.chmod(tmp, 0o700)
        _write_private(tmp / "manifest.jsonl", manifest)
        _write_private(tmp / "frozen.json", json.dumps(frozen, sort_keys=True, indent=2) + "\n")
        try:
            os.rename(tmp, run_dir)
        except OSError:
            if os.path.lexists(run_dir):
                shutil.rmtree(tmp, ignore_errors=True)
                print("Refused: run directory already exists.")
                return 2
            raise
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    print(f"run_id: {run_id}")
    for h, s in strata.items():
        print(f"{h}: N={s['N']} n={s['n']}")
    print(f"purpose: {frozen['purpose']}; detector: {options.detector}; prior runs excluded from: "
          f"{len(prior_runs)}; excluded tune pairs: {frozen['excluded_prior_tune']}"
          + (f"; excluded test pairs: {frozen['excluded_prior_test']}" if options.tune else ""))
    return 0
