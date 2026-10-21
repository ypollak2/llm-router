"""LC-10j: local-check diagnostics and comparable measurements; fakes only."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

from llm_router import model_discovery
from llm_router.commands import local_check as C
from llm_router.local_check import attempts as A, consent, recommend as R, runner, runtimes as T, score as S
from tests.test_lc5_scoring import BANK, FakeAdapter
from tests.test_lc6_cli import MODEL, deps, run


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))


def test_installed_unmeasurable_and_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(model_discovery, "_ollama_url", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(model_discovery, "probe_ollama", lambda: [])
    monkeypatch.setattr(T, "_json_request", lambda *a, **k: {
        "models": [{"name": "fake-embed:1b"}, {"name": "fake-decision:1b"}]})
    endpoints = T.discover(ports=())
    assert len(endpoints) == 1 and endpoints[0].models == []
    assert endpoints[0].unmeasurable == ["fake-embed:1b", "fake-decision:1b"]
    d, _, _ = deps(tmp_path)
    for name in endpoints[0].unmeasurable:
        assert C._pick_model(d, endpoints, name) == (
            f"model '{name}' is installed on ollama http://127.0.0.1:11434 but is not a "
            "chat/completion model (embedding or decision-only), so local-check cannot measure it. "
            "Measurable models: none.")
    assert "was not found" in C._pick_model(d, endpoints, "fake-missing:1b")
    assert "reports no chat models" in C._pick_model(d, endpoints, None)
    endpoints[0].models = ["fake-chat:1b"]
    assert "Measurable models: fake-chat:1b." in C._pick_model(d, endpoints, "fake-embed:1b")


def test_raw_inventory_failure_is_optional(monkeypatch):
    monkeypatch.setattr(model_discovery, "_ollama_url", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(model_discovery, "probe_ollama", lambda: ["fake-chat:1b"])
    def fail(*args, **kwargs):
        raise OSError("fake failure")
    monkeypatch.setattr(T, "_json_request", fail)
    endpoint, = T.discover(ports=())
    assert endpoint.models == ["fake-chat:1b"] and endpoint.unmeasurable == []


def test_explicit_ollama_unmeasurable(monkeypatch):
    monkeypatch.setattr(T, "identify", lambda *a, **k: "ollama")
    monkeypatch.setattr(T, "_json_request", lambda *a, **k: {
        "models": [{"name": "fake-chat:1b"}, {"name": "fake-decision:1b"}, {"name": "fake-embed:1b"}]})
    monkeypatch.setattr(model_discovery, "_is_completion_model", lambda n: n == "fake-chat:1b")
    endpoint, = T.discover("http://127.0.0.1:11434")
    assert endpoint.models == ["fake-chat:1b"]
    assert endpoint.unmeasurable == ["fake-decision:1b", "fake-embed:1b"]


def test_no_judge_scored_log_and_status(tmp_path):
    d, sink, seen = deps(tmp_path, recommend=lambda *a, **k: [])
    assert run(["--yes", "--no-judge"], d) == C.EXIT_NOT_PASSED
    entries = A.AttemptLog(tmp_path / "runs").entries()
    assert any(e["state"] == "scored" and not e["consumed"] for e in entries)
    assert seen.judge.calls == 0
    assert "Proceed" not in sink.text and sink.text.count("consent: given by --yes") == 1
    d, sink, _ = deps(tmp_path)
    assert run(["status"], d) == 0
    assert "0 of 2 judged draws used" in sink.text and "1 programmatic-only" in sink.text


def data(version):
    return {"generated": "2026-10-20", "entries": [{
        "family": "fake-family", "ids": {"ollama": "fake-chat:1b"},
        "min_ram_gb": 1, "download_size_gb": 1, "licence": "fake-licence",
        "status": "measured", "bench": {"bank_version": version, "pooled": {"k": 60, "n": 70}},
    }]}


@pytest.mark.parametrize("version", [None, 2, "PREREG-Q37 (pre-bank; different item set)"])
def test_incomparable_counts_hidden(tmp_path, version):
    source = data(version)
    original = deepcopy(source)
    recs = R.recommend({}, "ollama", data=source, bank_version=1, today=date(2026, 10, 21))
    row, = recs
    assert (row.status, row.bench, row.pooled_rate, row.untested_on) == ("untested", None, None, "v1")
    assert source == original
    d, _, _ = deps(tmp_path)
    d.load_bank = lambda: SimpleNamespace(version=1)
    d.recommend = lambda *a, **k: recs
    text = "\n".join(C._recommend_lines(d, SimpleNamespace(verdict=S.FAIL), MODEL, "ollama"))
    assert "untested on bank v1 (no number shown)" in text
    assert "60" not in text and "70" not in text


@pytest.mark.parametrize("version", [1, "v1", "V1"])
def test_comparable_counts_visible(tmp_path, version):
    source = data(version)
    d, _, _ = deps(tmp_path)
    d.load_bank = lambda: SimpleNamespace(version=1)
    d.recommend = lambda hw, rt, model, res, **kw: R.recommend(hw, rt, data=source, **kw)
    text = "\n".join(C._recommend_lines(d, SimpleNamespace(verdict=S.FAIL), MODEL, "ollama"))
    assert "measured 60/70 on v1" in text


def test_unknown_bank_demotes_and_sort_uses_size():
    source = data("v1")
    small = deepcopy(source["entries"][0])
    small.update(ids={"ollama": "fake-small:1b"}, status="untested", bench=None,
                 min_ram_gb=0.5, download_size_gb=0.5)
    source["entries"].append(small)
    rows = R.recommend({}, "ollama", data=source)
    assert [r.model_id for r in rows] == ["fake-small:1b", "fake-chat:1b"]
    assert rows[1].untested_on == "" and rows[1].bench is None


def test_bank_load_failure_and_legacy_recommender(tmp_path):
    d, _, _ = deps(tmp_path)
    def fail():
        raise ValueError("fake bank error")
    d.load_bank = fail
    seen = []
    def legacy(hw, rt, model, result):
        seen.append(rt)
        return []
    d.recommend = legacy
    assert C._recommend_lines(d, SimpleNamespace(verdict=S.FAIL), MODEL, "ollama")
    assert seen == ["ollama"]


def test_dry_run_estimates_without_generating_or_writing(tmp_path):
    d, sink, seen = deps(tmp_path)
    assert run(["--dry-run"], d) == 0
    timing = next(line for line in sink.out if line.startswith("Time:"))
    assert "estimate from the" in timing and "not measured" in timing and "items 2-3" in timing
    assert seen.adapter.calls == seen.judge.calls == 0
    assert not (tmp_path / "runs").exists() and not (tmp_path / "elig.json").exists()


@pytest.mark.parametrize("size,seconds", [("4", 8), ("7", 20), ("9", 20), ("16", 40), ("40", 90), ("41", 180), ("1.5", 8)])
def test_size_classes(size, seconds):
    model = f"fake-chat:{size}B"
    assert C.estimate_run_seconds(model, 2, {"gpu": "apple_unified"})[0] == 2 * seconds
    for gpu in (None, ""):
        estimate, label = C.estimate_run_seconds(model, 2, {"gpu": gpu})
        assert estimate == 2 * seconds
        assert label.endswith(" (GPU unknown)")
    assert C.estimate_run_seconds(model, 2, {"gpu": "none"})[0] == 10 * seconds
    assert C.estimate_run_seconds("fake-chat:latest", 2, {}) == (None, "")


def test_rate_ignores_cold_record():
    cold = {"status": "ok", "cold": True, "elapsed_s": 1000, "first_token_s": 900,
            "prompt_chars": 100, "completion_tokens": 20}
    warm = {**cold, "cold": False, "elapsed_s": 4, "first_token_s": 2}
    rate = runner.measure_rate(iter([cold, warm, warm]))
    assert rate.n == 2 and rate.mean_item_s == 4
    assert rate.prefill_chars_per_s == 50 and rate.decode_tok_per_s == 10
    assert runner.measure_rate([cold]).mean_item_s == 1000
    assert runner.measure_rate([]) is None
    assert runner.measure_rate([{"status": "error"}, warm]).mean_item_s == 4


@pytest.mark.parametrize("text", ["Details\nProceed? [y/N]", "Proceed with a local-only run? [y/N]"])
def test_yes_suppresses_question(text):
    lines = []
    assert consent.ask_consent(text, yes=True, print_fn=lines.append)
    assert "[y/N]" not in "\n".join(lines)
    assert lines[-1] == "consent: given by --yes"
    lines.clear()
    assert consent.ask_consent(text, input_fn=lambda _: "y", print_fn=lines.append)
    assert lines == [text]


def test_judged_yes_output(tmp_path):
    d, sink, _ = deps(tmp_path)
    assert run(["--yes"], d) == 0
    assert "[y/N]" not in sink.text and sink.text.count("consent: given by --yes") == 1


@pytest.mark.parametrize("args", [["--help"], ["-h"], ["run", "-h"], ["run", "--help"]])
def test_help_lists_subcommands(tmp_path, args):
    d, sink, seen = deps(tmp_path)
    assert run(args, d) == 0
    assert "--dry-run" in sink.text and "subcommands:\n  run      measure" in sink.text
    for name in ("status", "explain", "enable", "disable"):
        assert f"\n  {name}" in sink.text
    assert seen.discover_calls == [] and seen.adapter.calls == 0


def test_early_stop_header_and_table(tmp_path):
    bad = {i.id: "progfail" for i in BANK.draw(1)[:30]}
    d, sink, _ = deps(tmp_path, FakeAdapter(bad), recommend=lambda *a, **k: [])
    assert run(["--yes"], d) == C.EXIT_NOT_PASSED
    entry = next(e for e in A.AttemptLog(tmp_path / "runs").entries() if e.get("state") == "scored")
    n_scored = sum(row["present"] for row in entry["rows"])
    assert n_scored < 70
    expected = (f"== Verdict: not passed (NOT_PASSED) -- stopped early after {n_scored} of 70 items "
                f"({entry['k']} passed); pass bar: k >= 56, Wilson 95% lower bound >= 0.60 ==")
    assert expected in sink.text
    assert "Wilson LB" in sink.text and "pooled" in sink.text
