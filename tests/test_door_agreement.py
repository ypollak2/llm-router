"""scripts/door_agreement.py (PLAN v16 P1.6 task 0).

Every prompt here is synthetic. The real corpus is private and never enters a test.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "door_agreement.py"


@pytest.fixture(scope="module")
def da():
    spec = importlib.util.spec_from_file_location("_door_agreement_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve the defining module here
    spec.loader.exec_module(mod)
    return mod


# Ten prompts, three fake sites with planted differences:
#   a vs b: task_type differs on p1,p2,p3 (3); tier differs on p4 (1)
#   a vs c: task_type differs on p5 (1) and p9 where c declines (None) -> 2
#           tier differs on p9 only (1)
#   b vs c: task_type differs on p1,p2,p3,p5,p9 (5); tier on p4,p9 (2)
#   any site differs on task_type: p1,p2,p3,p5,p9 (5); on tier: p4,p9 (2)
PROMPTS = [f"p{i}" for i in range(10)]
A = {p: ("code", "simple") for p in PROMPTS}
B = dict(A, p1=("query", "simple"), p2=("analyze", "simple"), p3=("query", "simple"),
         p4=("code", "complex"))
C = dict(A, p5=("research", "simple"), p9=(None, None))


def _site(table):
    return lambda text: table[text]


def test_known_disagreements_give_exact_counts(da):
    r = da.measure(PROMPTS, {"a": _site(A), "b": _site(B), "c": _site(C)})
    assert r["n"] == 10
    assert r["pairs"]["a|b"]["task_type_disagree"] == 3
    assert r["pairs"]["a|b"]["tier_disagree"] == 1
    assert r["pairs"]["a|c"]["task_type_disagree"] == 2
    assert r["pairs"]["a|c"]["tier_disagree"] == 1
    assert r["pairs"]["b|c"]["task_type_disagree"] == 5
    assert r["pairs"]["b|c"]["tier_disagree"] == 2
    assert r["pairs"]["a|b"]["rate"] == 0.3
    assert r["pairs"]["a|b"]["wilson95"] == da.wilson95(3, 10)
    assert r["all_sites_disagree"] == 5
    assert r["all_sites_tier_disagree"] == 2
    assert r["per_site"]["c"]["task_type_counts"][da.NONE_LABEL] == 1


def test_identical_sites_disagree_zero(da):
    r = da.measure(PROMPTS, {"a": _site(A), "a2": _site(A)})
    assert r["pairs"]["a|a2"]["task_type_disagree"] == 0
    assert r["all_sites_disagree"] == 0
    assert r["pairs"]["a|a2"]["wilson95"][0] == 0.0


@pytest.mark.parametrize(
    "k,n,expected",
    [
        (5, 10, [0.236593, 0.763407]),  # textbook Wilson 95%: 0.2366 - 0.7634
        (0, 10, [0.0, 0.277533]),
        (10, 10, [0.722467, 1.0]),
    ],
)
def test_wilson_known_cases(da, k, n, expected):
    assert da.wilson95(k, n) == pytest.approx(expected, abs=1e-6)


def test_wilson_rejects_empty(da):
    with pytest.raises(ValueError):
        da.wilson95(0, 0)


def test_measure_rejects_empty(da):
    with pytest.raises(ValueError):
        da.measure([], {"a": _site(A), "b": _site(B)})


def test_cli_exits_nonzero_on_empty_corpus(tmp_path):
    corpus = tmp_path / "empty.jsonl"
    corpus.write_text("", encoding="utf-8")
    out = tmp_path / "out.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--corpus", str(corpus), "--sites", "hook,gateway",
         "--out", str(out)],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, HOME=str(tmp_path), LLM_ROUTER_HOME=str(tmp_path / ".llm-router")),
    )
    assert proc.returncode != 0
    assert "empty corpus" in proc.stderr
    assert not out.exists()


def test_cli_rejects_unknown_site(tmp_path):
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(json.dumps({"i": 0, "text": "write a function"}) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--corpus", str(corpus), "--sites", "hook,nope",
         "--out", str(tmp_path / "o.json")],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, HOME=str(tmp_path), LLM_ROUTER_HOME=str(tmp_path / ".llm-router")),
    )
    assert proc.returncode == 2


def test_cli_real_doors_counts_only_and_no_network(tmp_path):
    """End to end on the real hook, gateway and proxy with synthetic prompts: the output is
    counts and hashes only, and the run completes under the network guard."""
    texts = [
        "write a python function that parses a csv file and returns rows",
        "why is the sky blue",
        "analyze the trade-offs between postgres and sqlite for a cli tool",
        "hi",
    ]
    corpus = tmp_path / "c.jsonl"
    corpus.write_text("".join(json.dumps({"i": i, "text": t}) + "\n" for i, t in enumerate(texts)),
                      encoding="utf-8")
    out = tmp_path / "o.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--corpus", str(corpus), "--sites", "hook,gateway,proxy",
         "--out", str(out)],
        capture_output=True, text=True, timeout=180,
        env=dict(os.environ, HOME=str(tmp_path), LLM_ROUTER_HOME=str(tmp_path / ".llm-router")),
    )
    assert proc.returncode == 0, proc.stderr
    # Nothing even tried to connect: every no-network switch held.
    assert "refused during measurement" not in proc.stderr
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["n"] == 4
    assert d["network_refusals"] == 0
    assert d["labels"]["none_label"] == "<none>"
    assert set(d["pairs"]) == {"hook|gateway", "hook|proxy", "gateway|proxy"}
    p = d["pairs"]["hook|gateway"]
    assert 0 <= p["task_type_disagree"] <= 4 and 0 <= p["tier_disagree"] <= 4
    assert len(d["corpus_sha256"]) == 64
    assert d["no_llm_switches"]["hook"]["env"]["LLM_ROUTER_CLASSIFY_LOCAL_ONLY"] == "true"
    assert "LLM_ROUTER_OLLAMA_MODEL" in d["no_llm_switches"]["hook"]["env"]
    # gateway and proxy both classify with GATEWAY_POLICY; short prompts are not
    # truncated by tier_text, so the two must agree here.
    assert d["pairs"]["gateway|proxy"]["task_type_disagree"] == 0
    dumped = out.read_text(encoding="utf-8") + proc.stdout + proc.stderr
    for t in texts[:3]:
        assert t not in dumped


def test_freeze_writes_0600_and_refuses_overwrite(da, tmp_path, monkeypatch, capsys):
    import collections

    fake = types.ModuleType("measure_low_signal_rate")
    fake.collect = lambda: (["synthetic one", "synthetic two"], collections.Counter(x=3))
    monkeypatch.setitem(sys.modules, "measure_low_signal_rate", fake)
    path = tmp_path / "sub" / "corpus.jsonl"
    assert da.freeze_corpus(path) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert da.read_corpus(path) == ["synthetic one", "synthetic two"]
    printed = json.loads(capsys.readouterr().out)
    assert printed["n"] == 2 and printed["sha256"] == da.file_sha256(path)
    assert "synthetic" not in json.dumps(printed)
    before = path.read_bytes()
    assert da.freeze_corpus(path) == 3
    assert path.read_bytes() == before


# Shared-labels fixture: X can say "coordinate", Y can say "research"; neither
# can say the other's, so those prompts are left out of the shared rate.
SX = ["code", "query", "code", "coordinate"]
SY = ["code", "code", "query", "research"]


def _labels(seq):
    table = {f"s{i}": (t, "simple") for i, t in enumerate(seq)}
    return lambda text: table[text]


def test_shared_labels_rate_excludes_site_only_labels(da):
    prompts = [f"s{i}" for i in range(4)]
    r = da.measure(prompts, {"x": _labels(SX), "y": _labels(SY)})
    p = r["pairs"]["x|y"]
    assert p["task_type_disagree"] == 3
    sh = p["shared_labels_task_type"]
    assert sh["vocab"] == ["code", "query"]
    assert (sh["n"], sh["disagree"]) == (3, 2)
    assert sh["wilson95"] == da.wilson95(2, 3)
    assert p["shared_labels_tier"]["n"] == 4 and p["shared_labels_tier"]["disagree"] == 0


def test_shared_labels_excludes_none(da):
    r = da.measure(PROMPTS, {"a": _site(A), "c": _site(C)})
    sh = r["pairs"]["a|c"]["shared_labels_task_type"]
    assert sh["vocab"] == ["code"]
    assert (sh["n"], sh["disagree"]) == (8, 0)


def test_bucket_split_counts(da):
    prompts = [f"s{i}" for i in range(4)]
    r = da.measure(prompts, {"x": _labels(SX), "y": _labels(SY)},
                   buckets=["old", "old", "new", "new"])
    split = r["pairs"]["x|y"]["task_type_by_bucket"]
    assert (split["old"]["n"], split["old"]["disagree"]) == (2, 1)
    assert (split["new"]["n"], split["new"]["disagree"]) == (2, 2)
    assert r["bucket_counts"] == {"new": 2, "old": 2}


def test_network_guard_refuses_connect_and_dns_past_except_exception(da):
    import socket

    da.REFUSALS.clear()
    undo = da._install_network_guard()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(da.NetworkRefused):
            try:
                sock.connect(("127.0.0.1", 9))
            except Exception:  # noqa: BLE001 — a door's fallback must not swallow it
                pytest.fail("NetworkRefused was caught by except Exception")
        with pytest.raises(da.NetworkRefused):
            socket.getaddrinfo("example.com", 443)
        with pytest.raises(da.NetworkRefused):
            socket.create_connection(("127.0.0.1", 9))
        assert sorted(da.REFUSALS) == sorted(["connect", "getaddrinfo", "create_connection"])
    finally:
        undo()
        if sock is not None:
            sock.close()
        da.REFUSALS.clear()
    assert socket.getaddrinfo("127.0.0.1", 9)  # restored


def test_write_exclusive_leaves_nothing_on_a_crash(da, tmp_path):
    path = tmp_path / "corpus.jsonl"

    def lines():
        yield "one\n"
        raise RuntimeError("crash mid-write")

    with pytest.raises(RuntimeError):
        da.write_exclusive(path, lines())
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_freeze_dates_sidecar_is_aligned_and_textless(da, tmp_path, monkeypatch, capsys):
    import collections

    rec = types.SimpleNamespace
    day = 86400
    fake = types.ModuleType("measure_low_signal_rate")
    fake.collect_records = lambda: ([
        rec(text="synthetic old", ts=1_758_585_600 + 3600),          # 2025-09-23 UTC
        rec(text="synthetic new", ts=1_758_585_600 + 2 * day),        # 2025-09-25
        rec(text="synthetic new", ts=1_758_585_600 + 3 * day),        # later repeat
        rec(text="synthetic nots", ts=None),
    ], collections.Counter())
    monkeypatch.setitem(sys.modules, "measure_low_signal_rate", fake)
    corpus = tmp_path / "c.jsonl"
    texts = ["synthetic new", "synthetic old", "synthetic gone", "synthetic nots"]
    corpus.write_text("".join(json.dumps({"i": i, "text": t}) + "\n" for i, t in enumerate(texts)),
                      encoding="utf-8")
    side = tmp_path / "dates.jsonl"
    assert da.freeze_dates(corpus, side) == 0
    assert stat.S_IMODE(side.stat().st_mode) == 0o600
    raw = side.read_text(encoding="utf-8")
    assert "synthetic" not in raw and "synthetic" not in capsys.readouterr().out
    dates = da.read_dates(side, da.file_sha256(corpus), 4)
    assert dates == ["2025-09-25", "2025-09-23", None, None]
    assert da.date_buckets(dates, "2025-09-23") == [">2025-09-23", "<=2025-09-23", "undated", "undated"]
    assert da.freeze_dates(corpus, side) == 3
    with pytest.raises(ValueError):
        da.read_dates(side, "0" * 64, 4)


@pytest.mark.parametrize("when", ["load", "classify"])
def test_unswallowed_refusal_exits_4_with_no_output(da, tmp_path, monkeypatch, when):
    """A connection nobody catches propagates NetworkRefused (a BaseException);
    main() must turn that into exit 4 with no output, not a traceback."""
    import socket

    def connect():
        socket.create_connection(("127.0.0.1", 9))

    def load():
        if when == "load":
            connect()
        return lambda text: (connect(), ("code", "simple"))[1]

    monkeypatch.setitem(da.SITES, "net", da.Site("net", "test site that connects", load))
    monkeypatch.setitem(da.SITES, "quiet", da.Site("quiet", "test site", lambda: _site(A)))
    corpus = tmp_path / "c.jsonl"
    corpus.write_text("".join(json.dumps({"i": i, "text": p}) + "\n" for i, p in enumerate(PROMPTS)),
                      encoding="utf-8")
    out = tmp_path / "o.json"
    da.REFUSALS.clear()
    try:
        assert da.main(["--corpus", str(corpus), "--sites", "net,quiet", "--out", str(out)]) == 4
        assert da.REFUSALS
    finally:
        da.REFUSALS.clear()
    assert not out.exists()
    assert socket.getaddrinfo("127.0.0.1", 9)  # guard undone after the run
