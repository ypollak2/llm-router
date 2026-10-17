---
id: CLS-FREEZE-1
status: fixed in `fix/cls-pins-trunc`
---
## CLS-FREEZE-1. CLS-DEV pins could not resolve a bare Ollama name, kept only the last digest of a repeated name, and C3 truncation cut the digest first

- **Symptom (CLS-DEV freeze prep, 2026-10-17, PR #545 report).** `cls_pins.py pins --static` exited 9
  (`static pins not computable: ollama`): `OLLAMA_MODELS` asks for `nomic-embed-text`, `/api/tags` lists only
  `nomic-embed-text:latest`; B0's C2 embedder recorded `model_digest: null` for the same reason. `qwen3.5:latest`
  is listed twice in `/api/tags` with two digests and the pin kept whichever came last. CD-18 says the digest is
  kept whole and the prompt tail kept; C3 pre-trimmed by whitespace words and the ModernBERT tokenizer then
  truncated from the LEFT over the whole string, cutting `[ctx] <digest>` first; C2 counted words against a
  512 cap that words under-count.
- **Cause.** Exact-string lookup of the name in a `{name: digest}` dict built from `/api/tags` (last row wins), in
  `cls_pins.compute_static` and `cls_dev_lib.Ollama.tags`; `cmd_fit_modernbert` passed `count_words` to
  `model_input` and `modernbert_oof` loaded the tokenizer with `truncation_side="left"`.
- **Fix (D-169 (1)-(3)).** `cls_pins.ollama_name` normalises a bare name to `<name>:latest`;
  `cls_pins.ollama_digest_sets` keeps the sorted set of every digest listed under a name. The ollama pin and B0's
  `embedder.model_digests` record that list, so `pins --verify` fails when the set changes. C3 counts real tokens
  (`hf_token_counter` over the ModernBERT tokenizer, special tokens included) so `model_input` keeps the digest
  whole and the prompt tail within 512 tokens; the tokenizer truncates on the right. C2's cap is 384 whitespace
  words (`C2_WORD_CAP`, an approximation: no local tokenizer), digest whole, tail kept; artifacts record
  `input_cap` with its unit. C1 unchanged.
- **Test.** `tests/test_cls_bl.py`: `test_ollama_name_bare_means_latest`,
  `test_ollama_digest_sets_keep_every_digest_sorted_never_last_wins`,
  `test_static_pin_resolves_bare_nomic_name_to_latest`,
  `test_static_pin_records_all_digests_of_a_duplicate_name_and_verify_fails_on_set_change`.
  `tests/test_cls_dev_run.py`: `test_c2_embedder_digests_bare_name_latest_and_every_duplicate`,
  `test_c2_input_cap_384_words_digest_whole_tail_kept`, `test_c3_inputs_real_token_cap_digest_whole_tail_kept`,
  `test_c3_tokenizer_truncates_right_never_left`. Ten source mutants (one or more per item) each turn at least
  one of them red.
