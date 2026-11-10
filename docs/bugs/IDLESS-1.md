---
id: IDLESS-1
status: fixed in `fix/proxy-idless-local-reply`
---
## IDLESS-1. A local backend reply without an `id` turned a served step into a 500 with no ledger row

- **Symptom.** Found by code reading, not from a live row (no n): the coordinator's tracker listed
  "server.py message["id"] KeyError on id-less local reply (500, no row; pre-existing)". The served-local path did
  `row.update(..., msg_id=message["id"], ...)` (`src/llm_router/proxy/server.py:871` on `0ca251b4`), so a missing key raised
  KeyError after the reply had passed validation: the client got a 500 and the ledger got no row.
- **Cause.** The `Backend` protocol (`src/llm_router/proxy/backends.py:270`) returns `dict | None` and does not promise an
  `id`. Today `from_ollama` (`translate.py:212`) and the edit protocol (`local_agent/capability.py:335`) both call
  `new_msg_id()`, so no shipped backend produces an id-less message; any other backend (a new adapter, a test double) does.
- **Fix.** `proxy/server.py`: right after `backend.complete` returns, a message with no `id` gets `new_msg_id()` (the helper
  every other served message uses, prefix `msg_lr`); the same guard runs again just before `guard.record_served`, so a reply
  that replaced the first one (the edit protocol) is covered too. The edit-protocol replacement has no test: it is not reachable through a fake. The ledger write reads `message.get("id")`, so a missing id can no
  longer abort the row.
- **Test.** `tests/test_proxy_local_mode.py::test_an_id_less_backend_reply_is_served_with_an_id_and_a_ledger_row`: a backend
  that strips `id` is served 200 with an `msg_lr` id and exactly one `served` ledger row carrying that `msg_id`. Mutation
  check (fix reverted by `git diff` reverse-apply, n=1 run): the test fails with the KeyError path.
