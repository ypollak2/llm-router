---
id: STEP-SIDECALL-2
status: fixed in `fix/step-sidecall-2` (stacked on `fix/step-sidecall`, PR #425; deploy: restart the proxy)
---
## STEP-SIDECALL-2. Wire-form gaps in the human-text test beside tool results (follow-up to STEP-SIDECALL-1)

- **Symptom.** Review of #425 listed five gaps in `steps._is_non_user_text`, the test that decides
  whether text beside `tool_result` is a typed prompt (`turn_first`) or agent/harness text
  (`continuation`). Two mislabelled in opposite directions: verified Slack human turns read as
  non-user, and some non-user text read as human. No ledger count: the ledger keeps no body.
- **Cause and outcome per item** (all read from the Claude Code 2.1.296 binary, `strings`, 2026-10-11).
  1. `async_hook_response`: the premise was wrong. The attachment normaliser renders it as
     `yl([xe({content: UT(text), isMeta: true})])`; `yl` maps every text through `Xl`, which prefixes
     `<system-reminder>\n` (`NO="<system-reminder>"`). `UT` only escapes a trailing `<`. So it is
     already stripped as a harness block. No code change; a pin test.
  2. `observer-activity`: `rye()` returns the text bare (`case"observer-activity":return e`). The
     origin is delivered only to an observer agent (`promptOrigin:{kind:"observer-activity"}`), and
     the digest frames itself: `<NAME-activity ...>` tags (first line names a nonce) and the postamble
     "The activity above is a read-only digest of the agent you are observing". Both now exclude.
     `unclassified` (and `slack-ping`) go through `QM`, which prefixes
     `[MESSAGE FROM NON-USER SOURCE - NOT USER INPUT]`: already excluded. New: activity seen in a
     bound conversation is framed `Activity in the bound conversation ...` / `Activity was observed in
     the bound conversation`; excluded.
  3. "arrived in the bound thread": `rye()` leads a post with `A message arrived in the bound thread
     while you were working:\n` only when `verifiedSlackHumanTurn` is set and the origin is human-class
     (`wb(origin)`); a batch uses `Messages arrived in the bound thread while you were working:\n`. The
     harness's own trust text calls a turn that opens with either lead the user speaking. Every
     non-user kind gets a different prefix instead. The marker is removed and a text that OPENS with
     either lead is human. A lead quoted inside another message is not human.
  4. User-queued prefix: a substring test accepted `The user sent a new message while you were
     working` anywhere in the first 400 characters, so a peer/coordinator message quoting it was
     human. It is now `lstrip().startswith("The user sent a new message while you were working:")`.
  5. `tests/test_proxy_step_sidecall.py`: `step_kind(body) == CONTINUATION or len(tail) > 1` could not
     fail for a multi-message tail. Now `== CONTINUATION` for all three tails.
- **Fix** (`proxy/steps.py`: `_USER_QUEUED`, `_HUMAN_BOUND_THREAD`, `_NON_USER_MARKERS`,
  `_OBSERVER_DIGEST_*`, `_is_non_user_text`).
- **Serving.** Unchanged. `step_class` reads `_is_continuation`, never `_is_non_user_text`; 18 non-thread
  body shapes give the same `step_class` before (2e28abec) and after.
- **Known limit.** No raw wire body was captured; the prefixes are read from the binary's strings and
  code, not from a recorded request. The observer digest tag/postamble tests are synthesized.
- **Rule.** A wire-form marker is checked at the start of the text it frames; a lead the harness
  places only on trusted posts may not be dropped just because it contains "while you were working".
- **Test.** `tests/test_proxy_step_sidecall.py`, section STEP-SIDECALL-2: async-hook pin; observer
  digest (3 forms); unclassified and bound-conversation activity; verified Slack lead (3 forms) is
  `turn_first`; the lead inside a non-user message is `continuation`; queued prefix must open the text;
  `step_class` for 18 non-thread shapes against golden values taken on 2e28abec. Items 2 (digest,
  bound-conversation activity), 3 and 4 are red on 2e28abec and green here; items 1 and 5 are pins and
  a strengthened assertion (green on both).
