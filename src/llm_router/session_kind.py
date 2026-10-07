"""Session tagging for the KPI scorecard: organic / research / harness / headless.

Why: the North Star and its guardrails are defined on ORGANIC sessions only
(KPI-SPEC). On 2026-10-03, 90% of ``proxy_calls.jsonl`` came from one
sub-agent-heavy research session, so an unfiltered rate described that session,
not how the owner works. This module tags; it never drops data. Every reader
decides what to include.

Kinds (precedence, first match wins). The owner's override FILE comes first of all
(``session_kind_overrides.json``, below); these are the rules that classify a session
that has no override::

    1. LLM_ROUTER_SESSION_KIND=<kind>   explicit override (always wins)
    2. research   cwd is under ~/.rsi or contains a ``scratchpad`` path part
    3. harness    cwd is a benchmark/test sandbox (``/tmp``, ``/private/tmp``,
                  ``/var/folders``, ``/private/var/folders``): the same shape
                  ``scripts/groundtruth/sources.py`` excludes from measurement
    4. headless   CLAUDE_CODE_ENTRYPOINT starts with ``sdk`` (``claude -p``, the
                  Agent SDK): no human is typing
    5. organic    everything else

Known limit, stated rather than faked: a *sub-agent* session cannot be
recognised from its own hooks. Claude Code transcripts carry no parent link
(``northstar.py``: ``isSidechain`` is false on 100% of ~46k records) and the
SubagentStart payload has no child session id. Sub-agents spawned from an
organic session therefore stay tagged by their own cwd/entrypoint, and the
override env var is the way to mark one explicitly.

Override file (2026-10-07, plan M0.0): ``session_kind_overrides.json`` in the state dir,
``{session_id: {"kind": "<kind>", "reason": "<why>"}}``. It exists for a session whose
tag, or whose rows' own stamps, say the wrong thing and cannot be rewritten (the ledgers
are append-only): session b9f04425 is a research session, but 424 of its proxy rows are
stamped ``organic`` and 751 carry no stamp, and it is 99.3% of the organic turn-first rows
in the pinned window. Precedence for every reader: **override, then the tag file, then the
record's own stamp**, then the rest of :meth:`KindIndex.resolve`. The override beats a
record's stamp, so a row stamped ``organic`` for an overridden session is read as the
override's kind. :func:`kind_of` (the proxy's stamp, the edit ledger's stamp, the usage
outcome judge) applies it too, so rows written from now on carry the override's kind.
:func:`tag_session` still writes what the SessionStart hook classified (the file is a record
of what the hook saw) and reports the effective kind. :func:`tag_kind_of` is the tag file
alone, for the one caller that must compare the tag with something else. The file is read
with one ``stat`` per call and re-read when it changes; a missing, unreadable or malformed
file, or entry, is "no override" and never raises. Only a valid kind counts.

Persistence: the SessionStart hook calls :func:`tag_session`, which writes
``session_kind_<sid>.json`` in the state dir. The proxy, which sees neither cwd
nor environment, reads it back with :func:`kind_of`. ``None`` means "never
tagged" (a session that predates this, or no hook ran) and is reported as its
own bucket; it is never silently counted as organic.

A session's kind is decided once, at its first sighting, and never changes: the
tag file is write-once (only the explicit ``LLM_ROUTER_SESSION_KIND`` override
replaces it). It used to be rewritten by every SessionStart, so a session
resumed from another directory changed kind: 2026-10-03/04 one session's tag file
(created 20:27Z) was rewritten at 23:00Z to ``research`` (cwd under ``~/.rsi``) and at
19:33Z to ``organic`` (cwd ``$HOME``), while the proxy, which caches hits, kept
stamping the first value, so one session read as two kinds across the ledgers.

SessionStart alone leaves a session untagged when it was already running when
the tagging hook was deployed, or is resumed without a SessionStart: 2026-10-04,
311 of 2,737 proxy rows (11.4%) were written before the one tag that session
ever got. The UserPromptSubmit hook therefore also calls :func:`tag_session` on
every prompt (a stat when the tag exists), so such a session is tagged at its
next prompt. Rows already written stay null: the ledger is append-only.

Joining a tag onto a record that has none of its own (a north-star unit, a
proxy row): :class:`KindIndex`. Precedence, first match wins: (1) the session's
tag file; (2) the kind stamped on the record itself when it was written; (3)
the kind stamped on that session's proxy rows, if they all agree. Nothing
resolvable stays ``None``; a session whose proxy rows disagree with each other
and has no tag file stays ``None`` too (source ``conflict``). ``None`` is never
counted as organic by any reader.

A session from before tagging existed has no tag file and no stamped rows. For those
(and only those) ``llm-router kpi --backfill-tags`` writes a SIDECAR,
``session_kind_backfill.jsonl`` (module ``session_kind_backfill``): a kind derived from
the Claude Code transcript's first ``cwd`` and ``entrypoint`` by the very rules above
(:func:`classify_with_basis`), or ``unknown`` when the transcript cannot say. The sidecar
is the LAST step of :meth:`KindIndex.resolve`, after the tag file, the record's own stamp
and the proxy rows, so a live tag always wins; it is never consulted by :func:`kind_of`
(the proxy hot path and :func:`tag_session`'s own write-once check), so it can never
shadow or pre-empt a live tag. Deleting the file restores the earlier behaviour exactly.

WHERE THE SIDECAR IS READ, AND WHERE IT IS NOT. It is read from disk only by a
:class:`KindIndex` built with ``backfill=True`` (the default of this class), once per
index, and only when a session reaches ``resolve``'s last step. The callers:

* READS it: ``llm-router kpi`` (``commands/kpi.py``: its own ``KindIndex`` for D3 and
  ``northstar.units(backfill=True)`` for NS, D1 and D2) and ``llm-router kpi
  --backfill-tags`` / ``--validate-backfill`` (``session_kind_backfill``).
* NEVER reads it: :func:`kind_of`, :func:`tag_session`, the proxy, every hook, and
  everything that reaches ``northstar.build_sessions`` / ``units`` / ``report``
  without asking, which includes the Stop hook's ``current_session_line``, the
  quality breaker (called by the UserPromptSubmit, Agent and Stop hooks) and
  ``llm-router northstar``. ``backfill`` is off by default all the way down
  (``units`` -> ``build_sessions`` -> ``_scan_proxy_ledger``), so a new caller on a
  hot path cannot start reading the file by accident.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from llm_router import paths

KIND_ORGANIC = "organic"
KIND_RESEARCH = "research"
KIND_HARNESS = "harness"
KIND_HEADLESS = "headless"
VALID_KINDS = (KIND_ORGANIC, KIND_RESEARCH, KIND_HARNESS, KIND_HEADLESS)

_SANDBOX_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")


def _norm(cwd: str | None) -> str:
    if not cwd:
        return ""
    try:
        text = os.path.expanduser(cwd)
    except Exception:  # noqa: BLE001 - classification must not raise
        text = cwd
    return text.replace("\\", "/").rstrip("/") + "/"


#: Which rule decided a kind. Coarse by design: a code, never a path or a prompt.
BASIS_OVERRIDE = "override"            # LLM_ROUTER_SESSION_KIND
BASIS_CWD_RSI = "cwd_rsi"              # cwd under ~/.rsi
BASIS_CWD_SCRATCHPAD = "cwd_scratchpad"  # cwd has a ``scratchpad`` path part
BASIS_CWD_SANDBOX = "cwd_sandbox"      # cwd under /tmp, /var/folders, ...
BASIS_ENTRYPOINT_SDK = "entrypoint_sdk"  # CLAUDE_CODE_ENTRYPOINT starts with ``sdk``
BASIS_ORDINARY = "ordinary"            # no research / harness / headless marker present


def classify_with_basis(*, cwd: str | None, entrypoint: str | None = None,
                        override: str | None = None, home: str | None = None) -> tuple[str, str]:
    """``(kind, basis)`` for these signals: the one place the precedence lives, so the
    live tagger and the backfill cannot drift apart. Pure and deterministic; see the
    module docstring for the precedence. An unrecognised ``override`` is ignored."""
    forced = (override or "").strip().lower()
    if forced in VALID_KINDS:
        return forced, BASIS_OVERRIDE
    path = _norm(cwd)
    base = _norm(home if home is not None else "~")
    parts = [p for p in path.split("/") if p]
    if path.startswith(base + ".rsi/"):
        return KIND_RESEARCH, BASIS_CWD_RSI
    if "scratchpad" in parts:
        return KIND_RESEARCH, BASIS_CWD_SCRATCHPAD
    if any(path.startswith(p) for p in _SANDBOX_PREFIXES):
        return KIND_HARNESS, BASIS_CWD_SANDBOX
    if (entrypoint or "").strip().lower().startswith("sdk"):
        return KIND_HEADLESS, BASIS_ENTRYPOINT_SDK
    return KIND_ORGANIC, BASIS_ORDINARY


def classify(*, cwd: str | None, entrypoint: str | None = None, override: str | None = None,
             home: str | None = None) -> str:
    """The session kind for these signals (see :func:`classify_with_basis`)."""
    return classify_with_basis(cwd=cwd, entrypoint=entrypoint, override=override, home=home)[0]


OVERRIDES_FILENAME = "session_kind_overrides.json"
_OVERRIDES_CACHE: dict[str, tuple[tuple[int, int], dict[str, dict[str, str]]]] = {}


def overrides_path() -> Path:
    return paths.state_path(OVERRIDES_FILENAME)


def overrides() -> dict[str, dict[str, str]]:
    """``{session_id: {"kind", "reason"}}`` from the override file: valid entries only.

    One ``stat`` per call; the file is parsed again only when its mtime or size changes.
    Never raises: no file, an unreadable or non-object file, and an entry that is not
    ``{"kind": <valid kind>, ...}`` all read as "no override" (that entry only)."""
    path = overrides_path()
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        _OVERRIDES_CACHE.pop(key, None)
        return {}
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _OVERRIDES_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    out: dict[str, dict[str, str]] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a broken file is "no overrides", never a crash
        raw = None
    if isinstance(raw, dict):
        for sid, entry in raw.items():
            if not isinstance(sid, str) or not sid or not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            kind = kind.strip().lower() if isinstance(kind, str) else None
            if kind not in VALID_KINDS:
                continue
            reason = entry.get("reason")
            out[sid] = {"kind": kind, "reason": reason if isinstance(reason, str) else ""}
    _OVERRIDES_CACHE[key] = (stamp, out)
    return out


def override_of(session_id: str | None) -> str | None:
    """The owner's override kind for ``session_id``, or ``None``."""
    if not session_id:
        return None
    entry = overrides().get(session_id)
    return entry["kind"] if entry else None


def _safe(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "unknown"


def _tag_path(session_id: str) -> Path:
    return paths.state_path(f"session_kind_{_safe(session_id)}.json")


def tag_session(session_id: str | None, cwd: str | None, *, env: dict | None = None) -> str | None:
    """Classify and persist this session's kind, unless it already has one: the
    first tag wins (see the module docstring), and the existing kind is returned.
    ``LLM_ROUTER_SESSION_KIND`` is the one thing that replaces a tag. Returns the
    kind, or ``None`` when there is no session id to key it by. Best-effort write:
    never raises."""
    if not session_id:
        return None
    environ = os.environ if env is None else env
    forced = (environ.get("LLM_ROUTER_SESSION_KIND") or "").strip().lower()
    existing = tag_kind_of(session_id)
    if existing is not None and (forced not in VALID_KINDS or forced == existing):
        return override_of(session_id) or existing  # first tag wins; a forced kind rewrites only when it differs
    entrypoint = environ.get("CLAUDE_CODE_ENTRYPOINT")
    kind = classify(cwd=cwd, entrypoint=entrypoint, override=environ.get("LLM_ROUTER_SESSION_KIND"))
    try:
        path = _tag_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"session_id": session_id, "kind": kind, "cwd": cwd,
                                    "entrypoint": entrypoint, "ts": time.time()}),
                        encoding="utf-8")
        _FOUND[str(path)] = kind
    except Exception as exc:  # noqa: BLE001 - telemetry must never block a session
        # Not silent (T-14): an untagged session shows up as its own bucket in the
        # scorecard, and this counter says why.
        try:
            from llm_router import failopen
            failopen.record("CHZ-FO-SESSION-KIND-TAG", exc)
        except Exception:  # noqa: BLE001
            return override_of(session_id) or kind
    return override_of(session_id) or kind


_FOUND: dict[str, str] = {}  # tag path -> kind; only hits are cached (the proxy asks per request)


def kind_of(session_id: str | None) -> str | None:
    """The kind for ``session_id``: the owner's override, else the persisted tag, else
    ``None`` (never tagged)."""
    if not session_id:
        return None
    return override_of(session_id) or tag_kind_of(session_id)


def tag_kind_of(session_id: str | None) -> str | None:
    """The persisted tag file's kind alone (no override), or ``None`` if never tagged."""
    if not session_id:
        return None
    path = _tag_path(session_id)
    cached = _FOUND.get(str(path))
    if cached is not None:
        return cached
    try:
        kind = json.loads(path.read_text(encoding="utf-8")).get("kind")
    except Exception:  # noqa: BLE001 - missing/corrupt tag is "untagged"
        return None
    if kind not in VALID_KINDS:
        return None
    _FOUND[str(path)] = kind
    return kind


# ── joining a kind onto records that carry none ──────────────────────────────

SOURCE_OVERRIDE = "override"    # the owner's override file: beats every source below
SOURCE_TAG = "tag"              # the session's tag file
SOURCE_STAMP = "stamp"          # stamped on the record itself when it was written
SOURCE_LEDGER = "proxy_ledger"  # stamped on that session's proxy rows (all agreeing)
SOURCE_CONFLICT = "conflict"    # proxy rows disagree and there is no tag file: unresolved
SOURCE_BACKFILL = "backfill"    # derived later from transcript metadata (the sidecar); last resort


@dataclass(frozen=True)
class Resolution:
    kind: str | None
    source: str | None
    #: A tag file exists and some of the session's proxy rows carry another kind.
    ledger_disagrees: bool = False


class KindIndex:
    """Resolve a session's kind for a record that has none of its own.

    Built once per report from the proxy ledger rows; every lookup of a session is
    memoised, so reading the tag file costs one stat per distinct session, not one
    per record."""

    def __init__(self, ledger_rows: Iterable[dict] = (), *, backfill: bool = True) -> None:
        self._ledger: dict[str, set[str]] = {}
        self._tags: dict[str, str | None] = {}
        #: ``backfill=False`` never reads the sidecar: what the backfill command itself
        #: uses to ask "does live evidence already resolve this session?", and what
        #: ``northstar`` passes on every path except ``llm-router kpi``.
        self._use_backfill = backfill
        self._backfill: dict[str, str] | None = None  # loaded on first need, once per index
        for row in ledger_rows:
            self.add(row.get("session_id"), row.get("session_kind"))

    def add(self, session_id: object, kind: object) -> None:
        """Record that a proxy row of ``session_id`` was stamped ``kind`` (ignored
        unless both are usable: a null stamp is the absence of evidence)."""
        if isinstance(session_id, str) and session_id and kind in VALID_KINDS:
            self._ledger.setdefault(session_id, set()).add(kind)  # type: ignore[arg-type]

    def _backfilled(self, session_id: object) -> str | None:
        """The sidecar's kind for ``session_id`` (a usable kind only), or ``None``. Read
        lazily and once: an index that never needs it never opens the file."""
        if not self._use_backfill or not isinstance(session_id, str) or not session_id:
            return None
        if self._backfill is None:
            try:
                from llm_router import session_kind_backfill
                self._backfill = session_kind_backfill.load_sidecar()
            except Exception:  # noqa: BLE001 - an unreadable sidecar is "no backfill", not a crash
                self._backfill = {}
        kind = self._backfill.get(session_id)
        # "unknown" (and anything hand-edited into the file) is not a kind: it resolves to
        # nothing, so such a session stays exactly as invisible as an untagged one.
        return kind if kind in VALID_KINDS else None

    def _tag(self, session_id: str) -> str | None:
        if session_id not in self._tags:
            self._tags[session_id] = tag_kind_of(session_id)
        return self._tags[session_id]

    def resolve(self, session_id: str | None, stamp: str | None = None) -> Resolution:
        """``stamp`` is the kind the record itself was written with, if any."""
        seen = self._ledger.get(session_id, set()) if isinstance(session_id, str) else set()
        forced = override_of(session_id) if isinstance(session_id, str) else None
        if forced is not None:  # the owner's override: no tag, stamp or row can outvote it
            return Resolution(forced, SOURCE_OVERRIDE, ledger_disagrees=bool(seen - {forced}))
        tag = self._tag(session_id) if isinstance(session_id, str) and session_id else None
        if tag is not None:
            return Resolution(tag, SOURCE_TAG, ledger_disagrees=bool(seen - {tag}))
        if stamp in VALID_KINDS:
            return Resolution(stamp, SOURCE_STAMP)
        if len(seen) == 1:
            return Resolution(next(iter(seen)), SOURCE_LEDGER)
        if len(seen) > 1:
            return Resolution(None, SOURCE_CONFLICT)
        backfilled = self._backfilled(session_id)
        if backfilled is not None:
            return Resolution(backfilled, SOURCE_BACKFILL)
        return Resolution(None, None)
