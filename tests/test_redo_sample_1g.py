"""P3.6-c PR1g (D-275): one all-evidence rule for --include-untagged, and refusal on unreadable evidence.

Synthetic transcripts, tag files, overrides, sidecar and ledger rows in an isolated state dir only.
"""
import json
from datetime import datetime, timezone

import pytest

from llm_router import paths, redo_sample as sample, session_kind, session_kind_backfill
from llm_router.proxy import ledger as proxy_ledger

NOW = datetime.now(timezone.utc).timestamp()
SID = "s-probe"


def record(text, ts, role="user"):
    return {"type": role, "timestamp": datetime.fromtimestamp(
        ts, timezone.utc).isoformat(), "message": {"content": text}}


def write(root, sid, n=3):
    project = root / "project"
    project.mkdir(exist_ok=True)
    prompts = ["initial"] + [f"claude: item{i}" for i in range(n)]
    entries = []
    for i, prompt in enumerate(prompts):
        entries.extend([record(prompt, NOW - 1000 + i),
                        record(f"answer{i}", NOW - 999 + i, "assistant")])
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
    monkeypatch.setattr(session_kind, "_OVERRIDES_CACHE", {})
    write(root, SID)
    return root


def set_tag(kind):
    paths.state_path(f"session_kind_{SID}.json").write_text(
        json.dumps({"kind": kind}), encoding="utf-8")


def set_override(kind):
    session_kind.overrides_path().write_text(
        json.dumps({SID: {"kind": kind, "reason": "synthetic"}}), encoding="utf-8")


def set_sidecar(kind):
    session_kind_backfill.sidecar_path().write_text(
        json.dumps({"session_id": SID, "kind": kind}) + "\n", encoding="utf-8")


def ledger(*kinds, ts=None):
    proxy_ledger.ledger_path().write_text("".join(
        json.dumps({"ts": NOW if ts is None else ts, "session_id": SID, "session_kind": k}) + "\n"
        for k in kinds), encoding="utf-8")


def run(root, **kwargs):
    return sample.collect(projects_dir=root, now=NOW, served_loader=lambda sid: [],
                          include_untagged=True, **kwargs)


def outcome(root, **kwargs):
    pool, counts = run(root, **kwargs)
    return pool, counts


def assert_total_identity(counts):
    assert counts["sessions_total"] == (
        sum(counts["sessions_by_kind"].values()) + counts["excluded_by_evidence"])


# (test id, evidence setup, expected sessions_by_kind entry, excluded_by_evidence)
def _setup(**sources):
    def apply():
        if "override" in sources:
            set_override(sources["override"])
        if "tag" in sources:
            set_tag(sources["tag"])
        if "sidecar" in sources:
            set_sidecar(sources["sidecar"])
        if "ledger" in sources:
            ledger(*sources["ledger"])
    return apply


TRUTH = [
    ("ledger_organic_sidecar_research", dict(ledger=["organic"], sidecar="research"),
     None, 1),                                                 # r3 blocker: conflict, excluded
    ("ledger_research_sidecar_research", dict(ledger=["research"], sidecar="research"),
     "research", 0),
    ("tag_organic_ledger_research", dict(tag="organic", ledger=["research"]), None, 1),
    ("override_organic_ledger_research", dict(override="organic", ledger=["research"]),
     "organic", 0),
    ("override_research_all_organic", dict(override="research", tag="organic",
                                          sidecar="organic", ledger=["organic"]),
     "research", 0),
    ("all_organic", dict(tag="organic", sidecar="organic", ledger=["organic", "organic"]),
     "organic", 0),
    ("sidecar_organic_only", dict(sidecar="organic"), "organic", 0),
    ("no_evidence", {}, "untagged", 0),
    ("unknown_is_no_evidence", dict(tag="unknown", sidecar="unknown"), "untagged", 0),
    ("research_harness_conflict", dict(ledger=["research", "harness"]), None, 1),
    ("tag_research_only", dict(tag="research"), "research", 0),
]


@pytest.mark.parametrize("name,sources,kind,excluded", TRUTH, ids=[t[0] for t in TRUTH])
def test_truth_table(isolated, name, sources, kind, excluded):
    _setup(**sources)()
    pool, counts = run(isolated)
    assert counts["excluded_by_evidence"] == excluded
    assert counts["ledger_conflict_excluded"] == excluded
    assert_total_identity(counts)
    if kind is not None:
        assert counts["sessions_by_kind"][kind] == 1
    assert counts["sessions_total"] == 1
    # Only organic and untagged sessions ever reach the pool.
    expected_kinds = {kind} if kind in ("organic", "untagged") else set()
    assert {p.kind for p in pool} == expected_kinds


def test_pure_function_rows():
    f = sample.resolve_with_all_evidence
    R = session_kind.Resolution
    assert f(override="organic", tag=None, sidecar="research", ledger=["research"]) == R(
        "organic", session_kind.SOURCE_OVERRIDE)
    assert f(override=None, tag="organic", sidecar=None, ledger=["research"]) == R(
        None, session_kind.SOURCE_CONFLICT)
    assert f(override=None, tag=None, sidecar="unknown", ledger=[""]) == R(None, None)
    assert f(override=None, tag=None, sidecar="harness", ledger=[]) == R(
        "harness", session_kind.SOURCE_BACKFILL)
    assert f(override=None, tag=None, sidecar=None, ledger=["organic"]) == R(
        "organic", session_kind.SOURCE_LEDGER)


def test_old_ledger_stamp_research_excludes(isolated):
    ledger("research", ts=NOW - 35 * 86400)
    pool, counts = run(isolated, days=30)
    assert counts["sessions_by_kind"]["research"] == 1
    assert pool == []


def test_conflict_is_counted_in_census_output(isolated, capsys):
    ledger("research", "harness")
    assert sample.cmd_redo_sample(["--census", "--include-untagged"]) == 0
    output = capsys.readouterr().out
    assert "excluded_by_evidence: 1" in output
    assert "WARNING: 1 session(s) with conflicting kind stamps were excluded" in output


# ---- refusal matrix -------------------------------------------------------------------------

def snapshot():
    state = paths.state_path()
    return sorted((str(p.relative_to(state)), p.stat().st_size if p.is_file() else -1)
                  for p in state.rglob("*"))


def corrupt_overrides():
    session_kind.overrides_path().write_text("{not json", encoding="utf-8")


def corrupt_tag():
    paths.state_path(f"session_kind_{SID}.json").write_text("{not json", encoding="utf-8")


def nonobject_tag():
    paths.state_path(f"session_kind_{SID}.json").write_text("[]", encoding="utf-8")


def corrupt_sidecar():
    session_kind_backfill.sidecar_path().write_text("{broken\n[]\n", encoding="utf-8")


def unreadable_sidecar():
    session_kind_backfill.sidecar_path().mkdir()


def unreadable_ledger():
    proxy_ledger.ledger_path().mkdir()


def garbage_ledger():
    proxy_ledger.ledger_path().write_text("garbage\n{nope\n\xff\n", encoding="utf-8")


def nonobject_overrides():
    session_kind.overrides_path().write_text("[]", encoding="utf-8")


REFUSALS = [
    ("overrides_corrupt", corrupt_overrides, "overrides=1 tag_files=0 sidecar=0 ledger=0"),
    ("overrides_not_object", nonobject_overrides, "overrides=1 tag_files=0 sidecar=0 ledger=0"),
    ("tag_corrupt", corrupt_tag, "overrides=0 tag_files=1 sidecar=0 ledger=0"),
    ("tag_not_object", nonobject_tag, "overrides=0 tag_files=1 sidecar=0 ledger=0"),
    ("sidecar_corrupt", corrupt_sidecar, "overrides=0 tag_files=0 sidecar=1 ledger=0"),
    ("sidecar_unreadable", unreadable_sidecar, "overrides=0 tag_files=0 sidecar=1 ledger=0"),
    ("ledger_unreadable", unreadable_ledger, "overrides=0 tag_files=0 sidecar=0 ledger=1"),
    ("ledger_all_garbage", garbage_ledger, "overrides=0 tag_files=0 sidecar=0 ledger=1"),
]


@pytest.mark.parametrize("mode", ["--census", "--freeze"])
@pytest.mark.parametrize("name,setup,counts", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_unreadable_evidence_refuses(isolated, capsys, mode, name, setup, counts):
    setup()
    before = snapshot()
    assert sample.cmd_redo_sample([mode, "--include-untagged", "--allow-below-threshold",
                                   "--allow-empty-exclusion"]) == 2
    assert capsys.readouterr().out == (
        f"Refused: 1 unreadable evidence source(s): {counts}\n")
    assert snapshot() == before
    assert not paths.state_path("redo_validation").exists()
    with pytest.raises(sample.EvidenceUnreadable):
        run(isolated)


def test_refusal_counts_every_bad_tag_file(isolated, capsys):
    for sid in ("s-b", "s-c"):
        write(isolated, sid)
        paths.state_path(f"session_kind_{sid}.json").write_text("{x", encoding="utf-8")
    corrupt_overrides()
    assert sample.cmd_redo_sample(["--census", "--include-untagged"]) == 2
    assert capsys.readouterr().out == (
        "Refused: 3 unreadable evidence source(s): overrides=1 tag_files=2 sidecar=0 ledger=0\n")


def test_missing_sources_are_no_evidence(isolated, capsys):
    # no overrides file, no tag file, no sidecar: allowed; a missing ledger only warns
    assert not session_kind.overrides_path().exists()
    assert sample.cmd_redo_sample(["--census", "--include-untagged"]) == 0
    output = capsys.readouterr().out
    assert "untagged" in output and sample.LEDGER_MISSING_WARNING in output
    ledger("organic")
    _, counts = run(isolated)
    assert counts["sessions_by_kind"]["organic"] == 1


def test_partly_garbage_ledger_is_not_corrupt(isolated):
    proxy_ledger.ledger_path().write_text(
        "garbage\n" + json.dumps({"session_id": SID, "session_kind": "organic"}) + "\n",
        encoding="utf-8")
    _, counts = run(isolated)
    assert counts["proxy_ledger"] == "present"
    assert counts["sessions_by_kind"]["organic"] == 1


# ---- flag off: other callers unchanged -------------------------------------------------------

def test_flag_off_keeps_tag_wins_and_session_kind_resolve(isolated, capsys):
    set_tag("organic")
    ledger("organic", "research")
    corrupt_overrides()    # the lenient reader treats it as no overrides, as before
    pool, counts = sample.collect(projects_dir=isolated, now=NOW, served_loader=lambda sid: [])
    assert counts["sessions_by_kind"] == {"organic": 1, "untagged": 0}
    assert {(p.kind, p.kind_source) for p in pool} == {("organic", session_kind.SOURCE_TAG)}
    assert "excluded_by_evidence" not in counts and "proxy_ledger" not in counts
    assert sample.cmd_redo_sample(["--census"]) == 0
    assert "excluded_by_evidence" not in capsys.readouterr().out
    # KindIndex.resolve (every other caller) still lets a live tag win over the ledger.
    index = session_kind.KindIndex([{"session_id": SID, "session_kind": "research"}],
                                   backfill=False)
    assert index.resolve(SID) == session_kind.Resolution(
        "organic", session_kind.SOURCE_TAG, ledger_disagrees=True)
