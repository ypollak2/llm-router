The release outcome audit scores routed work correctly, audit v2 (BIND W3; D-356). On the 16.0.0 window
(2026-10-04..2026-11-03, 564 units, hand-checked in BIND-R1 and BIND-R2) the audit had nine defects, each recorded
in `docs/bugs/BINDAUDIT-1..9.md` with its test: Codex units paired with the latest Agent call, not their own (31% wrong);
sub-agent transcripts never read (nested spawns unknown); `session_kind_overrides.json` ignored (182 of the 564 units
were in two sessions the owner marked non-organic); MCP answers scored on the JSON envelope, header and footer
(a verbatim relay reached `used` 9 of 113 times); tool inputs and `llm_local_task` file effects not counted as use;
fixed-reply probes and status views in the denominator; replayed history counted twice (8); a backgrounded call scored on
its stub; and the gate comparing rates across methods. Checked against the hand labels on the same window (read-only,
`scripts/release/audit_agreement.py`, overrides lifted): the 35 Codex units BIND-R1 followed are all paired with their own
call (the old audit: 26) and all `redone / claude_subagent_ran`; 144 of the 158 `routed_mcp` rows carry BIND-R2's hand
label (the old audit: 43, 27.2%), and the 10 it found used are the 10 the audit now calls used (the old audit: 0). The
fixed audit reads 10 used of 467 attempted (2.141%) on that window where the old one read 0 of 564, so it is not
comparable with any earlier number: `outcome_gate.py` records `audit_version`, compares only within a version, and on a
mismatch prints it and FAILS unless `--cross-version rebaseline|compare` is given. Owner decision D-359 is REBASELINE: the 16.0.0 audit v2 number becomes the baseline, compared with nothing, and `scripts/release/pre-release-verify.sh --outcome-cross-version rebaseline` forwards the flag to step 13 once. 15.4.0's recorded 0.229% (2/873)
is an audit v1 number and cannot be re-scored (transcripts purged). Not fixed: BIND-R2 class (a) "the host did the same
job itself" (8 rows) and class (c) "adopted, reworded" (3) have no lexical signal and stay `unused`/`unknown`; 67 router calls
made inside sub-agent transcripts (30 days, all sessions) are not scored.
