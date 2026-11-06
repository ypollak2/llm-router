---
id: CLS-REMOTE-1
status: fixed in `w10/local-classifier-decides` (review follow-up): the classifier refuses a non-loopback Ollama, and the secret door runs first
---
## CLS-REMOTE-1. The classifier would send a prompt to any `LLM_ROUTER_OLLAMA_URL`

- **Symptom (independent review of 437f5f93).** `local_classifier._base_url()` takes `LLM_ROUTER_OLLAMA_URL` and `local_privacy.open_url` falls back to plain
  `urllib.request.urlopen` for a base that is not loopback (`src/llm_router/local_privacy.py`), so with such a setting every classified prompt
  (the hooks' and the proxy's) could leave the machine. And the hooks started the verdict before the secret door.
- **Cause.** The classifier trusted the configured base, and `door_verdict.start` did not scan the text first.
- **Fix.** `local_classifier` refuses a base that `local_privacy.is_loopback_base` does not accept (`refused_remote`, no request) unless
  `LLM_ROUTER_CLASSIFIER_ALLOW_REMOTE=1`; the shared client, the async path, the warm-up and the hook's residency probe all go through it.
  `door_verdict.start` runs `local_privacy.scan_secrets` first (the door the Agent hook's Codex path uses; an error holds): a prompt that holds a
  secret is classified by no model, loopback or not (`withheld_secret`), in both hooks.
- **Test.** `tests/test_door_verdict.py`: `test_a_non_loopback_ollama_is_refused_before_any_request`, `test_the_remote_opt_in_is_explicit_and_only_that_env`,
  `test_with_the_opt_in_a_remote_base_is_used`, `test_a_prompt_holding_a_secret_is_classified_by_no_model`;
  `tests/test_door_verdict_hooks.py`: `test_a_prompt_with_a_secret_is_never_sent_to_the_model_by_either_hook`.
