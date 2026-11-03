---
id: BINDAUDIT-4
status: fixed in this change (BIND W3, branch `bind/audit-metrics`; audit v2)
---
## BINDAUDIT-4. A verbatim relay of an MCP answer almost never scored used: the audit compared the JSON envelope, header and footer

- **Symptom.** `~/.rsi/research/primary-plan/v16/coord/BIND-R2-mcp-unused-20261103.md` section 8 (the n=113 `llm` results of `~/.rsi/research/primary-plan/v16/rel16-main/release-artifacts/outcome-audit/outcome_audit_16.0.0.jsonl`): a verbatim copy of the answer body scored
  `used` for 9, `unknown` for 35 and `unused` for 69. Unwrapping the envelope alone gave 29 / 18 / 66. 62 of the 73 ack
  bodies are under 8 words, which no 8-word shingle can match. Over the 150 distinct routed_mcp calls the old audit
  read 0 used; the hand reading found 10.
- **Cause.** `label_routed_mcp` in `scripts/release/outcome_audit.py` took 8-word shingles of the raw tool_result:
  the `{"result": ...}` envelope, the `> 🤖 **model** · N tokens · $x · Nms` header, the `─────` footer, with the JSON
  `\n` glued onto the next word ("nhttp", "nok"). Those words are not in anything the host relays. A body under 8 words
  has no shingle at all.
- **Fix.** `answer_body` unwraps the envelope and removes the header, a `_validation:` line, a route line and the
  footer (`llm`, cache hits, `llm_route`, `llm_text_job`). `shingle_size` follows the body: 8 words from 20 words up,
  4 from 8, the whole body for 3 to 7 words (containment), and under 3 words nothing can be attributed (`unknown`,
  because any host text "contains" `Hash.`). On the 16.0.0 population (n=150 distinct calls, overrides lifted, via
  `scripts/release/audit_agreement.py`) the two bodies BIND-R2 found relayed verbatim (a git one-liner at 75% of
  4-grams, a commit message at 92%) are the two `used` text results.
- **Test.** `tests/test_bind_audit.py::test_the_raw_envelope_never_reached_the_used_threshold_but_the_body_does`,
  `::test_answer_body_strips_envelope_header_and_footer`,
  `::test_short_bodies_use_containment_and_under_three_words_cannot_be_attributed` and
  `::test_medium_body_uses_four_word_shingles`.
