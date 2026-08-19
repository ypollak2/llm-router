"""AC-3: session-end.py must derive its host-baseline price from cost.py, not a
stale hardcoded copy. Before the fix it used $15/$75 (Opus 4.6) independently of
cost.py's current constant, mispricing the end-of-session savings summary.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "src" / "llm_router" / "hooks" / "session-end.py"


def _load_session_end():
    spec = importlib.util.spec_from_file_location("session_end_mod", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_session_end_baseline_matches_cost_module():
    from llm_router import cost

    se = _load_session_end()
    assert se.HOST_INPUT_PER_M == float(cost._HOST_INPUT_PER_M)
    assert se.HOST_OUTPUT_PER_M == float(cost._HOST_OUTPUT_PER_M)


def test_baseline_helper_uses_synced_price():
    from llm_router import cost

    se = _load_session_end()
    # 1M input + 1M output tokens priced at the SAME rate cost.py would use.
    got = se._host_baseline(1_000_000, 1_000_000)
    expected = float(cost._HOST_INPUT_PER_M) + float(cost._HOST_OUTPUT_PER_M)
    assert abs(got - expected) < 1e-9
