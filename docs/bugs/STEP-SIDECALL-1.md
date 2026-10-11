---
id: STEP-SIDECALL-1
status: fixed in `fix/step-sidecall` (D-59; deploy: restart the proxy; rows written before it keep the old label)
---
## STEP-SIDECALL-1. Human typed-prompt answers were labelled `side_call` (Message Threads) or `continuation` (prompt typed during a tool run)

- **Symptom.** Independent review with `~/.rsi/research/primary-plan/scripts/o3_integrity.py`, window
  2026-09-29T00:00:00Z to 2026-10-11T12:00:00Z, ledger copy `proxy_calls.jsonl` (43,642 rows, md5
  db7657a1, last row 2026-10-11T14:03Z). Session 6cf3b1b8 (proxy-fronted, 26 transcript human turns,
  origin kind `human`): the rows that answer them are 17 `side_call`, 2 `continuation`, 3 `turn_first`,
  4 no row (bypass). Six headless sessions (6808e0b6, e2c48240, e41d3fad, 5ca03cff, 7c91ca57,
  70ed4ff9; 60 to 74 human prompts each): 9 to 26 of each session's first answers are `continuation`
  (94 of 419 with a row). Joined by transcript `message.id` = ledger `msg_id`; counts only, no text
  read. Pooled over organic + headless + research sessions with a transcript human turn in the window
  (N=609): 355 `turn_first`, 96 `continuation`, 17 `side_call`, 39 `subagent_*`, 22 null (pre-GE1),
  80 no row. Of 9,306 `side_call` rows in the window, 6,942 have a request body of under 0.5 bytes per
  token beside more than 100K tokens (median body 21 KB; the 17 human-turn rows: 41 to 120 KB beside
  349K to 958K cached tokens). A title or probe has no such context; these are main-thread calls.
- **Cause 1 (17 of 26 in 6cf3b1b8): Message Threads.** Claude Code 2.1.294 to 2.1.296 (beta
  `message-threads-2026-08-12`, flag `tengu_quiet_heron`) sends a CONTINUE as
  `{"thread": {"type": "continue", "previous_message_id": <reply id>}, "messages": <only the messages
  after that reply>, "tools": <omitted when unchanged>, "system": <trimmed>}` (read from the shipped
  2.1.296 binary on 2026-10-11; the ledger stores no body, so no raw continue was captured).
  `steps.step_kind` answered `side_call` whenever the body had no tool with an `input_schema`
  (`steps.py` `if not _has_client_tools(body): return STEP_SIDE_CALL`), so every continue was a side
  call: the typed prompt, and the tool-result follow-ups beside it. The request bodies in the ledger
  agree: at 2026-10-08T21:29Z a 2.1 MB `turn_first` row (705,036 cached tokens) is followed 17 s later by
  a 73.5 KB `side_call` row on the same prefix (706,881 cached tokens, 1,917 new) that stopped on
  `tool_use`. The last such row is 2026-10-10T00:15:59Z; why they stopped is not established. Message
  Threads is flag-driven and still in the binary, so it can return.
- **Cause 2 (2 of 26, and 94 of 419 headless): a prompt typed during a tool run.** Claude Code adds it
  to the user turn that holds the tool results, as a text block (transcript: a `queue-operation`
  entry before the user entry). `_is_continuation_kind` accepted any text beside `tool_result`, so the
  call was a `continuation` and the prompt's first answer was never a turn.
- **Fix** (`proxy/steps.py`, `proxy/server.py`).
  1. A request with `thread.type == "continue"` is not a side call. Its newest turn may be the only
     message (`_newest_turn_kinds`), it is never a first call (`is_first_call`), and a tool-result
     turn is a `continuation` as before.
  2. A continue has no launcher to read, so main-thread-ness comes from its parent reply: the proxy
     remembers `msg_id -> is_main_thread` (bounded, 8,192) as rows are written and passes it as
     `parent_main`. Sub-agent evidence in the trimmed `system` still vetoes. An unknown parent (proxy
     restarted, parent bypassed) is not proof of a main-thread human turn, so it is `subagent_turn`
     and the ledger's `is_main_thread` is null (same rule as TURNFIRST-2).
  3. A user turn with `tool_result` AND human text (harness blocks and `[Request interrupted` do not
     count) is a human turn, not a continuation: `turn_first` on the main thread, `subagent_turn`
     otherwise.
  4. New text-free ledger column `thread` (`create` | `continue` | null) so the next audit can
     re-label offline; this audit could not, because the ledger did not keep it.
  Not changed: serving (`step_class`) and `tiers.py` still use `has_client_tools`, so a thread
  continue is never served, never re-tiered and never given to the Haiku arm (a model switch inside a
  server-side thread would be refused). `prev_tools` / `prev_tool_class` stay empty for a threaded
  continuation (the tool_use is in the part of the history the body leaves out).
- **Rule.** A missing `tools` field is not evidence that a call is a side call. Read the shape that
  carries the history (`thread`), and take the label of a threaded call from the call it continues.
- **Test.** `tests/test_proxy_step_sidecall.py` (synthesized bodies, no prompt text): human prompt
  on a continue is `turn_first`; tool-result continue is `continuation`; prompt beside tool results
  is `turn_first` (main) or `subagent_turn` (sub-agent); unknown parent and sub-agent marker are not
  main thread; title, probe, summary, server-tool-only and `thread: create` bodies stay `side_call`;
  interrupt and reminder text beside tool results stay `continuation`; serving unchanged; end to end
  through the proxy (create, typed prompt, tool follow-up, mid-run prompt, unseen parent).
  Red on origin/main, green here.
