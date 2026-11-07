"""D-407: coordinated (override-listed, non-organic) sessions get their own S1 line; organic and
release_clock are unchanged."""
from __future__ import annotations

import json

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
