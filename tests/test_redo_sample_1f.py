"""P3.6-c PR1f: untagged causes, ledger resolution, exclusion validation.

Synthetic transcripts, tag files, sidecar and ledger rows in an isolated state dir only.
"""
import json
import os
from datetime import datetime, timezone

import pytest

from llm_router import paths, redo_sample as sample, session_kind, session_kind_backfill
from llm_router.proxy import ledger as proxy_ledger

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
    tag("s-corrupt", json.dumps({"kind": ""}))   # parses, no usable kind: no evidence
    tag("s-badkind", json.dumps({"kind": "bogus"}))
    rows = [{"session_id": "s-research", "session_kind": "research"},
            {"session_id": "s-organic", "session_kind": "organic"},
            {"session_id": "s-conflict", "session_kind": "research"},
            {"session_id": "s-conflict", "session_kind": "harness"},
            {"session_id": "s-none", "session_kind": None}]
    pool, counts = collect(isolated, include_untagged=True, ledger_rows=rows)
    assert counts["sessions_by_kind"]["research"] == 1
    assert counts["sessions_by_kind"]["organic"] == 1
    # s-conflict carries research AND harness stamps: excluded, never drawn as untagged.
    assert counts["sessions_by_kind"]["untagged"] == 3
    assert counts["ledger_conflict_excluded"] == 1
    assert counts["sessions_total"] == 6
    assert "s-conflict" not in {p.session_id for p in pool}
    assert {p.kind for p in pool} == {"organic", "untagged"}
    assert {p.kind_source for p in pool if p.kind == "organic"} == {
        session_kind.SOURCE_LEDGER}
    assert counts["proxy_ledger"] == "present"
    assert counts["proxy_ledger_rows"] == 5
    assert counts["backfill_sidecar"] == "missing"
    assert counts["untagged_causes"] == {
        "ledger": {"no_ledger_stamp": 3},
        "sidecar": {"sidecar_corrupt": 0, "sidecar_missing": 3, "sidecar_no_entry": 0,
                    "sidecar_unknown": 0, "sidecar_unreadable": 0},
        "tag": {"corrupt_tag": 2, "no_tag_file": 1},
    }
    facet_sums(counts)
    # Default (organic only): the ledger is not read, so its stamps do not resolve anything.
    pool, default = collect(isolated, ledger_rows=rows)
    assert default["sessions_by_kind"]["untagged"] == 6
    assert set(default) == FLAG_OFF_KEYS
    assert pool == []


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
        "sidecar_corrupt": 0, "sidecar_missing": 0, "sidecar_no_entry": 1,
        "sidecar_unknown": 1, "sidecar_unreadable": 0}
    facet_sums(counts)
    target = session_kind_backfill.sidecar_path()
    target.unlink()
    target.mkdir()          # a directory cannot be read as a file: unreadable, not empty
    assert session_kind_backfill.sidecar_status() == "unreadable"
    with pytest.raises(sample.EvidenceUnreadable):   # D-275: unreadable evidence refuses
        collect(isolated, include_untagged=True, ledger_rows=[])
    assert sample.cmd_redo_sample(["--census", "--include-untagged"]) == 2
    output = capsys.readouterr().out
    assert output == ("Refused: 1 unreadable evidence source(s): "
                      "overrides=0 tag_files=0 sidecar=1 ledger=0\n")


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
    assert output == "Refused: local exclusion file has 2 invalid line(s)\n"
    assert secret not in output and str(listing) not in output


def test_invalid_shipped_exclusion_refuses(isolated, tmp_path, monkeypatch, capsys):
    write(isolated, "s-organic")
    shipped = tmp_path / "synthetic-shipped.txt"
    shipped.write_text("# synthetic\nNOTAHASH\n", encoding="utf-8")
    original = sample.load_excluded

    def load(extra=None, source=None):
        return original(extra, source=shipped if source is None else source)

    monkeypatch.setattr(sample, "load_excluded", load)
    assert sample.cmd_redo_sample(["--census"]) == 2
    output = capsys.readouterr().out
    assert output == "Refused: shipped exclusion file has 1 invalid line(s)\n"
    assert str(shipped) not in output


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


FLAG_OFF_KEYS = {
    "sessions_total", "organic_pairs_eligible", "no_ts", "excluded_m09", "flagged_ineligible",
    "exclusion_list_size", "exclusion_list_sha256", "exclusion_source", "sessions_by_kind",
    "eligible_after_exclusion", "N_F", "N_S", "N_R", "flagged_per_signal", "cheap_served",
    "n_sessions", "max_pairs_per_session", "largest_session_share", "median_pairs_per_session",
    "n_sessions_F", "largest_session_share_F"}
UNTAGGED_ONLY = ("backfill_sidecar", "untagged_causes", "proxy_ledger", "ledger_conflict",
                 "include_untagged", "by_kind")


def ledger_file(rows):
    proxy_ledger.ledger_path().write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_flag_off_census_keys_match_base(isolated, capsys):
    write(isolated, "s-plain")
    sidecar([{"session_id": "s-plain", "kind": "unknown"}])
    ledger_file([{"ts": NOW, "session_id": "s-plain", "session_kind": "research"}])
    _, counts = collect(isolated)
    assert set(counts) == FLAG_OFF_KEYS
    assert counts["sessions_by_kind"] == {"organic": 0, "untagged": 1}  # the ledger is not read
    assert sample.cmd_redo_sample(["--census"]) == 0
    output = capsys.readouterr().out
    assert sample.LEDGER_MISSING_WARNING not in output
    printed = {line.split(":")[0] for line in output.splitlines()}
    assert FLAG_OFF_KEYS <= printed
    assert not any(line.startswith(UNTAGGED_ONLY) for line in printed)


def test_conflict_with_sidecar_kind_stays_excluded(isolated):
    write(isolated, "s-conflict")
    write(isolated, "s-plain")
    sidecar([{"session_id": "s-conflict", "kind": "research"}])
    rows = [{"session_id": "s-conflict", "session_kind": "organic"},
            {"session_id": "s-conflict", "session_kind": "headless"}]
    pool, counts = collect(isolated, include_untagged=True, ledger_rows=rows)
    assert counts["ledger_conflict_excluded"] == 1
    assert {p.session_id for p in pool} == {"s-plain"}
    assert counts["sessions_by_kind"] == {"organic": 0, "untagged": 1}
    assert counts["untagged_causes"]["sidecar"]["sidecar_no_entry"] == 1
    facet_sums(counts)


def test_conflict_including_organic_is_neither_organic_nor_untagged(isolated):
    write(isolated, "s-conflict")
    write(isolated, "s-plain")
    rows = [{"session_id": "s-conflict", "session_kind": "organic"},
            {"session_id": "s-conflict", "session_kind": "research"}]
    pool, counts = collect(isolated, include_untagged=True, ledger_rows=rows)
    assert counts["ledger_conflict_excluded"] == 1
    assert "s-conflict" not in {p.session_id for p in pool}
    assert counts["sessions_by_kind"] == {"organic": 0, "untagged": 1}
    assert counts["by_kind"]["organic"]["eligible"] == 0


def test_a_tag_file_no_longer_beats_a_ledger_conflict(isolated):
    # D-275 changed this: the old outcome (tag wins, session organic) is gone.
    write(isolated, "s-tagged")
    tag("s-tagged", json.dumps({"kind": "organic"}))
    rows = [{"session_id": "s-tagged", "session_kind": "organic"},
            {"session_id": "s-tagged", "session_kind": "research"}]
    pool, counts = collect(isolated, include_untagged=True, ledger_rows=rows)
    assert counts["ledger_conflict_excluded"] == 1
    assert pool == []


@pytest.mark.parametrize("mode", ["--census", "--freeze"])
def test_conflict_warning_is_printed_and_frozen(isolated, capsys, mode):
    for i in range(2):
        write(isolated, f"s-conflict-{i}")
    write(isolated, "s-plain")
    ledger_file([{"ts": NOW, "session_id": f"s-conflict-{i}", "session_kind": k}
                 for i in range(2) for k in ("research", "harness")])
    args = [mode, "--include-untagged", "--allow-below-threshold", "--allow-empty-exclusion"]
    assert sample.cmd_redo_sample(args if mode == "--freeze" else args[:2]) == 0
    output = capsys.readouterr().out
    assert sample.LEDGER_MISSING_WARNING not in output
    assert "WARNING: 2 session(s) with conflicting kind stamps were excluded" in output
    assert "s-conflict" not in output and SENTINEL not in output
    if mode == "--census":
        assert "ledger_conflict_excluded: 2" in output
        assert "proxy_ledger: present" in output
    else:
        run_dir = next(paths.state_path("redo_validation").iterdir())
        frozen = json.loads((run_dir / "frozen.json").read_text(encoding="utf-8"))
        assert frozen["ledger_conflict_excluded"] == 2
        assert frozen["counts"]["ledger_conflict_excluded"] == 2


@pytest.mark.parametrize("mode", ["--census", "--freeze"])
def test_missing_ledger_is_allowed_and_reported(isolated, capsys, mode):
    write(isolated, "s-plain")
    _, counts = collect(isolated, include_untagged=True)
    assert (counts["proxy_ledger"], counts["proxy_ledger_rows"]) == ("missing", 0)
    assert sample.cmd_redo_sample(["--census", "--include-untagged"]) == 0
    output = capsys.readouterr().out
    assert "proxy_ledger: missing" in output
    assert sample.LEDGER_MISSING_WARNING in output
    if mode == "--freeze":
        args = [mode, "--include-untagged", "--allow-below-threshold", "--allow-empty-exclusion"]
        assert sample.cmd_redo_sample(args) == 0
        assert sample.LEDGER_MISSING_WARNING in capsys.readouterr().out
    assert sample.cmd_redo_sample(["--census"]) == 0
    assert sample.LEDGER_MISSING_WARNING not in capsys.readouterr().out


def make_unreadable(shape):
    target = proxy_ledger.ledger_path()
    if shape == "directory":
        target.mkdir()
    else:
        ledger_file([{"ts": NOW, "session_id": "s-plain", "session_kind": "research"}])
        target.chmod(0o000)
    return target


@pytest.mark.parametrize("shape", ["directory", "mode-000"])
@pytest.mark.parametrize("mode", ["--census", "--freeze"])
def test_unreadable_ledger_refuses_untagged(isolated, capsys, shape, mode):
    if shape == "mode-000" and os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    write(isolated, "s-plain")
    target = make_unreadable(shape)
    try:
        with pytest.raises(sample.EvidenceUnreadable):
            collect(isolated, include_untagged=True)
        assert sample.cmd_redo_sample([mode, "--include-untagged"]) == 2
        assert capsys.readouterr().out == (
            "Refused: 1 unreadable evidence source(s): "
            "overrides=0 tag_files=0 sidecar=0 ledger=1\n")
        assert not paths.state_path("redo_validation").exists()
        # With the flag off the ledger is never read: the run proceeds as before.
        assert sample.cmd_redo_sample(["--census"]) == 0
        output = capsys.readouterr().out
        assert "proxy_ledger" not in output and str(target) not in output
    finally:
        if shape == "mode-000":
            target.chmod(0o600)


def test_ledger_read_has_no_time_cutoff(isolated):
    for sid in ("s-old", "s-recent", "s-no-ts", "s-edge"):
        write(isolated, sid)
    since = NOW - 30 * 86400
    ledger_file([
        {"ts": since - 10 * 86400, "session_id": "s-old", "session_kind": "research"},
        {"ts": since - 1 * 86400, "session_id": "s-edge", "session_kind": "headless"},
        {"ts": NOW - 50, "session_id": "s-recent", "session_kind": "harness"},
        {"session_id": "s-no-ts", "session_kind": "research"},
        "not a row", {"ts": True, "session_id": "s-old", "session_kind": None}])
    pool, counts = collect(isolated, include_untagged=True)
    assert counts["proxy_ledger_rows"] == 5    # every dict row, including ts: True
    assert counts["sessions_by_kind"] == {
        "harness": 1, "headless": 1, "organic": 0, "research": 2, "untagged": 0}
    # A research stamp older than the window still resolves the session.
    assert pool == []


def test_stamp_older_than_window_keeps_session_out_of_untagged(isolated, capsys):
    write(isolated, "s-probe")
    ledger_file([{"ts": NOW - 35 * 86400, "session_id": "s-probe", "session_kind": "research"}])
    pool, counts = collect(isolated, include_untagged=True, days=30)
    assert counts["sessions_by_kind"]["research"] == 1
    assert counts["sessions_by_kind"]["untagged"] == 0
    assert "s-probe" not in {p.session_id for p in pool}


def test_default_path_streams_without_read_rows(isolated, monkeypatch):
    write(isolated, "s-headless")
    ledger_file([{"ts": NOW, "session_id": "s-headless", "session_kind": "headless"}])

    def refuse(*args, **kwargs):
        raise AssertionError("read_rows builds the whole row list")

    monkeypatch.setattr(proxy_ledger, "read_rows", refuse)
    _, counts = collect(isolated, include_untagged=True)
    assert counts["sessions_by_kind"]["headless"] == 1


def test_sidecar_is_read_once_per_run(isolated, monkeypatch):
    for sid in ("s-a", "s-b", "s-c"):
        write(isolated, sid)
    sidecar([{"session_id": "s-a", "kind": "harness"}])
    calls = []
    real = session_kind_backfill.read_sidecar

    def counting(path=None):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(session_kind_backfill, "read_sidecar", counting)
    monkeypatch.setattr(session_kind_backfill, "load_sidecar",
                        lambda path=None: pytest.fail("second sidecar read"))
    _, counts = collect(isolated, include_untagged=True, ledger_rows=[])
    assert len(calls) == 1
    assert counts["sessions_by_kind"]["harness"] == 1


def test_all_corrupt_sidecar_is_reported_corrupt(isolated, capsys):
    write(isolated, "s-a")
    write(isolated, "s-b")
    session_kind_backfill.sidecar_path().write_text(
        "{broken\n[]\n" + json.dumps({"session_id": "", "kind": "harness"}) + "\n",
        encoding="utf-8")
    assert session_kind_backfill.read_sidecar() == ("corrupt", {})
    with pytest.raises(sample.EvidenceUnreadable):
        collect(isolated, include_untagged=True, ledger_rows=[])
    assert sample.cmd_redo_sample(["--census", "--include-untagged"]) == 2
    assert "sidecar=1" in capsys.readouterr().out


def test_bom_on_the_first_line_is_stripped(isolated, tmp_path, capsys):
    write(isolated, "s-organic")
    listing = tmp_path / "bom.txt"
    listing.write_bytes(b"\xef\xbb\xbf" + VALID.encode() + b"\n")
    listing.chmod(0o600)
    assert sample.load_excluded(source=listing) == {VALID}
    assert sample.cmd_redo_sample(["--census", "--exclusion-file", str(listing)]) == 0
    assert "exclusion_source: local-file" in capsys.readouterr().out
