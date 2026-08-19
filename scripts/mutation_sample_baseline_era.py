#!/usr/bin/env python3
"""G-F, second attempt: a sample that is measurable at BOTH SHAs.

G-F requires "mutation score >= mutation_baseline + 0.15, floor 0.80". The
original frozen sample could not produce a baseline term: SEVEN of its ten
mutations did not apply at the pre-remediation SHA, because they target code the
remediation CREATED (``pricing.py`` did not exist). Comparing 1.00 over ten
scored mutations against 0.67 over three is not a comparison, and by the
harness's own "refuse a verdict below 8 scored" floor the baseline was not a
verdict at all. G-F was recorded UNEVALUABLE.

The owner chose to author a baseline-era set. This is it.

THE HONEST WEAKNESS, STATED FIRST BECAUSE IT IS THE WHOLE PROBLEM
------------------------------------------------------------------
**These ten mutations were chosen AFTER seeing the remediation.** That is
precisely the post-hoc selection the frozen-sample design exists to prevent: an
author who picks mutations knowing which code changed can, consciously or not,
pick ones that flatter the result.

Two things limit it, neither of which eliminates it:

1. **Selection was mechanical.** Candidates came from a script that lists lines
   IDENTICAL AND UNIQUE in both trees, inside the money/routing/verification
   modules. I did not check which mutations the remediation would happen to kill
   before choosing them.
2. **They target invariants, not diffs.** Each is a change a reviewer would call
   a real defect on its own terms — a swapped rate, a guard that always passes, a
   degradation reported as clean — rather than "the thing that was fixed here".

A reader should treat the resulting delta as **weaker evidence than a
pre-registered sample would be**, and should not treat it as equivalent to the
original frozen sample's design intent. It is the best available measurement, not
a clean one.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No mutation targets `savings.py`, `provenance.py`, `coverage.py`, `failopen.py`,
`net_bind.py`, `env_registry.py` or `pricing.py` — all created by the
remediation. Including them would guarantee N/A at the baseline and rebuild the
exact problem this file exists to solve.

Every mutation carries a BEHAVIOUR PROBE (see ``mutation_sample.py``): a survivor
counts as a coverage hole only once the mutation is confirmed to change
observable behaviour. That check exists because the original M2 was an equivalent
mutant reported as a hole.

KNOWN LIMIT: B2 AND B3 CANNOT BE PROBED BY THIS HARNESS
--------------------------------------------------------
Both mutate lines inside ``async def log_usage``, which writes to the ledger. A
correct probe would have to point LLM_ROUTER_DB_PATH at a tmpdir, run the coroutine,
and SELECT the stored row back -- several statements. The probe contract is a
SINGLE EXPRESSION evaluated as ``print(<expr>)`` in a subprocess with only
``src`` on the path, so there is nowhere to put the setup.

My first attempt probed ``_claude_cost`` instead, which never touches either
mutated line. Both came back EQUIVALENT -- and that verdict was about the PROBE,
not the mutation. The harness's message has been corrected to say so.

They are therefore EXCLUDED from the denominator rather than counted, which is
the safe direction: an unprobed survivor cannot inflate or deflate the score.
The proper fix is a ``probe_file`` option (a script whose stdout is the probe
value) -- deliberately not bolted on mid-measurement, because changing the
harness while reading its output is how a result stops being trustworthy.

With B6's probe corrected the sample scores 8 mutations, which meets the
harness's own "refuse a verdict below 8 scored" floor without B2 and B3.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "mutation_sample", Path(__file__).resolve().parent / "mutation_sample.py"
)
_ms = importlib.util.module_from_spec(_spec)
sys.modules["mutation_sample"] = _ms          # dataclass needs it registered
_spec.loader.exec_module(_ms)

Mutation = _ms.Mutation

#: Every anchor below was verified to appear EXACTLY ONCE in both the HEAD tree
#: and the c2c2882 worktree before being written here. That is the necessary
#: condition the original sample failed.
BASELINE_ERA_MUTATIONS: list[Mutation] = [
    # ── money ────────────────────────────────────────────────────────────────
    Mutation(
        "B1", "money", "src/llm_router/execution_ledger.py",
        "return float(_HOST_INPUT_PER_M), float(_HOST_OUTPUT_PER_M)",
        "return float(_HOST_OUTPUT_PER_M), float(_HOST_INPUT_PER_M)",
        ["tests/economics/", "tests/test_execution_ledger.py"],
        "Input and output rates swapped. Output tokens cost several times input, "
        "so every baseline-equivalent figure is wrong by a large factor while "
        "staying plausibly shaped — the failure mode that lets a wrong number "
        "survive review.",
        probe="__import__('llm_router.execution_ledger', fromlist=['x'])._host_opus_rates()",
    ),
    Mutation(
        "B2", "money", "src/llm_router/cost.py",
        'cost_usd = 0.0 if response.provider in {"ollama", "codex", "gemini_cli"} else response.cost_usd',
        'cost_usd = response.cost_usd',
        ["tests/economics/", "tests/test_savings.py"],
        "Free local providers start being charged their notional cost, so every "
        "saving they produce shrinks. Silent: the numbers stay positive.",
        probe=(
            "[__import__('llm_router.cost', fromlist=['x'])._claude_cost('claude-opus-5', 1000, 1000)]"
        ),
    ),
    Mutation(
        "B3", "money", "src/llm_router/cost.py",
        "saved_usd = potential_cost_usd - cost_usd",
        "saved_usd = potential_cost_usd",
        ["tests/economics/", "tests/test_savings.py"],
        "Savings stop subtracting what was actually spent — gross reported as "
        "net. This is AUD-06's sibling: not a clamp, but the same 'the number "
        "looks right and is not' shape.",
        probe="__import__('llm_router.cost', fromlist=['x'])._claude_cost('claude-sonnet-5', 500, 500)",
    ),
    # ── routing ──────────────────────────────────────────────────────────────
    Mutation(
        "B4", "routing", "src/llm_router/tool_surface.py",
        "return True if reg is None else name in reg",
        "return True",
        ["tests/test_tool_surface.py", "tests/routing/"],
        "The registration guard always says 'registered'. This is the Q3(c) "
        "shape exactly: a check that cannot answer no, which the audit recorded "
        "as CLOSED while it was blind.",
        probe="__import__('llm_router.tool_surface', fromlist=['x']).is_registered('llm_bogus_zzz', 'core')",
    ),
    Mutation(
        "B5", "routing", "src/llm_router/tool_surface.py",
        "return _TIERS.get(tier, None)",
        'return _TIERS.get("off", None)',
        ["tests/test_tool_surface.py", "tests/routing/"],
        "Every tier resolves as 'off', so tier restrictions silently vanish and "
        "hints name tools the active tier does not expose.",
        probe="sorted(__import__('llm_router.tool_surface', fromlist=['x']).registered_tools('core') or [])",
    ),
    Mutation(
        "B6", "routing", "src/llm_router/tool_surface.py",
        "return ToolCall(candidate, logical=logical, degraded=True)",
        "return ToolCall(candidate, logical=logical, degraded=False)",
        ["tests/test_tool_surface.py", "tests/routing/"],
        "A DEGRADED fallback reports itself as clean. Nothing downstream can "
        "tell a first-choice route from a salvage — the same 'observed vs "
        "unobserved' collapse as I-1. PROBE NOTE: the first version passed "
        "slim='minimal', which is NOT a tier -- _TIERS holds core/routing/"
        "consolidated/off, so it fell back to None meaning EVERYTHING is "
        "registered, resolve() never degraded, and the probe returned False "
        "both ways. A probe naming a value that does not exist fails OPEN "
        "here: it reports EQUIVALENT rather than erroring.",
        probe=(
            "__import__('llm_router.tool_surface', fromlist=['x']).resolve('llm_generate', 'core').degraded"
        ),
    ),
    # ── verification / ledger ────────────────────────────────────────────────
    Mutation(
        "B7", "verification", "src/llm_router/execution_ledger.py",
        'return Path.home() / ".llm-router" / "usage.db"',
        'return Path.home() / ".llm-router" / "usage_alt.db"',
        ["tests/test_execution_ledger.py", "tests/telemetry/"],
        "The ledger reads a different file than everything writes. Every "
        "accounting query returns empty, which renders as 'you routed nothing' "
        "— RED2-02's failure-reads-as-zero at the storage layer.",
        probe="str(__import__('llm_router.execution_ledger', fromlist=['x'])._db_path())",
    ),
    Mutation(
        "B8", "verification", "src/llm_router/budget.py",
        "return _pending_spend_by_key.get(key, 0.0)",
        "return 0.0",
        # OWNERSHIP CORRECTED (Phase 0). This named tests/test_budget.py +
        # tests/economics/, and B8 SURVIVED that subset -- which read as absent
        # coverage. A dedicated full-suite run showed it is CAUGHT, by three
        # tests in tests/test_t2_m1_budget_key.py. The coverage existed; the
        # sample pointed at the wrong owner. Misattribution corrupts the
        # diagnostic even when the score is unaffected.
        ["tests/test_t2_m1_budget_key.py", "tests/test_budget.py"],
        "In-flight spend always reads as zero, so concurrent calls each believe "
        "the full budget is free. The guard still runs and still passes.",
        probe=(
            "(lambda b: (b._pending_spend_by_key.update({'k': 4.2}), b.pending_spend_for('k'))[1])"
            "(__import__('llm_router.budget', fromlist=['x']))"
        ),
    ),
    Mutation(
        "B9", "verification", "src/llm_router/budget.py",
        "return min(pending / bucket_size, 0.5) # Cap at 50% to avoid over-blocking",
        "return min(pending / bucket_size, 0.05) # Cap at 50% to avoid over-blocking",
        ["tests/test_budget.py", "tests/economics/"],
        "The pressure cap drops 50% -> 5%, so budget pressure almost never "
        "reaches a level that blocks. The comment still says 50%, which is how "
        "this survives a reading. The probe PRIMES pending tokens first: with "
        "none pending the function short-circuits to 0.0 and the cap is never "
        "reached, so an unprimed probe would report this real mutation as "
        "EQUIVALENT -- a probe must exercise the line it claims to watch.",
        probe=(
            "(lambda b: (b._pending_tokens.update({'openai': 10**6}), b._get_pending_pressure_offset('openai'))[1])(__import__('llm_router.budget', fromlist=['x']))"
        ),
    ),
    Mutation(
        "B10", "verification", "src/llm_router/budget.py",
        "return BudgetState(provider=provider, pressure=0.0)",
        "return BudgetState(provider=provider, pressure=1.0)",
        ["tests/test_budget.py", "tests/economics/"],
        "The neutral/unknown state becomes MAXIMUM pressure instead of none. "
        "Inverts the fail-direction: an unreadable budget blocks everything "
        "rather than nothing. Wrong either way — the point is that a test "
        "should notice the default flipping.",
        probe=(
            "__import__('llm_router.budget', fromlist=['x'])._neutral('openai').pressure"
        ),
    ),
]


if __name__ == "__main__":
    # Swap the frozen sample for the baseline-era one, then reuse the existing
    # runner wholesale -- including its uniqueness enforcement, its behaviour
    # probes, its EQUIVALENT/UNVERIFIED classification and its refusal to render
    # a verdict below 8 scored.
    _ms.MUTATIONS[:] = BASELINE_ERA_MUTATIONS
    sys.exit(_ms.main())
