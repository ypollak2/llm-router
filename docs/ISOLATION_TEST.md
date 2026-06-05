# Router Isolation Test Suite

A comprehensive test framework for verifying that llm-router is working correctly in isolated environments. Ensures:

1. **No cache contamination** — each test run is fresh, not relying on stale cache
2. **Routing logic integrity** — routing decisions are sensible (simple prompts don't overspend, etc.)
3. **Dashboard accuracy** — cost tracking, savings reports, and usage metrics are accurate and fresh

## Quick Start

### Run tests manually

```bash
# Run all isolation tests
./scripts/router_isolation_test.sh

# Check the latest result
./scripts/router_isolation_test.sh status

# View test logs
./scripts/router_isolation_test.sh logs
```

### Set up automatic testing

```bash
# Show cron examples
./scripts/router_isolation_test.sh cron-example

# Add to crontab (e.g., run every 6 hours)
(crontab -l 2>/dev/null; echo "0 */6 * * * /full/path/to/router_isolation_test.sh") | crontab -

# Enable Slack alerts (optional)
export LLM_ROUTER_ALERT_WEBHOOK="https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
```

---

## What the tests do

### Isolation Tests

**`test_no_cache_between_runs`**
- Runs the same prompt twice in separate isolated subprocess environments
- Verifies that the second run doesn't use cache from the first
- Confirms routing happens fresh each time

**`test_cache_isolation_across_processes`**
- Spawns multiple parallel processes with isolated environments
- Ensures they don't contaminate each other's routing cache
- Validates that concurrent requests work independently

### Routing Sanity Tests

**`test_routing_decisions_are_reasonable`**
- Routes diverse prompts from the RouterArena dataset
- Validates that routing decisions are sensible:
  - No invalid model selections (e.g., "unknown", "error")
  - Variety in model selection across different prompts
  - Each prompt gets a reasonable model choice

**`test_routing_classification_consistency`**
- Routes the same prompts multiple times
- Verifies classification (easy/medium/hard complexity) is stable
- Ensures routing decisions are deterministic (or have reasonable variation)

### Dashboard Accuracy Tests

**`test_dashboard_savings_accuracy`**
- Routes a few test prompts
- Verifies that `llm-router status` shows accurate usage metrics
- Checks that `llm-router last` shows the prompts we just routed
- Confirms that `savings-report` totals are consistent

**`test_dashboard_cost_tracking_fresh`**
- Routes a prompt and immediately checks the dashboard
- Verifies that cost tracking is up-to-date (not stale)
- Ensures the dashboard reflects real-time routing decisions

---

## Understanding Results

### Report Format

After running tests, results are saved to `.router-test-results/latest.json`:

```json
{
  "status": "passed",
  "timestamp": "2026-06-05T11:30:45+00:00",
  "duration_seconds": 42,
  "tests": {
    "isolation": "passed",
    "routing": "passed",
    "dashboard": "passed"
  },
  "details": "All isolation and routing tests passed"
}
```

### Interpreting Failures

**`isolation: failed`**
- Cache contamination between runs
- **Action**: Check that your `.llm-router/` cache directory doesn't have stale entries
- **Fix**: Run `llm-router doctor` to diagnose cache issues; consider clearing cache with `rm -rf ~/.llm-router/cache`

**`routing: failed`**
- Routing decisions are inconsistent or invalid
- **Action**: Check recent changes to routing logic or policy
- **Possible causes**:
  - Policy file (`LLM_ROUTER_POLICY`) is malformed
  - Provider quotas are exhausted
  - Model pool has invalid entries

**`dashboard: failed`**
- Cost tracking or savings reporting is inaccurate
- **Action**: Check that `llm-router last`, `status`, and `savings-report` commands work
- **Fix**: Run `llm-router doctor` to check dashboard health

---

## Advanced Usage

### Run specific test

```bash
pytest tests/test_isolation_routing.py::test_no_cache_between_runs -v
```

### Run with detailed output

```bash
pytest tests/test_isolation_routing.py -vv --tb=long
```

### Run with coverage

```bash
pytest tests/test_isolation_routing.py --cov=llm_router --cov-report=html
```

### Configure cron with custom schedule

```bash
# Run every 4 hours with logging to a file
0 */4 * * * /path/to/router_isolation_test.sh >> /var/log/router-test.log 2>&1

# Run daily at 2 AM with Slack alert on failure
0 2 * * * bash -c 'export LLM_ROUTER_ALERT_WEBHOOK="https://..."; /path/to/router_isolation_test.sh'
```

### Enable email alerts

```bash
# Set up mail command (macOS example)
export MAIL_RECIPIENT="your-email@example.com"

# In the script, mail will be sent on test failure
```

---

## Troubleshooting

### "llm-router not found in PATH"

**Error**: `ERROR: llm-router not found in PATH`

**Fix**: Make sure `llm-router` is installed and on your PATH:
```bash
which llm-router
llm-router --version
```

If not installed, install via `uv`:
```bash
uv tool install llm-routing
```

### "pytest not found"

**Error**: `ERROR: pytest not installed`

**Fix**: Install pytest:
```bash
pip install pytest
# or
uv pip install pytest
```

### Tests timeout

**Error**: `subprocess.TimeoutExpired`

**Cause**: Routing is taking longer than expected (likely external API call delays)

**Fix**: Increase timeout in `test_isolation_routing.py` (default: 30s)

### Cache files not cleaned up

**Error**: Isolation tests show cache from previous runs

**Fix**: Clear the test cache directory:
```bash
rm -rf ~/.llm-router/cache
rm -rf /tmp/llm_router_isolation_*
```

---

## Integration with CI/CD

### GitHub Actions Example

```yaml
name: Router Isolation Tests

on:
  schedule:
    # Run every 6 hours
    - cron: '0 */6 * * *'
  workflow_dispatch:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install pytest
      - run: uv tool install llm-routing
      - run: ./scripts/router_isolation_test.sh
      - if: failure()
        uses: slackapi/slack-notify-action@v1
        with:
          webhook_url: ${{ secrets.SLACK_WEBHOOK }}
          text: 'Router isolation tests failed'
```

### GitLab CI Example

```yaml
router_isolation_test:
  stage: test
  script:
    - pip install pytest
    - uv tool install llm-routing
    - ./scripts/router_isolation_test.sh
  artifacts:
    paths:
      - .router-test-results/
    expire_in: 30 days
  only:
    - schedules
  allow_failure: true
```

---

## Architecture

### Test Isolation Strategy

Each test that requires isolation uses a temporary directory:

1. **Create temp dir**: `/tmp/llm_router_isolation_<random>/`
2. **Override environment**:
   - `HOME=/tmp/llm_router_isolation_<random>/`
   - `LLM_ROUTER_DB=/tmp/.../router.db`
   - `LLM_ROUTER_CACHE=/tmp/.../cache`
3. **Run subprocess**: Fresh Python environment with no inherited cache
4. **Cleanup**: Temp dir is automatically cleaned after test

This ensures:
- No cache bleed between tests
- No interaction with the user's real `~/.llm-router/` state
- Parallel test execution is safe

### Test Data

Tests sample prompts from the **RouterArena dataset**:
- Location: `~/../RouterArena/dataset/router_data_10.json`
- Format: JSON list of prompt objects
- Provides diverse, real-world routing scenarios across 9 domains and 44 categories

If the dataset is not available, tests are skipped with a clear message.

---

## Maintenance

### Update test data

To use a different dataset or expand the sample:

```python
# In test_isolation_routing.py, modify:
ROUTERARENA_DATA = Path("path/to/your/dataset.json")

# Or add additional datasets:
ADDITIONAL_DATA = Path("path/to/extra_prompts.jsonl")
```

### Add custom test cases

To add test cases specific to your routing policy:

```python
@pytest.mark.parametrize("prompt,expected_complexity", [
    ("What is 2+2?", "easy"),
    ("Explain quantum entanglement", "medium"),
])
def test_custom_routing(prompt, expected_complexity):
    result = _run_router_live(prompt)
    assert result["complexity"] == expected_complexity
```

### Extend dashboard validation

To verify additional dashboard metrics:

```python
def test_custom_dashboard_metric():
    # Run router
    _run_router_isolated("Test prompt", ...)

    # Check custom metric
    status = _run_llm_router_cmd(["budget"])
    assert "usage" in status.lower()
```

---

## FAQ

**Q: How often should I run these tests?**

A: Start with daily (2 AM) and adjust based on your deployment schedule. If you release daily, run hourly or after each release.

**Q: Can tests run in CI without my API keys?**

A: Tests can run in "demo mode" without live API calls (using cached routing decisions). To test actual routing, ensure API keys are available via CI secrets.

**Q: What if tests flake intermittently?**

A: Intermittent failures usually indicate:
- External API timeouts (increase timeout, retry logic)
- Cache races (ensure temp dirs are fully isolated)
- Quota limits (check your API limits)

Run with verbose logging: `pytest -vv --tb=long`

**Q: How do I debug a failed test?**

A: View the test log:
```bash
./scripts/router_isolation_test.sh logs
```

Or run the specific test with debugging:
```bash
pytest tests/test_isolation_routing.py::test_name -vv --capture=no
```

**Q: Can I customize the test scope?**

A: Yes! Modify test parameters in `test_isolation_routing.py`:
- Change number of sample prompts (line: `sample_indices = [...]`)
- Adjust timeout (line: `timeout=30`)
- Filter by prompt category (modify `diverse_test_prompts` fixture)

---

## See Also

- `llm-router doctor` — diagnose router health
- `llm-router last` — view recent routing decisions
- `llm-router savings-report` — detailed cost breakdown
- `docs/policies/` — routing policy documentation
