# Router Isolation Test — Quick Start

Tests that llm-router is working correctly: routing is sensible, no cache contamination, dashboard is accurate.

## Run Tests

```bash
# Manual run
./scripts/router_isolation_test.sh

# Check results
./scripts/router_isolation_test.sh status

# View logs
./scripts/router_isolation_test.sh logs
```

## Automate with Cron

```bash
# Show examples
./scripts/router_isolation_test.sh cron-example

# Add to crontab (every 6 hours)
(crontab -l 2>/dev/null; echo "0 */6 * * * $(pwd)/scripts/router_isolation_test.sh") | crontab -

# Enable Slack alerts (optional)
export LLM_ROUTER_ALERT_WEBHOOK="https://hooks.slack.com/services/YOUR/WEBHOOK"
```

## What's Tested

| Test | Purpose |
|---|---|
| `test_no_cache_between_runs` | Same prompt twice → both fresh (no cache reuse) |
| `test_cache_isolation_across_processes` | Parallel runs → isolated cache (no contamination) |
| `test_routing_decisions_are_reasonable` | Diverse prompts → sensible model selection |
| `test_routing_classification_consistency` | Repeat prompts → stable classification |
| `test_dashboard_savings_accuracy` | Route → dashboard shows correct costs |
| `test_dashboard_cost_tracking_fresh` | Route → dashboard reflects immediately |

## Results Location

- **Report**: `.router-test-results/latest.json`
- **Logs**: `.router-test-results/test.log`

## Troubleshooting

**Tests timeout?** Check `llm-router --version` and API key availability.

**llm-router not found?** Install with `uv tool install llm-routing`.

**Cache issues?** Clear with `rm -rf ~/.llm-router/cache`.

See the isolation tests under `tests/` for details.
