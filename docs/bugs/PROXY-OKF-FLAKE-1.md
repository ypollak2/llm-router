---
id: PROXY-OKF-FLAKE-1
status: fixed in `fix/okf-context-flake`
---
## PROXY-OKF-FLAKE-1. test_served_step_reaches_the_local_backend_with_the_knowledge flaked on a slow runner

- **Symptom (main CI run 37906269660, job test (3.13), commit 8e85f2f1).** `assert ('<knowledge_context>' in system and 'billing/invoice_reconciler.py' in system)` failed, preceded by `llm_router.failopen {'code': 'LR-FO-PROXY-OKF-ATTACH-TIMEOUT', 'exc': 'TimeoutError'}`.
- **Cause.** Test bug, not product. The end-to-end proxy tests ran the real attach under the product's 2 s wall-clock budget (`proxy.server.OKF_ATTACH_TIMEOUT_S`). On a slow runner the file/git I/O exceeded it, the attach failed open as designed, and the block was never added. The product timeout and its fail-open are correct and have their own test.
- **Fix.** The `policy` fixture in `tests/test_proxy_okf_context.py` pins `OKF_ATTACH_TIMEOUT_S` to 60 s. `test_a_slow_attach_is_bounded_and_the_step_is_still_served` sets its own 0.2 s after the fixture and still covers the timeout. No product change.
- **Test.** The same test file. With a temporary 2.5 s sleep inside `okf_context.attach`, the old test failed with the same assertion and the same failopen line; with the fix the file (22 tests) passes, and 20 of 20 repeated runs pass.
