"""scripts/cls_rules_tier.py (B-R): synthetic fixtures only; no network, no model call, HOME is a tmp dir."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cls_dev_lib as D  # noqa: E402
import cls_pins as P  # noqa: E402
import cls_rules_tier as R  # noqa: E402

POLICY = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "proxy" / "claude_tiers.yaml"
SENT = "ZZSENTINEL"
COMMIT = "a" * 40
# (item_id, kind, timestamp): two real before the live date, two after, two DEV-SYN
ITEMS = [("r1", "real", "2026-10-01T10:00:00Z"), ("r2", "real", "2026-10-08T23:59:59Z"),
         ("r3", "real", "2026-10-09T00:00:00Z"), ("r4", "real", "2026-10-12T08:00:00Z"),
         ("s1", "syn", None), ("s2", "syn", None),
         ("r5", "real", "2026-10-09T01:00:00+02:00"), ("r6", "real", "2026-10-08T20:00:00-05:00"),
         ("r7", "real", "2026-10-10T00:00:00Z"), ("s3", "syn", None)]
PAD = " Consider the surrounding module, the data flow, and the constraints of the deployment."
SONNET_TEXT = "Analyze the performance regression thoroughly." + PAD * 8       # 742 chars: analyze/moderate
OPUS_TEXT = "Summarize the design notes." + PAD * 30                           # 2637 chars: generate/complex
# exact tier the repo policy (claude_tiers.yaml) gives each item, probed with the section 4 resolver
EXPECT_TIER = {"r1": "haiku", "r2": "haiku", "r3": "haiku", "r4": "haiku", "s1": "haiku", "s2": "haiku",
               "r5": "sonnet", "r6": "haiku", "r7": "opus", "s3": "sonnet"}
# record_utc must be the true UTC instant of the offset timestamp
EXPECT_UTC = {"r5": "2026-10-08T23:00:00Z", "r6": "2026-10-09T01:00:00Z"}
TEXTS = {"r1": "fix the typo in the readme", "r2": "design a distributed cache with failover and write the code",
         "r3": "what is two plus two", "r4": "refactor the whole module and add tests",
         "s1": "explain how a hash map works", "s2": "implement a lock free queue in rust",
         "r5": SONNET_TEXT, "r6": "what is three plus three", "r7": OPUS_TEXT, "s3": SONNET_TEXT}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "cls"
    root.mkdir()
    pol = tmp_path / "policy.yaml"
    shutil.copy(POLICY, pol)
    man = {"items": [{"item_id": i, "kind": k, "requested": None} for i, k, _ in ITEMS]}
    (tmp_path / "manifest.json").write_text(json.dumps(man))
    real = [{"item_id": i, "text": "%s-%s %s" % (SENT, i, TEXTS[i]), "timestamp": t} for i, k, t in ITEMS if k == "real"]
    syn = [{"item_id": i, "text": "%s-%s %s" % (SENT, i, TEXTS[i])} for i, k, _ in ITEMS if k == "syn"]
    (root / "items.jsonl").write_text("\n".join(json.dumps(x) for x in real) + "\n")
    (root / "syn.jsonl").write_text("\n".join(json.dumps(x) for x in syn) + "\n")
    return tmp_path


def argv(env, out="rt.jsonl", **over):
    a = {"--cls-root": str(env / "cls"), "--manifest": str(env / "manifest.json"),
         "--items": str(env / "cls" / "items.jsonl"), "--syn-items": str(env / "cls" / "syn.jsonl"),
         "--tier-policy": str(env / "policy.yaml"), "--policy-sha256": hashlib.sha256(POLICY.read_bytes()).hexdigest(),
         "--resolver-commit": COMMIT, "--out": str(env / out)}
    a.update(over)
    return [x for kv in a.items() for x in kv]


def rows_of(path):
    lines = Path(path).read_text().splitlines()
    return json.loads(lines[0]), [json.loads(x) for x in lines[1:]]


def test_validates_against_frozen_reader(env, monkeypatch):
    assert R.main(argv(env)) == 0
    out = env / "rt.jsonl"
    monkeypatch.setattr(D, "require_run_pin", lambda *a, **k: None)
    header, rows = D.read_rules_tier(out, env / "none")
    assert len(rows) == len(ITEMS) and set(rows) == {i for i, _, _ in ITEMS}
    assert all(r["rules_tier"] in D.TIERS for r in rows.values())
    assert P.rules_tier_header(out) == header
    eff, unl = D.rules_arms(rows, [i for i, _, _ in ITEMS])
    assert all(v in ("sonnet", "opus") for v in eff.values()) and set(unl) == set(eff)


def test_header_fields(env):
    assert R.main(argv(env)) == 0
    h, _ = rows_of(env / "rt.jsonl")
    assert h["schema"] == "rules_tier/1" and h["resolver_commit"] == COMMIT
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", h["produced_utc"])


def test_record_utc_and_haiku_live(env):
    assert R.main(argv(env)) == 0
    _, rows = rows_of(env / "rt.jsonl")
    by = {r["item_id"]: r for r in rows}
    for i, k, t in ITEMS:
        if k == "syn":
            assert by[i]["record_utc"] is None and by[i]["haiku_live"] is True
        else:
            utc = EXPECT_UTC.get(i, t)
            assert by[i]["record_utc"] == utc
            assert by[i]["haiku_live"] is (utc >= "2026-10-09T00:00:00Z")


def test_deterministic(env):
    assert R.main(argv(env, out="a.jsonl")) == 0 and R.main(argv(env, out="b.jsonl")) == 0
    (_, a), (_, b) = rows_of(env / "a.jsonl"), rows_of(env / "b.jsonl")
    assert a == b and len(a) == len(ITEMS)


def test_refuse_overwrite(env):
    assert R.main(argv(env)) == 0
    before = (env / "rt.jsonl").read_bytes()
    assert R.main(argv(env)) == 2
    assert (env / "rt.jsonl").read_bytes() == before


def test_counts_only_and_mode(env, capsys):
    assert R.main(argv(env)) == 0
    cap = capsys.readouterr()
    assert SENT not in cap.out + cap.err and SENT not in (env / "rt.jsonl").read_text()
    lines = cap.out.strip().splitlines()
    assert len(lines) == 1
    c = json.loads(lines[0])
    assert c["n"] == len(ITEMS) and sum(c["tiers"].values()) == len(ITEMS)
    assert c["n_syn"] == 3 and c["n_real"] == 7 and c["n_haiku_live"] == 7
    assert c["tiers"] == {"haiku": 7, "sonnet": 2, "opus": 1}
    assert (env / "rt.jsonl").stat().st_mode & 0o777 == 0o600


def test_bad_policy_sha_refused(env, capsys):
    assert R.main(argv(env, **{"--policy-sha256": "0" * 64})) == 5
    assert not (env / "rt.jsonl").exists() and SENT not in capsys.readouterr().err


def test_missing_text_refused(env, capsys):
    (env / "cls" / "syn.jsonl").write_text("")
    assert R.main(argv(env)) == 5
    assert not (env / "rt.jsonl").exists() and SENT not in capsys.readouterr().err


def test_exact_tier_per_item(env):
    assert R.main(argv(env)) == 0
    _, rows = rows_of(env / "rt.jsonl")
    assert {r["item_id"]: r["rules_tier"] for r in rows} == EXPECT_TIER
    assert set(EXPECT_TIER.values()) == {"haiku", "sonnet", "opus"}


def test_offset_timestamps_converted_to_utc(env):
    assert R.main(argv(env)) == 0
    _, rows = rows_of(env / "rt.jsonl")
    by = {r["item_id"]: r for r in rows}
    assert by["r5"]["record_utc"] == "2026-10-08T23:00:00Z" and by["r5"]["haiku_live"] is False
    assert by["r6"]["record_utc"] == "2026-10-09T01:00:00Z" and by["r6"]["haiku_live"] is True


def _tree(root):
    root = Path(root)
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns, p.stat().st_size)
                  for p in root.rglob("*")) if root.exists() else None


def test_ambient_engine_env_does_not_change_result(env, monkeypatch):
    assert R.main(argv(env, out="clean.jsonl")) == 0
    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "decide")
    monkeypatch.setenv("LLM_ROUTER_ABSTAIN_BELOW", "0.99")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")
    assert R.main(argv(env, out="dirty.jsonl")) == 0
    assert rows_of(env / "dirty.jsonl")[1] == rows_of(env / "clean.jsonl")[1]
    import os
    assert os.environ["LLM_ROUTER_ENGINE_MODE"] == "decide"   # caller env restored


def test_caller_home_and_router_home_untouched(env, monkeypatch):
    home, lr = env / "home", env / "lrhome"
    home.mkdir(exist_ok=True)
    lr.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(lr))
    before = (_tree(home), _tree(lr))
    assert R.main(argv(env)) == 0
    assert (_tree(home), _tree(lr)) == before == ([], [])


def test_scratch_removed(env, monkeypatch):
    tmp = env / "tmpdir"
    tmp.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(tmp))
    assert R.main(argv(env)) == 0
    assert list(tmp.iterdir()) == []


def test_unresolved_tier_refused_no_fallback(env, monkeypatch, capsys):
    import p17d_synthetic as P17
    monkeypatch.setattr(P17, "rules_tier", lambda body, policy, req: ("code", "simple", None))
    man = json.loads((env / "manifest.json").read_text())
    for m in man["items"]:
        m["requested"] = "opus"          # must not be used as a fallback
    (env / "manifest.json").write_text(json.dumps(man))
    assert R.main(argv(env)) == 5
    err = capsys.readouterr().err
    assert "Refused: resolver returned no tier for %d item(s)" % len(ITEMS) in err and SENT not in err
    assert not (env / "rt.jsonl").exists()
