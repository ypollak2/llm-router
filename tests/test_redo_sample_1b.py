"""Synthetic CLI regressions for exclusion provenance and atomic freezes."""
from __future__ import annotations

import hashlib
import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from llm_router import offload_share, redo_sample as sample, session_kind
from tests.test_redo_sample import isolated as isolated, record

EMPTY = "M0.9 exclusion list EMPTY: spent M0.9 pairs are NOT excluded"
WARNING = (
    "WARNING: most sessions are untagged: N_F shortfall may be a tagging gap, "
    "not detector supply -- do not tune the detector on this result"
)
PROMPT = "PRIVATE_PROMPT_MARKER"
ANSWER = "PRIVATE_ANSWER_MARKER"
SID = "PRIVATE_SESSION_MARKER"


def write(root, sid):
    project = root / "project"
    project.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).timestamp()
    entries = [record(PROMPT, now - 60),
               record(ANSWER, now - 59, "assistant"),
               record("claude: " + PROMPT, now - 58),
               record(ANSWER, now - 57, "assistant")]
    (project / f"{sid}.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in entries), encoding="utf-8")


@pytest.fixture
def transcripts(isolated, monkeypatch):
    # Keep collect/KindIndex real; never consult live served ledgers.
    monkeypatch.setattr(offload_share, "action_units", lambda *a: {"actions": []})
    monkeypatch.setattr(session_kind, "override_of", lambda sid: None)
    monkeypatch.setattr(session_kind, "tag_kind_of", lambda sid: "organic")
    write(isolated, SID)
    return isolated


def freeze(root, run_id="fixture", *extra):
    return sample.cmd_redo_sample([
        "--freeze", "--projects-dir", str(root), "--run-id", run_id,
        "--allow-below-threshold", *extra])


def run_dir(root, run_id="fixture"):
    return root.parent / "state" / "redo_validation" / run_id


@pytest.mark.parametrize("nonempty", [False, True])
def test_census_exclusion_notice(transcripts, monkeypatch, capsys, nonempty):
    if nonempty:
        monkeypatch.setattr(sample, "load_excluded", lambda extra=None, source=None: {"abc"})
    assert sample.cmd_redo_sample(["--census"]) == 0
    output = capsys.readouterr().out
    assert (EMPTY in output.splitlines()) is (not nonempty)
    assert f"exclusion_list_size: {int(nonempty)}" in output


@pytest.mark.parametrize("nonempty", [False, True])
def test_freeze_exclusion_provenance(transcripts, monkeypatch, capsys, nonempty):
    if nonempty:
        monkeypatch.setattr(sample, "load_excluded", lambda extra=None, source=None: {"abc"})
    else:
        assert freeze(transcripts) == 5
        output = capsys.readouterr().out
        assert EMPTY in output.splitlines()
        assert ("Refused: --allow-empty-exclusion is required to freeze without an M0.9 "
                "exclusion list.") in output.splitlines()
        assert not run_dir(transcripts).parent.exists()
    extra = () if nonempty else ("--allow-empty-exclusion",)
    assert freeze(transcripts, "fixture", *extra) == 0
    run = run_dir(transcripts)
    frozen = json.loads((run / "frozen.json").read_text())
    sha = hashlib.sha256(b"abc" if nonempty else b"").hexdigest()
    assert frozen["exclusion_list_size"] == int(nonempty)
    assert frozen["exclusion_list_sha256"] == sha
    assert frozen["allow_empty_exclusion"] is (not nonempty)
    assert frozen["counts"]["exclusion_list_size"] == int(nonempty)
    assert frozen["counts"]["exclusion_list_sha256"] == sha
    rows = [json.loads(line) for line in (run / "manifest.jsonl").read_text().splitlines()]
    assert rows
    assert all(row["exclusion_list_sha256"] == sha for row in rows)
    assert frozen["manifest_sha256"] == hashlib.sha256(
        (run / "manifest.jsonl").read_bytes()).hexdigest()
    assert stat.S_IMODE(run.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in run.iterdir())


def test_threshold_refusal_precedes_empty_exclusion(transcripts, capsys):
    assert sample.cmd_redo_sample(["--freeze", "--run-id", "threshold"]) == 3
    assert "--allow-below-threshold is required" in capsys.readouterr().out
    assert not run_dir(transcripts).parent.exists()


@pytest.mark.parametrize("untagged", [1, 2])
@pytest.mark.parametrize("mode", ["census", "freeze"])
def test_kind_gate(transcripts, monkeypatch, capsys, untagged, mode):
    kinds = {SID: "organic", "research": "research", "harness": "harness"}
    kinds.update({f"untagged-{i}": None for i in range(untagged)})
    for sid in kinds:
        write(transcripts, sid)
    monkeypatch.setattr(session_kind, "tag_kind_of", kinds.get)
    # Isolated state has no sidecar: backfill=True must leave these unresolved.
    assert session_kind.KindIndex(backfill=True).resolve("untagged-0").kind is None
    if mode == "census":
        assert sample.cmd_redo_sample(["--census"]) == 0
    else:
        assert freeze(transcripts, "fixture", "--allow-empty-exclusion") == 0
    lines = capsys.readouterr().out.splitlines()
    gate = next(i for i, line in enumerate(lines) if line.startswith("N_F="))
    assert lines[gate].startswith("N_F=1 vs threshold 19: NO-GO")
    assert lines[gate].endswith(
        f"; sessions: organic=1 untagged={untagged} other_kind=2")
    if untagged == 1:
        reason = "insufficient_flagged_supply"
        assert ("NO-GO (insufficient_flagged_supply) -- next step: detector tune to v3"
                in lines[gate])
        assert WARNING not in lines
    else:
        reason = "tagging_gap"
        assert ("NO-GO (tagging_gap) -- next step: fix session tagging, then re-census; "
                "do not tune the detector on this result" in lines[gate])
        assert "detector tune to v3" not in lines[gate]
        assert lines[gate + 1] == WARNING
    if mode == "freeze":
        frozen = json.loads((run_dir(transcripts) / "frozen.json").read_text())
        assert frozen["no_go"] == reason


@pytest.mark.parametrize("n,untagged,reason", [
    (5, 0, "insufficient_flagged_supply"),
    (5, 1, "insufficient_flagged_supply"),
    (5, 2, "tagging_gap"),
    (19, 2, None),
])
def test_gate(n, untagged, reason):
    line = sample._gate(n, {"organic": 1, "untagged": untagged})
    if reason is None:
        assert line.startswith(f"N_F={n} vs threshold 19: GO; sessions:")
        assert "tagging_gap" not in line
    else:
        assert line.startswith(f"N_F={n} vs threshold 19: NO-GO ({reason}) -- next step:")


@pytest.mark.parametrize("exists", [False, True])
@pytest.mark.parametrize("mode", ["--census", "--freeze"])
def test_no_transcripts(isolated, capsys, exists, mode):
    root = isolated / "private-parent" / "empty-projects"
    if exists:
        root.mkdir(parents=True)
    assert sample.cmd_redo_sample([mode, "--projects-dir", str(root)]) == 4
    captured = capsys.readouterr()
    assert captured.err.strip() == (
        "WARNING: projects dir missing or contains no transcripts: empty-projects; "
        "refusing to report a census")
    assert str(root.parent) not in captured.err
    assert "GO" not in captured.out


@pytest.mark.parametrize("run_id", ["first", "second"])
def test_atomic_write_cleanup_and_retry(transcripts, monkeypatch, run_id):
    original = sample._write_private
    calls = 0

    def fail_second(path, content):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic write failure")
        original(path, content)

    monkeypatch.setattr(sample, "_write_private", fail_second)
    with pytest.raises(OSError, match="synthetic write failure"):
        freeze(transcripts, run_id, "--allow-empty-exclusion")
    run = run_dir(transcripts, run_id)
    assert not run.exists()
    assert not list(run.parent.glob(".tmp-*"))
    monkeypatch.setattr(sample, "_write_private", original)
    assert freeze(transcripts, run_id, "--allow-empty-exclusion") == 0
    assert {p.name for p in run.iterdir()} == {"manifest.jsonl", "frozen.json"}
    assert not list(run.parent.glob(".tmp-*"))


@pytest.mark.parametrize("collision", [False, True])
def test_atomic_rename_cleanup(transcripts, monkeypatch, capsys, collision):
    def fail_rename(source, destination):
        if collision:
            destination.mkdir()
            (destination / "winner").write_text("keep")
        raise OSError("synthetic rename failure")

    monkeypatch.setattr(sample.os, "rename", fail_rename)
    if collision:
        assert freeze(transcripts, "fixture", "--allow-empty-exclusion") == 2
        assert "Refused: run directory already exists." in capsys.readouterr().out
        assert (run_dir(transcripts) / "winner").read_text() == "keep"
    else:
        with pytest.raises(OSError, match="synthetic rename failure"):
            freeze(transcripts, "fixture", "--allow-empty-exclusion")
        assert not run_dir(transcripts).exists()
    assert not list(run_dir(transcripts).parent.glob(".tmp-*"))


def test_cli_determinism(transcripts, monkeypatch, tmp_path):
    results = []
    for name in ("state-a", "state-b"):
        state = tmp_path / name
        monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
        assert freeze(transcripts, "fixed", "--seed", "123", "--allow-empty-exclusion") == 0
        run = state / "redo_validation" / "fixed"
        frozen = json.loads((run / "frozen.json").read_text())
        for key in ("created_utc", "window", "run_dir"):
            frozen.pop(key, None)
        results.append(((run / "manifest.jsonl").read_bytes(), frozen))
    assert results[0][0]
    assert results[0] == results[1]


def test_cli_end_to_end_privacy(transcripts, capsys):
    assert sample.cmd_redo_sample([
        "--census", "--projects-dir", str(transcripts)]) == 0
    census_output = capsys.readouterr()
    assert freeze(transcripts, "privacy", "--allow-empty-exclusion") == 0
    freeze_output = capsys.readouterr()
    contents = [census_output.out, census_output.err, freeze_output.out, freeze_output.err]
    run = run_dir(transcripts, "privacy")
    files = [p for p in run.rglob("*") if p.is_file()]
    assert {p.name for p in files} == {"manifest.jsonl", "frozen.json"}
    contents.extend(Path.read_text(p) for p in files)
    for content in contents:
        assert all(marker not in content for marker in (PROMPT, ANSWER, SID))
