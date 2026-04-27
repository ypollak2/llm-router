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

### Data Handling

**What llm-router does with your prompts:**
- ✅ Routes to the best model based on complexity
- ✅ Tracks token usage locally (SQLite on your machine)
- ✅ Caches classification results (locally, with TTL)
- ❌ Does NOT store your prompts permanently
- ❌ Does NOT log full prompts (only task type + complexity)
- ❌ Does NOT share prompts between providers unless you ask

**Multi-provider routing:**
When you route a task, your prompt is sent to:
1. **Local Ollama** (if configured) — stays on your machine
2. **Your configured providers** (OpenAI, Gemini, etc.) — governed by their privacy policies
3. **Not** to llm-router servers — we don't run servers

**Your responsibility:**
- Ensure your prompt doesn't contain sensitive data you don't want to share
- If using team features, be aware prompts may be logged for auditing
- Review provider privacy policies (especially if routing to Gemini or GPT-4o)

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

## Known Limitations

- **SQLite concurrency:** Not suitable for teams (use Redis backend instead)
- **Token estimation:** Approximate; real costs may differ
- **Provider dependence:** If a provider goes down, routing fails for that provider
- **Prompt visibility:** Your prompts are sent to configured providers

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
