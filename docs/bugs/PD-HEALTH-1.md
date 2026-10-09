---
id: PD-HEALTH-1
status: fixed in `fix/proxy-default-health-v2` (hook version 27)
---
## PD-HEALTH-1. The proxy-default health check never asked whether the session routes through the proxy

- **Symptom (2026-10-08).** A session with no `ANTHROPIC_BASE_URL` printed "installed but not
  answering on 127.0.0.1:8787 - every API call ... will fail". The opposite case went unreported:
  the sentinel said enabled while `~/.claude/settings.json` had lost `env.ANTHROPIC_BASE_URL`, so
  routing was off for hours with no warning.
- **Cause.** `_check_proxy_default_health` read only `port` (and `upstream_port`) from
  `~/.llm-router/proxy_default.json` and TCP-probed it. It never resolved the session's effective
  base URL.
- **Fix.** `_effective_base_url`: `os.environ` first (Claude Code applies settings `env` to hook
  processes; the repo has no test proving that, so it falls back to project
  `settings.local.json`, project `settings.json`, then user `settings.json`). Not routed to
  `port` or `upstream_port` on localhost: one warning "routing is OFF" naming where the setting is
  missing or points, plus `llm-router install --proxy-default`; host shown only through
  `proxy_liveness._host_of` (no userinfo, path, query, bare key). Routed and down: the old warning
  and per-hop probes unchanged. Not installed or `enabled: false`: silent. Still one local TCP
  connect per hop, 1 s timeout. `upstream_port` is already written by `write_sentinel` on main; its
  absence is handled (single-hop). Hook version 26 -> 27.
- **Test.** `tests/test_session_start_proxy_default.py`: 3 new tests failed before the fix (not
  routed + dead port, settings lost the key with proxy up, project override to another host with
  credentials in the URL); routed-and-dead, not-installed, precedence and direct-upstream tests
  pin the other cases.
