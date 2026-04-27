# Security Design & Implementation

## Architecture

### Threat Model

We assume:
- ✅ Local user is trusted
- ✅ Your API keys are kept secret
- ⚠️ Providers (OpenAI, Gemini) are trusted but may be compromised
- ❌ Network is potentially untrusted (HTTPS mitigates)
- ❌ Machines you don't control may run hooks

### Layers

1. **Input layer** — Prompt sanitization
2. **Classification layer** — Injection detection
3. **Routing layer** — Provider selection
4. **Call layer** — API key management
5. **Logging layer** — Secret scrubbing
6. **Persistence layer** — SQLite protection

## Security Implementation

### Prompt Injection Prevention

- All user input is wrapped with delimiters before sending to classifier
- Dangerous keywords (override, bypass, jailbreak, etc.) are detected
- Injection attempts are logged for audit

**Implementation details:**
- PromptSanitizer class wraps all user prompts with marker tokens
- Regex patterns detect common injection markers
- Classification model trained to ignore injection attempts
- Failed injection attempts logged with timestamp and context

### Secret Scrubbing

- Structured logging automatically masks:
  - API keys (sk-*, AIza*, etc.)
  - Bearer tokens
  - Passwords
  - Environment variables
- Patterns are regex-based and extensible

**Implementation details:**
- SecretScrubber uses regex patterns for common formats
- Custom patterns can be added via configuration
- All structured logs pass through scrubber before persistence
- Scrubbing happens at log write time (before SQLite storage)

### Hook Security

- Hooks are analyzed for deadlock risks before installation
- Core tools (Read, Edit, Bash, Agent) cannot all be blocked
- Recovery procedure is `export LLM_ROUTER_ENFORCE=off`

**Implementation details:**
- HookDeadlockDetector analyzes all ~/.claude/hooks/ files
- DFS-based circular dependency detection
- Subprocess timeout validation (>1 hour flagged)
- Resource contention detection (file conflicts)
- Pre-installation check in install_hooks.py

**Deadlock prevention guarantees:**
- _BASE_BLOCK_TOOLS never contains: Read, Glob, Grep, LS, Agent
- File-op detection triggers before blocklist check
- Auto-pivot from hard to soft enforcement after 2 violations
- Session type tracking prevents over-aggressive blocking

### Provider Integration

- Each provider's API call is authenticated with the user's key
- Requests use HTTPS (no plaintext)
- Timeouts prevent hanging requests
- Error responses are sanitized before logging

**Implementation details:**
- APIKeyManager stores keys from .env and config.yaml
- Each provider chain includes timeout configuration (30s default)
- ProviderHealthTracker monitors provider availability
- Failed provider calls logged with error class (not details)

## Audit & Compliance

### What's Logged

- ✅ Timestamp
- ✅ Task type + complexity
- ✅ Selected model
- ✅ Token count estimate
- ✅ Cost estimate
- ✅ Success/failure outcome
- ❌ Full prompt (only task_type)
- ❌ API keys
- ❌ Provider response content

### Audit Log Access

- SQLite stored at `~/.llm-router/usage.db`
- Only readable by your user (0600 permissions)
- SQL queries via `llm-router` CLI
- Daily backups recommended for important deployments

**Audit query examples:**
```bash
# View all calls from today
llm_query "SELECT * FROM routing_calls WHERE timestamp > datetime('now', '-1 day')"

# Find expensive calls
llm_query "SELECT * FROM routing_calls WHERE estimated_cost > 0.01 ORDER BY cost DESC"

# Audit trail by model
llm_query "SELECT model, COUNT(*), SUM(cost) FROM routing_calls GROUP BY model"
```

## Testing

### Security Test Coverage

- `test_prompt_injection.py` — 8+ injection vectors
- `test_secret_leakage.py` — 6+ secret types
- `test_hook_deadlock_detection.py` — 3 risk types
- CI runs security scans (pip-audit, bandit, etc.)

**Test categories:**
1. **Injection tests** — Verify injection markers are detected
2. **Secret tests** — Verify secrets are scrubbed from logs
3. **Hook tests** — Verify deadlock detection works
4. **Dependency tests** — Verify no known CVEs in deps
5. **Rate limit tests** — Verify limits respected
6. **Error handling tests** — Verify errors are sanitized

### Running Security Tests

```bash
# Fast security tests
uv run pytest tests/ -k security -q

# Full security suite
uv run pytest tests/test_prompt_injection.py tests/test_secret_leakage.py -v

# Dependency audit
pip-audit --skip-editable

# Bandit code analysis
bandit -r src/ -ll
```

## Known Gaps (Future Work)

1. **Multi-user SQLite** — Need Redis backend
   - Status: Planned for v7.7
   - Impact: Currently not suitable for team environments
   - Mitigation: Deploy separate per-user instances

2. **Encrypted hook storage** — Keys visible in filesystem
   - Status: Planned for v7.8
   - Impact: Compromised machine can read hook scripts
   - Mitigation: Use strong machine authentication

3. **Audit log signing** — Logs could be modified
   - Status: Planned for v8.0
   - Impact: Cannot verify log integrity
   - Mitigation: Store offline backups

4. **Hardware key support** — No HSM integration yet
   - Status: Planned for v8.1
   - Impact: API keys stored in filesystem
   - Mitigation: Use environment variables with restricted access

## Dependencies & CVE Monitoring

### Core Dependencies

| Package | Version | Purpose | CVE Status |
|---------|---------|---------|-----------|
| mcp | >=0.7.0 | Protocol layer | OK |
| litellm | >=1.40.0 | Provider abstraction | OK |
| pydantic | >=2.0.0 | Data validation | OK |
| aiosqlite | >=0.19.0 | Async SQLite | OK |

### Optional Dependencies

| Package | Version | Purpose | CVE Status |
|---------|---------|---------|-----------|
| fastapi | >=0.104.0 | Dashboard API | OK |
| opentelemetry-api | >=1.18.0 | Tracing | OK |

### Monitoring

- CI runs `pip-audit` on every commit
- GitHub dependabot alerts enabled
- Manual review of security advisories weekly
- Security patches released within 24 hours of disclosure

## Incident Response

### If a vulnerability is reported:

1. **Immediate (0-2 hours)**
   - Acknowledge receipt
   - Triage severity
   - Open private security advisory

2. **Assessment (2-24 hours)**
   - Reproduce the issue
   - Determine scope and impact
   - Estimate patch timeline

3. **Development (1-7 days)**
   - Develop fix
   - Write security tests
   - Prepare release notes

4. **Release (1-2 days after fix)**
   - Release patch version
   - Publish security advisory
   - Credit researcher (if requested)

5. **Post-mortem (1-2 weeks)**
   - Analyze root cause
   - Add preventive tests
   - Update documentation

---

Last updated: 2026-04-27
Document version: 1.0.0
