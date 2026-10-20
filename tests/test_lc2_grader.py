"""LC-2: the ported local-check rubric/grader/stats reproduce the frozen Q37 behaviour."""

from __future__ import annotations

import ast
import gzip
import json
import re
from pathlib import Path

import pytest

from llm_router.local_check import grader, grader_cases, rubric, stats

FIXTURE = Path(__file__).parent / "fixtures" / "lc2" / "q37_items.jsonl.gz"
PKG = Path(grader.__file__).parent
ALLOWED_LICENCES = {"MIT", "BSD-3-Clause", "Apache-2.0", "MIT OR Apache-2.0", "Unlicense OR MIT"}
FIXTURE_MAX_BYTES = 400_000


def _load() -> list[dict]:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# (1) self-test cases -------------------------------------------------------------------------
def test_grader_self_test_cases_all_pass() -> None:
    r = grader_cases.run()
    assert r["n"] == 103, "Q37 froze 103 grader cases"
    assert r["n_accept"] > 0 and r["n_reject"] > 0
    assert r["unexpected"] == []


@pytest.mark.parametrize("case", grader_cases.CASES, ids=[c[0] for c in grader_cases.CASES])
def test_each_grader_case(case: tuple) -> None:
    name, job, fx, text, ok, reason = case
    diff, files = grader_cases.FIX[fx]
    g = grader.grade_text({"stratum": job, "diff": diff, "files": files}, text)
    assert g["ok"] == ok
    if not ok:
        assert reason in g["reasons"]


# (2) port equivalence ------------------------------------------------------------------------
def test_port_equivalence_on_stored_q37_answers() -> None:
    items = _load()
    checked = failing = 0
    mismatches = []
    for it in items:
        for arm in ("qwen", "sonnet"):
            exp = it["expected"][arm]
            g = grader.grade_text({**it, "messages": [{"role": "user", "content": it["context"]}]}, it["answers"][arm])
            checked += 1
            failing += not exp["prog_ok"]
            if (g["ok"], g["reasons"]) != (exp["prog_ok"], exp["reasons"]):
                mismatches.append((it["id"], arm, g["reasons"], exp["reasons"]))
    assert checked == 2 * len(items) == 128
    assert failing >= 5, "the equivalence set must contain programmatic failures, or it proves nothing"
    assert mismatches == []


def test_fixture_size_cap_and_licences() -> None:
    assert FIXTURE.stat().st_size <= FIXTURE_MAX_BYTES
    items = _load()
    assert len(items) == 64
    assert {it["stratum"] for it in items} == {"commit_message", "pr_description"}
    for it in items:
        assert it["repo"] and it["repo_url"].startswith("https://github.com/") and it["commit"]
        assert it["licence"] in ALLOWED_LICENCES, (it["repo"], it["licence"])
        assert "pylint" not in it["repo"].lower()


# (3) no numeric limit literal in grader code -------------------------------------------------
def test_no_limit_literal_in_grader_code() -> None:
    limits = {v for lane in rubric.LIMITS.values() for v in lane.values()}
    assert limits == {72, 150, 8, 120, 1}
    tree = ast.parse((PKG / "grader.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            # small structural indices/offsets (0, 2, 3 ...) are not limits; every rubric value is
            assert node.value not in limits - {1}, f"limit literal {node.value} at line {node.lineno}"
    # 1 is both a limit (plan_min_bullets) and an index/slice offset; forbid it only in comparisons against counts
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for c in node.comparators + [node.left]:
                if isinstance(c, ast.Constant) and c.value in limits:
                    raise AssertionError(f"limit literal compared at line {node.lineno}")
    src = (PKG / "grader.py").read_text(encoding="utf-8")
    assert re.search(r"LIMITS\[", src) and "from .rubric import LIMITS" in src


def test_prompts_render_from_limits() -> None:
    c = rubric.commit_prompt("DIFF")
    assert str(rubric.LIMITS["commit_message"]["subject_max_chars"]) in c and c.endswith("DIFF")
    p = rubric.pr_prompt("T", "DIFF")
    assert str(rubric.LIMITS["pr_description"]["summary_max_words"]) in p and "The PR title is: T" in p


def test_modules_do_no_io_at_import() -> None:
    for name in ("rubric", "grader", "stats", "grader_cases"):
        src = (PKG / f"{name}.py").read_text(encoding="utf-8")
        assert "open(" not in src and "~/.rsi" not in src and "prereg" not in src.lower().replace("preregistered", "").replace("pre-registered", "").replace("prereg-q37", "")


# (4) stats -----------------------------------------------------------------------------------
def test_wilson_known_values() -> None:
    p, lo, hi = stats.wilson(56, 70)
    assert p == pytest.approx(0.8)
    assert lo == pytest.approx(0.69183, abs=1e-4) and hi == pytest.approx(0.87695, abs=1e-4)
    p, lo, hi = stats.wilson(0, 10)
    assert (p, lo) == (0.0, 0.0) and hi == pytest.approx(0.2775, abs=1e-3)
    assert all(x != x for x in stats.wilson(0, 0))


def test_bar_thresholds() -> None:
    assert stats.kmin(70) == 56
    assert stats.kfail(70) == 49
    assert stats.kmin(140) == 112
    assert stats.wilson(55, 70)[0] < 0.80
    assert stats.wilson(49, 70)[2] < 0.80 <= stats.wilson(50, 70)[2]


def test_newcombe_and_power_helpers() -> None:
    d, lo, hi = stats.newcombe10(30, 0, 0, 30)
    assert d == 0.0 and lo < 0 < hi
    assert stats.binom_sf(10, 0.5, 0) == pytest.approx(1.0)
    row = stats.power_table(70, ps=(0.9,))[0]
    assert row["P_pass"] + row["P_powered_fail"] + row["P_inconclusive"] == pytest.approx(1.0, abs=1e-3)
