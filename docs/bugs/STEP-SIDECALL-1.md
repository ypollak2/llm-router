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
     and the ledger's `is_main_thread` is null (same rule as TURNFIRST-2). Null is never stored, so the
     rest of that thread stays unknown, and its human-text continues `subagent_turn`, until a new
     `create` or a continue that carries tools (launcher visible) re-establishes it. That is not a
     one-time miss: it lasts for the remainder of a thread that began before a proxy restart.
  3. A user turn with `tool_result` AND human text (harness blocks and `[Request interrupted` do not
     count) is a human turn, not a continuation: `turn_first` on the main thread, `subagent_turn`
     otherwise.
  4. New text-free ledger column `thread` (`create` | `continue` | null) so the next audit can
     re-label offline; this audit could not, because the ledger did not keep it.
  Serving: `steps.step_class` (the only serving gate) and the strict `_is_continuation` return
  nothing for any thread continue, so a continue is never served, even when it carries a client tool
  and a one-message tail (the first push relaxed the 3-message rule for the label only by changing
  `_newest_turn_kinds`, which the serving predicate also read; review round 2 caught it). `tiers.py`
  and the Haiku arm still use `has_client_tools`, so a tools-less continue is never re-tiered either.
  `prev_tools` / `prev_tool_class` stay empty for a threaded continuation (the tool_use is in the part
  of the history the body leaves out).
  Human text beside tool results is human only if it carries no non-user wire prefix. Read from the
  Claude Code 2.1.296 binary (2026-10-11): the user's own queued prompt is
  `The user sent a new message while you were working:\n<text>\nThis is how Claude Code surfaces...`;
  a coordinator is prefixed `The coordinator sent a message while you were working:`; a peer is
  `<cross-session-message from=...>` / `Another Claude session sent a message...`; a plugin
  `The <name> plugin sent a message...`; an observer `Your background observer (...) sent a report...`;
  a task notification `[SYSTEM NOTIFICATION - NOT USER INPUT]`; other non-user sources
  `[MESSAGE FROM NON-USER SOURCE - NOT USER INPUT]`, `[SCHEDULED TASK`, `... in the bound thread while
  you were working`. Hook output (`hook_additional_context`, `hook_success`, `hook_blocking_error`,
  `hook_stopped_continuation`) is built as `xe({content: UEe(...), isMeta: true})` where `UEe` wraps in
  `<system-reminder>` (`NO="<system-reminder>"`), so the existing harness-tag strip removes it. The
  silent-turn reminder's wrapper was not found; its text ("The user hasn't heard from you in a
  while") is excluded by marker. Known limit: the review's transcript copy (attachment text, before the
  wire prefix is added) over-counted 19 of 53 such blocks; on the wire these carry the prefixes
  above, but no raw wire body was captured, so a coordinator or peer message without its prefix would
  still be counted as human.
  Further known limits (coordinator review of 7192ac31, CC 2.1.296 binary): `async_hook_response`
  output is rendered with `UT(...)` (escape only, no `<system-reminder>` wrap), so its text beside tool
  results would count as human (0 occurrences in a 3-day, 556-transcript sample); `observer-activity`
  and unclassified origin kinds pass through bare (0 seen); the "arrived in the bound thread" marker also
  drops verified Slack human turns; the opt-in `--serve local-agent` path (`local_mode.is_agent_turn`)
  does not consult `step_class` (unchanged from main; protected by the conversation pin). Follow-up:
  STEP-SIDECALL-2.
- **Rule.** A missing `tools` field is not evidence that a call is a side call. Read the shape that
  carries the history (`thread`), and take the label of a threaded call from the call it continues.
- **Test.** `tests/test_proxy_step_sidecall.py` (synthesized bodies, no prompt text): human prompt
  on a continue is `turn_first`; tool-result continue is `continuation`; prompt beside tool results
  is `turn_first` (main) or `subagent_turn` (sub-agent); unknown parent and sub-agent marker are not
  main thread; title, probe, summary, server-tool-only and `thread: create` bodies stay `side_call`;
  interrupt and reminder text beside tool results stay `continuation`; stateless (non-thread) serving
  is byte-identical to origin/main on 12 body shapes (coordinator review); end to end
  through the proxy (create, typed prompt, tool follow-up, mid-run prompt, unseen parent, an unknown
  chain resolved by a continue that carries tools); a continue with client tools and a one- or
  multi-message tail is never served and `try_serve` is never reached; agent-to-agent and harness
  prefixes beside tool results stay `continuation`.
  Red on origin/main, green here.
