"""scripts/cls_attach.py (CLS-DEV B-A, PREREG-CLS-DEV section 1.3) on SYNTHETIC fixtures only.

No real data, no home path, no network, no model. The pinned GE3 record helpers (gt500.py, supply.py)
are replaced by fake modules for the record-recovery test.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cls_attach as A  # noqa: E402
import cls_dev_lib as D  # noqa: E402
import cls_pins as P  # noqa: E402
import cls_send_lib as S  # noqa: E402

SENTINEL = "quokkamarker"
SM = "sessmarker"
S1, S2, S3, S4 = (SM + "-0001", SM + "-0002", SM + "-0003", SM + "-0004")


def txt(n: int, words: str) -> str:
    return "%s %s item %d please look at the module and report back with details" % (words, SENTINEL, n)


T = {k: txt(i, w) for i, (k, w) in enumerate([
    ("g_t1", "refactor the parser"), ("g_h1", "explain the cache bug"), ("g_h2", "design the router eval"),
    ("g_x", "not in any split"), ("g_nolab", "held out but unlabelled"),
    ("e_c1", "calibration prompt about tiers"), ("e_h1", "held out prompt about sessions"),
    ("e_nots", "a prompt with no timestamp"),
    ("t1", "rename a variable"), ("t2", "write a migration plan"), ("t3", "odd split prompt"),
    ("p_f1", "fresh window prompt about hooks"), ("p_f2", "fresh unlabelled prompt"),
])}
T["p_g1"] = T["g_h2"]  # P17D G is the GE3 held-out again (same text, session and timestamp)


def wjsonl(path: Path, rows) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def p17d_rows():
    base = {"earlier": [], "assistant_tail": "", "cwd": "/work/repo", "requested_model": "claude-opus-5-5"}
    return [
        dict(base, item_id="pf1", group="F", session_id=S3, ts="2026-09-20T08:00:00Z", prompt=T["p_f1"],
             assembled_context="Working directory: /work/repo\n\nctx " + SENTINEL, context_withheld=False,
             requested_tier="opus"),
        dict(base, item_id="pf2", group="F", session_id=S3, ts="2026-09-20T08:05:00Z", prompt=T["p_f2"],
             assembled_context="", context_withheld=False, requested_tier="opus"),
        dict(base, item_id="pg1", group="G", session_id=S2, ts="2026-09-12T09:10:00Z", prompt=T["p_g1"],
             assembled_context="ctx " + SENTINEL, context_withheld=True, requested_tier="sonnet"),
    ]


@pytest.fixture(scope="module")
def item_key():
    try:
        return A._load_script("p17d_synthetic").item_key
    except Exception as e:  # noqa: BLE001
        pytest.skip("p17d_synthetic not importable here: %s" % type(e).__name__)


def make_world(tmp: Path, item_key=None, *, with_p17d=True) -> dict:
    src = tmp / "src"
    src.mkdir()
    ge3 = [
        {"id": "g-t1", "session_id": S1, "timestamp": "2026-09-10T10:00:00Z", "prompt": T["g_t1"],
         "context": "earlier turn " + SENTINEL},
        {"id": "g-h1", "session_id": S2, "timestamp": "2026-09-12T09:00:00.250Z", "prompt": T["g_h1"],
         "context": "withheld " + SENTINEL, "context_withheld": True},
        {"id": "g-h2", "session_id": S2, "timestamp": "2026-09-12T09:10:00Z", "prompt": T["g_h2"]},
        {"id": "g-x", "session_id": S2, "timestamp": "2026-09-12T09:20:00Z", "prompt": T["g_x"]},
        {"id": "g-nolab", "session_id": S2, "timestamp": "2026-09-12T09:30:00Z", "prompt": T["g_nolab"]},
    ]
    files = {
        "ge3_items": wjsonl(src / "ge3_items.jsonl", ge3),
        "ge3_split": src / "split.json",
        "ge3_opus_labels": wjsonl(src / "labels_opus.jsonl", [
            {"id": "g-t1", "tier": "opus", "batch": 3}, {"id": "g-h1", "tier": "sonnet"},
            {"id": "g-h2", "tier": "haiku"}, {"id": "g-x", "tier": "haiku"}]),
        "ge3_sonnet_labels": wjsonl(src / "labels_sonnet.jsonl", [
            {"id": "g-t1", "tier": "sonnet"}, {"id": "g-h1", "tier": "sonnet"}]),
        "e2_sample": wjsonl(src / "sample.jsonl", [
            {"id": "e-c1", "split": "calibration", "session_id": S4, "timestamp": "2026-08-01T00:00:00Z",
             "text": T["e_c1"], "truth": "haiku"},
            {"id": "e-h1", "split": "heldout", "session_id": S4, "timestamp": "2026-08-01T00:10:00Z",
             "text": T["e_h1"], "truth": "Opus"},
            {"id": "e-nots", "split": "heldout", "session_id": S4, "text": T["e_nots"], "truth": "sonnet"}]),
        "e1_tasks": src / "tasks_local.json",
        "e1_opus_labels": wjsonl(src / "e1_labels.jsonl", [
            {"id": "t1", "tier": "haiku"}, {"id": "t2", "tier": "sonnet"}, {"id": "t3", "tier": "opus"}]),
    }
    files["ge3_split"].write_text(json.dumps({"tune_ids": ["g-t1"], "heldout_ids": ["g-h1", "g-h2", "g-nolab"]}))
    files["e1_tasks"].write_text(json.dumps([{"id": "t1", "split": "tune", "text": T["t1"]},
                                             {"id": "t2", "split": "heldout", "text": T["t2"]},
                                             {"id": "t3", "split": "weird", "text": T["t3"]}]))
    if with_p17d:
        rows = p17d_rows()
        files["p17d_items"] = wjsonl(src / "p17d_items.jsonl", rows)
        labs = []
        for r in rows:
            if r["item_id"] == "pf2":
                continue  # unlabelled candidate
            sid, key = item_key(r)
            labs.append({"text_sha": key, "session_id": sid, "truth": "opus"})
        files["p17d_labels"] = wjsonl(src / "p17d_labels.jsonl", labs)
    cls = tmp / "cls"
    (cls / "dev").mkdir(parents=True)
    return {"cls": cls, "out": cls / "dev" / "attach1", "files": files}


def attach_argv(w: dict, *, extra=(), out=None, allow_e1=True) -> list[str]:
    argv = ["attach", "--cls-root", str(w["cls"]), "--out", str(out or w["out"])]
    if allow_e1 and "e1_tasks" in w["files"]:  # the E1 fixture rows carry no timestamp (recovery tests supply them)
        argv += ["--allow-missing-timestamps", "e1_tune", "--allow-missing-timestamps", "e1_ho"]
    for k, v in w["files"].items():
        argv += ["--" + k.replace("_", "-"), str(v)]
    if "e2_sample" in w["files"]:
        argv += ["--e2-opus-labels", str(w["files"]["e2_sample"]), "--e2-label-tier", "truth"]
    return argv + list(extra)


def read_out(out: Path, name: str) -> list[dict]:
    return [json.loads(x) for x in (out / name).read_text(encoding="utf-8").splitlines() if x.strip()]


def assert_private_stdout(text: str, out: Path, tmp: Path) -> None:
    assert SENTINEL not in text and SM not in text
    assert str(tmp) not in text.replace(str(out), "")


# ---------------------------------------------------------------------------------------------

def test_attach_all_sources(tmp_path, capsys, item_key):
    w = make_world(tmp_path, item_key)
    rc = A.main(attach_argv(w))
    cap = capsys.readouterr()
    assert rc == 0, cap.err
    assert_private_stdout(cap.out + cap.err, w["out"], tmp_path)
    stdout = json.loads(cap.out)
    assert "inputs" not in stdout and stdout["out_dir"] == str(w["out"])

    rows = read_out(w["out"], A.ITEMS)
    for r in rows:
        assert set(r) == set(A.ITEM_KEYS)
        assert isinstance(r["item_id"], str) and len(r["item_id"]) == 16
        assert isinstance(r["text"], str) and SENTINEL in r["text"]
        assert r["timestamp"] is None or D.parse_utc(r["timestamp"]) is not None
        assert r["session_id"] is None or isinstance(r["session_id"], str)
        assert r["group"] == r["session_id"]
        assert r["batch"] is None or isinstance(r["batch"], str)
        assert r["digest"] is None or isinstance(r["digest"], str)
        assert r["requested"] in (None, "haiku", "sonnet", "opus")
    by_text = {r["text"]: r for r in rows if r["source_set"] != "p17d_g"}
    assert [r["item_id"] for r in rows] == sorted(r["item_id"] for r in rows)
    assert by_text[T["g_t1"]]["source_set"] == "ge3_tune" and by_text[T["g_t1"]]["batch"] == "3"
    assert by_text[T["g_t1"]]["digest"].startswith("earlier turn")
    assert by_text[T["g_h1"]]["digest"] is None                       # context_withheld
    assert by_text[T["g_h1"]]["timestamp"] == "2026-09-12T09:00:00.250Z"  # never re-formatted
    assert by_text[T["e_c1"]]["source_set"] == "e2_cal" and by_text[T["e_h1"]]["source_set"] == "e2_ho"
    assert by_text[T["t1"]]["source_set"] == "e1_tune" and by_text[T["t2"]]["source_set"] == "e1_ho"
    assert by_text[T["t1"]]["timestamp"] is None and by_text[T["t1"]]["session_id"] is None
    assert by_text[T["p_f1"]]["source_set"] == "p17d_f" and by_text[T["p_f1"]]["requested"] == "opus"
    assert by_text[T["p_f1"]]["digest"].startswith("Working directory")
    pg = [r for r in rows if r["source_set"] == "p17d_g"]
    assert len(pg) == 1 and pg[0]["digest"] is None and pg[0]["requested"] == "sonnet"
    assert T["g_x"] not in by_text and T["t3"] not in by_text
    # unlabelled items are KEPT (the frozen primary-missing guard counts them); they get no legacy row
    assert by_text[T["g_nolab"]]["source_set"] == "ge3_ho" and by_text[T["p_f2"]]["source_set"] == "p17d_f"

    counts = json.loads((w["out"] / A.COUNTS).read_text())
    assert counts["dropped"] == {"e1_unknown_split": 1, "ge3_not_in_split": 1}
    assert counts["per_source"]["p17d_f"]["n_no_opus_label"] == 1 and counts["per_source"]["ge3_ho"]["n_no_opus_label"] == 1
    assert counts["digest_basis"] == "raw_assembled_context"
    assert counts["allow_missing_timestamps"] == ["e1_ho", "e1_tune"]
    eva = counts["expected_vs_actual"]
    assert eva["ge3_tune"] == {"expected": 108, "actual": 1} and eva["e2_cal"] == {"expected": 91, "actual": 1}
    assert set(eva) == {"ge3_tune", "ge3_ho", "e2_cal", "e2_ho", "e1_tune", "e1_ho", "p17d_f", "p17d_g"}
    assert "expected_vs_actual" in stdout and "WARNING: source set ge3_tune expected 108" in cap.err
    assert counts["per_source"]["ge3_ho"]["digest_withheld"] == 1
    assert counts["per_source"]["e1_tune"]["n_no_timestamp"] == 1
    assert counts["exact_overlap"] == {"ge3_ho|p17d_g": 1}
    assert counts["legacy"]["conflict"] == {"opus": 1, "sonnet": 0}
    assert set(counts["inputs"]) == set(w["files"]) | {"e2_opus_labels"}
    assert counts["n_items"] == len(rows) == 12

    for name in (A.LEG_OPUS, A.LEG_SONNET):
        leg = read_out(w["out"], name)
        assert leg and all(set(x) == set(A.LEGACY_KEYS) and x["truth"] in D.TIERS for x in leg)
        assert SENTINEL not in (w["out"] / name).read_text()
        for x in leg:
            assert len(x["text_sha"]) == 64
    opus = read_out(w["out"], A.LEG_OPUS)
    conflict_key = (S2, S.text_sha(T["g_h2"]))
    assert conflict_key not in {(x["session_id"], x["text_sha"]) for x in opus}
    assert len(opus) == 8  # 12 items, 2 unlabelled, 9 keys; the conflicting key (2 items) is written for neither
    assert len(read_out(w["out"], A.LEG_SONNET)) == 2
    blob = (w["out"] / A.COUNTS).read_text()
    assert SENTINEL not in blob and SM not in blob


def test_build_manifest_accepts_attach_output(tmp_path, capsys, item_key):
    w = make_world(tmp_path, item_key)
    assert A.main(attach_argv(w)) == 0
    capsys.readouterr()
    rows = read_out(w["out"], A.ITEMS)
    legacy = {"opus": read_out(w["out"], A.LEG_OPUS), "sonnet": read_out(w["out"], A.LEG_SONNET)}
    man, lengths = D.build_manifest(rows, [], ctx=S.ScrubContext(), t0=D.parse_utc(D.T0_DEFAULT),
                                    cutoff=D.parse_utc(D.CUTOFF_DEFAULT), legacy=legacy)
    c = man["counts"]
    assert c["n_input_real"] == len(rows)
    n_no_ts = sum(1 for r in rows if r["timestamp"] is None)
    assert n_no_ts == 3 and c["n_no_timestamp_dropped"] == n_no_ts
    assert c["n_dropped_dup"] == 1                       # GE3 held-out vs P17D G, exact text
    assert c["n_real_dev"] == len(rows) - n_no_ts - 1
    assert set(c["per_source_n"]) <= {"ge3_tune", "ge3_ho", "e2_cal", "e2_ho", "p17d_f", "p17d_g"}
    leg_keys = {(x["session_id"], x["text_sha"]) for x in legacy["opus"]}
    sid = {r["item_id"]: r["session_id"] or "" for r in rows}
    want = sum(1 for x in man["items"] if (sid[x["item_id"]], x["text_sha"]) in leg_keys)
    assert want > 0 and c["n_existing_labels"]["opus"] == want
    unmatched_rows = {(r["session_id"] or "", S.text_sha(r["text"])) for r in rows if r["timestamp"] is None}
    assert c["n_unmapped_labels"]["opus"] == len(leg_keys & unmatched_rows)
    assert c["n_existing_labels"]["sonnet"] == 2
    assert sum(lengths.values()) == c["n_real_dev"]


def test_modes(tmp_path, capsys, item_key):
    w = make_world(tmp_path, item_key)
    assert A.main(attach_argv(w)) == 0
    assert stat.S_IMODE(os.stat(w["out"]).st_mode) == 0o700
    for name in A.OUTPUTS:
        assert stat.S_IMODE(os.stat(w["out"] / name).st_mode) == 0o600


def test_refuse_overwrite(tmp_path, capsys, item_key):
    w = make_world(tmp_path, item_key)
    assert A.main(attach_argv(w)) == 0
    before = {n: (w["out"] / n).read_bytes() for n in A.OUTPUTS}
    capsys.readouterr()
    assert A.main(attach_argv(w)) == 2
    cap = capsys.readouterr()
    assert "exists" in cap.err and cap.out == ""
    assert {n: (w["out"] / n).read_bytes() for n in A.OUTPUTS} == before
    # one stray output file is enough to refuse
    out2 = w["cls"] / "dev" / "attach2"
    out2.mkdir()
    (out2 / A.COUNTS).write_text("{}")
    assert A.main(attach_argv(w, out=out2)) == 2
    assert not (out2 / A.ITEMS).exists()


def test_path_guards(tmp_path, capsys):
    w = make_world(tmp_path, with_p17d=False)
    assert A.main(attach_argv(w, out=Path("rel/out"))) == 2
    argv = attach_argv(w)
    argv[argv.index("--ge3-split") + 1] = "relative/split.json"
    assert A.main(argv) == 2
    assert A.main(attach_argv(w, out=w["cls"] / "elsewhere")) == 2
    assert A.main(attach_argv(w, out=w["cls"] / "test-syn" / "x")) == 2
    (w["cls"] / "test-syn").mkdir()
    os.symlink(w["cls"] / "test-syn", w["cls"] / "dev" / "sneaky")
    assert A.main(attach_argv(w, out=w["cls"] / "dev" / "sneaky" / "x")) == 2
    assert not (w["cls"] / "test-syn" / "x").exists()
    err = capsys.readouterr().err
    assert SENTINEL not in err and SM not in err


def test_source_flag_rules(tmp_path, capsys):
    w = make_world(tmp_path, with_p17d=False)
    base = ["attach", "--cls-root", str(w["cls"]), "--out", str(w["out"])]
    assert A.main(base) == 2                                                   # no source
    assert A.main(base + ["--ge3-items", str(w["files"]["ge3_items"])]) == 2    # split + labels missing
    assert A.main(base + ["--e1-tasks", str(w["files"]["e1_tasks"])]) == 2      # labels missing
    assert A.main(base + ["--e1-tasks", str(w["files"]["e1_tasks"]), "--e1-opus-labels",
                          str(w["files"]["e1_opus_labels"]), "--ledger", str(w["files"]["e1_tasks"])]) == 2
    assert not w["out"].exists()


# ---------------------------------------------------------------------------------------------
# each source reader on its own tiny fixture
# ---------------------------------------------------------------------------------------------

def test_reader_ge3(tmp_path):
    w = make_world(tmp_path, with_p17d=False)
    f = w["files"]
    drops = A.Counter()
    items = A.read_ge3(f["ge3_items"], f["ge3_split"], f["ge3_opus_labels"], f["ge3_sonnet_labels"], drops)
    assert sorted((x["native"], x["source_set"], x["opus"], x["sonnet"]) for x in items) == [
        ("g-h1", "ge3_ho", "sonnet", "sonnet"), ("g-h2", "ge3_ho", "haiku", None), ("g-nolab", "ge3_ho", None, None),
        ("g-t1", "ge3_tune", "opus", "sonnet")]
    assert drops["ge3_not_in_split"] == 1 and "no_opus_label" not in drops
    assert [(x["native"], x["opus"]) for x in items if x["opus"] is None] == [("g-nolab", None)]


def test_reader_e2_inline_labels(tmp_path):
    f = make_world(tmp_path, with_p17d=False)["files"]
    drops = A.Counter()
    items = A.read_e2(f["e2_sample"], f["e2_sample"], "id", "truth", drops)
    assert sorted((x["native"], x["source_set"], x["opus"]) for x in items) == [
        ("e-c1", "e2_cal", "haiku"), ("e-h1", "e2_ho", "opus"), ("e-nots", "e2_ho", "sonnet")]
    assert [x["timestamp"] for x in items if x["native"] == "e-nots"] == [None]


def test_reader_e1(tmp_path):
    f = make_world(tmp_path, with_p17d=False)["files"]
    drops = A.Counter()
    items = A.read_e1(f["e1_tasks"], f["e1_opus_labels"], "id", "tier", drops)
    assert sorted((x["native"], x["source_set"], x["opus"]) for x in items) == [
        ("t1", "e1_tune", "haiku"), ("t2", "e1_ho", "sonnet")]
    assert drops["e1_unknown_split"] == 1


def test_reader_labels_conflict_and_bad_tier(tmp_path):
    p = wjsonl(tmp_path / "l.jsonl", [{"id": "a", "tier": "opus"}, {"id": "a", "tier": "haiku"},
                                     {"id": "b", "tier": "large"}, {"tier": "opus"}, {"id": "c", "truth": "sonnet"}])
    drops = A.Counter()
    m = A.label_map(p, "x", "id", ("tier", "truth"), drops)
    assert m == {"c": ("sonnet", None)}
    assert drops == {"label_key_conflict": 1, "label_bad_tier": 1, "label_row_key_missing": 1}


def test_reader_p17d(tmp_path, item_key):
    f = make_world(tmp_path, item_key)["files"]
    drops = A.Counter()
    items = A.read_p17d(f["p17d_items"], f["p17d_labels"], drops)
    assert sorted((x["native"], x["source_set"], x["opus"], x["requested"]) for x in items) == [
        ("pf1", "p17d_f", "opus", "opus"), ("pf2", "p17d_f", None, "opus"), ("pg1", "p17d_g", "opus", "sonnet")]
    assert "no_opus_label" not in drops
    assert [x["digest"] for x in items if x["native"] == "pg1"] == [None]


# ---------------------------------------------------------------------------------------------
# record recovery with fake GE3 helpers
# ---------------------------------------------------------------------------------------------

def _text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


FAKE_GT = types.SimpleNamespace(_is_human=lambda r: r["prompt_source"] == "typed")
FAKE_SUPPLY = types.SimpleNamespace(REMINDER=re.compile(r"<system-reminder>.*?</system-reminder>", re.S),
                                    text_of=_text_of)


def _user(sid, ts, text, source="typed"):
    return {"type": "user", "sessionId": sid, "timestamp": ts, "promptSource": source, "entrypoint": "cli",
            "message": {"role": "user", "content": text}}


def _asst(sid, ts, mid, model):
    return {"type": "assistant", "sessionId": sid, "timestamp": ts,
            "message": {"id": mid, "model": model, "role": "assistant", "content": [{"type": "text", "text": "ok"}]}}


def test_record_recovery_fills_e1_and_requested(tmp_path, capsys):
    w = make_world(tmp_path, with_p17d=False)
    claude = tmp_path / "claude"
    (claude / "proj1").mkdir(parents=True)
    (claude / "proj2").mkdir()
    wjsonl(claude / "proj1" / "a.jsonl", [
        _user(S1, "2026-09-05T10:00:00Z", T["t1"] + "<system-reminder>hook " + SENTINEL + "</system-reminder>"),
        _asst(S1, "2026-09-05T10:00:05Z", "m1", "claude-sonnet-5-5"),
        _user(S1, "2026-09-05T10:01:00Z", [{"type": "text", "text": T["t2"]}]),
        _asst(S1, "2026-09-05T10:01:05Z", "m3", "claude-opus-5-5"),
        _user(S1, "2026-09-05T10:02:00Z", T["g_t1"], source="system"),
    ])
    wjsonl(claude / "proj2" / "b.jsonl", [
        _user(S2, "2026-09-06T10:00:00Z", T["t1"]),             # later record of the same text: loses
        _asst(S2, "2026-09-06T10:00:05Z", "m2", "claude-opus-5-5"),
        _user(S1, "2026-09-10T10:00:00Z", T["g_t1"]),           # the GE3 item's own (session, timestamp)
        _asst(S1, "2026-09-10T10:00:04Z", "m4", "claude-sonnet-5-5"),
    ])
    ledger = wjsonl(tmp_path / "proxy_calls.jsonl", [{"msg_id": "m1", "requested_model": "claude-haiku-4-5"},
                                                     {"msg_id": "m9", "requested_model": "claude-opus-5-5"}])
    files = {k: w["files"][k] for k in ("ge3_items", "ge3_split", "ge3_opus_labels", "e1_tasks", "e1_opus_labels")}
    w2 = dict(w, files=files)
    argv = attach_argv(w2, extra=["--claude-root", str(claude), "--gt-scripts", str(tmp_path / "unused"),
                                  "--ledger", str(ledger)])
    a = A.build_parser().parse_args(argv)
    pa = A._load_script("p17d_attach")
    counts = A.run_attach(a, mods={"gt": FAKE_GT, "supply": FAKE_SUPPLY, "pa": pa})
    rows = {r["text"]: r for r in read_out(w["out"], A.ITEMS)}
    t1, t2, g = rows[T["t1"]], rows[T["t2"]], rows[T["g_t1"]]
    assert (t1["session_id"], t1["timestamp"], t1["requested"]) == (S1, "2026-09-05T10:00:00Z", "haiku")
    assert (t2["session_id"], t2["timestamp"], t2["requested"]) == (S1, "2026-09-05T10:01:00Z", "opus")
    assert t1["group"] == S1
    assert (g["session_id"], g["timestamp"], g["requested"]) == (S1, "2026-09-10T10:00:00Z", "sonnet")
    assert counts["per_source"]["e1_tune"]["requested_basis"] == {"ledger": 1}
    assert counts["per_source"]["e1_ho"]["requested_basis"] == {"transcript_served": 1}
    assert counts["per_source"]["ge3_tune"]["requested_basis"] == {"transcript_served": 1}
    assert counts["per_source"]["ge3_ho"]["requested_basis"] == {"none": 3}
    assert counts["record_recovery"] == {"record_joined": 3, "session_from_record": 2, "timestamp_from_record": 2}
    assert counts["record_index"] == {"transcript_files": 2, "records": 4, "unreadable_file": 0}
    # the recovered E1 items now pass the manifest's time filter and their labels join
    out_rows = read_out(w["out"], A.ITEMS)
    man, _ = D.build_manifest(out_rows, [], ctx=S.ScrubContext(), t0=D.parse_utc(D.T0_DEFAULT),
                              cutoff=D.parse_utc(D.CUTOFF_DEFAULT), legacy={"opus": read_out(w["out"], A.LEG_OPUS)})
    assert man["counts"]["n_no_timestamp_dropped"] == 0
    assert man["counts"]["n_unmapped_labels"]["opus"] == 0
    assert len(out_rows) == 6 and man["counts"]["n_existing_labels"]["opus"] == 5  # g-nolab has no label


def test_record_recovery_refuses_bad_helper_pins(tmp_path, capsys):
    w = make_world(tmp_path, with_p17d=False)
    gts = tmp_path / "gts"
    gts.mkdir()
    (gts / "supply.py").write_text("X = 1\n")
    (gts / "gt500.py").write_text("Y = 1\n")
    claude = tmp_path / "claude"
    claude.mkdir()
    assert A.main(attach_argv(w, extra=["--claude-root", str(claude), "--gt-scripts", str(gts)])) == 2
    assert "pinned" in capsys.readouterr().err
    assert not (w["out"] / A.ITEMS).exists()


# ---------------------------------------------------------------------------------------------
# denylist / .env: counts and sha only
# ---------------------------------------------------------------------------------------------

def test_denylist_and_env_counts_only(tmp_path, capsys):
    w = make_world(tmp_path, with_p17d=False)
    den = tmp_path / "denylist.txt"
    den.write_text("# comment\nprojectcodename\n")
    env = tmp_path / ".env"
    secret = "zz" + SENTINEL + "secretvalue"
    env.write_text("API_KEY=%s\nOTHER=plainvalue123\nSHORT_TOKEN=abc\n" % secret)
    assert A.main(attach_argv(w, extra=["--denylist", str(den), "--env-file", str(env)])) == 0
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["denylist"]["entries"] == 1 and doc["env"]["qualifying_values"] == 1
    assert doc["env"]["sha256"] == S.sha256_file(env)
    assert secret not in out and "projectcodename" not in out
    for name in (A.COUNTS, A.LEG_OPUS, A.LEG_SONNET, A.ITEMS):
        assert secret not in (w["out"] / name).read_text()


# ---------------------------------------------------------------------------------------------
# export-real
# ---------------------------------------------------------------------------------------------

def _manifest_for(w: dict) -> Path:
    rows = read_out(w["out"], A.ITEMS)
    man, _ = D.build_manifest(rows, [], ctx=S.ScrubContext(), t0=D.parse_utc(D.T0_DEFAULT),
                              cutoff=D.parse_utc(D.CUTOFF_DEFAULT))
    mdir = w["cls"] / "dev" / "m1"
    mdir.mkdir()
    p = mdir / "dev_manifest.json"
    p.write_text(json.dumps(man))
    return p


def test_export_real(tmp_path, capsys, item_key):
    w = make_world(tmp_path, item_key)
    assert A.main(attach_argv(w)) == 0
    mpath = _manifest_for(w)
    man = json.loads(mpath.read_text())
    out = w["cls"] / "dev" / "real_dev_items.jsonl"
    argv = ["export-real", "--cls-root", str(w["cls"]), "--items", str(w["out"] / A.ITEMS),
            "--manifest", str(mpath), "--out", str(out)]
    capsys.readouterr()
    assert A.main(argv) == 0
    cap = capsys.readouterr()
    assert_private_stdout(cap.out + cap.err, out, tmp_path)
    rows = read_out(out.parent, out.name)
    real = {x["item_id"]: x for x in man["items"] if x["kind"] == "real"}
    assert len(rows) == len(real) == json.loads(cap.out)["n_real"]
    for r in rows:
        assert set(r) == {"item_id", "text", "group"} and r["group"] == real[r["item_id"]]["group"]
        assert S.text_sha(r["text"]) == real[r["item_id"]]["text_sha"]
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert A.main(argv) == 2                                     # never overwritten
    # PINS-RUN: a manifest without a matching entry refuses; with one, it passes
    pins = w["cls"] / "pins_run.jsonl"
    out2 = w["cls"] / "dev" / "real2.jsonl"
    argv2 = argv[:-1] + [str(out2), "--pins-run", str(pins)]
    assert A.main(argv2) == 2 and not out2.exists()
    P.append_run_entry(str(pins), "dev_manifest", [mpath])
    assert A.main(argv2) == 0
    # a manifest item missing from items.jsonl refuses
    man["items"].append(dict(next(iter(real.values())), item_id="ffffffffffffffff"))
    bad = w["cls"] / "dev" / "m1" / "bad_manifest.json"
    bad.write_text(json.dumps(man))
    out3 = w["cls"] / "dev" / "real3.jsonl"
    assert A.main(argv[:5] + ["--manifest", str(bad), "--out", str(out3)]) == 2
    assert "lacks 1 manifest items" in capsys.readouterr().err
    assert not out3.exists()


# ---------------------------------------------------------------------------------------------
# review repairs: unlabelled items kept, label join modes, timestamp refusal, sizes, E2 splits, modes
# ---------------------------------------------------------------------------------------------

def synth_items(n: int, n_unlabelled: int, tmp: Path):
    """n E2-shaped real items over many sessions; the first n_unlabelled have no Opus label."""
    rows, labs = [], []
    for i in range(n):
        t = "synthetic prompt number %d about topic %s %s" % (i, "abcdefghij"[i % 10] * 3, SENTINEL)
        rows.append({"id": "i%d" % i, "split": "calibration" if i % 2 else "heldout", "text": t,
                     "session_id": "%s-s%d" % (SM, i // 2), "timestamp": "2026-08-%02dT00:%02d:00Z" % (1 + i % 20, i % 60)})
        if i >= n_unlabelled:
            labs.append({"id": "i%d" % i, "tier": "opus" if i % 3 == 0 else "sonnet"})
    return wjsonl(tmp / "sample.jsonl", rows), wjsonl(tmp / "labs.jsonl", labs)


def test_unlabelled_items_reach_the_frozen_primary_missing_guard(tmp_path, capsys):
    """The old behaviour DROPPED items without an Opus label, so the manifest saw 0 percent missing and the
    frozen floor (primary truth missing > 5 percent, PREREG 1.3) could never fire. Now ~10 percent is visible."""
    sample, labs = synth_items(100, 10, tmp_path)
    (tmp_path / "cls" / "dev").mkdir(parents=True)
    out = tmp_path / "cls" / "dev" / "a"
    argv = ["attach", "--cls-root", str(tmp_path / "cls"), "--out", str(out), "--e2-sample", str(sample),
            "--e2-opus-labels", str(labs)]
    assert A.main(argv) == 0
    capsys.readouterr()
    rows = read_out(out, A.ITEMS)
    assert len(rows) == 100
    assert len(read_out(out, A.LEG_OPUS)) == 90                      # no legacy row for an unlabelled item
    counts = json.loads((out / A.COUNTS).read_text())
    assert sum(v["n_no_opus_label"] for v in counts["per_source"].values()) == 10
    man, _ = D.build_manifest(rows, [], ctx=S.ScrubContext(), t0=D.parse_utc(D.T0_DEFAULT),
                              cutoff=D.parse_utc(D.CUTOFF_DEFAULT), legacy={"opus": read_out(out, A.LEG_OPUS)})
    real = [x for x in man["items"] if x["kind"] == "real"]
    assert len(real) == 100
    # the guard computation of cls_dev_run (~986-990), with the primary truth = the joined Opus label
    n_prim_real = sum(1 for x in real if x["truth"]["opus"])
    floor_counts = dict(man["counts"], n_truth_opus=sum(1 for x in real if x["truth"]["opus"] == "opus"),
                        primary_missing_share=(1 - n_prim_real / len(real)) if real else 1.0)
    assert abs(floor_counts["primary_missing_share"] - 0.10) < 0.02
    floor = D.validity_floor(floor_counts)
    assert "primary_truth_missing_above_5_percent" in floor["failed"] and floor["status"] == "NOT_INFORMATIVE"


def test_label_join_modes_and_wrong_join(tmp_path, capsys):
    sample, _ = synth_items(20, 0, tmp_path)
    rows = read_rows_jsonl(sample)
    sha_labels = wjsonl(tmp_path / "sha_labs.jsonl", [{"text_sha": S.text_sha(r["text"]), "session_id": r["session_id"],
                                                        "truth": "opus"} for r in rows[:15]])
    key_labels = wjsonl(tmp_path / "key_labs.jsonl", [{"id": r["id"], "truth": "opus"} for r in rows[:15]])
    # text_sha mode: 15 of 20 join, 5 stay unlabelled
    d = A.Counter()
    items = A.read_e2(sample, sha_labels, "id", "truth", d, "text_sha")
    assert len(items) == 20 and sum(1 for x in items if x["opus"] == "opus") == 15
    # key mode on the same items
    items = A.read_e2(sample, key_labels, "id", "truth", A.Counter(), "key")
    assert len(items) == 20 and sum(1 for x in items if x["opus"] == "opus") == 15
    # a wrong join (sha labels read by id, id labels read by sha) loses no item and shows up as n_no_opus_label
    for lab, mode in ((sha_labels, "key"), (key_labels, "text_sha")):
        d = A.Counter()
        items = A.read_e2(sample, lab, "id", "truth", d, mode)
        assert len(items) == 20 and all(x["opus"] is None for x in items)
    cls = tmp_path / "cls"
    (cls / "dev").mkdir(parents=True)
    out = cls / "dev" / "wrong"
    assert A.main(["attach", "--cls-root", str(cls), "--out", str(out), "--e2-sample", str(sample),
                   "--e2-opus-labels", str(key_labels), "--e2-label-join", "text_sha"]) == 0
    counts = json.loads((out / A.COUNTS).read_text())
    assert counts["n_items"] == 20 and counts["label_join"]["e2"] == "text_sha"
    assert sum(v["n_no_opus_label"] for v in counts["per_source"].values()) == 20
    assert counts["dropped_by_source"]["e2"] == {"label_row_key_missing": 15}
    assert read_out(out, A.LEG_OPUS) == []


def test_ge3_text_sha_join(tmp_path):
    w = make_world(tmp_path, with_p17d=False)
    f = w["files"]
    labs = wjsonl(tmp_path / "ge3_sha.jsonl", [{"text_sha": S.text_sha(T["g_t1"]), "truth": "opus"},
                                               {"text_sha": S.text_sha(T["g_h1"]), "truth": "sonnet"}])
    items = A.read_ge3(f["ge3_items"], f["ge3_split"], labs, None, A.Counter(), "text_sha")
    assert sorted((x["native"], x["opus"]) for x in items) == [("g-h1", "sonnet"), ("g-h2", None), ("g-nolab", None),
                                                               ("g-t1", "opus")]


def read_rows_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def test_source_set_without_timestamps_refused(tmp_path, capsys):
    w = make_world(tmp_path, with_p17d=False)
    argv = attach_argv(w, allow_e1=False)
    assert A.main(argv) == 2
    cap = capsys.readouterr()
    assert "Refused: source set e1_ho has no timestamps (provide record recovery flags)" in cap.err
    assert cap.out == "" and not (w["out"] / A.ITEMS).exists()
    assert SENTINEL not in cap.err and SM not in cap.err
    # explicit override, recorded
    assert A.main(attach_argv(w, extra=["--allow-missing-timestamps", "e1_ho"], allow_e1=False)) == 2
    assert "source set e1_tune has no timestamps" in capsys.readouterr().err
    assert A.main(attach_argv(w, extra=["--allow-missing-timestamps", "e1_tune", "--allow-missing-timestamps", "e1_ho"],
                              allow_e1=False)) == 0
    counts = json.loads((w["out"] / A.COUNTS).read_text())
    assert counts["allow_missing_timestamps"] == ["e1_ho", "e1_tune"]
    assert A.main(attach_argv(w, extra=["--allow-missing-timestamps", "nope"], out=w["cls"] / "dev" / "b")) == 2


def test_own_timestamp_without_session_takes_no_foreign_session(tmp_path):
    other = {"session_id": S2, "timestamp": "2026-09-01T00:00:00Z", "msg_id": None, "served_model": None}
    same = {"session_id": S1, "timestamp": "2026-09-02T00:00:00Z", "msg_id": None, "served_model": None}
    mk = lambda ts: {"text": "x y z", "timestamp": ts, "session_id": None, "requested": None, "requested_basis": None}  # noqa: E731
    a_, b_ = mk("2026-09-05T00:00:00Z"), mk("2026-09-02T00:00:00Z")
    c = A.Counter()
    sha = S.text_sha("x y z")
    A.recover([a_], ({}, {sha: other}), None, lambda m: None, c)
    assert a_["session_id"] is None and c["session_not_taken_from_other_record"] == 1 and c["record_joined"] == 0
    A.recover([b_], ({}, {sha: same}), None, lambda m: None, c)
    assert b_["session_id"] == S1 and c["session_from_record"] == 1
    # downstream: no session -> the manifest's unk clustering
    row = {"item_id": "a" * 16, "text": "x y z", "timestamp": a_["timestamp"], "session_id": None, "group": None,
           "source_set": "e1_tune", "batch": None, "digest": None, "requested": None}
    man, _ = D.build_manifest([row], [], ctx=S.ScrubContext(), t0=D.parse_utc(D.T0_DEFAULT),
                              cutoff=D.parse_utc(D.CUTOFF_DEFAULT))
    assert man["counts"]["n_unk_group"] == 1


def test_expected_sizes_warn_not_refuse(tmp_path, capsys, item_key):
    w = make_world(tmp_path, item_key)
    assert A.main(attach_argv(w)) == 0
    cap = capsys.readouterr()
    doc = json.loads(cap.out)
    for s_, n in A.EXPECTED_SIZES.items():
        assert doc["expected_vs_actual"][s_]["expected"] == n
    assert A.EXPECTED_SIZES == {"ge3_tune": 108, "ge3_ho": 234, "e2_cal": 91, "e2_ho": 89, "e1_ho": 20,
                                "e1_tune": 30, "p17d_f": 32, "p17d_g": 119}
    warns = [x for x in cap.err.splitlines() if x.startswith("WARNING")]
    assert len(warns) == 8 and all(SENTINEL not in x and SM not in x for x in warns)


def test_e2_unknown_split_counted_not_mapped(tmp_path):
    sample = wjsonl(tmp_path / "s2.jsonl", [
        {"id": "a", "split": "calibration", "text": T["e_c1"], "truth": "haiku"},
        {"id": "b", "split": "HeldOut", "text": T["e_h1"], "truth": "opus"},
        {"id": "c", "split": "dev2", "text": T["e_nots"], "truth": "opus"},
        {"id": "d", "text": T["t1"], "truth": "opus"}])
    d = A.Counter()
    items = A.read_e2(sample, sample, "id", "truth", d)
    assert sorted((x["native"], x["source_set"]) for x in items) == [("a", "e2_cal"), ("b", "e2_ho")]
    assert d["e2_unknown_split"] == 2


def test_export_real_chmods_existing_parent(tmp_path, capsys, item_key):
    w = make_world(tmp_path, item_key)
    assert A.main(attach_argv(w)) == 0
    mpath = _manifest_for(w)
    parent = w["cls"] / "dev" / "exports"
    parent.mkdir()
    os.chmod(parent, 0o755)
    out = parent / "real.jsonl"
    assert A.main(["export-real", "--cls-root", str(w["cls"]), "--items", str(w["out"] / A.ITEMS),
                   "--manifest", str(mpath), "--out", str(out)]) == 0
    assert stat.S_IMODE(os.stat(parent).st_mode) == 0o700
