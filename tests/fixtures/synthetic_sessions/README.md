Synthetic replay fixture for `scripts/synthetic_replay.py` (tests/test_synthetic_replay.py).

Same record format as the v16 synthetic corpus: one JSON object per line,
`{session_id, turn, step: turn_first|continuation|subagent, role, messages, intended_category, lang, thread, t}`.
Records are deltas: the request at a step is every earlier `messages` list of the same
`(session_id, thread)` concatenated with this one. All text is invented for this fixture;
no real prompt was read or copied. 4 sessions (6, 5, 2 and 1 turns), 22 records.
