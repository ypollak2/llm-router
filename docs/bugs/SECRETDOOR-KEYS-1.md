---
id: SECRETDOOR-KEYS-1
status: fixed in `w11/codex-meter-reserve-local-fallback` (takes effect for every door that scans: restart the MCP server; the hook is `agent-route.py` stamp 28)
---
## SECRETDOOR-KEYS-1. The secret door let a private-key block through to Codex

- **Symptom (2026-11-04).** An offline census of the delegation gates (131 real first prompts of workflow agents plus synthetic
  controls, `scratchpad/gate0d-census/tools/gate0d_census.py`) ran four fake secrets through `agent-route.py`'s `_secret_door`:
  a GitHub token, an AWS key id and an Anthropic key were held, `-----BEGIN RSA PRIVATE KEY-----` + a one-line body +
  `-----END RSA PRIVATE KEY-----` was not. A prompt like that is sent to Codex, which is OpenAI, and to every other cloud door that
  scans with `engine.secret_findings`.
- **Cause.** `engine._pem_block_is_real` took a key header as a hit only when the next 4,096 characters, whitespace removed, were at least 40
  characters of pure base64 (`[A-Za-z0-9+/=]+`). Four ordinary ways a key reaches a prompt failed that: a truncated paste (the census case,
  17 characters); a key inside a JSON or YAML string, where the line breaks are the two characters backslash-n (the `private_key` of a Google
  service-account file); a legacy encrypted PEM, whose `Proc-Type:` / `DEK-Info:` header lines are not base64; and a PGP block, whose header
  `-----BEGIN PGP PRIVATE KEY BLOCK-----` the header regex (`PRIVATE KEY-----`) never matched. PuTTY `.ppk` files and the four-dash ssh.com
  form had no shape at all, and ten common provider keys with a distinctive prefix (Stripe live, SendGrid, Hugging Face, npm, GitLab, PyPI,
  DigitalOcean, a Slack webhook URL, age, an Azure storage account key) had none either.
- **Fix.** `engine.py`: the header regex covers `PRIVATE KEY BLOCK` and four dashes; the body is the leading run of base64 tokens of at least 16
  characters after armor headers are dropped and escaped line breaks (`\n`, `\\n`, `\r`, `\t`) are read as breaks, and what follows that run is
  ignored; `putty_key` and the ten provider shapes are added to `HIGH_PRECISION` with labels. A bare header in a question, an elided body (`...`),
  filler (`xxxx`), `<placeholder>` and a sentence after a header still pass. `hf_` and `npm_` (3 and 4 characters) must start a run: unanchored,
  one turned up by chance in 2 of 4 random 100 KB blobs (`tests/test_secdet_token_not_surroundings.py` caught it). `secret_scrubber.py`
  redacts the same header forms. Checked on the real population: 131 full first prompts of workflow agents (675,930 characters,
  `scratchpad/gate0d-census/workflows-copy`) and 1,388 stored sub-agent prompt previews (692,392 characters, `agent_calls_ledger.jsonl` copy of
  2026-11-04): 0 high-precision hits before the change and 0 after, no text changed its verdict.
- **Test.** `tests/test_w11_secret_door_key_formats.py` (104 cases): 17 private-key formats and 11 provider keys must be held, 13 questions,
  placeholders and public certificates must not, the existing placeholder corpus still passes, the hook's own `_secret_door` holds the census
  case, a key past the 16 KiB scan window is found, 18 adversarial 48 KB inputs scan in under 250 ms each (timing lane), and the long-blob false positive stays closed. The first version of the header check read 4 KB after every header and took 250.6 ms on the CI runner for the repeated-header input of `tests/test_lc12h_repair3.py` (budget 250 ms; 23 ms before, 7.5 ms now): it reads 1 KB lazily.
  Residual: a key glued directly to letters in front of `hf_` or `npm_` is not found.
