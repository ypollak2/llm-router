"""Plan 07 Cat G — operational tooling tests.

Covers three modules:

* ``llm_router.benchmark`` — Protocol + plugin registry contracts.
* ``llm_router.policy_diff`` — head-model prediction and report diffing.
* ``llm_router.benchmark.regression`` — store / load / detect pipeline.

The CLI wrappers (``commands/benchmark.py``, ``commands/policy.py``) are
thin argparse adapters; the substance lives in the modules under test
here. Integration tests for the CLI go through a single happy-path smoke
run to keep coverage honest without re-testing argparse.
"""

from __future__ import annotations

import json

import pytest

from llm_router.benchmark import (
    BenchmarkResult,
    Prediction,
    Prompt,
    SubmissionResult,
    get_runner,
    list_runners,
    register_runner,
)
from llm_router.benchmark.regression import (
    DEFAULT_DROP_THRESHOLD,
    BenchmarkRunRecord,
    detect_regressions,
    format_report,
    load_history,
    store_result,
)
from llm_router.benchmark.runners.routerarena import RouterArenaRunner
from llm_router.policy import RoutingPolicy
from llm_router.policy_diff import (
    Sample,
    diff_policies,
    format_diff_report,
    predict_head_model,
)
from llm_router.types import TaskType


# ── Registry ────────────────────────────────────────────────────────────────


class TestRegistry:
    """Plug-in name lookup contracts."""

    def test_routerarena_auto_registers(self):
        """Importing the package via the runners sub-module must register."""
        assert "routerarena" in list_runners()
        assert get_runner("routerarena").name == "routerarena"

    def test_missing_runner_lists_known_names(self):
        """KeyError message must surface what *is* available."""
        with pytest.raises(KeyError, match="routerarena"):
            get_runner("nonexistent-benchmark")

    def test_register_overwrites(self):
        """Re-registering the same name should silently replace — tests need this."""

        class Fake:
            name = "routerarena"

            def load_dataset(self, split):
                return []

            def format_prediction(self, prompt, prediction):
                return {}

            def evaluate(self, predictions, dataset):
                return BenchmarkResult(
                    benchmark=self.name, split="", score=0, n_samples=0,
                )

            def submit(self, predictions):
                return None

        original = get_runner("routerarena")
        try:
            register_runner(Fake())
            assert get_runner("routerarena") is not original
        finally:
            register_runner(original)


# ── RouterArena runner ──────────────────────────────────────────────────────


class TestRouterArenaRunner:
    """Concrete plug-in exercising the full Protocol."""

    def test_missing_dataset_returns_empty(self, tmp_path):
        """No file → empty list, not a crash. CLI translates to "fetch first" hint."""
        runner = RouterArenaRunner(dataset_root=tmp_path / "absent")
        assert runner.load_dataset("sub_10") == []

    def test_load_dataset_parses_jsonl(self, tmp_path):
        """Per-line JSON object → Prompt with id/text/reference/subject."""
        data = tmp_path / "sub_10.jsonl"
        data.write_text(
            json.dumps({"id": "p1", "text": "Capital of France?", "reference": "Paris",
                        "subject": "history"}) + "\n"
            + json.dumps({"id": "p2", "prompt": "2+2", "answer": "4",
                          "subject": "math", "task_type": "query"}) + "\n"
        )
        runner = RouterArenaRunner(dataset_root=tmp_path)
        prompts = runner.load_dataset("sub_10")
        assert [p.id for p in prompts] == ["p1", "p2"]
        # Second row uses `prompt`/`answer` aliases — alias support is part
        # of the runner contract, not the dataset author's job.
        assert prompts[1].text == "2+2"
        assert prompts[1].reference == "4"
        assert prompts[1].subject == "math"

    def test_corrupt_line_is_skipped_not_fatal(self, tmp_path):
        """A regression benchmark with one bad row is more useful than no benchmark."""
        data = tmp_path / "x.jsonl"
        data.write_text(
            json.dumps({"id": "ok", "text": "q", "reference": "a"}) + "\n"
            + "{not-json}\n"
            + json.dumps({"id": "ok2", "text": "q", "reference": "a"}) + "\n"
        )
        runner = RouterArenaRunner(dataset_root=tmp_path)
        prompts = runner.load_dataset("x")
        assert {p.id for p in prompts} == {"ok", "ok2"}

    def test_evaluate_exact_match_with_per_subject(self):
        """Normalized exact match with per-subject breakdown — the headline contract."""
        runner = RouterArenaRunner()
        dataset = [
            Prompt(id="p1", text="?", reference="Paris", subject="history"),
            Prompt(id="p2", text="?", reference="4", subject="math"),
            Prompt(id="p3", text="?", reference="Tokyo", subject="history"),
        ]
        preds = [
            Prediction(prompt_id="p1", model="x", response="paris  ",  # normalize-case
                       cost_usd=0, latency_ms=0),
            Prediction(prompt_id="p2", model="x", response="5",  # wrong
                       cost_usd=0, latency_ms=0),
            Prediction(prompt_id="p3", model="x", response="Tokyo",
                       cost_usd=0, latency_ms=0),
        ]
        result = runner.evaluate(preds, dataset)
        assert result.score == pytest.approx(2 / 3, abs=1e-4)
        assert result.per_subject == {"history": 1.0, "math": 0.0}
        assert result.metadata["n_correct"] == 2

    def test_submit_returns_informative_stub(self):
        """Stub must not be ``None`` so CLI shows the next-step hint."""
        out = RouterArenaRunner().submit([])
        assert isinstance(out, SubmissionResult)
        assert out.submitted is False
        assert "scripts" in out.message


# ── Policy diff ─────────────────────────────────────────────────────────────


def _policy(name: str, *, workhorses: list[str], specialists: dict[str, str] | None = None) -> RoutingPolicy:
    return RoutingPolicy(
        name=name,
        description=f"test policy {name}",
        workhorses=workhorses,
        specialists=specialists or {},
    )


class _FakeManager:
    """In-memory PolicyManager double for tests — avoids touching ~/.llm-router."""

    def __init__(self, policies: dict[str, RoutingPolicy]) -> None:
        self._policies = policies

    def load_policy(self, name: str) -> RoutingPolicy:
        try:
            return self._policies[name]
        except KeyError as err:
            raise FileNotFoundError(name) from err


class TestPolicyDiff:
    """Head-model prediction and the diff aggregator."""

    def test_predict_head_prefers_specialist(self):
        """Subject specialist beats workhorses[0] — that's the whole point."""
        pol = _policy(
            "p", workhorses=["openai/gpt-4o"],
            specialists={"code": "openai/o3"},
        )
        assert predict_head_model(pol, "code") == "openai/o3"
        assert predict_head_model(pol, "general") == "openai/gpt-4o"

    def test_predict_head_unconfigured_returns_sentinel(self):
        """Behaviour-only policies (no chains) get a sentinel, not a crash."""
        pol = _policy("p", workhorses=[])
        assert predict_head_model(pol, "code") == "<unconfigured>"

    def test_diff_emits_only_differing_entries(self):
        """Sample where both policies agree must not appear in `differences`."""
        a = _policy("a", workhorses=["openai/gpt-4o"], specialists={"code": "openai/o3"})
        b = _policy("b", workhorses=["openai/gpt-4o"], specialists={"code": "anthropic/sonnet"})
        mgr = _FakeManager({"a": a, "b": b})
        report = diff_policies(
            "a", "b",
            [
                Sample(id="s1", subject="general"),  # both pick gpt-4o
                Sample(id="s2", subject="code"),  # differ
            ],
            manager=mgr,
        )
        assert report.n_samples == 2
        assert report.n_differences == 1
        assert report.differences[0].sample_id == "s2"

    def test_diff_projects_cost(self):
        """Cost projection must run for both sides so totals are comparable."""
        a = _policy("a", workhorses=["claude-sonnet-4-6"])
        b = _policy("b", workhorses=["claude-sonnet-4-6"])
        mgr = _FakeManager({"a": a, "b": b})
        report = diff_policies(
            "a", "b",
            [Sample(id="s", subject="general", task_type=TaskType.QUERY, input_tokens=500)],
            manager=mgr,
        )
        # Same policy on both sides → identical totals → zero delta percentage.
        assert report.total_cost_a == pytest.approx(report.total_cost_b)
        assert report.cost_delta_pct == 0

    def test_format_report_smoke(self):
        """Render must not crash and must contain both policy names."""
        a = _policy("a", workhorses=["openai/gpt-4o"])
        b = _policy("b", workhorses=["openai/gpt-4o-mini"])
        mgr = _FakeManager({"a": a, "b": b})
        report = diff_policies("a", "b", [Sample(id="s", subject="general")], manager=mgr)
        text = format_diff_report(report)
        assert "a" in text and "b" in text
        assert "Differences" in text


# ── Regression detector — pure logic ────────────────────────────────────────


def _record(version: str, score: float, per_subject: dict[str, float] | None = None) -> BenchmarkRunRecord:
    return BenchmarkRunRecord(
        version=version,
        policy="standard",
        benchmark="routerarena",
        split="sub_10",
        score=score,
        n_samples=10,
        timestamp=f"2026-01-01 00:00:0{version[-1]}",
        per_subject=per_subject or {},
    )


class TestDetectRegressions:
    """Pure logic over a history list — no DB."""

    def test_empty_or_single_history_no_regressions(self):
        assert detect_regressions([]) == []
        assert detect_regressions([_record("v1", 0.70)]) == []

    def test_drop_above_threshold_flagged(self):
        history = [_record("v1", 0.700), _record("v2", 0.693)]  # delta = -0.007
        regs = detect_regressions(history, threshold=0.005)
        assert len(regs) == 1
        assert regs[0].current_version == "v2"
        assert regs[0].delta < 0

    def test_drop_below_threshold_not_flagged(self):
        """Drops smaller than the threshold do not regress (boundary clear of FP fuzz)."""
        history = [_record("v1", 0.700), _record("v2", 0.697)]  # delta = -0.003
        assert detect_regressions(history, threshold=0.005) == []

    def test_improvement_never_flagged(self):
        """Score going up cannot be a regression regardless of threshold."""
        history = [_record("v1", 0.700), _record("v2", 0.750)]
        assert detect_regressions(history, threshold=0.005) == []

    def test_pairwise_walk_catches_compound_drops(self):
        """Plan-spec rationale: sliding 0.70 → 0.695 → 0.690 → 0.685 → 0.68
        should trip pairwise even though a single-baseline check might miss it."""
        # Tiny per-step drops below threshold won't flag — but a single bigger
        # drop two steps in should be caught, that's the contract.
        history = [
            _record("v1", 0.700),
            _record("v2", 0.700),  # no-op
            _record("v3", 0.690),  # -0.010 vs v2 → flagged
            _record("v4", 0.689),  # -0.001 vs v3 → not flagged
        ]
        regs = detect_regressions(history, threshold=0.005)
        assert len(regs) == 1
        assert regs[0].previous_version == "v2"
        assert regs[0].current_version == "v3"

    def test_subject_breakdown_populated(self):
        history = [
            _record("v1", 0.700, per_subject={"medical": 0.90, "code": 0.65}),
            _record("v2", 0.680, per_subject={"medical": 0.81, "code": 0.65}),
        ]
        regs = detect_regressions(history, threshold=0.005)
        assert regs[0].subject_breakdown["medical"] == (0.90, 0.81)
        assert regs[0].subject_breakdown["code"] == (0.65, 0.65)

    def test_default_threshold_matches_plan_spec(self):
        """Plan 07: "sub_10 Arena Score must not drop > 0.005"."""
        assert DEFAULT_DROP_THRESHOLD == 0.005


# ── Regression detector — DB round-trip ─────────────────────────────────────


@pytest.mark.slow
class TestRegressionStorage:
    """End-to-end: store → load → detect → format."""

    async def test_round_trip(self, temp_db):
        await store_result(
            version="v1.0.0", policy="standard", benchmark="routerarena",
            split="sub_10", score=0.700, n_samples=100,
            per_subject={"medical": 0.9},
        )
        await store_result(
            version="v1.1.0", policy="standard", benchmark="routerarena",
            split="sub_10", score=0.685, n_samples=100,
            per_subject={"medical": 0.8},
        )
        history = await load_history(
            policy="standard", benchmark="routerarena", split="sub_10",
        )
        assert [r.version for r in history] == ["v1.0.0", "v1.1.0"]
        assert history[1].per_subject == {"medical": 0.8}

        regs = detect_regressions(history)
        assert len(regs) == 1
        assert regs[0].previous_version == "v1.0.0"
        assert regs[0].current_version == "v1.1.0"

    async def test_load_filters_since_version(self, temp_db):
        for ver, score in [("v1", 0.70), ("v2", 0.71), ("v3", 0.69)]:
            await store_result(
                version=ver, policy="standard", benchmark="routerarena",
                split="sub_10", score=score, n_samples=10,
            )
        history = await load_history(
            policy="standard", benchmark="routerarena", split="sub_10",
            since_version="v2",
        )
        assert [r.version for r in history] == ["v2", "v3"]

    async def test_format_report_renders_history_and_regressions(self, temp_db):
        for ver, score in [("v1", 0.700), ("v2", 0.685)]:
            await store_result(
                version=ver, policy="standard", benchmark="routerarena",
                split="sub_10", score=score, n_samples=10,
            )
        from llm_router.benchmark.regression import build_report
        report = await build_report(
            policy="standard", benchmark="routerarena", split="sub_10",
        )
        text = format_report(report)
        assert "v1" in text and "v2" in text
        assert "Regressions" in text
        assert report.has_regressions is True
