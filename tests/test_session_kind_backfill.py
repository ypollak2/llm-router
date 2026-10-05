"""Backfill ``session_kind`` for sessions that predate PR #250's tagging hook.

Primary KPI this enables: NS/D1 measurability -- more of the sessions that already
exist in the ledgers become resolvable, without ever touching the ledgers themselves
or guessing "organic" for a session with no evidence. Each test pins one promise from
the module docstring / the owner's contract: write-once, live-tag-always-wins, unknown
on insufficient evidence, dry-run writes nothing, 0600, the KPI join states how much of
its n was backfilled, deleting the sidecar is a clean revert, and a corrupt sidecar line
is skipped, not fatal.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from llm_router import northstar as ns
from llm_router import session_kind, session_kind_backfill as skb
from llm_router.commands import kpi
from llm_router.proxy import ledger as pl

NOW = 1_800_000_000.0
_REAL_UNIT_SESSION_IDS = skb.unit_session_ids


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """conftest isolates LLM_ROUTER_HOME per test but not the transcripts directory.
    Unit enumeration (a full transcript parse) is stubbed out by default so each test
    states its own candidate set; the two tests that need the real one restore it."""
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.setattr(skb, "unit_session_ids", lambda root=None: set())
    session_kind._FOUND.clear()
    yield
    session_kind._FOUND.clear()


def _write_transcript(root: Path, session_id: str, records: list[dict],
                      project: str = "-Users-someone-project") -> Path:
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def _proxy_row(session_id: str, **extra) -> dict:
    return {"session_id": session_id, "ts": 1_700_000_000.0, **extra}


# ── derive_kind: the reconstruction rules ─────────────────────────────────────

def test_derive_kind_organic_from_transcript_cwd(tmp_path):
    _write_transcript(tmp_path, "sid-organic",
                      [{"type": "user", "cwd": "/Users/someone/project", "entrypoint": "cli"}])
    kind, basis = skb.derive_kind("sid-organic", root=tmp_path)
    assert (kind, basis) == ("organic", session_kind.BASIS_ORDINARY)


def test_derive_kind_headless_from_sdk_entrypoint(tmp_path):
    _write_transcript(tmp_path, "sid-headless",
                      [{"type": "system", "cwd": "/Users/someone/project", "entrypoint": "sdk-cli"}])
    kind, basis = skb.derive_kind("sid-headless", root=tmp_path)
    assert (kind, basis) == ("headless", session_kind.BASIS_ENTRYPOINT_SDK)


def test_derive_kind_matches_live_classify_same_rules(tmp_path):
    """The backfill must reach the SAME verdict `classify()` would have reached live,
    for every signal combination -- they share one function."""
    cwd, entrypoint = "/tmp/private/sandboxed", None
    _write_transcript(tmp_path, "sid-harness", [{"type": "user", "cwd": cwd}])
    kind, _ = skb.derive_kind("sid-harness", root=tmp_path)
    assert kind == session_kind.classify(cwd=cwd, entrypoint=entrypoint)


def test_derive_kind_unknown_when_no_transcript_file(tmp_path):
    kind, basis = skb.derive_kind("sid-missing", root=tmp_path)
    assert (kind, basis) == (skb.UNKNOWN_KIND, skb.BASIS_NO_TRANSCRIPT)
    assert kind not in session_kind.VALID_KINDS  # never a live-resolvable kind


def test_derive_kind_unknown_when_transcript_has_no_cwd_or_entrypoint(tmp_path):
    _write_transcript(tmp_path, "sid-bare", [{"type": "user", "message": {"role": "user"}}])
    kind, basis = skb.derive_kind("sid-bare", root=tmp_path)
    assert (kind, basis) == (skb.UNKNOWN_KIND, skb.BASIS_NO_CWD_EVIDENCE)


def test_derive_kind_unknown_when_transcript_unreadable(tmp_path):
    d = tmp_path / "-proj"
    d.mkdir(parents=True)
    (d / "sid-junk.jsonl").write_text("not json at all\nstill not json\n", encoding="utf-8")
    kind, basis = skb.derive_kind("sid-junk", root=tmp_path)
    assert (kind, basis) == (skb.UNKNOWN_KIND, skb.BASIS_UNREADABLE_TRANSCRIPT)


def test_derive_kind_never_defaults_to_organic_on_empty_file(tmp_path):
    d = tmp_path / "-proj"
    d.mkdir(parents=True)
    (d / "sid-empty.jsonl").write_text("", encoding="utf-8")
    kind, _ = skb.derive_kind("sid-empty", root=tmp_path)
    assert kind == skb.UNKNOWN_KIND


# ── derive_kind: bounded reads ───────────────────────────────────────────────

class _CountingFile:
    """Wraps a transcript file object and counts the bytes ``derive_kind`` pulls out."""
    def __init__(self, fh, counter):
        self._fh, self._counter = fh, counter

    def __enter__(self):
        self._fh.__enter__()
        return self

    def __exit__(self, *exc):
        return self._fh.__exit__(*exc)

    def readline(self, *a):
        data = self._fh.readline(*a)
        self._counter["bytes"] += len(data)
        return data

    def read(self, *a):
        data = self._fh.read(*a)
        self._counter["bytes"] += len(data)
        return data

    def __iter__(self):
        raise AssertionError("derive_kind must read with a bounded readline, not iterate lines")


@pytest.fixture
def bytes_read(monkeypatch):
    counter = {"bytes": 0}
    real_open = Path.open

    def counting_open(self, *a, **kw):
        fh = real_open(self, *a, **kw)
        mode = a[0] if a else kw.get("mode", "r")
        return _CountingFile(fh, counter) if (self.suffix == ".jsonl" and mode == "rb") else fh

    monkeypatch.setattr(Path, "open", counting_open)
    return counter


def _junk(n_bytes: int, line_bytes: int = 200) -> bytes:
    """Valid JSON lines with no cwd/entrypoint, ``n_bytes`` in all."""
    body = b'{"type": "assistant", "pad": "' + b"x" * (line_bytes - 33) + b'"}\n'
    assert len(body) == line_bytes
    return body * (n_bytes // line_bytes)


def _evidence(cwd="/Users/x/p", entrypoint="cli") -> bytes:
    return (json.dumps({"type": "user", "cwd": cwd, "entrypoint": entrypoint}) + "\n").encode()


def _raw_transcript(root: Path, sid: str, data: bytes) -> None:
    d = root / "-Users-someone-project"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_bytes(data)


def test_derive_kind_stops_at_the_total_cap_and_says_unknown_not_organic(tmp_path, bytes_read):
    """Evidence only AFTER the cap: it is never read, and the answer is unknown -- not
    organic, which is what the partial read would otherwise look like."""
    _raw_transcript(tmp_path, "sid-late", _junk(skb.MAX_TRANSCRIPT_BYTES + 100_000) + _evidence())
    kind, basis = skb.derive_kind("sid-late", root=tmp_path)
    assert (kind, basis) == (skb.UNKNOWN_KIND, skb.BASIS_EVIDENCE_CAP)
    assert bytes_read["bytes"] <= skb.MAX_TRANSCRIPT_BYTES + 1   # the cap, plus the one peek byte


def test_derive_kind_does_not_slurp_one_enormous_line(tmp_path, bytes_read):
    _raw_transcript(tmp_path, "sid-huge", b'{"cwd": "' + b"x" * (6 * 1024 * 1024) + b'"}\n' + _evidence())
    kind, basis = skb.derive_kind("sid-huge", root=tmp_path)
    assert (kind, basis) == (skb.UNKNOWN_KIND, skb.BASIS_EVIDENCE_CAP)
    assert bytes_read["bytes"] <= skb.MAX_LINE_BYTES + 1   # one bounded chunk, not 6 MiB


def test_derive_kind_evidence_inside_the_cap_wins_and_the_rest_of_a_huge_file_is_not_read(tmp_path, bytes_read):
    _raw_transcript(tmp_path, "sid-early", _evidence(entrypoint="sdk-cli") + _junk(3 * 1024 * 1024))
    assert skb.derive_kind("sid-early", root=tmp_path) == ("headless", session_kind.BASIS_ENTRYPOINT_SDK)
    assert bytes_read["bytes"] < 1024     # it stopped as soon as it had both fields


def test_a_missing_entrypoint_past_the_cap_is_unknown_but_the_same_small_file_is_not(tmp_path):
    """The false-organic channel the cap closes for big files: cwd seen, entrypoint never
    seen. Inside the cap the whole file was read and the verdict stands (the existing
    rule); past it, the unread remainder might hold an ``sdk`` entrypoint."""
    cwd_only = (json.dumps({"type": "user", "cwd": "/Users/x/p"}) + "\n").encode()
    _raw_transcript(tmp_path, "sid-small", cwd_only + _junk(50_000))
    assert skb.derive_kind("sid-small", root=tmp_path) == ("organic", session_kind.BASIS_ORDINARY)
    _raw_transcript(tmp_path, "sid-big", cwd_only + _junk(skb.MAX_TRANSCRIPT_BYTES + 50_000))
    assert skb.derive_kind("sid-big", root=tmp_path) == (skb.UNKNOWN_KIND, skb.BASIS_EVIDENCE_CAP)


def test_cap_boundaries_are_exact(tmp_path, monkeypatch):
    monkeypatch.setattr(skb, "MAX_TRANSCRIPT_BYTES", 4096)
    monkeypatch.setattr(skb, "MAX_LINE_BYTES", 1024)
    # A file of EXACTLY the total cap, read completely, is a complete read: no cap hit.
    _raw_transcript(tmp_path, "sid-exact", _junk(4096, 64))
    assert skb.derive_kind("sid-exact", root=tmp_path) == (skb.UNKNOWN_KIND, skb.BASIS_NO_CWD_EVIDENCE)
    # One byte more is a cap hit.
    _raw_transcript(tmp_path, "sid-over", _junk(4096, 64) + b"\n")
    assert skb.derive_kind("sid-over", root=tmp_path) == (skb.UNKNOWN_KIND, skb.BASIS_EVIDENCE_CAP)

    def line_of(content_bytes: int) -> bytes:
        row = json.dumps({"type": "user", "cwd": "/Users/x/p", "entrypoint": "cli", "pad": ""})
        pad = content_bytes - len(row)
        assert pad >= 0
        return json.dumps({"type": "user", "cwd": "/Users/x/p", "entrypoint": "cli", "pad": "x" * pad}).encode() + b"\n"

    # A line of exactly the per-line cap is read; one byte longer is a cap hit.
    _raw_transcript(tmp_path, "sid-line-ok", line_of(1024))
    assert skb.derive_kind("sid-line-ok", root=tmp_path) == ("organic", session_kind.BASIS_ORDINARY)
    _raw_transcript(tmp_path, "sid-line-long", line_of(1025))
    assert skb.derive_kind("sid-line-long", root=tmp_path) == (skb.UNKNOWN_KIND, skb.BASIS_EVIDENCE_CAP)


# ── derive_kind: the glob fallback only accepts a session id, never a path ───────

@pytest.mark.parametrize("bad", [
    "../../etc/passwd", "../outside", "a/b", "a\\b", "/etc/passwd", "..", ".", "", ".hidden",
    "x*", "x?", "x[ab]", "has space", "nul\x00byte", "é", "a" * 129,
])
def test_transcript_path_fallback_rejects_anything_that_is_not_a_session_id(tmp_path, bad):
    root = tmp_path / "a" / "claude-projects"
    (root / "-proj").mkdir(parents=True)
    # Decoys a traversal or a glob metacharacter would reach.
    (tmp_path / "a" / "etc").mkdir()
    (tmp_path / "a" / "etc" / "passwd.jsonl").write_text(json.dumps({"cwd": "/tmp/sandbox"}) + "\n")
    (root / "outside.jsonl").write_text(json.dumps({"cwd": "/tmp/sandbox"}) + "\n")
    (root / "-proj" / "xa.jsonl").write_text(json.dumps({"cwd": "/tmp/sandbox"}) + "\n")
    assert skb._transcript_path(bad, root) is None
    assert skb.derive_kind(bad, root=root) == (skb.UNKNOWN_KIND, skb.BASIS_NO_TRANSCRIPT)


def test_the_traversal_decoy_is_really_reachable_by_the_raw_pattern(tmp_path):
    """Control for the test above: without the id check, ``../../etc/passwd`` would have
    matched a file outside the projects directory, so the rejection is doing the work."""
    root = tmp_path / "a" / "claude-projects"
    (root / "-proj").mkdir(parents=True)
    (tmp_path / "a" / "etc").mkdir()
    decoy = tmp_path / "a" / "etc" / "passwd.jsonl"
    decoy.write_text("{}\n")
    raw = sorted(root.glob("*/" + __import__("glob").escape("../../etc/passwd") + ".jsonl"))
    assert [p.resolve() for p in raw] == [decoy.resolve()]


@pytest.mark.parametrize("good", ["d41e6a2c-7b3d-4f58-9a10-2d6e8c0b4f70", "agent-a1b2c3", "sid-organic", "s_1.2"])
def test_transcript_path_fallback_still_finds_ordinary_session_ids(tmp_path, good):
    _write_transcript(tmp_path, good, [{"type": "user", "cwd": "/Users/x/p"}])
    assert skb._transcript_path(good, tmp_path) is not None
    assert skb.derive_kind(good, root=tmp_path) == ("organic", session_kind.BASIS_ORDINARY)


# ── backfill_sessions: write-once, live-wins, dry-run ─────────────────────────

def test_write_once_second_run_adds_nothing(tmp_path):
    _write_transcript(tmp_path, "sid-a", [{"type": "user", "cwd": "/Users/x/p"}])
    pl.write_row(_proxy_row("sid-a"))
    first = skb.backfill_sessions(root=tmp_path)
    assert first["new_rows"] == 1 and first["written"] == 1
    second = skb.backfill_sessions(root=tmp_path)
    assert second["new_rows"] == 0 and second["written"] == 0
    assert second["already_backfilled"] == 1
    rows = list(skb._iter_jsonl(skb.sidecar_path()))
    assert len(rows) == 1  # not appended again


def test_live_tag_always_wins_no_sidecar_row_written(tmp_path):
    session_kind.tag_session("sid-tagged", "/Users/x/p", env={})
    assert session_kind.kind_of("sid-tagged") == "organic"
    # The transcript, if read, would say something else -- the live tag must still win,
    # and the backfill must not even bother writing a row for it.
    _write_transcript(tmp_path, "sid-tagged", [{"type": "user", "cwd": "/tmp/sandbox"}])
    pl.write_row(_proxy_row("sid-tagged"))
    result = skb.backfill_sessions(root=tmp_path)
    assert result["skipped_live_tag"] == 1
    assert "sid-tagged" not in skb.load_sidecar()
    index = session_kind.KindIndex([{"session_id": "sid-tagged", "session_kind": None}])
    assert index.resolve("sid-tagged") == session_kind.Resolution("organic", session_kind.SOURCE_TAG,
                                                                  ledger_disagrees=False)


def test_backfilled_kind_never_overrides_a_later_live_tag(tmp_path):
    """Write-once backfill first, then a live tag arrives (e.g. the hook finally ran
    for that session): the live tag must still win every lookup from then on."""
    _write_transcript(tmp_path, "sid-b", [{"type": "user", "cwd": "/tmp/sandbox"}])
    pl.write_row(_proxy_row("sid-b"))
    skb.backfill_sessions(root=tmp_path)
    assert skb.load_sidecar()["sid-b"] == "harness"
    session_kind.tag_session("sid-b", "/Users/x/p", env={})
    index = session_kind.KindIndex([])
    assert index.resolve("sid-b").kind == "organic"
    assert index.resolve("sid-b").source == session_kind.SOURCE_TAG


def test_proxy_stamped_session_gets_no_row(tmp_path):
    """Its proxy rows already say what it is; KindIndex never reaches the sidecar for it."""
    _write_transcript(tmp_path, "sid-stamped", [{"type": "user", "cwd": "/Users/x/p"}])
    pl.write_row(_proxy_row("sid-stamped", session_kind="research"))
    result = skb.backfill_sessions(root=tmp_path)
    assert (result["skipped_live_proxy_stamp"], result["new_rows"]) == (1, 0)
    assert not skb.sidecar_path().exists()


def test_session_whose_proxy_rows_conflict_gets_no_row(tmp_path):
    """A guess laid over a live disagreement would hide the disagreement; leave it."""
    _write_transcript(tmp_path, "sid-conflict", [{"type": "user", "cwd": "/Users/x/p"}])
    pl.write_row(_proxy_row("sid-conflict", session_kind="organic"))
    pl.write_row(_proxy_row("sid-conflict", session_kind="headless"))
    result = skb.backfill_sessions(root=tmp_path)
    assert (result["skipped_live_conflict"], result["new_rows"]) == (1, 0)


def test_unit_only_sessions_are_candidates_too(tmp_path, monkeypatch):
    """NS/D1/D2 units come from transcripts; most of their sessions are in no ledger."""
    _write_transcript(tmp_path, "sid-unit-only", [{"type": "user", "cwd": "/Users/x/p"}])
    monkeypatch.setattr(skb, "unit_session_ids", lambda root=None: {"sid-unit-only"})
    result = skb.backfill_sessions(root=tmp_path)
    assert result["candidates"] == 1 and result["candidates_only_from_units"] == 1
    assert skb.load_sidecar() == {"sid-unit-only": "organic"}


def test_with_units_false_skips_the_transcript_scan(tmp_path, monkeypatch):
    def boom(root=None):
        raise AssertionError("unit scan must not run")
    monkeypatch.setattr(skb, "unit_session_ids", boom)
    assert skb.backfill_sessions(root=tmp_path, with_units=False)["candidates"] == 0


def test_end_to_end_real_units_resolve_via_backfill_and_revert_on_delete(tmp_path, monkeypatch):
    """No stubs between the sidecar and the units: real transcripts -> real
    ``backfill_sessions`` (real unit enumeration) -> real ``ns.units`` stamping."""
    monkeypatch.setattr(skb, "unit_session_ids", _REAL_UNIT_SESSION_IDS)
    root = tmp_path / "claude-projects"
    start = time.time() - 600
    organic, headless, bare = (f"{c}1c4e6a2-7b3d-4f58-9a10-2d6e8c0b4f7{i}" for i, c in enumerate("abc"))
    for sid, extra in ((organic, {"cwd": "/Users/x/app", "entrypoint": "cli"}),
                       (headless, {"cwd": "/Users/x/app", "entrypoint": "sdk-cli"}),
                       (bare, {})):
        d = root / "-Users-x-proj"
        d.mkdir(parents=True, exist_ok=True)
        with (d / f"{sid}.jsonl").open("w", encoding="utf-8") as fh:
            for i in range(3):
                fh.write(json.dumps({"parentUuid": None, "isSidechain": False, "type": "user",
                                     "uuid": f"u{i}", "timestamp": _iso_ts(start + i), "sessionId": sid,
                                     "message": {"role": "user", "content": f"unrelated task {i}"},
                                     **extra}) + "\n")

    def stamped(**kw):
        got: dict[str, set] = {}
        for u in ns.units(days=None, root=root, **kw):
            got.setdefault(u["session_id"], set()).add((u["session_kind"], u["session_kind_source"]))
        return got

    never_had_one = stamped(backfill=True)
    assert never_had_one == {organic: {(None, None)}, headless: {(None, None)}, bare: {(None, None)}}

    result = skb.backfill_sessions(root=root)
    assert (result["candidates"], result["candidates_only_from_units"], result["written"]) == (3, 3, 3)
    assert result["by_kind"] == {"organic": 1, "headless": 1, "unknown": 1}
    assert stamped(backfill=True) == {organic: {("organic", "backfill")}, headless: {("headless", "backfill")},
                                     bare: {(None, None)}}   # unknown stays exactly as invisible as untagged
    assert stamped() == never_had_one   # backfill is opt-in: the default ignores the sidecar

    skb.sidecar_path().unlink()
    assert stamped(backfill=True) == never_had_one


def test_dry_run_writes_nothing(tmp_path):
    _write_transcript(tmp_path, "sid-c", [{"type": "user", "cwd": "/Users/x/p"}])
    pl.write_row(_proxy_row("sid-c"))
    result = skb.backfill_sessions(root=tmp_path, dry_run=True)
    assert result["dry_run"] is True
    assert result["new_rows"] == 1  # it computed what it WOULD write
    assert result["written"] == 0
    assert not skb.sidecar_path().exists()


def test_file_mode_is_0600(tmp_path):
    _write_transcript(tmp_path, "sid-d", [{"type": "user", "cwd": "/Users/x/p"}])
    pl.write_row(_proxy_row("sid-d"))
    skb.backfill_sessions(root=tmp_path)
    mode = stat.S_IMODE(skb.sidecar_path().stat().st_mode)
    assert mode == 0o600


def test_corrupt_sidecar_line_is_skipped_not_fatal(tmp_path):
    path = skb.sidecar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"session_id": "ok-1", "kind": "organic", "source": "backfill", "basis": "x", "ts": 1}\n'
        "not even json\n"
        '{"session_id": "ok-2"}\n'  # valid json, missing "kind" -- not usable either
        '{"session_id": "ok-3", "kind": "research", "source": "backfill", "basis": "x", "ts": 1}\n',
        encoding="utf-8")
    loaded = skb.load_sidecar(path)
    assert loaded == {"ok-1": "organic", "ok-3": "research"}


def test_duplicate_session_id_keeps_the_first_row(tmp_path):
    """Write-once means this should never happen from this module's own writer, but a
    hand-edited or manually-appended file must not silently let a later row win."""
    path = skb.sidecar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"session_id": "dup-1", "kind": "organic", "source": "backfill", "basis": "x", "ts": 1}\n'
        '{"session_id": "dup-1", "kind": "research", "source": "backfill", "basis": "y", "ts": 2}\n',
        encoding="utf-8")
    assert skb.load_sidecar(path) == {"dup-1": "organic"}


def test_non_dict_json_line_is_skipped_not_fatal(tmp_path):
    path = skb.sidecar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[1, 2, 3]\n"
        "42\n"
        '{"session_id": "ok", "kind": "organic", "source": "backfill", "basis": "x", "ts": 1}\n',
        encoding="utf-8")
    assert skb.load_sidecar(path) == {"ok": "organic"}


def test_unknown_in_sidecar_never_resolves_in_kindindex(tmp_path):
    """An "unknown" backfill row must leave the session exactly as unresolved as a
    session with no sidecar row at all -- never a fifth pseudo-kind leaking out."""
    path = skb.sidecar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_id": "sid-unk", "kind": "unknown",
                                "source": "backfill", "basis": "no_transcript", "ts": 1}) + "\n",
                    encoding="utf-8")
    index = session_kind.KindIndex([])
    res = index.resolve("sid-unk")
    assert res.kind is None and res.source is None


# ── KPI join: the "backfilled" annotation and the sidecar-delete revert ──────

def _iso_ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _unit(sid, kind="claude_main_call", outcome=ns.OUTCOME_NOT_ROUTED, **kw):
    return {"session_id": sid, "kind": kind, "outcome": outcome, "lever": None,
            "ts": _iso_ts(NOW - 100), **kw}


def _attempted():
    return sorted(ns.ATTEMPTED_KINDS)[0]


def _mixed(sid):
    return ([_unit(sid, _attempted(), ns.OUTCOME_USED) for _ in range(30)]
            + [_unit(sid, _attempted(), ns.OUTCOME_REDO) for _ in range(30)]
            + [_unit(sid) for _ in range(40)])


def _stream(monkeypatch, rows):
    monkeypatch.setattr(ns, "units", lambda days=30, session_id=None, root=None, backfill=False: iter(rows))


def _write_proxy_ledger(rows):
    path = pl.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _write_sidecar(sid, kind="organic", basis="ordinary"):
    path = skb.sidecar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"session_id": sid, "kind": kind, "source": "backfill",
                             "basis": basis, "ts": NOW}) + "\n")


def test_untagged_session_is_excluded_without_a_sidecar(monkeypatch):
    """Baseline the other tests compare against: no tag, no sidecar -> not counted."""
    _stream(monkeypatch, _mixed("s-pre-250"))
    k = kpi.compute_scorecard(days=7, now=NOW)
    assert k["kpis"]["NS"]["value"].startswith("not measurable: ")
    assert k["kpis"]["NS"]["n"] is None


def test_kpi_ns_d1_d2_resolve_via_the_sidecar_and_state_the_backfilled_count(monkeypatch):
    _stream(monkeypatch, _mixed("s-backfilled"))
    _write_sidecar("s-backfilled")
    k = kpi.compute_scorecard(days=7, now=NOW)["kpis"]
    assert k["NS"]["value"] == "30.0% (n=100, 100 backfilled)"
    assert k["D1"]["value"] == "60.0% (n=100, 100 backfilled)"
    assert k["D2"]["value"] == "50.0% (n=60, 60 backfilled)"   # D2's n is attempted units only
    assert (k["NS"]["backfilled"], k["D1"]["backfilled"], k["D2"]["backfilled"]) == (100, 100, 60)


def test_backfilled_is_a_share_of_n_not_all_or_nothing(monkeypatch):
    """One tagged session plus one backfilled: the annotation counts only the latter."""
    session_kind.tag_session("s-live", "/Users/x/p", env={})
    _stream(monkeypatch, _mixed("s-live") + _mixed("s-backfilled"))
    _write_sidecar("s-backfilled")
    k = kpi.compute_scorecard(days=7, now=NOW)
    assert k["kpis"]["NS"]["value"] == "30.0% (n=200, 100 backfilled)"
    assert k["joins"]["joined_by_source"] == {"tag": 100, "backfill": 100}


def test_live_tag_beats_a_conflicting_sidecar_row_in_the_kpi(monkeypatch):
    """The sidecar says organic; the live tag says research. Research is not an
    organic session, so the KPI must stay unmeasurable and nothing is annotated."""
    session_kind.tag_session("s-both", "/Users/x/work/scratchpad/p", env={})
    _write_sidecar("s-both", kind="organic")
    _stream(monkeypatch, _mixed("s-both"))
    k = kpi.compute_scorecard(days=7, now=NOW)["kpis"]
    assert k["NS"]["value"].startswith("not measurable: ")


def test_a_proxy_ledger_stamp_beats_the_sidecar(monkeypatch):
    _write_proxy_ledger([{"ts": NOW, "session_id": "s-stamped", "session_kind": "research"}] * 3)
    _write_sidecar("s-stamped", kind="organic")
    _stream(monkeypatch, _mixed("s-stamped"))
    k = kpi.compute_scorecard(days=7, now=NOW)
    assert k["kpis"]["NS"]["value"].startswith("not measurable: ")
    assert k["joins"]["joined_by_source"] == {"proxy_ledger": 100}


def test_unknown_sidecar_row_keeps_the_session_excluded_not_organic(monkeypatch):
    _stream(monkeypatch, _mixed("s-unk"))
    _write_sidecar("s-unk", kind="unknown", basis="no_transcript")
    k = kpi.compute_scorecard(days=7, now=NOW)
    assert k["kpis"]["NS"]["value"].startswith("not measurable: ")
    assert k["kpis"]["NS"]["n"] is None
    assert "backfill" not in k["joins"]["joined_by_source"]


def test_a_backfilled_research_session_stays_out_of_organic_kpis(monkeypatch):
    _stream(monkeypatch, _mixed("s-research"))
    _write_sidecar("s-research", kind="research", basis="cwd_scratchpad")
    k = kpi.compute_scorecard(days=7, now=NOW)
    assert k["kpis"]["NS"]["value"].startswith("not measurable: ")
    assert k["joins"]["joined_by_kind"] == {"research": 100}


def test_no_backfilled_note_when_nothing_is_backfilled(monkeypatch):
    _write_proxy_ledger([{"ts": NOW, "session_id": "s-ledger", "session_kind": "organic"}] * 3)
    _stream(monkeypatch, _mixed("s-ledger"))
    k = kpi.compute_scorecard(days=7, now=NOW)["kpis"]
    assert k["NS"]["value"] == "30.0% (n=100)"   # the pre-backfill format, unchanged
    assert k["NS"]["backfilled"] == 0


def test_too_few_line_also_states_the_backfilled_count(monkeypatch):
    rows = [_unit("s-small", _attempted(), ns.OUTCOME_USED) for _ in range(10)]
    _stream(monkeypatch, rows)
    _write_sidecar("s-small")
    ns_kpi = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["NS"]
    assert ns_kpi["value"] == f"{kpi.TOO_FEW} (n=10, 10 backfilled)"
    assert ns_kpi["measurable"] is False


def test_d3_counts_backfilled_decided_rows(monkeypatch):
    from llm_router import usage_outcome

    rows = ([{"outcome": usage_outcome.OUTCOME_USED, "session_id": "s-d3", "session_kind": None, "ts": NOW - 5}] * 45
            + [{"outcome": usage_outcome.OUTCOME_REDONE, "session_id": "s-d3", "session_kind": None, "ts": NOW - 4}] * 15
            + [{"outcome": usage_outcome.OUTCOME_REDONE, "session_id": "s-other", "session_kind": None}] * 40)
    monkeypatch.setattr(usage_outcome, "judge_recent", lambda days=7, root=None: rows)
    _stream(monkeypatch, [])
    _write_sidecar("s-d3")
    d3 = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["D3"]
    assert d3["value"] == "25.0% (n=60, 60 backfilled)"   # s-other has no kind: excluded, not counted
    assert d3["backfilled"] == 60


def test_deleting_the_sidecar_restores_identical_kpi_output(monkeypatch):
    """The owner's "reversible by deleting the file", stated as an equality: the whole
    scorecard after deletion is the scorecard from a machine that never had a sidecar."""
    _stream(monkeypatch, _mixed("s-gone"))
    never_had_one = kpi.compute_scorecard(days=7, now=NOW)
    _write_sidecar("s-gone")
    with_sidecar = kpi.compute_scorecard(days=7, now=NOW)
    assert with_sidecar["kpis"]["NS"]["value"] == "30.0% (n=100, 100 backfilled)"
    assert with_sidecar != never_had_one
    skb.sidecar_path().unlink()
    assert kpi.compute_scorecard(days=7, now=NOW) == never_had_one
    assert kpi.render_scorecard(kpi.compute_scorecard(days=7, now=NOW)) \
        == kpi.render_scorecard(never_had_one)


def test_corrupt_sidecar_does_not_break_the_scorecard(monkeypatch):
    _stream(monkeypatch, _mixed("s-ok"))
    _write_sidecar("s-ok")
    with skb.sidecar_path().open("a", encoding="utf-8") as fh:
        fh.write("this line is not json\n")
    k = kpi.compute_scorecard(days=7, now=NOW)["kpis"]
    assert k["NS"]["value"] == "30.0% (n=100, 100 backfilled)"


def test_health_still_works_with_a_sidecar(monkeypatch):
    _stream(monkeypatch, _mixed("s-health"))
    _write_sidecar("s-health")
    data = kpi.compute_scorecard(days=7, now=NOW)
    health = kpi.compute_health(data)
    assert health["kpis"]["NS"]["state"] == kpi.STATE_MEASURED
    assert "backfilled" in kpi.render_scorecard(data)


# ── the sidecar is read by KindIndex only: kind_of (hot path, write-once check) ignores it ──

def test_kind_of_never_reads_the_sidecar():
    _write_sidecar("s-hot", kind="organic")
    assert session_kind.kind_of("s-hot") is None  # tag_session's write-once check must not see it
    session_kind.tag_session("s-hot", "/Users/x/work/scratchpad/p", env={})
    assert session_kind.kind_of("s-hot") == "research"  # a backfilled row never pre-empts a live tag


def test_kindindex_backfill_false_never_reads_the_sidecar():
    _write_sidecar("s-off", kind="organic")
    assert session_kind.KindIndex([], backfill=False).resolve("s-off").kind is None
    res = session_kind.KindIndex([]).resolve("s-off")
    assert (res.kind, res.source) == ("organic", session_kind.SOURCE_BACKFILL)


# ── where the sidecar is read: kpi yes, the hook / Stop-line path no ─────────────

_UNTAGGED_SID = "d41e6a2c-7b3d-4f58-9a10-2d6e8c0b4f70"


def _untagged_session(root: Path, sid: str = _UNTAGGED_SID, n: int = 3) -> str:
    """A real transcript of recent user turns for a session with no tag, no stamp and no
    proxy row: the only thing that can resolve its kind is the backfill sidecar."""
    start = time.time() - 600
    d = root / "-Users-x-proj"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{sid}.jsonl").open("w", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(json.dumps({"parentUuid": None, "isSidechain": False, "type": "user",
                                 "uuid": f"u{i}", "timestamp": _iso_ts(start + i), "sessionId": sid,
                                 "cwd": "/Users/x/app", "entrypoint": "cli",
                                 "message": {"role": "user", "content": f"unrelated task {i}"}}) + "\n")
    return sid


def _count_sidecar_loads(monkeypatch) -> list:
    calls: list = []
    real = skb.load_sidecar

    def counting(path=None):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(skb, "load_sidecar", counting)
    return calls


def test_stop_line_never_reads_the_sidecar_but_the_kpi_opt_in_does(tmp_path, monkeypatch):
    """The Stop hook runs ``current_session_line`` on EVERY turn. It shows a share, not a
    kind, so it must not open the sidecar: with an untagged session and a populated
    sidecar (the one case that WOULD resolve through it) it makes zero loads. The control
    below is the same fixture through the explicit ``backfill=True`` opt-in, so a zero is a
    measurement, not an empty fixture."""
    root = tmp_path / "claude-projects"
    sid = _untagged_session(root)
    _write_sidecar(sid)
    calls = _count_sidecar_loads(monkeypatch)

    line = ns.current_session_line(sid, root=root)
    assert line.startswith("north star") and "unavailable" not in line
    assert calls == []

    stamps = {(u["session_kind"], u["session_kind_source"])
              for u in ns.units(days=2, session_id=sid, root=root, backfill=True)}
    assert stamps == {("organic", "backfill")}      # the fixture does resolve through the sidecar
    assert len(calls) == 1                          # ... and that cost exactly one load


def test_hook_callers_of_northstar_units_never_read_the_sidecar(tmp_path, monkeypatch):
    """``quality_breaker`` reads ``northstar.units()`` for the UserPromptSubmit, Agent and
    Stop hooks; ``llm-router northstar`` reads ``report()``. None of them uses the kind."""
    from llm_router import quality_breaker

    root = tmp_path / "claude-projects"
    sid = _untagged_session(root)
    _write_sidecar(sid)
    calls = _count_sidecar_loads(monkeypatch)

    quality_breaker._fetch_class_units("agent_route", None, None)
    ns.report(days=2, session_id=sid, root=root)
    list(ns.units(days=2, session_id=sid, root=root))
    ns.build_sessions(days=2, root=root)
    assert calls == []

    # Control: the fixture is not empty -- the explicit opt-in resolves it through the sidecar.
    stamps = {(u["session_kind"], u["session_kind_source"])
              for u in ns.units(days=2, session_id=sid, root=root, backfill=True)}
    assert stamps == {("organic", "backfill")} and len(calls) == 1


def test_kpi_still_resolves_the_same_fixture_through_the_sidecar(tmp_path, monkeypatch):
    """Same untagged session, same populated sidecar, through the real (unstubbed)
    ``ns.units`` that ``kpi`` calls: it must resolve to organic via the sidecar and say so."""
    root = tmp_path / "claude-projects"
    sid = _untagged_session(root, n=4)
    _write_sidecar(sid)
    calls = _count_sidecar_loads(monkeypatch)

    k = kpi.compute_scorecard(days=7)
    assert len(calls) >= 1
    assert k["joins"]["joined_by_source"] == {"backfill": 4}
    assert k["joins"]["joined_by_kind"] == {"organic": 4}
    assert k["kpis"]["NS"]["backfilled"] == 4
    assert "(n=4, 4 backfilled)" in k["kpis"]["NS"]["value"]


def test_kpi_asks_northstar_for_the_backfill_explicitly(monkeypatch):
    """The kpi path opts in by name rather than by a default, so flipping northstar's
    default back on (the Stop-line regression) cannot be what keeps kpi working."""
    asked: dict = {}

    def fake_units(days=30, session_id=None, root=None, backfill=False):
        asked["backfill"] = backfill
        return iter([])

    monkeypatch.setattr(ns, "units", fake_units)
    kpi.compute_scorecard(days=7, now=NOW)
    assert asked == {"backfill": True}


def test_backfill_is_off_by_default_at_every_northstar_layer(monkeypatch):
    seen: list = []
    real = session_kind.KindIndex

    class Spy(real):
        def __init__(self, *a, backfill=True, **kw):
            seen.append(backfill)
            super().__init__(*a, backfill=backfill, **kw)

    monkeypatch.setattr(session_kind, "KindIndex", Spy)
    ns._scan_proxy_ledger()
    ns._load_proxy_served()
    ns.build_sessions(days=2)
    list(ns.units(days=2))
    assert seen == [False] * 4
    ns._scan_proxy_ledger(backfill=True)
    assert seen[-1] is True


# ── the production stamping branch of the KPI join ───────────────────────────────

def _stamped_rows(sid, kind, source, n=100):
    """The shape ``northstar.units()`` really yields: it has already stamped each unit
    with ``session_kind`` and ``session_kind_source``, so the KPI must trust that stamp
    and not go back to its own index."""
    return [dict(r, session_kind=kind, session_kind_source=source) for r in _mixed(sid)[:n]]


def test_production_stamped_units_count_their_own_source_not_the_index(monkeypatch):
    """No sidecar, no tag, no proxy rows: the KPI's own index has nothing for this
    session, so the ``backfilled`` count can only come from the unit's own stamp."""
    rows = (_stamped_rows("s-bf", "organic", "backfill", 40) + _stamped_rows("s-tag", "organic", "tag", 30)
            + _stamped_rows("s-st", "organic", "stamp", 20) + _stamped_rows("s-px", "organic", "proxy_ledger", 10))
    monkeypatch.setattr(ns, "units", lambda days=30, session_id=None, root=None, backfill=False: iter(rows))
    assert not skb.sidecar_path().exists()
    k = kpi.compute_scorecard(days=7, now=NOW)
    assert k["joins"]["joined_by_source"] == {"backfill": 40, "tag": 30, "stamp": 20, "proxy_ledger": 10}
    assert k["kpis"]["NS"]["backfilled"] == 40
    assert k["kpis"]["NS"]["n"] == 100
    assert "n=100, 40 backfilled" in k["kpis"]["NS"]["value"]


def test_production_stamp_of_none_is_not_rescued_by_the_index(monkeypatch):
    """A unit that ``units()`` stamped ``None`` stays untagged even if the KPI's own index
    could have resolved the session: the stamp is the one source of truth on this branch."""
    _write_sidecar("s-none", kind="organic")
    rows = _stamped_rows("s-none", None, None)
    monkeypatch.setattr(ns, "units", lambda days=30, session_id=None, root=None, backfill=False: iter(rows))
    k = kpi.compute_scorecard(days=7, now=NOW)
    assert k["kpis"]["NS"]["value"].startswith("not measurable: ")
    assert k["joins"]["joined"] == 0 and k["joins"]["untagged"] == 100


def test_production_stamp_of_a_non_organic_kind_is_excluded(monkeypatch):
    rows = _stamped_rows("s-res", "research", "backfill")
    monkeypatch.setattr(ns, "units", lambda days=30, session_id=None, root=None, backfill=False: iter(rows))
    k = kpi.compute_scorecard(days=7, now=NOW)
    assert k["kpis"]["NS"]["value"].startswith("not measurable: ")
    assert k["joins"]["joined_by_kind"] == {"research": 100}


def test_classify_with_basis_agrees_with_classify_on_every_combination():
    home = Path("/home/u")
    cwds = [None, "", "/home/u/proj", "/home/u/.rsi/x", "/home/u/work/scratchpad/p",
            "/tmp/private/x", "/private/tmp/claude-501/x/scratchpad"]
    for cwd in cwds:
        for entrypoint in (None, "cli", "sdk-cli"):
            for override in (None, "research", "bogus"):
                kind, basis = session_kind.classify_with_basis(
                    cwd=cwd, entrypoint=entrypoint, override=override, home=home)
                assert kind == session_kind.classify(
                    cwd=cwd, entrypoint=entrypoint, override=override, home=home)
                assert kind in session_kind.VALID_KINDS and basis


def test_cwd_rules_outrank_the_sdk_entrypoint_rule():
    """Pinned to a literal expected verdict, not to classify() (classify() IS
    classify_with_basis()[0] -- comparing the two proves nothing about ORDER)."""
    home = Path("/home/u")
    # A cwd that qualifies as both research (under ~/.rsi) and -- if the cwd check were
    # skipped -- headless (sdk entrypoint): research must win.
    kind, basis = session_kind.classify_with_basis(
        cwd="/home/u/.rsi/proj", entrypoint="sdk-cli", home=home)
    assert (kind, basis) == (session_kind.KIND_RESEARCH, session_kind.BASIS_CWD_RSI)
    # Same for the scratchpad research rule.
    kind, basis = session_kind.classify_with_basis(
        cwd="/home/u/work/scratchpad/p", entrypoint="sdk-cli", home=home)
    assert (kind, basis) == (session_kind.KIND_RESEARCH, session_kind.BASIS_CWD_SCRATCHPAD)
    # And the sandbox/harness cwd rule over the sdk entrypoint rule.
    kind, basis = session_kind.classify_with_basis(
        cwd="/tmp/private/x", entrypoint="sdk-cli", home=home)
    assert (kind, basis) == (session_kind.KIND_HARNESS, session_kind.BASIS_CWD_SANDBOX)
    # With no cwd rule matched, the sdk entrypoint rule is what decides headless.
    kind, basis = session_kind.classify_with_basis(
        cwd="/home/u/ordinary", entrypoint="sdk-cli", home=home)
    assert (kind, basis) == (session_kind.KIND_HEADLESS, session_kind.BASIS_ENTRYPOINT_SDK)


def test_derive_kind_ignores_a_non_string_cwd_but_still_reads_entrypoint(tmp_path):
    """A transcript record with a malformed (non-string) cwd must not be mistaken for
    real cwd evidence -- it must fall through exactly as if cwd were absent, so a
    later record's entrypoint (or a later record's valid cwd) still decides the kind,
    never a silent "no evidence found" when evidence of the other kind exists."""
    _write_transcript(tmp_path, "sid-bad-cwd",
                      [{"type": "user", "cwd": 12345, "entrypoint": "sdk-cli"}])
    kind, basis = skb.derive_kind("sid-bad-cwd", root=tmp_path)
    assert (kind, basis) == ("headless", session_kind.BASIS_ENTRYPOINT_SDK)


# ── the command ───────────────────────────────────────────────────────────────

def test_cli_backfill_tags_dry_run_prints_counts_and_writes_nothing(tmp_path, monkeypatch, capsys):
    root = tmp_path / "claude-projects"
    _write_transcript(root, "sid-cli", [{"type": "user", "cwd": "/Users/x/secret-project-name"}])
    _write_proxy_ledger([_proxy_row("sid-cli")])
    rc = kpi.cmd_kpi(["--backfill-tags", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "would write 1 new sidecar row" in out
    assert "secret-project-name" not in out  # counts and codes only, never a path
    assert not skb.sidecar_path().exists()


def test_cli_backfill_tags_writes_then_is_a_noop(tmp_path, capsys):
    root = tmp_path / "claude-projects"
    _write_transcript(root, "sid-cli2", [{"type": "user", "cwd": "/Users/x/p"}])
    _write_proxy_ledger([_proxy_row("sid-cli2")])
    assert kpi.cmd_kpi(["--backfill-tags", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["written"] == 1 and first["by_kind"] == {"organic": 1}
    assert kpi.cmd_kpi(["--backfill-tags", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["written"] == 0 and second["already_backfilled"] == 1


def test_cli_dry_run_alone_is_an_error():
    with pytest.raises(SystemExit):
        kpi.cmd_kpi(["--dry-run"])


def test_sidecar_rows_hold_only_the_documented_fields(tmp_path):
    root = tmp_path / "claude-projects"
    _write_transcript(root, "sid-fields", [{"type": "user", "cwd": "/Users/x/secret", "entrypoint": "cli",
                                            "message": {"content": "PRIVATE PROMPT TEXT"}}])
    _write_proxy_ledger([_proxy_row("sid-fields")])
    skb.backfill_sessions(root=root)
    text = skb.sidecar_path().read_text(encoding="utf-8")
    row = json.loads(text.strip())
    assert set(row) == {"session_id", "kind", "source", "basis", "ts"}
    assert row["source"] == "backfill"
    assert "PRIVATE" not in text and "secret" not in text


# ── concurrency: the append is a locked, re-checked critical section ────────────

def _row(sid, kind="organic"):
    return {"session_id": sid, "kind": kind, "source": "backfill", "basis": "ordinary", "ts": NOW}


def test_append_rows_skips_a_session_another_run_already_wrote():
    """The deterministic core of the race: this run's snapshot was taken before another
    run wrote. ``_append_rows`` must re-check inside its lock and write only what is
    still missing -- and say how many that was."""
    path = skb.sidecar_path()
    assert skb._append_rows(path, [_row("a"), _row("b")]) == 2
    assert skb._append_rows(path, [_row("b", "research"), _row("c"), _row("c")]) == 1  # b exists; c once
    ids = [json.loads(line)["session_id"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert ids == ["a", "b", "c"]
    assert skb.load_sidecar(path)["b"] == "organic"    # the first row is the one that stays


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root ignores directory modes")
def test_append_rows_reports_a_read_only_home_as_a_permission_error_not_a_lock_clash(monkeypatch, tmp_path):
    home = tmp_path / "ro-home"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    ro_state = skb.sidecar_path().parent
    ro_state.mkdir(parents=True, exist_ok=True)
    ro_state.chmod(0o555)
    try:
        with pytest.raises(OSError) as ei:
            skb._append_rows(skb.sidecar_path(), [_row("a")])
        msg = str(ei.value)
        assert "cannot write" in msg and "nothing was written" in msg
        assert "another --backfill-tags running" not in msg
        assert "denied: permission denied" not in msg.lower()
        assert not skb.sidecar_path().exists()
    finally:
        ro_state.chmod(0o755)


def test_append_rows_reports_an_uncreatable_state_dir_and_says_nothing_was_written(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")                                  # a file where the dir must go
    monkeypatch.setenv("LLM_ROUTER_HOME", str(blocker / "state"))
    with pytest.raises(OSError) as ei:
        skb._append_rows(skb.sidecar_path(), [_row("a")])
    assert "cannot create" in str(ei.value) and "nothing was written" in str(ei.value)
    assert "another --backfill-tags running" not in str(ei.value)


def test_append_rows_refuses_to_write_without_the_lock(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def never_held(_path, timeout=0):
        yield False

    monkeypatch.setattr(skb, "exclusive_lock", never_held)
    with pytest.raises(OSError, match="could not lock"):
        skb._append_rows(skb.sidecar_path(), [_row("a")])
    assert not skb.sidecar_path().exists()    # nothing written, not even unlocked


def test_backfill_report_says_when_a_concurrent_run_got_there_first(tmp_path, monkeypatch):
    _write_transcript(tmp_path, "sid-raced", [{"type": "user", "cwd": "/Users/x/p"}])
    pl.write_row(_proxy_row("sid-raced"))
    real = skb._append_rows

    def lose_the_race(path, rows):
        real(path, rows)          # "the other run" writes the same row first ...
        return real(path, rows)   # ... so this run's append finds it already there

    monkeypatch.setattr(skb, "_append_rows", lose_the_race)
    result = skb.backfill_sessions(root=tmp_path)
    assert (result["new_rows"], result["written"]) == (1, 0)
    assert "1 of them were already written by a concurrent run" in skb.render_backfill_report(result)
    assert len(skb.sidecar_path().read_text(encoding="utf-8").splitlines()) == 1


_RACER = textwrap.dedent("""
    import json, sys, time
    from pathlib import Path
    from llm_router import session_kind_backfill as skb

    idx, n, sync = int(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
    real, calls = skb.load_sidecar, [0]

    def gated(path=None):
        out = real(path)
        calls[0] += 1
        if calls[0] == 1:   # the snapshot backfill_sessions decides "new" from: hold every
            (sync / f"ready-{idx}").write_text("")   # process here until all have taken theirs
            deadline = time.time() + 60
            while len(list(sync.glob("ready-*"))) < n:
                if time.time() > deadline:
                    raise SystemExit("rendezvous timed out")
                time.sleep(0.005)
        elif calls[0] == 2:   # the re-check inside the lock: widen check-then-append so that
            time.sleep(0.05)  # without the lock every process would still see "nothing there"
        return out

    skb.load_sidecar = gated
    r = skb.backfill_sessions(with_units=False)
    print(json.dumps({"written": r["written"], "new_rows": r["new_rows"]}))
""")


def test_six_concurrent_backfills_write_every_session_exactly_once(tmp_path):
    """Six real processes, all holding a stale "nothing is in the sidecar yet" snapshot
    (a rendezvous after the first read guarantees it), then writing at once. Without the
    lock and the re-check each writes all N rows: 6N lines, interleaved mid-line."""
    procs, n_sessions = 6, 400
    root = tmp_path / "claude-projects"
    sids = [f"c{i:03d}1c4e6a2-7b3d-4f58-9a10-2d6e8c0b4f70" for i in range(n_sessions)]
    for sid in sids:
        _write_transcript(root, sid, [{"type": "user", "cwd": "/Users/x/p", "entrypoint": "cli"}])
    _write_proxy_ledger([_proxy_row(sid) for sid in sids])
    sync = tmp_path / "sync"
    sync.mkdir()
    env = {**os.environ, "CLAUDE_PROJECTS_DIR": str(root), "OLLAMA_HOST": "127.0.0.1:1"}
    children = [subprocess.Popen([sys.executable, "-c", _RACER, str(i), str(procs), str(sync)],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for i in range(procs)]
    outs = [c.communicate(timeout=120) for c in children]
    assert [c.returncode for c in children] == [0] * procs, [o[1][-400:] for o in outs]
    reports = [json.loads(o[0].strip().splitlines()[-1]) for o in outs]
    assert {r["new_rows"] for r in reports} == {n_sessions}    # every process believed all were new

    lines = skb.sidecar_path().read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]                # a torn line would not parse
    assert all(set(r) == {"session_id", "kind", "source", "basis", "ts"} for r in rows)
    got = [r["session_id"] for r in rows]
    assert len(got) == len(set(got)) == n_sessions, f"{len(got)} rows for {n_sessions} sessions"
    assert sorted(got) == sorted(sids)
    assert sum(r["written"] for r in reports) == n_sessions    # each row written by exactly one run


# ── the parent directory: 0700 only if this module made it ───────────────────────

def test_a_directory_the_module_creates_is_0700(tmp_path, monkeypatch):
    home = tmp_path / "fresh-home"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    assert not home.exists()
    old = os.umask(0)   # the worst case: nothing masked off, so any looseness would show
    try:
        skb._append_rows(skb.sidecar_path(), [_row("a")])
    finally:
        os.umask(old)
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE(skb.sidecar_path().stat().st_mode) == 0o600


def test_an_existing_directory_is_never_chmodded(tmp_path, monkeypatch):
    home = tmp_path / "users-own-dir"
    home.mkdir()
    home.chmod(0o755)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    skb._append_rows(skb.sidecar_path(), [_row("a")])
    assert stat.S_IMODE(home.stat().st_mode) == 0o755   # the user's choice, untouched
    assert stat.S_IMODE(skb.sidecar_path().stat().st_mode) == 0o600


# ── validation against live evidence ──────────────────────────────────────────

def test_validate_against_live_scores_agreement_and_a_confusion_table(tmp_path):
    root = tmp_path / "claude-projects"
    # live tag organic, transcript agrees
    session_kind.tag_session("v-agree", "/Users/x/p", env={})
    _write_transcript(root, "v-agree", [{"type": "user", "cwd": "/Users/x/p"}])
    # live kind headless via stamps only, transcript says organic -> a disagreement
    _write_transcript(root, "v-miss", [{"type": "user", "cwd": "/Users/x/p", "entrypoint": "cli"}])
    pl.write_row(_proxy_row("v-miss", session_kind="headless"))
    # live tag organic, no transcript at all -> unknown (a miss under the strict count)
    session_kind.tag_session("v-nofile", "/Users/x/p", env={})
    pl.write_row(_proxy_row("v-nofile"))  # in a ledger (so it has data to join), but no transcript
    # conflicting stamps -> excluded, not scored
    _write_transcript(root, "v-conflict", [{"type": "user", "cwd": "/Users/x/p"}])
    pl.write_row(_proxy_row("v-conflict", session_kind="organic"))
    pl.write_row(_proxy_row("v-conflict", session_kind="research"))
    # untagged, unstamped -> not in the validation population at all
    _write_transcript(root, "v-untagged", [{"type": "user", "cwd": "/Users/x/p"}])

    v = skb.validate_against_live(root=root)
    assert (v["n"], v["agree"], v["unknown"], v["disagree"]) == (3, 1, 1, 1)
    assert v["live_conflicts_excluded"] == 1
    assert v["by_live_source"] == {"tag": 2, "stamp": 1}
    assert v["agreement_strict"] == pytest.approx(1 / 3)
    assert v["agreement_decided"] == pytest.approx(1 / 2)
    assert v["confusion"] == {"headless": {"organic": 1}, "organic": {"organic": 1, "unknown": 1}}
    assert not skb.sidecar_path().exists()  # validation never writes


def test_validate_with_no_live_sessions_is_not_measurable(tmp_path):
    v = skb.validate_against_live(root=tmp_path / "claude-projects")
    assert v["n"] == 0 and v["agreement_strict"] is None and v["agreement_decided"] is None
    assert "not measurable" in skb.render_validation(v)


def test_cli_validate_backfill_prints_counts_only(tmp_path, capsys):
    root = tmp_path / "claude-projects"
    session_kind.tag_session("v-cli", "/Users/x/secret-project", env={})
    _write_transcript(root, "v-cli", [{"type": "user", "cwd": "/Users/x/secret-project"}])
    assert kpi.cmd_kpi(["--validate-backfill"]) == 0
    out = capsys.readouterr().out
    assert "1 session(s) scored" in out and "secret-project" not in out
    assert not skb.sidecar_path().exists()
