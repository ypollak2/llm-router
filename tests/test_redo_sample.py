"""Synthetic transcript shapes only; no live state, network or timing assertions."""
from __future__ import annotations

import hashlib
import json
import stat
from collections import Counter
from datetime import datetime, timezone

import pytest

from llm_router import cli, offload_share, redo_sample as sample, session_kind

NOW = 1_800_000_000.0
SENTINEL = "ZQXSENTINEL"


def record(text, ts=NOW - 100, role="user"):
    return {"type": role, "timestamp": datetime.fromtimestamp(
        ts, timezone.utc).isoformat(), "message": {"content": text}}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(root))
    return root


def write(root, sid, prompts):
    project = root / "project"
    project.mkdir(exist_ok=True)
    entries = []
    for i, prompt in enumerate(prompts):
        entries.extend([record(prompt, NOW - 1000 + i),
                        record(f"{SENTINEL}answer{i}", NOW - 999 + i, "assistant")])
    path = project / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in entries))
    return path


def organic(sid):
    return session_kind.Resolution("organic", "tag")


def collect(root, **kwargs):
    return sample.collect(projects_dir=root, now=NOW, resolve=organic,
                          served_loader=lambda sid: [], **kwargs)


def candidate(sid, k, h):
    return sample.Candidate(sid, k, NOW - k, ("reask",) if h == "F" else (),
                            h == "S", "organic", "tag")


def big_pool(f=70, s=40, r=100):
    return [candidate(f"private-session-{k % 10}", k + 1, h)
            for h, n in (("F", f), ("S", s), ("R", r))
            for k in range({"F": 0, "S": f, "R": f + s}[h],
                           {"F": 0, "S": f, "R": f + s}[h] + n)]


def test_exact_census(isolated):
    sid = "private-organic-session"
    path = write(isolated, sid, [
        f"{SENTINEL}initial",
        f"that didn't work try again {SENTINEL}failure",
        f"claude: {SENTINEL}prefix",
        f"{SENTINEL}short",
    ])
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    entries.extend([record(f"{SENTINEL}next"),
                    record(f"claude: {SENTINEL}ineligible")])
    entries.extend([record(f"{SENTINEL}answer", role="assistant"),
                    {**record(f"{SENTINEL}missing"), "timestamp": None}])
    path.write_text("".join(json.dumps(r) + "\n" for r in entries))
    write(isolated, "private-research-session", [f"{SENTINEL}research"])
    write(isolated, "private-untagged-session", [f"{SENTINEL}untagged"])

    def resolve(s):
        return session_kind.Resolution(
            "organic" if s == sid else "research" if "research" in s else None, "tag")

    counts = sample.census(projects_dir=isolated, now=NOW, resolve=resolve,
                           served_loader=lambda sid: [])
    assert counts["sessions_total"] == 3
    assert counts["sessions_by_kind"] == {"organic": 1, "research": 1, "untagged": 1}
    assert counts["organic_pairs_eligible"] == 4
    assert counts["eligible_after_exclusion"] == 4
    assert counts["no_ts"] == 1
    assert counts["N_F"] == 2
    assert counts["flagged_per_signal"] == {"reask": 1, "contradiction": 1, "repeat": 0}
    assert counts["flagged_ineligible"] == 1
    assert (counts["N_S"], counts["N_R"]) == (2, 0)
    assert counts["n_sessions"] == counts["n_sessions_F"] == 1
    assert counts["max_pairs_per_session"] == counts["median_pairs_per_session"] == 4
    assert counts["largest_session_share"] == counts["largest_session_share_F"] == 1


def test_exclusions_and_comments(isolated, tmp_path):
    assert sample.load_excluded() == set()
    write(isolated, "excluded-private-session", [
        f"{SENTINEL}initial", f"claude: {SENTINEL}later"])
    key = sample.pair_key("excluded-private-session", 1)
    assert key == hashlib.sha256(b"excluded-private-session|1").hexdigest()[:16]
    pool, counts = collect(isolated, excluded={key})
    assert pool == []
    assert counts["excluded_m09"] == 1
    assert counts["N_F"] == 0
    listing = tmp_path / "excluded.txt"
    listing.write_text("\n# comment\n  # more\nabc\n", encoding="utf-8")
    assert sample.load_excluded({"def"}, source=listing) == {"abc", "def"}


def test_draw_counts_weights_and_determinism():
    pool = big_pool()
    rows = sample.draw(pool, 360, "salt")
    assert rows == sample.draw(reversed(pool), 360, "salt")
    assert rows != sample.draw(pool, 361, "salt")
    assert Counter(r["stratum"] for r in rows) == {"F": 60, "S": 30, "R": 60}
    assert {r["weight_N_h"] / r["n_h"] for r in rows if r["stratum"] == "F"} == {70 / 60}
    assert set(rows[0]) == {
        "pair_id", "session_hash", "turn_index", "day", "stratum", "signals",
        "detector_version", "kind", "kind_source", "prev_served_class", "weight_N_h", "n_h"}
    small = sample.draw(big_pool(10, 2, 3), 360, "salt")
    assert Counter(r["stratum"] for r in small) == {"F": 10, "S": 2, "R": 3}


def test_concentrated_cap():
    pool = [candidate("dominant-private-session", k, "F") for k in range(80)]
    pool += [candidate(f"other-private-session-{k}", 1, "F") for k in range(20)]
    rows = sample.draw(pool, 360, "salt")
    dominant = sample._sha("salt|dominant-private-session")[:16]
    assert sum(r["session_hash"] == dominant for r in rows) == 15
    assert len(rows) == 35


def test_real_kind_index_default(isolated, monkeypatch):
    for sid in ("organic-private-session", "research-private-session"):
        write(isolated, sid, [f"{SENTINEL}first", f"claude: {SENTINEL}later"])
    monkeypatch.setattr(session_kind, "override_of", lambda sid: None)
    monkeypatch.setattr(session_kind, "tag_kind_of",
                        lambda sid: "organic" if sid.startswith("organic") else "research")
    counts = sample.census(projects_dir=isolated, now=NOW, served_loader=lambda sid: [])
    assert counts["sessions_by_kind"] == {"organic": 1, "research": 1, "untagged": 0}
    assert counts["N_F"] == 1


def test_served_and_unmeasured(isolated):
    write(isolated, "served-private-session",
          [f"{SENTINEL}initial", f"claude: {SENTINEL}later"])
    cheap = sorted(offload_share.OFFLOADED_CLASSES)[0]
    pool, counts = sample.collect(
        projects_dir=isolated, now=NOW, resolve=organic,
        served_loader=lambda sid: [(NOW - 1001, cheap), (NOW, "frontier")])
    assert counts["cheap_served"] == 1
    assert pool[0].prev_served_class == cheap

    def broken(sid):
        raise RuntimeError("synthetic ledger failure")

    pool, counts = sample.collect(projects_dir=isolated, now=NOW,
                                  resolve=organic, served_loader=broken)
    assert counts["cheap_served"] is None
    assert pool[0].prev_served_class is None


@pytest.mark.parametrize("n_flagged", [2, 24])
def test_cli_gate_privacy_modes_and_metadata(isolated, monkeypatch, capsys, n_flagged):
    for k in range(4):
        write(isolated, f"private-fixture-session-{k}",
              [f"{SENTINEL}initial{k}"] +
              [f"claude: {SENTINEL}item{k}_{j}" for j in range(n_flagged // 4)])
    # The small case needs exactly two flags rather than four empty sessions.
    if n_flagged == 2:
        write(isolated, "private-fixture-session-0",
              [f"{SENTINEL}initial", f"claude: {SENTINEL}a", f"claude: {SENTINEL}b"])
    pool, counts = collect(isolated)
    monkeypatch.setattr(sample, "collect", lambda **kwargs: (pool, counts))
    assert sample.cmd_redo_sample(["--census"]) == 0
    output = capsys.readouterr().out
    assert f"N_F={n_flagged} vs threshold 19:" in output
    assert ("NO-GO" if n_flagged < 19 else ": GO") in output
    assert SENTINEL not in output and "private-fixture-session" not in output
    run = isolated.parent / "state" / "redo_validation" / "fixture"
    args = ["--freeze", "--run-id", "fixture", "--allow-empty-exclusion"]
    if n_flagged < 19:
        assert sample.cmd_redo_sample(args) == 3
        assert not run.parent.exists()
        assert "Refused:" in capsys.readouterr().out
        args.append("--allow-below-threshold")
    assert sample.cmd_redo_sample(args) == 0
    output = capsys.readouterr().out
    assert SENTINEL not in output and "private-fixture-session" not in output
    assert stat.S_IMODE(run.stat().st_mode) == 0o700
    assert stat.S_IMODE(run.parent.stat().st_mode) == 0o700
    for path in run.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        content = path.read_text()
        assert SENTINEL not in content and "private-fixture-session" not in content
    frozen = json.loads((run / "frozen.json").read_text())
    assert frozen["allow_below_threshold"] is (n_flagged < 19)
    assert frozen["no_go"] == ("insufficient_flagged_supply" if n_flagged < 19 else None)
    assert frozen["strata"]["F"] == {"N": n_flagged, "n": n_flagged}
    assert frozen["weights"]["F"] == 1
    assert frozen["weights"]["R"] is None
    assert frozen["shortfall"] == 150 - n_flagged
    assert frozen["concentrated"] is (n_flagged < 19)
    assert frozen["manifest_sha256"] == hashlib.sha256(
        (run / "manifest.jsonl").read_bytes()).hexdigest()
    assert sample.cmd_redo_sample(args) == 2


def test_cli_help_is_inert(isolated, capsys):
    assert "redo-sample" in cli._KNOWN_SUBCOMMANDS
    assert "redo-sample" in cli._OWN_HELP_ANYWHERE
    assert "llm-router redo-sample --census|--freeze" in cli.__doc__
    assert cli._subcommand_help(["redo-sample", "--help"]) is False
    with pytest.raises(SystemExit) as exc:
        sample.cmd_redo_sample(["--help"])
    assert exc.value.code == 0
    assert "usage: llm-router redo-sample" in capsys.readouterr().out
    assert not (isolated.parent / "state").exists()

    pool, counts = sample.collect(projects_dir=isolated, now=NOW,
                                  resolve=organic, served_loader=lambda sid: [])
    assert counts["cheap_served"] is None       # an empty ledger measures nothing
