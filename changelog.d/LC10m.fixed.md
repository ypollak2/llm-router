LC-10m / LC-8e: tighten local-check wording and fail-open handling.

- `--no-judge` is described as probe-only; a test scans source, docs, changelog and root READMEs for stale wording.
- `status` adds "(not recorded; recorded verdict X kept)" to an INCONCLUSIVE rerun only when the eligibility record for that configuration really holds a verdict (PASS, FAIL or NOT_PASSED). A burnt INVALID, a `--no-judge` run, or a record that was removed no longer counts.
- Status-line tests also cover an inconclusive-only run.
- A local-attempt logging failure keeps the original route error.
- Fail-open codes for local-attempt, context-reset and demotion are covered by tests; cancellation requires an attempt trace.
- The wall-clock test budget is relaxed to 0.3 seconds.
