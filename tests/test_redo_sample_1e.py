"""D-266: synthetic transcript shapes and isolated state only."""
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

import pytest

from llm_router import offload_share, paths, redo_sample as sample, session_kind

NOW = datetime.now(timezone.utc).timestamp()
SENTINEL = "ZQXSENTINEL"


def record(text, ts=NOW - 100, role="user"):
    return {"type": role, "timestamp": datetime.fromtimestamp(
        ts, timezone.utc).isoformat(), "message": {"content": text}}


def write(root, sid, n=10):
    project = root / "project"
    project.mkdir(exist_ok=True)
    prompts = [f"{SENTINEL}initial"] + [
        f"claude: {SENTINEL}item{i}" for i in range(n)]
    entries = []
    for i, prompt in enumerate(prompts):
        entries.extend([record(prompt, NOW - 1000 + i),
                        record(f"{SENTINEL}answer{i}", NOW - 999 + i, "assistant")])
    (project / f"{sid}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in entries), encoding="utf-8")


def resolve(sid):
    kind = sid.split("-")[0]
    return session_kind.Resolution(None if kind == "missing" else kind, "tag")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(root))
    monkeypatch.setattr(session_kind, "override_of", lambda sid: None)
    monkeypatch.setattr(session_kind, "tag_kind_of", lambda sid: resolve(sid).kind)
    monkeypatch.setattr(session_kind, "tag_evidence",     # D-275 path reads tags through this
                        lambda sid: ("present", resolve(sid).kind))
    return root


def collect(root, **kwargs):
    return sample.collect(projects_dir=root, now=NOW, resolve=resolve,
                          served_loader=lambda sid: [], **kwargs)


def test_census_split_and_default(isolated):
    for kind in ("organic", "missing", "untagged", "research", "headless", "harness", "exec"):
        write(isolated, f"{kind}-private-session")
    excluded = {sample.pair_key("missing-private-session", 1)}
    default_pool, default = collect(isolated, excluded=excluded)
    pool, counts = collect(isolated, excluded=excluded, include_untagged=True)
    assert {p.kind for p in default_pool} == {"organic"}
    assert "by_kind" not in default and "include_untagged" not in default
    assert counts["include_untagged"] is True
    assert counts["organic_pairs_eligible"] == default["organic_pairs_eligible"] == 10
    assert counts["N_F"] == counts["eligible_after_exclusion"] == 29
    assert counts["excluded_m09"] == 1
    assert {p.kind for p in pool} == {"organic", "untagged"}
    assert counts["exclusion_source"] == default["exclusion_source"] == "shipped"
    keys = list(default)
    assert keys[keys.index("exclusion_list_sha256") + 1] == "exclusion_source"
    for kind, eligible, remaining, sessions, maximum in (
            ("organic", 10, 10, 1, 10), ("untagged", 20, 19, 2, 10)):
        sub = counts["by_kind"][kind]
        assert sub["eligible"] == eligible
        assert sub["eligible_after_exclusion"] == sub["N_F"] == remaining
        assert sub["N_S"] == sub["N_R"] == 0
        assert sub["flagged_per_signal"] == {
            "reask": remaining, "contradiction": 0, "repeat": 0}
        assert sub["cheap_served"] is None
        assert sub["n_sessions"] == sub["n_sessions_F"] == sessions
        assert sub["max_pairs_per_session"] == maximum
        assert sub["median_pairs_per_session"] == remaining / sessions
        assert sub["largest_session_share"] == maximum / remaining
        assert sub["largest_session_share_F"] == maximum / remaining


def test_measured_and_failed_ledger_preserve_kind(isolated):
    for kind in ("organic", "missing"):
        write(isolated, f"{kind}-private-session", n=1)
    cheap = sorted(offload_share.OFFLOADED_CLASSES)[0]
    pool, counts = sample.collect(
        projects_dir=isolated, now=NOW, resolve=resolve, include_untagged=True,
        served_loader=lambda sid: [(NOW - 2000, cheap)])
    assert counts["cheap_served"] == 2
    assert all(sub["cheap_served"] == 1 for sub in counts["by_kind"].values())
    assert all(p.prev_served_class == cheap for p in pool)

    def broken(sid):
        if sid.startswith("missing"):
            raise RuntimeError("synthetic ledger failure")
        return [(NOW - 2000, cheap)]

    pool, counts = sample.collect(
        projects_dir=isolated, now=NOW, resolve=resolve, include_untagged=True,
        served_loader=broken)
    assert {p.kind for p in pool} == {"organic", "untagged"}
    assert all(p.prev_served_class is None for p in pool)
    assert counts["cheap_served"] is None
    assert all(sub["cheap_served"] is None for sub in counts["by_kind"].values())


def test_combined_gate_and_tagging_gap(isolated):
    write(isolated, "organic-private-session")
    write(isolated, "missing-private-session")
    _, default = collect(isolated)
    _, combined = collect(isolated, include_untagged=True)
    kinds = combined["sessions_by_kind"]
    assert default["N_F"] == 10 and combined["N_F"] == 20
    assert "NO-GO" in sample._gate(default["N_F"], kinds)
    assert ": GO;" in sample._gate(combined["N_F"], kinds, True)
    majority = {"organic": 1, "untagged": 3}
    assert sample._no_go_reason(10, majority) == "tagging_gap"
    assert any("WARNING" in line for line in sample._gate_lines(10, majority))
    assert sample._no_go_reason(18, majority, True) == "insufficient_flagged_supply"
    assert sample._no_go_reason(19, majority, True) is None
    lines = sample._gate_lines(18, majority, True)
    assert len(lines) == 1 and "include_untagged=on" in lines[0]
    assert "tagging_gap" not in lines[0]


def test_freeze_kinds_and_no_text(isolated, capsys):
    sessions = ("organic-private-session", "missing-private-session", "research-private-session")
    for sid in sessions:
        write(isolated, sid)
    assert sample.cmd_redo_sample([
        "--freeze", "--run-id", "mixed", "--include-untagged",
        "--allow-empty-exclusion"]) == 0
    output = capsys.readouterr().out
    assert ": GO;" in output and "include_untagged=on" in output
    run = paths.state_path("redo_validation", "mixed")
    frozen = json.loads((run / "frozen.json").read_text())
    rows = [json.loads(line) for line in (run / "manifest.jsonl").read_text().splitlines()]
    assert Counter(row["kind"] for row in rows) == {"organic": 10, "untagged": 10}
    assert frozen["include_untagged"] is True
    assert frozen["exclusion_source"] == "shipped"
    assert frozen["no_go"] is None
    assert frozen["sizes_by_kind"] == frozen["counts"]["by_kind"]
    for h in ("F", "S", "R"):
        n = 10 if h == "F" else 0
        assert frozen["strata"][h]["by_kind"] == {
            kind: {"N": n, "n": n} for kind in ("organic", "untagged")}
    contents = [output] + [p.read_text() for p in run.rglob("*") if p.is_file()]
    for text in contents:
        assert SENTINEL not in text
        assert all(sid not in text for sid in sessions)


@pytest.mark.parametrize("home_override", [True, False])
def test_local_default_exclusion(isolated, monkeypatch, capsys, home_override):
    if not home_override:
        monkeypatch.delenv("LLM_ROUTER_HOME")
    sid = "organic-private-session"
    write(isolated, sid, n=2)
    listing = paths.state_path("redo_m09_excluded.txt")
    listing.parent.mkdir(parents=True)
    key = sample.pair_key(sid, 1)
    listing.write_text("# local hashes only\n" + key + "\n", encoding="utf-8")
    listing.chmod(0o600)
    before = listing.read_bytes()
    assert sample.cmd_redo_sample(["--census"]) == 0
    output = capsys.readouterr().out
    assert "excluded_m09: 1" in output and "N_F: 1" in output
    assert "exclusion_list_size: 1" in output
    assert "exclusion_list_sha256: " + hashlib.sha256(key.encode()).hexdigest() in output
    assert "exclusion_source: local-file" in output
    assert listing.read_bytes() == before


def test_explicit_exclusion_and_union(isolated, tmp_path, monkeypatch, capsys):
    sid = "organic-private-session"
    write(isolated, sid, n=3)
    keys = [sample.pair_key(sid, k) for k in (1, 2)]
    local = tmp_path / "local.txt"
    local.write_text(keys[0] + "\n", encoding="utf-8")
    assert sample.cmd_redo_sample(["--census", "--exclusion-file", str(local)]) == 0
    assert "exclusion_source: local-file" in capsys.readouterr().out
    shipped = tmp_path / "synthetic-shipped.txt"
    shipped.write_text("# synthetic shipped-source fixture\n" + keys[1] + "\n")
    original = sample.load_excluded
    assert original(source=shipped) == {keys[1]}

    def load(extra=None, source=None):
        return original(extra, source=shipped if source is None else source)

    monkeypatch.setattr(sample, "load_excluded", load)
    pool, counts = collect(isolated, excluded=original(source=local),
                           exclusion_source="local-file")
    assert len(pool) == 1 and counts["excluded_m09"] == 2
    assert counts["exclusion_list_size"] == 2
    assert counts["exclusion_list_sha256"] == hashlib.sha256(
        "\n".join(sorted(keys)).encode()).hexdigest()
    assert counts["exclusion_source"] == "local-file"


@pytest.mark.parametrize("shape", ["missing", "directory", "invalid-utf8"])
def test_bad_explicit_exclusion_precedes_window_and_root(tmp_path, capsys, shape):
    listing = tmp_path / "excluded"
    if shape == "directory":
        listing.mkdir()
    elif shape == "invalid-utf8":
        listing.write_bytes(b"\xff")
    assert sample.cmd_redo_sample([
        "--census", "--exclusion-file", str(listing), "--days", "-1",
        "--projects-dir", str(tmp_path / "missing-projects")]) == 2
    assert capsys.readouterr().out == "Refused: local exclusion file not found or unreadable.\n"
