# Security Policy

## Reporting Security Vulnerabilities

If you discover a security vulnerability in llm-router, please **do not open a public GitHub issue**. Instead, email:

**yali.pollak@gmail.com**

Please include:
- Description of the vulnerability
- Steps to reproduce (if applicable)
- Potential impact
- Suggested fix (if you have one)

**Response timeline:**
- We will acknowledge receipt within 24 hours
- We will provide an initial assessment within 3 business days
- We will work on a fix and provide a timeline for release
- We will credit you in the security advisory (unless you request anonymity)

## Security Best Practices

### Data Handling & Storage Mapping

**What llm-router does with your prompts:**
- ✅ Routes to the best model based on complexity
- ✅ Tracks token usage locally (SQLite on your machine)
- ✅ Caches classification results (locally, with TTL)
- ❌ Does NOT store your prompts permanently
- ❌ Does NOT log full prompts (only task type + complexity)
- ❌ Does NOT share prompts between providers unless you ask

**Data Storage Locations:**

| Data Type | Storage | Location | Encryption | Retention |
|-----------|---------|----------|-----------|-----------|
| **Prompts** | Not stored | Transient (memory) | N/A | 0 seconds |
| **API Keys** | File | `.env` / `~/.llm-router/config.yaml` | Filesystem perms | Until rotated |
| **Usage metrics** | SQLite DB | `~/.llm-router/usage.db` | Filesystem perms | 180 days (configurable) |
| **Classification cache** | In-memory LRU | Process memory | N/A | 24 hours |
| **Hook scripts** | Files | `~/.claude/hooks/` | Filesystem perms | Until uninstalled |
| **Logs** | Structured JSON | Stdout + optional file | Filesystem perms | 7 days default |
| **OAuth tokens** | File (encrypted) | `~/.cache/anthropic/` | OS keyring | Until revoked |

**Multi-provider routing:**
When you route a task, your prompt is sent to:
1. **Local Ollama** (if configured) — stays on your machine
2. **Your configured providers** (OpenAI, Gemini, etc.) — governed by their privacy policies
3. **Not** to llm-router servers — we don't run servers

**Encryption Status:**
- ✅ API keys in transit: TLS (1.2+) to providers
- ✅ OAuth tokens: Stored in OS keyring (macOS Keychain, Linux Secret Service)
- ⚠️ SQLite database: Stored unencrypted on disk (encrypted filesystem recommended)
- ⚠️ Config files: Stored with filesystem permissions (600)

**Data Retention & Deletion:**
- Usage metrics: Kept for 180 days (configurable via `LLM_ROUTER_METRICS_RETENTION_DAYS`)
- Classification cache: Cleared every 24 hours or on process restart
- OAuth tokens: Cached until revoked or manually deleted
- Logs: Rotated daily, kept for 7 days
- Prompts: Never persisted to disk

To delete all llm-router data:
```bash
# Remove all stored data
rm -rf ~/.llm-router/
rm -rf ~/.cache/anthropic/
rm ~/.env  # If used

# Remove hooks
~/.claude/hooks/auto-route.py --uninstall
```

**Your responsibility:**
- Ensure your prompt doesn't contain sensitive data you don't want to share
- If using team features, be aware prompts may be logged for auditing
- Review provider privacy policies (especially if routing to Gemini or GPT-4o)
- Keep filesystem encrypted if storing sensitive metrics

### API Key Storage

**How API keys are stored:**
- `.env` file (project-level) — local, not checked in git
- `~/.llm-router/config.yaml` — user-level, readable by user only
- Environment variables — volatile (only for current session)

**Key rotation:**
- Keys are never refreshed automatically (except OAuth tokens)
- Replace keys in `.env` or `config.yaml` to rotate
- llm-router doesn't cache keys beyond the current session

### Prompt Injection Protection

**llm-router protects against:**
- ✅ Prompts that try to override routing rules
- ✅ Prompts that contain secret formatting (delimited in logs)
- ✅ Injection markers detected and logged

**Limitations:**
- ❌ Cannot prevent model jailbreaks (up to provider)
- ❌ Cannot guarantee secrets won't be exposed if user puts them in prompt
- ❌ Malicious prompts still reach the model (as intended)

### Dependency Security

**llm-router dependencies:**
- Core deps: mcp, litellm, pydantic, aiosqlite
- Optional deps: fastapi (dashboard), opentelemetry (tracing)

**Our commitment:**
- ✅ Use only well-maintained, widely-used packages
- ✅ Pin dependency versions to avoid surprise updates
- ✅ Run `pip-audit` in CI to detect known CVEs
- ✅ Subscribe to security advisories for key dependencies
- ❌ Do not use unaudited or experimental packages

**Your responsibility:**
- Keep llm-router updated (we release security patches promptly)
- Keep Python updated (3.10–3.13 supported)
- Keep your API keys secret

### Hook Security

**Hooks are shell scripts installed in ~/.claude/hooks/:**
- ✅ Hooks are subject to deadlock detection before installation
- ✅ Hooks cannot block core tools (Read, Edit, Bash) — would create deadlock
- ✅ Hooks are local (don't phone home)

**Risks:**
- ⚠️ If your machine is compromised, hooks could be modified
- ⚠️ Hooks run with your user permissions

**Your responsibility:**
- Inspect hooks before installation: `cat ~/.claude/hooks/auto-route.sh`
- Use `--check` flag to preview changes: `llm-router install --check`
- Report suspicious hook behavior

### Rate Limiting & Abuse

**llm-router does NOT:**
- ❌ Bypass provider rate limits
- ❌ Help you spam APIs
- ❌ Hide requests from audit logs

**llm-router DOES:**
- ✅ Respect provider rate limits
- ✅ Add request authentication (your API keys)
- ✅ Log all routed calls for audit purposes

**Provider policies:**
- Ensure you comply with each provider's terms of service
- OpenAI, Gemini, etc. have their own abuse policies
- llm-router cannot protect you from violating those policies

## Threat Model

This section describes threats llm-router is designed to protect against and known limitations.

### Threats We Protect Against

| Threat | Scenario | Mitigation |
|--------|----------|-----------|
| **Prompt Leakage** | Accidental prompt in logs | Prompts never written to disk; only task metadata logged |
| **API Key Exposure** | Keys in git or plaintext | `.env` in `.gitignore`; keys in `config.yaml` with 600 perms |
| **Routing Injection** | Malicious prompt tries to override routing | Routing rules in code, not parsed from prompts |
| **Token Theft** | OAuth token copied from memory | OAuth tokens stored in OS keyring (macOS/Linux) |
| **Untrusted Hooks** | Malicious auto-route hook | Hooks reviewed at install; deadlock detection prevents tool blocking |
| **Provider Man-in-the-Middle** | Network attack on API calls | All provider calls use TLS 1.2+; provider auth via keys |
| **Unauthorized Access** | Unauthorized user runs routing | Routed prompts respect system user permissions |
| **Rate Limit Bypass** | User overrides rate limits | Rate limits enforced by providers, not bypassed |

### Threats We DON'T Protect Against

| Threat | Why | Responsibility |
|--------|-----|-----------------|
| **Model Jailbreaks** | Provider's problem, not ours | Depends on model vendor security |
| **Prompt Injection in Model** | Cannot prevent at routing layer | Separate LLM security discipline needed |
| **Stolen API Keys** | Key in environment/config | Store keys securely; rotate regularly |
| **Machine Compromise** | Malware can read all data | Use full-disk encryption; keep OS updated |
| **Provider Data Breach** | Provider's infrastructure | Review provider SLA/privacy policy |
| **Social Engineering** | Phishing for keys/prompts | User training and careful access control |
| **Accidental Data Exposure** | User puts secrets in prompts | User discipline; review before routing |
| **Malicious Dependencies** | Compromised upstream package | Keep dependencies updated; use pip-audit |

### Insider Threats

If you believe llm-router or a contributor has a security vulnerability:

1. **Do NOT** open a public issue
2. Email **yali.pollak@gmail.com** with details
3. Include: description, reproduction steps, impact, suggested fix
4. Response timeline:
   - 24 hours: Receipt acknowledgment
   - 3 business days: Initial assessment
   - 30 days: Patch release or mitigation

## Incident Response

### If You Suspect a Breach

1. **Immediate Actions:**
   - Stop using llm-router immediately
   - Rotate all API keys (OpenAI, Gemini, etc.)
   - Review recent logs: `tail -f ~/.llm-router/session.log`
   - Check ~/.llm-router for unauthorized files

2. **Report to Us:**
   - Email yali.pollak@gmail.com with details
   - Include timeline, affected systems, and scope

3. **Notify Providers:**
   - Contact OpenAI, Gemini, etc. if API keys were exposed
   - Request audit of recent calls
   - May be able to revoke tokens

4. **Mitigation:**
   - Clear all data: `rm -rf ~/.llm-router/`
   - Reinstall: `pip install --upgrade claude-code-llm-router`
   - Reconfigure with new API keys

### If We Report a Vulnerability

When llm-router maintainers report a security vulnerability:

1. **Patch released:** Update immediately with `pip install --upgrade`
2. **GitHub Security Advisory:** Posted to notify all users
3. **Release notes:** Security fixes highlighted
4. **CVE tracking:** Critical issues tracked in official databases

### Our Commitment

- ✅ Respond to security reports within 24 hours
- ✅ Provide patches within 30 days for non-critical issues, 7 days for critical
- ✅ Credit responsible disclosures (unless requested otherwise)
- ✅ Post security advisories publicly after patches are available

## Known Limitations

- **SQLite concurrency:** Not suitable for teams (use Redis backend instead)
- **Token estimation:** Approximate; real costs may differ
- **Provider dependence:** If a provider goes down, routing fails for that provider
- **Prompt visibility:** Your prompts are sent to configured providers
- **No prompt encryption:** Prompts sent to providers in plaintext (provider's TLS handles transit security)

## Security Roadmap

- [ ] v7.7: Redis backend for distributed cost tracking
- [ ] v7.8: End-to-end encryption for hook scripts
- [ ] v8.0: Audit log with cryptographic signing
- [ ] v8.1: Hardware security module (HSM) support for key storage

## Credits

Security researchers who've responsibly disclosed issues:
- (To be added as reports come in)

---

Last updated: 2026-04-27
Policy version: 1.0.0
