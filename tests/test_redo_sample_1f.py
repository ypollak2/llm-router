"""P3.6-c PR1f: untagged causes, ledger resolution, exclusion validation.

Synthetic transcripts, tag files, sidecar and ledger rows in an isolated state dir only.
"""
import json
from datetime import datetime, timezone

import pytest

from llm_router import paths, redo_sample as sample, session_kind, session_kind_backfill

NOW = datetime.now(timezone.utc).timestamp()
SENTINEL = "ZQXSENTINEL"


def record(text, ts, role="user"):
    return {"type": role, "timestamp": datetime.fromtimestamp(
        ts, timezone.utc).isoformat(), "message": {"content": text}}


def write(root, sid, n=3):
    project = root / "project"
    project.mkdir(exist_ok=True)
    prompts = [f"{SENTINEL}initial"] + [f"claude: {SENTINEL}item{i}" for i in range(n)]
    entries = []
    for i, prompt in enumerate(prompts):
        entries.extend([record(prompt, NOW - 1000 + i),
                        record(f"{SENTINEL}answer{i}", NOW - 999 + i, "assistant")])
    (project / f"{sid}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in entries), encoding="utf-8")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    paths.state_path().mkdir(parents=True)
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(root))
    monkeypatch.setattr(session_kind, "_FOUND", {})
    return root


def tag(sid, content):
    paths.state_path(f"session_kind_{sid}.json").write_text(content, encoding="utf-8")


def sidecar(rows):
    session_kind_backfill.sidecar_path().write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def collect(root, **kwargs):
    return sample.collect(projects_dir=root, now=NOW, served_loader=lambda sid: [], **kwargs)


def facet_sums(counts):
    untagged = counts["sessions_by_kind"]["untagged"]
    assert untagged > 0
    for facet, causes in counts["untagged_causes"].items():
        assert sum(causes.values()) == untagged, facet


def test_ledger_stamps_resolve_untagged_and_causes(isolated):
    for sid in ("s-research", "s-organic", "s-conflict", "s-none", "s-corrupt", "s-badkind"):
        write(isolated, sid)
    tag("s-corrupt", "{not json")
    tag("s-badkind", json.dumps({"kind": "bogus"}))
    rows = [{"session_id": "s-research", "session_kind": "research"},
            {"session_id": "s-organic", "session_kind": "organic"},
            {"session_id": "s-conflict", "session_kind": "research"},
            {"session_id": "s-conflict", "session_kind": "harness"},
            {"session_id": "s-none", "session_kind": None}]
    pool, counts = collect(isolated, include_untagged=True, ledger_rows=rows)
    assert counts["sessions_by_kind"]["research"] == 1
    assert counts["sessions_by_kind"]["organic"] == 1
    assert counts["sessions_by_kind"]["untagged"] == 4
    assert {p.kind for p in pool} == {"organic", "untagged"}
    assert {p.kind_source for p in pool if p.kind == "organic"} == {
        session_kind.SOURCE_LEDGER}
    assert counts["proxy_ledger_rows"] == 5
    assert counts["backfill_sidecar"] == "missing"
    assert counts["untagged_causes"] == {
        "ledger": {"ledger_conflict": 1, "no_ledger_stamp": 3},
        "sidecar": {"sidecar_missing": 4, "sidecar_no_entry": 0,
                    "sidecar_unknown": 0, "sidecar_unreadable": 0},
        "tag": {"corrupt_tag": 2, "no_tag_file": 2},
    }
    facet_sums(counts)
    # Default (organic only): the ledger is not read, so its stamps do not resolve anything.
    _, default = collect(isolated, ledger_rows=rows)
    assert default["sessions_by_kind"]["untagged"] == 6
    assert "proxy_ledger_rows" not in default
    assert default["untagged_causes"]["ledger"] == {"ledger_conflict": 0, "ledger_not_read": 6}
    facet_sums(default)


def test_default_ledger_reader_is_the_proxy_ledger(isolated):
    write(isolated, "s-headless")
    write(isolated, "s-plain")
    paths.state_path("proxy_calls.jsonl").write_text(
        json.dumps({"session_id": "s-headless", "session_kind": "headless"}) + "\n",
        encoding="utf-8")
    _, counts = collect(isolated, include_untagged=True)
    assert counts["sessions_by_kind"]["headless"] == 1
    assert counts["sessions_by_kind"]["untagged"] == 1
    assert counts["proxy_ledger_rows"] == 1


def test_sidecar_entries_and_states(isolated, capsys):
    for sid in ("s-backfilled", "s-unknown", "s-absent"):
        write(isolated, sid)
    sidecar([{"session_id": "s-backfilled", "kind": "harness"},
             {"session_id": "s-unknown", "kind": "unknown"}])
    _, counts = collect(isolated, include_untagged=True, ledger_rows=[])
    assert counts["backfill_sidecar"] == "present"
    assert counts["sessions_by_kind"]["harness"] == 1
    assert counts["untagged_causes"]["sidecar"] == {
        "sidecar_missing": 0, "sidecar_no_entry": 1,
        "sidecar_unknown": 1, "sidecar_unreadable": 0}
    facet_sums(counts)
    target = session_kind_backfill.sidecar_path()
    target.unlink()
    target.mkdir()          # a directory cannot be read as a file: unreadable, not empty
    assert session_kind_backfill.sidecar_status() == "unreadable"
    _, counts = collect(isolated, include_untagged=True, ledger_rows=[])
    assert counts["backfill_sidecar"] == "unreadable"
    assert counts["untagged_causes"]["sidecar"]["sidecar_unreadable"] == 3
    assert sample.cmd_redo_sample(["--census", "--include-untagged"]) == 0
    output = capsys.readouterr().out
    assert "backfill_sidecar: unreadable" in output
    assert "WARNING: backfill sidecar unreadable" in output
    assert "untagged_causes: " in output
    assert SENTINEL not in output and "s-unknown" not in output
    assert str(target) not in output


def test_tag_file_state():
    assert session_kind.TAG_STATE_MISSING == "no_tag_file"
    assert session_kind.TAG_STATE_CORRUPT == "corrupt_tag"


def test_tag_file_state_values(isolated):
    assert session_kind.tag_file_state("s-x") == "no_tag_file"
    tag("s-x", json.dumps({"kind": "organic"}))
    assert session_kind.tag_file_state("s-x") == "valid_tag"
    tag("s-x", "[]")
    assert session_kind.tag_file_state("s-x") == "corrupt_tag"


VALID = "0123456789abcdef"


@pytest.mark.parametrize("lines,invalid", [
    ([VALID], 0),
    (["# comment", "", VALID, "0" * 64], 1),
    ([VALID.upper(), "0" * 64, "xyz", VALID], 3),
])
def test_load_excluded_validates(tmp_path, lines, invalid):
    listing = tmp_path / "excluded.txt"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if invalid:
        with pytest.raises(sample.InvalidExclusionFile) as info:
            sample.load_excluded(source=listing)
        assert info.value.count == invalid
    else:
        assert sample.load_excluded(source=listing) == {VALID}


def test_invalid_local_exclusion_refuses_with_count_only(isolated, tmp_path, capsys):
    write(isolated, "s-organic")
    secret = "F" * 16
    listing = tmp_path / "local.txt"
    listing.write_text(f"{VALID}\n{secret}\n{'a' * 64}\n", encoding="utf-8")
    listing.chmod(0o600)
    assert sample.cmd_redo_sample(["--census", "--exclusion-file", str(listing)]) == 2
    output = capsys.readouterr().out
    assert output == "Refused: exclusion file has 2 invalid line(s)\n"
    assert secret not in output


def test_invalid_shipped_exclusion_refuses(isolated, tmp_path, monkeypatch, capsys):
    write(isolated, "s-organic")
    shipped = tmp_path / "synthetic-shipped.txt"
    shipped.write_text("# synthetic\nNOTAHASH\n", encoding="utf-8")
    original = sample.load_excluded

    def load(extra=None, source=None):
        return original(extra, source=shipped if source is None else source)

    monkeypatch.setattr(sample, "load_excluded", load)
    assert sample.cmd_redo_sample(["--census"]) == 2
    assert capsys.readouterr().out == "Refused: exclusion file has 1 invalid line(s)\n"


@pytest.mark.parametrize("mode,warned", [(0o600, False), (0o640, True), (0o604, True)])
def test_local_exclusion_mode_warning(isolated, tmp_path, capsys, mode, warned):
    write(isolated, "s-organic")
    listing = tmp_path / "local-private.txt"
    listing.write_text(VALID + "\n", encoding="utf-8")
    listing.chmod(mode)
    assert sample.cmd_redo_sample(["--census", "--exclusion-file", str(listing)]) == 0
    output = capsys.readouterr().out
    assert ("group- or world-readable" in output) is warned
    assert str(listing) not in output and "local-private" not in output


def test_combined_weight_labels(isolated):
    for i in range(20):
        write(isolated, f"s-organic-{i}")
    rows = [{"session_id": f"s-organic-{i}", "session_kind": "organic"} for i in range(20)]
    pool, _ = collect(isolated, include_untagged=True, ledger_rows=rows)
    drawn = sample.draw(pool, 360, "salt")
    assert drawn and all("weight_N_h_combined" in r and "n_h_combined" in r for r in drawn)
    assert not any("weight_N_h" in r or "n_h" in r for r in drawn)
