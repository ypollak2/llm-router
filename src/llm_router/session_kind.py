"""Session tagging for the KPI scorecard: organic / research / harness / headless.

Why: the North Star and its guardrails are defined on ORGANIC sessions only
(KPI-SPEC). On 2026-10-03, 90% of ``proxy_calls.jsonl`` came from one
sub-agent-heavy research session, so an unfiltered rate described that session,
not how the owner works. This module tags; it never drops data. Every reader
decides what to include.

Kinds (precedence, first match wins)::

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

Persistence: the SessionStart hook calls :func:`tag_session`, which writes
``session_kind_<sid>.json`` in the state dir. The proxy, which sees neither cwd
nor environment, reads it back with :func:`kind_of`. ``None`` means "never
tagged" (a session that predates this, or no hook ran) and is reported as its
own bucket; it is never silently counted as organic.
"""
from __future__ import annotations

import json
import os
import re
import time
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


def classify(*, cwd: str | None, entrypoint: str | None = None, override: str | None = None,
             home: str | None = None) -> str:
    """The session kind for these signals. Pure and deterministic; see the module
    docstring for the precedence. An unrecognised ``override`` is ignored."""
    forced = (override or "").strip().lower()
    if forced in VALID_KINDS:
        return forced
    path = _norm(cwd)
    base = _norm(home if home is not None else "~")
    parts = [p for p in path.split("/") if p]
    if path.startswith(base + ".rsi/") or "scratchpad" in parts:
        return KIND_RESEARCH
    if any(path.startswith(p) for p in _SANDBOX_PREFIXES):
        return KIND_HARNESS
    if (entrypoint or "").strip().lower().startswith("sdk"):
        return KIND_HEADLESS
    return KIND_ORGANIC


def _safe(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "unknown"


def _tag_path(session_id: str) -> Path:
    return paths.state_path(f"session_kind_{_safe(session_id)}.json")


def tag_session(session_id: str | None, cwd: str | None, *, env: dict | None = None) -> str | None:
    """Classify and persist this session's kind. Returns the kind, or ``None``
    when there is no session id to key it by. Best-effort write: never raises."""
    if not session_id:
        return None
    environ = os.environ if env is None else env
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
            return kind
    return kind


_FOUND: dict[str, str] = {}  # tag path -> kind; only hits are cached (the proxy asks per request)


def kind_of(session_id: str | None) -> str | None:
    """The persisted kind for ``session_id``, or ``None`` if it was never tagged."""
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
