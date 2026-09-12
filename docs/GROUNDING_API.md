# `POST /ground` — check a draft before you trust it

A cheap model gave you an answer. Before you relay it, one question is worth
asking and costs almost nothing: **does the code it cites actually exist?**

That is what this endpoint answers. It is structural — no model call, no judge,
no second inference — so it runs in milliseconds and costs nothing per check.

## What it is for

The economics of routing break on a specific failure: a small model, asked about
a repository it cannot see, writes a confident answer citing
`src/auth/middleware.py` and `validate_session_token()`. Both are plausible.
Neither exists. Relaying that costs more than the Claude call it replaced,
because someone now has to discover it is wrong.

A path is either in the repository or it is not. That question needs no
intelligence to answer, which is why it should not cost an inference.

## Request

```http
POST /ground
Content-Type: application/json

{
  "draft":        "required — the candidate answer to check",
  "context":      "optional — the material the model was given",
  "prompt":       "optional — the original question",
  "project_root": "optional — which repo to check against"
}
```

`project_root` may instead be sent as the `X-LLM-Router-Project` header, which
takes precedence. Both are subject to `LLM_ROUTER_PROJECT_ALLOWLIST`.

## Response

```json
{
  "relayable": false,
  "violations": ["src/auth/middleware.py"],
  "symbol_violations": ["validate_session_token"],
  "checked": { "paths": true, "symbols": true }
}
```

| Field | Meaning |
|---|---|
| `relayable` | Nothing in the draft is provably invented |
| `violations` | Paths cited that exist in neither the context nor the repo |
| `symbol_violations` | Function/method names cited that exist in neither |
| `checked` | Which checks actually ran |

## What `relayable: true` does not mean

**It is not a correctness claim.** A draft that cites only real files can still
be completely wrong about what they do. This endpoint detects fabrication, not
error. A caller that treats it as a quality score will ship confidently wrong
answers with a green light attached — which is worse than no check, because it
launders the failure.

The honest reading is: *"this draft is worth a human's attention"*, not *"this
draft is right"*.

## Why `checked` exists

`symbol_violations` only runs when the draft was given repo material
(`<knowledge_context>` in `context`). The symbol index knows **this**
repository. Outside it, a symbol's absence from the index is not evidence that
the symbol does not exist — `numpy.einsum` is real and will never be in your
index.

So when the symbol check cannot run, it returns nothing, and `checked.symbols`
is `false`. Reporting an empty list as a clean bill of health would be a lie of
omission, and callers gating on `relayable` deserve to know which half of the
check produced it.

## Integrating

```python
import httpx

draft = cheap_model.answer(question)

verdict = httpx.post("http://127.0.0.1:8080/ground", json={
    "draft": draft, "context": material, "prompt": question,
}).json()

if verdict["relayable"]:
    return draft
return expensive_model.answer(question)   # fabrication detected — escalate
```

The fallback is the point. A failed check is not an error; it is the router
doing its job, and the correct response is to spend the money you were trying to
save.

### Notes

- **Loopback only.** Non-loopback `Host` and cross-site `Origin` are rejected
  (403) to stop DNS rebinding and CSRF from a browser. CLI clients and SDKs are
  unaffected.
- **Fail open, not closed.** If the check itself errors you get a 502. Treat
  that as "unchecked", not "clean" — the safe response is to escalate.
- **400** on a missing or empty `draft`, or a non-string `context` / `prompt`.
