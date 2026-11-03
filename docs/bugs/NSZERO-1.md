---
id: NSZERO-1
status: fixed in `bind/honest-statusline` (deploy: refresh the installed status line files and restart the MCP server; see the PR)
---
## NSZERO-1. NS printed `0.0% n=2069` on every surface: a structural zero, not a measurement

- **Symptom (2026-11-03).** The status line's third row read `NS 0.0% n=2069` and `llm-router kpi` printed `0.0% (n=2088)` for the
  same window (7 d, organic, n=2,088 units, 4 sessions, recomputed from a copy of `~/.llm-router`, `~/.rsi/research/primary-plan/v16/coord/NS-STATUSLINE-ANALYSIS-20261103.md`). The owner could not square
  it with the router's own ledger rows in the same screen. The figure could not have been anything else: 0 of 2,088 units.
- **Cause.** NS counts a unit only when a non-Claude model served it AND its `verify` record shows a passing test
  (`src/llm_router/northstar.py` `is_strict_used`, owner decision D-2). `compute_scorecard` builds its unit list without
  `verify_records=True`, so no unit ever carries a record, and the verify rows that exist (n=55) are all `unavailable`. The numerator
  cannot exceed 0 whatever the router does, and a measured-looking `0.0% (n=...)` hid that. `statusline_tick.py` already promised
  `n/a` for a value it does not have, and kept the promise for an unknown share but not for an unobservable numerator.
- **Fix (owner decision D-364: NS stays STRICT and reads `blind` while no verifier is connected).** `northstar.has_verdict` and
  `VERDICT_STATUSES` name a real verdict; `kpi._ns_d1_d2` marks NS and D2 `blind` (`measurable` false, `blind` true, the 0/n kept, the
  reason `northstar.BLIND_REASON`) when no counted unit carries a verdict, and the scorecard text, `--health` (state `blind`),
  `statusline_refresh._ns` and the status line (`NS strict 0/n (blind) 7d units`) all read that one result. The seam for the verifier
  is `northstar.NS_ATTACH_VERIFY_RECORDS` (False): the PR that wires a verifier sets it True and NS is measured from the verdicts with
  no other change. A too-few or not-measurable result keeps its own words.
- **Test.** `tests/test_w8_ns_blind.py` (24 cases: the rule, each verdict status, a verdict outside the counted population, the
  seam off and on through the real ledger, health, the cache and the tick for every cache shape);
  `tests/test_kpi_strict_used.py`, `tests/test_kpi_command.py` and the other KPI tests now assert the blind value where they asserted
  `0.0%`. Reverting `kpi._ns_d1_d2` to the old rate fails the blind cases.
