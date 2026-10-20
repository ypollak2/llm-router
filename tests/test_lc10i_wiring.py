"""LC-10i: the wiring of `llm-router local-check` to routing (LC-8), retention (LC-7), display (LC-7), recommend (LC-9) and the quota re-check (LC-4).

Fakes only: FakeAdapter / FakeJudge from the LC-5 tests, injected Deps, LLM_ROUTER_HOME in the test dir. No model, no network.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router.commands import local_check as LCC
from llm_router.local_check import retention, routing, runtimes, score as S
from tests.test_lc5_scoring import BANK, FakeAdapter
from tests.test_lc6_cli import MODEL, deps, run


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))


def passing() -> FakeAdapter:
    return FakeAdapter({i.id: "judgefail" for i in BANK.draw(1)[:10]})        # 60/70


def failing() -> FakeAdapter:
    return FakeAdapter({i.id: "progfail" for i in BANK.draw(1)[:30]})


# ------------------------------------------------------------------------------------------ 1. enable / disable
def test_enable_and_disable_switch_one_lane_with_the_inventory_name(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path, inventory=lambda: [MODEL])
    assert run(["enable", "commit_message"], d) == LCC.EXIT_OK
    assert routing.is_enabled(MODEL, "commit_message") and not routing.is_enabled(MODEL, "pr_description")
    assert routing.entry(MODEL, "commit_message")["source"] == "manual"
    assert f"routing: on for commit_message on {MODEL}" in sink.text
    d, sink, _ = deps(tmp_path, inventory=lambda: [MODEL])
    assert run(["disable", "commit_message"], d) == LCC.EXIT_OK
    assert not routing.is_enabled(MODEL, "commit_message") and routing.entry(MODEL, "commit_message")["reason"] == "manual"
    assert "routing: off for commit_message" in sink.text


def test_enable_with_model_flag_keeps_the_tag_and_validates_lane_and_model(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path, inventory=lambda: ["a:1b", "b:2b"])
    assert run(["enable", "pr_description", "--model", "qwen3.5:latest"], d) == 0
    assert routing.is_enabled("qwen3.5:latest", "pr_description")
    assert run(["enable", "pr_description"], d) == LCC.EXIT_NO_RUNTIME and "pass --model" in sink.etext      # two models, none chosen
    assert run(["enable", "summarize_output", "--model", MODEL], d) == LCC.EXIT_USAGE                         # not a local-check lane
    assert run(["enable"], d) == LCC.EXIT_USAGE
    d, sink, _ = deps(tmp_path, inventory=lambda: [])
    assert run(["disable", "commit_message"], d) == LCC.EXIT_NO_RUNTIME and "no local model" in sink.etext


def test_enable_after_a_kept_off_lane_flips_it_back(tmp_path: Path) -> None:
    routing.disable(MODEL, "commit_message", "demoted: fallbacks")
    d, _, _ = deps(tmp_path, inventory=lambda: [MODEL])
    assert run(["enable", "commit_message"], d) == 0 and routing.is_enabled(MODEL, "commit_message")
    assert "reason" not in routing.entry(MODEL, "commit_message")


def test_status_shows_routing_state_from_the_display_lines_and_the_switch_file(tmp_path: Path) -> None:
    d, _, _ = deps(tmp_path, passing())
    assert run(["--yes", "--model", MODEL], d) == 0
    routing.disable(MODEL, "pr_description", "manual")
    calls = []
    d, sink, _ = deps(tmp_path)
    real = LCC._resolve(d).status_lines
    d.status_lines = lambda: calls.append(1) or real()
    assert run(["status"], d) == 0
    t = sink.text
    assert calls == [1]                                              # the LC-7 display seam, once; no second renderer
    assert "[routing: on]" in t or "routing: on" in t
    assert f"{MODEL} commit_message: on" in t and f"{MODEL} pr_description: off (manual)" in t
    assert "local-check enable pr_description" in t
    d, sink, _ = deps(tmp_path)
    assert run(["status", "--model", "other:7b"], d) == 0 and "routing switches: none" in sink.text


def test_status_prints_the_demotion_doctor_lines(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path)
    d.routing = SimpleNamespace(routing_path=routing.routing_path, norm_model=routing.norm_model, doctor_lines=lambda path=None: ["local-check: demoted X"])
    assert run(["status"], d) == 0 and "local-check: demoted X" in sink.text


# ------------------------------------------------------------------------------------------ 2. on_pass outputs
def test_pass_turns_both_lanes_on_and_says_so(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path, passing())
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_OK
    for lane in ("commit_message", "pr_description"):
        assert routing.is_enabled(MODEL, lane) and routing.entry(MODEL, lane)["source"] == "local_check"
        assert f"routing: on for {lane} -- disable with `llm-router local-check disable {lane}`" in sink.text


def test_pass_reports_already_on_and_kept_off(tmp_path: Path) -> None:
    routing.enable(MODEL, "commit_message", "manual")
    routing.disable(MODEL, "pr_description", "manual")
    d, sink, _ = deps(tmp_path, passing())
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_OK
    t = sink.text
    assert "routing: already on for commit_message" in t
    assert "routing: kept off for pr_description -- it was disabled; run `llm-router local-check enable pr_description`" in t
    assert not routing.is_enabled(MODEL, "pr_description")


def test_routing_runs_only_for_lanes_this_run_recorded_and_never_on_a_non_pass(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path, failing())
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_NOT_PASSED
    assert not routing.routing_path().exists() and "routing:" not in sink.text
    d, sink, _ = deps(tmp_path / "b", passing(), **{})
    called = []
    d.routing = SimpleNamespace(on_pass=lambda m, jobs, path=None: called.append((m, list(jobs))) or {j: "enabled" for j in jobs})
    assert run(["--yes", "--model", MODEL], d) == 0
    assert called == [(MODEL, ["commit_message", "pr_description"])]          # only recorded local-check lanes: a prereg-only lane never appears


def test_a_routing_file_error_does_not_crash_a_recorded_pass(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path, passing())

    def boom(model, jobs, path=None):
        raise OSError("disk full")
    d.routing = SimpleNamespace(on_pass=boom)
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_OK
    assert "routing: could not update (OSError)" in sink.text and "recorded: commit_message, pr_description" in sink.text


# ------------------------------------------------------------------------------------------ 3. retention.prune
@pytest.mark.parametrize("adapter,code", [(passing, 0), (failing, LCC.EXIT_NOT_PASSED)])
def test_prune_is_called_after_a_completed_run_whatever_the_verdict(tmp_path: Path, adapter, code: int) -> None:
    roots = []
    d, sink, _ = deps(tmp_path, adapter(), prune=lambda root: roots.append(root) or retention.PruneReport())
    assert run(["--yes", "--model", MODEL], d) == code
    assert roots == [tmp_path / "runs"]
    assert "retention" not in sink.text + sink.etext                         # a clean prune is silent


def test_prune_errors_are_reported_and_a_prune_exception_never_fails_the_run(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path, passing(), prune=lambda root: retention.PruneReport(errors=["abc: PermissionError"]))
    assert run(["--yes", "--model", MODEL], d) == 0 and "retention: abc: PermissionError" in sink.etext
    d, sink, _ = deps(tmp_path / "b", passing(), prune=lambda root: 1 / 0)
    assert run(["--yes", "--model", MODEL], d) == 0 and "retention: ZeroDivisionError" in sink.etext


def test_prune_not_called_on_a_dry_run_or_a_declined_consent(tmp_path: Path) -> None:
    roots = []
    d, _, _ = deps(tmp_path, prune=lambda root: roots.append(root))
    run(["--dry-run", "--model", MODEL], d)
    run(["--model", MODEL], d)                                                # non-tty without --yes: declined
    assert roots == []


def test_real_prune_keeps_the_open_run_of_an_invalid_verdict(tmp_path: Path) -> None:
    from tests.test_lc5_scoring import invalid_adapter
    d, _, _ = deps(tmp_path, invalid_adapter())
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_INVALID
    assert any((tmp_path / "runs").iterdir())


# ------------------------------------------------------------------------------------------ 5. recommend on a non-pass
def test_fail_prints_recommendations_measured_first_untested_labelled_pull_text_only(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path, failing())
    d.hardware = lambda: {"total_gb": 48, "usable_gb": 1.0, "gpu": None, "arch": "arm64"}
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_NOT_PASSED
    t = sink.text
    assert "== Models that may fit this machine (runtime ollama, ~38.4 GB usable for a model) ==" in t
    rows = [ln for ln in t.splitlines() if ln.startswith("  ") and "[" in ln and "GB download" in ln]
    assert rows and "measured" in rows[0] and any("untested" in r for r in rows[1:])
    assert "to try it, run yourself: ollama pull " in t and "Nothing is pulled or routed for you" in t


def test_empty_list_prints_the_runtime_reason(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path, failing(), endpoints=[runtimes.Endpoint("vllm", "http://127.0.0.1:8000", [MODEL])])
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_NOT_PASSED
    assert "no verified recommendation for vllm" in sink.text


def test_a_pass_prints_no_recommendations(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path, passing())
    run(["--yes", "--model", MODEL], d)
    assert "may fit this machine" not in sink.text


@pytest.mark.parametrize("verdict,n,shown", [(S.FAIL, 70, True), (S.NOT_PASSED, 40, True), (S.INCONCLUSIVE, 50, True), (S.INCONCLUSIVE, 70, False),
                                              (S.PASS, 70, False), (S.INVALID, 70, False), (S.PROBE, 70, False)])
def test_recommend_trigger_set(tmp_path: Path, verdict: str, n: int, shown: bool) -> None:
    d, _, _ = deps(tmp_path)
    got = LCC._recommend_lines(d, SimpleNamespace(verdict=verdict, n_assigned=n), MODEL, "ollama")
    assert bool(got) is shown


# ------------------------------------------------------------------------------------------ 6. quota re-check between judge batches
def quota_reader(values: list[float]):
    seq = list(values)
    reads = []

    def read():
        reads.append(1)
        return (seq.pop(0) if len(seq) > 1 else seq[0], "ok", 0.0)
    read.reads = reads
    return read


def test_quota_above_95_percent_mid_run_stops_the_run_resumably(tmp_path: Path) -> None:
    reader = quota_reader([0.50, 0.97])
    d, sink, seen = deps(tmp_path, passing())
    d.quota_reader = reader
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_BUDGET
    t = sink.text
    assert "Stopped: judge budget reached (subscription quota:" in t and "97%" in t and "resume with:" in t
    assert seen.judge.calls == LCC.QUOTA_BATCH                                 # stopped at the first batch boundary, not the end
    assert not (tmp_path / "elig.json").exists() and len(reader.reads) == 2


def test_quota_mid_run_force_overrides_and_warn_is_shown_once(tmp_path: Path) -> None:
    d, sink, seen = deps(tmp_path, passing())
    d.quota_reader = quota_reader([0.50, 0.97])
    assert run(["--yes", "--force", "--model", MODEL], d) == 0 and seen.judge.calls > LCC.QUOTA_BATCH
    d, sink, _ = deps(tmp_path / "b", passing())
    d.quota_reader = quota_reader([0.50, 0.85])
    assert run(["--yes", "--model", MODEL], d) == 0
    assert sink.text.count("Your Claude plan is at 85%") == 1


def test_quota_unreadable_mid_run_never_stops_it(tmp_path: Path) -> None:
    seq = [(0.5, "ok", 0.0)]
    d, _, _ = deps(tmp_path, passing())
    d.quota_reader = lambda: seq.pop(0) if seq else (None, "stale", 0.0)
    assert run(["--yes", "--model", MODEL], d) == 0


def test_api_route_is_not_quota_checked(tmp_path: Path) -> None:
    reader = quota_reader([0.99])
    d, _, _ = deps(tmp_path, passing(), env={"ANTHROPIC_API_KEY": "sk-ant-test-key-123456"})
    d.quota_reader = reader
    assert run(["--yes", "--model", MODEL], d) == 0 and reader.reads == []


# ------------------------------------------------------------------------------------------ 7. dry-run defects
def test_several_models_message_lists_each_model_once(tmp_path: Path) -> None:
    eps = [runtimes.Endpoint("ollama", "http://127.0.0.1:11434", ["qwen3.5:latest", "qwen3.5:latest", "other:7b"]),
           runtimes.Endpoint("ollama", "http://127.0.0.1:11434", ["qwen3.5:latest"])]
    d, sink, _ = deps(tmp_path, endpoints=eps)
    assert run(["--dry-run"], d) == LCC.EXIT_NO_RUNTIME
    assert sink.etext.count("qwen3.5:latest@ollama") == 1 and "other:7b@ollama" in sink.etext


def test_dedupe_keeps_the_same_name_on_a_different_runtime_or_endpoint(tmp_path: Path) -> None:
    eps = [runtimes.Endpoint("ollama", "http://127.0.0.1:11434", ["m:1"]), runtimes.Endpoint("lmstudio", "http://127.0.0.1:1234", ["m:1"])]
    d, sink, _ = deps(tmp_path, endpoints=eps, interactive=True, answers=["2"])
    assert run(["--dry-run"], d) == 0
    assert "1. m:1  (ollama)" in sink.text and "2. m:1  (lmstudio)" in sink.text and "[lmstudio at" in sink.text


def test_interactive_menu_has_no_duplicate_rows(tmp_path: Path) -> None:
    eps = [runtimes.Endpoint("ollama", "http://127.0.0.1:11434", ["qwen3.5:latest", "qwen3.5:latest", "other:7b"])]
    d, sink, _ = deps(tmp_path, endpoints=eps, interactive=True, answers=["2"])
    assert run(["--dry-run"], d) == 0
    assert sink.text.count("qwen3.5:latest") == 1 and "2. other:7b" in sink.text


def test_hardware_line_shows_capacity_not_free_memory(tmp_path: Path) -> None:
    d, sink, _ = deps(tmp_path)
    d.hardware = lambda: {"total_gb": 48, "usable_gb": 0.0, "gpu": None, "arch": "arm64"}
    assert run(["--dry-run", "--model", MODEL], d) == 0
    assert "RAM 48 GB, 38.4 GB usable for a model, arm64" in sink.text and "0.0 GB usable" not in sink.text
    d, sink, _ = deps(tmp_path / "b")
    d.hardware = lambda: {"total_gb": None, "usable_gb": 0.0, "arch": "x86_64"}
    assert run(["--dry-run", "--model", MODEL], d) == 0
    assert "usable" not in sink.text.split("Hardware:")[1].splitlines()[0]


def test_hardware_line_small_machine_uses_the_4gb_reserve() -> None:
    assert LCC._hardware_line({"total_gb": 8}) == "RAM 8 GB, 4 GB usable for a model"
