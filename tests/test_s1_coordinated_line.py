"""D-407: coordinated (override-listed, non-organic) sessions get their own S1 line; organic and
release_clock are unchanged."""
from __future__ import annotations

import json
import re

from llm_router import daily_offload as do
from tests.test_s1fix_synthetic_streak import DAY, NOW, QUALITY, _rows, home  # noqa: F401


def _kind_of(sid, stamp):
    return {"org": "organic", "syn": "research", "coo": "research"}.get(str(sid)[:3])


def _day(rows, ids):
    inp = do.Inputs(rows, [], [], [], QUALITY, _kind_of)
    return do.compute_day(DAY, inp, now=NOW, manifest_ids=ids, code_rev="t")["populations"]


def _write_overrides(home, sids):  # noqa: F811
    d = home / ".llm-router"
    d.mkdir(exist_ok=True)
    (d / "session_kind_overrides.json").write_text(
        json.dumps({s: {"kind": "research", "reason": "coordinator"} for s in sids}))


def _dump(p):
    return json.dumps(p, sort_keys=True)


def test_coordinated_line_leaves_organic_and_release_clock_byte_identical(home):  # noqa: F811
    org, syn, coo = _rows("org"), _rows("syn"), _rows("coo")
    ids = {f"syn-{i}" for i in range(3)}
    before = _day(org + syn, ids)                     # the day with no coordinated session at all
    plain = _day(org + syn + coo, ids)                # coordinated rows present, no override: they are "other"
    _write_overrides(home, {f"coo-{i}" for i in range(3)})
    after = _day(org + syn + coo, ids)
    for name in ("organic", "release_clock", "synthetic"):
        assert _dump(after[name]) == _dump(before[name]) == _dump(plain[name])
    assert after["coordinated"]["action"]["n"] > 0
    assert after["coordinated"]["action"]["n"] == plain["other"]["action"]["n"] - before["other"]["action"]["n"]
    assert after["other"]["action"]["n"] == before["other"]["action"]["n"]
    assert plain["coordinated"]["action"]["n"] == 0
    assert "COORDINATED" in after["coordinated"]["label"] and "not organic" in after["coordinated"]["label"]
    assert after["organic"]["action"]["n"] > 0


def test_override_on_organic_kind_is_not_coordinated(home):  # noqa: F811
    _write_overrides(home, set())
    (home / ".llm-router" / "session_kind_overrides.json").write_text(
        json.dumps({"org-0": {"kind": "organic", "reason": "x"}}))
    p = _day(_rows("org"), set())
    assert p["coordinated"]["action"]["n"] == 0


def test_manifest_listed_and_overridden_session_stays_synthetic(home):  # noqa: F811
    """Review of #850: synthetic is checked before the override, so release_clock cannot move."""
    syn = _rows("syn")
    ids = {f"syn-{i}" for i in range(3)}
    before = _day(syn, ids)
    _write_overrides(home, {"syn-0"})
    after = _day(syn, ids)
    assert _dump(after["synthetic"]) == _dump(before["synthetic"])
    assert _dump(after["release_clock"]) == _dump(before["release_clock"])
    assert after["synthetic"]["action"]["n"] > 0
    assert after["coordinated"]["action"]["n"] == 0


def _run_json(capsys, rows, manifest, force=False):
    inp = do.Inputs(rows, [], [], [], QUALITY, _kind_of)
    assert do.run_daily(date=str(DAY), backfill=0, force=force, partial=False, manifest=manifest, as_json=True,
                        inputs=inp, now=NOW) == 0
    return json.loads(capsys.readouterr().out)


def test_run_daily_reports_coordinated_fields_and_streak_is_unchanged(home, tmp_path, capsys):  # noqa: F811
    """Review of #850: the JSON output carries coordinated_action/turn, and the streak (release_clock and
    organic) is the same with and without the override. The manifest makes release_clock measured on DAY, so
    the streak compared is 1, not a vacuous 0."""
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("".join(json.dumps({"schema": "synth-live-manifest/1", "session_id": f"syn-{i}"}) + "\n"
                                for i in range(3)))
    rows = _rows("org") + _rows("syn") + _rows("coo")
    plain = _run_json(capsys, rows, str(manifest))
    streak_plain = do.streak(DAY, now=NOW)
    assert streak_plain["measured_streak"] == 1
    _write_overrides(home, {f"coo-{i}" for i in range(3)})
    over = _run_json(capsys, rows, str(manifest), force=True)
    streak_over = do.streak(DAY, now=NOW)
    assert len(plain) == len(over) == 1
    for o in (plain[0], over[0]):
        assert {"coordinated_action", "coordinated_turn"} <= set(o)
    assert plain[0]["coordinated_action"]["n"] == 0
    assert plain[0]["coordinated_turn"]["n"] == 0
    assert over[0]["coordinated_action"]["n"] > 0
    assert over[0]["coordinated_turn"]["n"] == 0   # non-manifest rows have no transcript turns
    for k in ("organic_action", "organic_turn", "release_clock_action", "release_clock_turn"):
        assert _dump(over[0][k]) == _dump(plain[0][k])
    assert _dump(streak_over) == _dump(streak_plain)


def _run_text(capsys, rows, force=False):
    inp = do.Inputs(rows, [], [], [], QUALITY, _kind_of)
    assert do.run_daily(date=str(DAY), backfill=0, force=force, partial=False, manifest=None,
                        inputs=inp, now=NOW) == 0
    return capsys.readouterr().out


def _coord_action_text(out):
    assert do.COORD_LABEL in out
    return re.search(r"COORDINATED sessions, not organic \(D-407\): action ([^ ]+ ?[^ ]*?)  turn", out).group(1)


def test_run_daily_text_line_labels_coordinated(home, capsys):  # noqa: F811
    rows = _rows("org") + _rows("coo")
    # the line is always printed; without an override the coordinated action count is 0
    assert _coord_action_text(_run_text(capsys, rows)) == "unmeasured n=0"
    _write_overrides(home, {f"coo-{i}" for i in range(3)})
    got = _coord_action_text(_run_text(capsys, rows, force=True))
    assert got != "unmeasured n=0"                               # override: coordinated n > 0 reaches the text
    assert re.fullmatch(r"(\d+/\d+=\S+|unmeasured n=[1-9]\d*)", got), got
