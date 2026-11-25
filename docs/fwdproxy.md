# Forward proxy for Remote Control (beta, opt-in)

Claude Code Remote Control needs `ANTHROPIC_BASE_URL` to be `api.anthropic.com`, so the
usual base-URL redirect to the router proxy cannot be used. `llm_router.fwdproxy` is a
local forward proxy that fills the gap: Claude Code is pointed at it with `HTTPS_PROXY`,
it terminates TLS for `api.anthropic.com` only, and pipes the decrypted stream to the
router proxy (`127.0.0.1:8787`).

**Status: beta, off by default.** `llm-router install` and `--proxy-default` never start it.
It only exists on a machine where you ran `llm-router fwdproxy ca` and `llm-router fwdproxy install`.
Not recommended for production use until it has had clean live days on your machine.

## Use

```
llm-router fwdproxy ca [--renew-leaf|--force]   # one-time: local CA + leaf
llm-router fwdproxy install                       # installs ~/.llm-router/bin/claude-rc and starts the listener
~/.llm-router/bin/claude-rc                       # run Claude Code through it (same args as `claude`)
llm-router fwdproxy status                        # also checks the listener on :8789 (TCP connect only)
llm-router fwdproxy uninstall                     # rollback
```

`ca` flags: `--renew-leaf` re-issues only the leaf from the existing CA (the leaf lasts 397 days);
`--force` replaces both the CA and the leaf. With neither flag it refuses to overwrite an existing CA.

Service files: macOS `~/Library/LaunchAgents/com.llm_router.fwdproxy.plist`; Linux
`~/.config/systemd/user/llm-router-fwdproxy.service`. Both are written mode 0600 and the
launcher `claude-rc` mode 0700, whatever the umask; paths with spaces, `&`, `<` or `%` are escaped.
`status` makes a bare TCP connect to the listener (no bytes), which the proxy logs as `PROBE`.

`claude-rc` fails open: if the forward proxy or the router proxy is not up it runs the real
`claude` directly, unrouted, logs `fallback-direct-unrouted` to `~/.llm-router/wrapper.log`
and prints a notice on stderr. `claude-rc` probes the listener with `nc -z` (connect, send
nothing); the proxy logs that as mode `PROBE`.

## Security model

| Item | What it does |
|---|---|
| Local CA | Created by `make-ca.sh` with openssl. RSA 2048, 825 days, `CA:TRUE, pathlen:0`, critical `nameConstraints` permitting only `DNS:api.anthropic.com`. It cannot validly sign any other host, even for a client that trusts it. |
| Leaf | SAN `api.anthropic.com`, serverAuth, 397 days; re-issue with `llm-router fwdproxy ca --renew-leaf`. |
| Trust scope | The CA is never added to the macOS keychain or the system store. `claude-rc` hands it only to the Claude process: `NODE_EXTRA_CA_CERTS` (merged with a caller's own value), and `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` pointing at a bundle of system roots plus this CA, for Python hook subprocesses (these also reach other child processes of that session). |
| Files | `~/.llm-router/fwd-ca` (dir 0700; `ca.crt`, `leaf.crt`, `leaf.key` and bundles 0600). The proxy reads only `leaf.crt` and `leaf.key`. The CA private key lives in `~/.llm-router/fwd-ca-key` (dir 0700, files 0600) and is used only to sign the leaf; move it offline if you want. |
| Listener | Loopback only. A non-loopback `FWDPROXY_BIND` makes it exit at start. Port 8789. |
| Intercepted | Only `CONNECT api.anthropic.com:443`: TLS terminated with the leaf (ALPN `http/1.1` only), bytes piped to the router proxy. If the router does not accept within 2 s, it fails open: TLS to the real `api.anthropic.com:443`. A session with no bytes either way for 600 s is closed (`IDLE-TIMEOUT`). |
| Tunnelled | Every other `CONNECT host:port` is a blind byte tunnel: no TLS termination, no inspection. Targets that are loopback/private/link-local IP literals or `localhost` are refused with 403 (`REFUSE`). A DNS name that resolves to a private address is not blocked. |
| Refused | Anything that is not `CONNECT` gets 405. |
| Logging | `~/.llm-router/logs/fwdproxy.log`, mode 0600, one line per connection: UTC time, mode (`INTERCEPT`, `FALLBACK`, `TUNNEL`, `PROBE`, `REFUSE`, `IDLE-TIMEOUT`, `ERR`), `host:port`, byte counts, error class. Never headers, bodies or request lines (a test sends a sentinel in all three and asserts it is absent from the log). |

What it cannot do: it sees the plaintext of the Anthropic API traffic of any process that uses
`claude-rc`'s environment, including the API key or OAuth token. That is the point of intercepting,
and why the CA is name-constrained, the key is kept apart from the leaf, and the listener is loopback only.

## Rollback

`llm-router fwdproxy uninstall` (runs `rollback.sh`): unloads the launchd job (or `systemctl --user disable --now`),
and renames, never deletes, the plist/unit, `~/.llm-router/bin/claude-rc` and `~/.llm-router/fwd-ca`
to `*.discard-<UTC stamp>`. `~/.llm-router/fwd-ca-key` is left in place with a note; remove it by hand.
Because nothing was added to a keychain or to `settings.json`, there is nothing else to revert. Plain
`claude` was never changed.

## Not covered yet

- Windows is not supported.
- `llm-router doctor` does not check the forward proxy.
- No live-use numbers are published for this feature; none are claimed here.
