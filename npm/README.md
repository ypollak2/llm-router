# llm-routing

**Stop hitting the limit on your Claude Pro or Max plan.**

Routes the routine prompts — "what does this error mean", "reformat this JSON",
"is the service up" — to free and cheap models, so your subscription quota is
still there when you need it at 4pm. Same workflow, same commands. The model
choice changes underneath.

```bash
npx -y llm-routing install
```

No API keys required. No hosted proxy. No account.

---

## Why this package is 3 kB

It contains three files, and that is deliberate.

`llm-router` is a Python program shipped as a **standalone binary** — roughly
300 MB per platform, because it carries its own interpreter and a large provider
library. Vendoring all four platforms into one npm tarball would exceed a
gigabyte and hand every user the three they cannot run.

So `postinstall` downloads the one binary that matches your platform from the
matching [GitHub release](https://github.com/ypollak2/llm-router/releases), and
`bin/llm-router.js` hands off to it. That is the whole package.

If the download fails, the install **fails loudly** rather than leaving a broken
`llm-router` on your PATH to be discovered mid-session.

## Why a proxy cannot do this

Every other router in this category is a proxy: you point your agent at a local
endpoint and it forwards requests using *your API keys*. That design has a hard
limit — **a proxy cannot intercept a session authenticated by a subscription,
because there is no key to forward.**

If you pay per token, a proxy serves you well and there are good ones. If you pay
a flat monthly fee and the thing you run out of is *quota*, this is the gap it
fills.

## Supported platforms

| Platform | Status |
|---|---|
| macOS arm64 | supported |
| macOS x86_64 | supported |
| Linux x86_64 | supported |
| Windows x86_64 | supported |

On an unsupported platform the install refuses cleanly and points you at
`pip install llm-routing`, which works anywhere Python 3.11+ does.

### macOS: Gatekeeper

The binaries are not yet notarised, so macOS quarantines them on first run. If
`llm-router` will not start:

```bash
xattr -dr com.apple.quarantine "$(dirname "$(readlink -f "$(which llm-router)")")"
```

## After installing

```bash
llm-router install     # wire up your host (Claude Code by default)
llm-router doctor      # verify everything is connected
llm-router status      # today's savings and quota
```

## Links

- **Source, docs and issues** — https://github.com/ypollak2/llm-router
- **PyPI** (`pip install llm-routing`) — https://pypi.org/project/llm-routing/
- **Benchmark write-up**, including what did *not* work —
  [docs/ROUTERARENA.md](https://github.com/ypollak2/llm-router/blob/main/docs/ROUTERARENA.md)
- **Host support matrix**, including what each host genuinely cannot do —
  [guide/HOST_SUPPORT_MATRIX.md](https://github.com/ypollak2/llm-router/blob/main/guide/HOST_SUPPORT_MATRIX.md)

MIT licensed.
