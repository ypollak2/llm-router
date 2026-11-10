---
id: P11-REDOS
status: fixed in `feat/p11-summary-gate` (#893, head 019ff94d) before merge; never shipped
---
## P11-REDOS. A fact regex over tool output backtracked super-linearly and could stall the proxy's event loop

- **Symptom (2026-11-10).** Review `~/.rsi/research/primary-plan/v16/coord/REVIEW-P11-893-20261110.md`
  found that the first P11 head (5b58c927) had a pytest node-id pattern `_NODE_ID` whose run time grew
  about 4x per doubling of input. The builder timed it in a scratch script, n=1 run per size, one
  machine: a run of `x` took 0.23 s at 12.5k chars, 0.95 s at 25k and 3.86 s at 50k, and 48k chars of
  hex took 3.84 s. Tool output in a single
  turn routinely reaches tens of thousands of chars. The gate ran on the proxy's event loop, so one
  such turn would have blocked every other request for seconds.
- **Cause.** Unanchored starts plus overlapping character classes allowed the regex engine to retry
  every start offset, with a nested scan at each one. Nothing bounded the input length, and the scan
  ran synchronously in the async request path.
- **Fix.** These are the rules for every regex in the proxy that runs over tool output or message text:
  - Anchor each start with a lookbehind, so a match cannot begin inside a longer token.
  - Bound whitespace runs and lookaheads.
  - Cap the scanned text (`MAX_SCAN_CHARS` 400k, `MAX_LINE` 8k).
  - Run the scan via `asyncio.to_thread`.

  The independent delta review (`~/.rsi/research/primary-plan/v16/coord/REVIEW-P11-893-DELTA-20261110.md`)
  built n=46 adversarial shapes that the test file does not contain. Its worst `extract()` time was
  0.011 s at 50k chars, 0.025 s at 100k and 0.045 s at 200k (n=1 run per shape and size, one machine,
  scratch timing), so the scan is linear.
- **Test.** `tests/test_p11_summary_gate.py::test_extraction_is_linear_on_adversarial_text` runs 10
  adversarial 200k-char inputs. Its tight group (the hex, `x`, `a/b/` and minified-JS cases, 4 of 10)
  must finish within half a second in total, n=1 run per CI job. A new regex over tool output needs its own adversarial case in a test like this one,
  run at 200k chars, before review.
