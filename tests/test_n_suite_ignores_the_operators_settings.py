"""N: the suite's outcome must not depend on the operator's own llm-router settings.

2026-09-24: `pre-release-verify.sh` runs pytest under the real HOME, and seven
tests failed that pass under a clean HOME. Cause: the operator's
~/.llm-router/.env (LLM_ROUTER_SEMANTIC_HISTORY=shadow, LLM_ROUTER_ENFORCE=soft
— both ordinary settings) reached the tests, because the routing hook calls
`_load_dotenv()` at IMPORT and copies it into os.environ for the rest of the
process; an exported shell variable does the same. Bisected: HISTORY=shadow
alone breaks the I3b test, ENFORCE=soft alone breaks a budget test.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VICTIMS = [
    "tests/test_i3b_session_seeded_retrieval.py::test_the_session_seeds_retrieval",
    "tests/test_audit_v5_fixes.py::TestBudgetEnforcement::test_budget_exceeded_cleanup_releases_reservation",
]


def test_operator_settings_do_not_reach_the_suite(tmp_path):
    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    (home / ".llm-router" / ".env").write_text("LLM_ROUTER_ENFORCE=soft\n")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LLM_ROUTER_", "PYTEST_"))}
    env.update(HOME=str(home), LLM_ROUTER_SEMANTIC_HISTORY="shadow", LLM_ROUTER_ENFORCE="soft")
    r = subprocess.run([sys.executable, "-m", "pytest", *VICTIMS, "-q", "-rA", "-p", "no:randomly",
                        "-p", "no:cacheprovider"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-2000:]
    assert r.stdout.count("PASSED tests/") == len(VICTIMS), r.stdout[-800:]  # not vacuous
