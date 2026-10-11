---
id: AUTO-PROFILE-OVERWRITE-1
status: fixed in `fix/p015-safety-floor` (PLAN v16 P0.15 task 1; owner decisions D-60b = A, D-65 = A)
---
## AUTO-PROFILE-OVERWRITE-1. The hourly rescan overwrote a user-edited `~/.llm-router/profile.yaml`

- **Symptom.** A user who edited `profile.yaml` (`routing.model_priority`, `quotas`, the commented
  `overrides:` and `budget:` blocks the file invites) lost the edit the next time a detected service
  or quota changed. No merge, no marker, no backup, no message.
- **Cause.** `auto_profile.rescan_and_update` regenerated the file and wrote it through
  `save_profile`, which was an unconditional `write_text`. The SessionEnd hook calls it hourly
  (`SCAN_INTERVAL_SECONDS = 3600`). Only `quotas` and `routing.model_priority` have readers
  (`user_routing_policy`, `dynamic_routing`), so the live routing effect was limited to those two;
  the trust damage covered the whole file.
- **Fix.** `save_profile` writes the file atomically and records the sha256 of the bytes it wrote
  in its own file, `profile_hash.json`. It is not kept in `profile_cache.json`, because
  `save_detection_cache` rewrites that whole file on every rescan and would erase it. A rescan
  writes `profile.yaml` only when the file is absent or still hashes to the recorded value. Any
  other file (user edit, legacy file with no recorded hash, CRLF, empty, non-YAML) is left byte for
  byte as it is; the regeneration goes to `profile.generated.yaml` (informational, no reader) and
  one note is produced per change of that content: in the SessionEnd stop note, in
  `llm-router doctor` and in `llm-router map`. The explicit `llm-router profile auto` copies the
  current file to `profile.yaml.<UTC timestamp>.bak` first (never overwriting a `.bak`), then
  writes and records the hash.
  **Accepted trade-off (D-60b = A).** Every existing user has no recorded hash, so every existing
  `profile.yaml` counts as edited: it stops auto-refreshing until `llm-router profile auto` is run,
  and the `quotas` and `routing.model_priority` it holds go stale for their two readers.
- **Test.** `tests/test_p015_profile_hash.py` (P0.15-a): an unmodified file is refreshed on three
  consecutive rescans and its hash survives `save_detection_cache`; five fixtures (edited,
  legacy-no-hash, CRLF, empty, non-YAML) are byte-identical after three rescans with a detected
  change each time; `profile auto` makes the backup and records the hash. T9f in
  `tests/test_p015_user_files_untouched.py` checks sha256 and a write recorder on six fixture
  HOMEs; its negative control (the old unconditional write) fails.
