context pack, cloud doors (owner decision D-101): a cloud model's pack window is now its registry context window, capped at 65,536 tokens (`context_pack.CLOUD_WINDOW_CAP`).
- The window comes from `model_registry.context_window`, read from `config/models.yaml`. It is matched by exact id, else by the longest registry id the model extends, else by the smallest window in the model's family. That last rule is how the routed `anthropic/claude-sonnet-4-6` resolves.
- Before, the MCP door used 8,192 for every cloud model, and the gateway and SDK used 32,768.
- A conversation of up to ~52k tokens now goes whole to a known cloud model. Beyond that, the pack (the last 7 messages plus the rolling summary) applies as before.
- Unchanged: models the registry does not know keep the old defaults, local models keep their `num_ctx`, and the claude-CLI dispatch path keeps 32,768.
- A wheel install ships no `config/models.yaml`, so most current models keep the old windows there.
- Expect larger cloud prompts: up to ~52k input tokens per call, about $0.16 at $3/M.
- New: `scripts/g1_synthetic_evidence.py equivalence` (PREREG-P11B-synthetic-R2), the $0 proof for the P1.1-b Sonnet stratum at the MCP door, with no model calls (D-102).
