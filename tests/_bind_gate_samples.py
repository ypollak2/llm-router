"""Invented answers for the blocked-answer gate (``agent-route.py::_blocked_marker``), with what each must do.

No real transcript text is in this file: the answers the gate was measured on in docs/bugs/BIND-2.md (317 delegated
texts, a labelled sample of 40 and the 28 weak-marker hits) are the owner's private sessions and stay out of the repo.
What is here is invented, in the wording of a run that was stopped by its environment (BLOCKED) and of a complete answer
that uses the same words about the code under review (COMPLETE):

* ``review``  the 26 the independent review of PR #818 wrote (rv-PR818-20261103.md, S3), which the first push missed 10 of
              and mis-rejected 4 of; the gate was tuned on them.
* ``r1``..``r3``  three rounds of 20 + 15 written after that, each first run on the then-current gate and not used for
              tuning until its first run was recorded; later rounds added vocabulary for what earlier rounds missed.
* ``r4``      the last round: written after the gate was frozen, evaluated once (first run: precision 100%, recall 60%),
              then two phrases were added for the one answer the first push caught and this gate lost.

``KNOWN_GAPS`` are blocked answers the gate does NOT flag, each with the reason; tests/test_bind_blocked_gate.py asserts
they are still gaps, so closing one forces this list (and the doc that cites it) to be updated.
"""

REVIEW_BLOCKED = {
    'npm-enotfound': 'I could not finish: `npm install` failed with ENOTFOUND registry.npmjs.org, so dependencies are missing and the tests did not run. Nothing was merged.',
    'net-unreachable': 'The build stopped at step 2. Network is unreachable from this environment, so I did not fetch the base branch. No changes were committed.',
    'gh-pr-create': 'I made the edits and committed them locally, but `gh pr create` failed: error connecting to api.github.com. No PR exists. Please open it from a connected session.',
    'git-lock': "I edited the files, but `git commit` failed: fatal: Unable to create '.git/index.lock': Operation not permitted. The changes are uncommitted.",
    'git-fetch-head': "error: cannot open '.git/FETCH_HEAD': Operation not permitted. I could not fetch origin/main, so the comparison against main was not made.",
    'approval-required': 'Running the test suite requires approval, which is unavailable here, so I stopped before running it. No tests were executed.',
    'pip-no-index': 'pip could not download the build dependencies (no matching distribution; connection error). The venv was not created and the tests were not run.',
    'tmp-denied': 'mkdir: /tmp/work: Operation not permitted. The scratch directory could not be created, so the probe was skipped.',
    'tests-not-run-plain': 'I implemented the change in `pager.py` and added a test, but I did not run the tests.',
    'partial-with-sandbox': 'Implemented items 1 and 2. Item 3 (the live smoke test) needs network access that this sandbox does not provide, so it remains open.',
    'needs-write-plain': 'The fix needs a write to ~/.config, which is outside the writable area, so I left it undone.',
    'net-off': 'With network access off I could not check the PR status or run the release workflow; those two steps are not done.',
    'sandbox-denied-write': 'The sandbox denied writing the report to the requested path; the findings below are the full report instead.\n\n1. ...',
    'ca-cert': 'curl: (6) Could not resolve host: api.github.com. The release notes were not uploaded.',
    'dns': 'Temporary failure in name resolution while cloning the repository; the work did not start.',
    'econnrefused': 'The smoke test failed: connect ECONNREFUSED 127.0.0.1:8798 (server not running); the run is incomplete.',
}
REVIEW_COMPLETE = {
    'review-cannot-connect': 'Finding 1 (MUST): the client cannot connect to the gateway when TLS is disabled, because `connect()` ignores `verify=False` (client.py:88). Fix: pass the flag through. Finding 2: ...',
    'review-session-cannot': 'This session id cannot be reused after logout: `SessionStore.close()` deletes the row but the cache keeps it (store.py:140). Fix: invalidate the cache entry. No other findings.',
    'review-sandbox-code': 'The sandbox module is correct: it denies writes outside the workspace, and the read-only mount flag is applied before exec (sandbox.py:55). The tests cover this. Verdict: no defects found.',
    'review-offline': 'The CLI works offline: it falls back to the cached map when the network is unavailable (resolver.py:30). I verified the fallback by reading the code. Verdict: pass.',
    'review-permission-denied': "The loader raises a clear error on 'permission denied' and retries once, then fails closed (loader.py:77). That behaviour is intended. Verdict: pass.",
    'answer-blocked-word': 'The queue is blocked when the lock is held; that is by design. The consumer cannot proceed until the lock is released, and the timeout is 30 s. Fix: none needed.',
    'review-read-only-db': 'The report reader opens the database read-only and prevents writes by design (the connection uses mode=ro). Tests pass in isolation. Verdict: no change needed.',
    'review-prevents': 'The ratchet prevents silent regressions: a decrease in the counter fails CI. I could not find a gap in the five call sites I checked.',
    'review-network-access': 'The proxy denies network access to the sandboxed child, as documented (proxy.py:210), and the test asserts the denial. Verdict: pass. Findings: 0 MUST, 1 SHOULD (docs).',
    'review-this-session': "In this session of the router the classifier cannot distinguish 'fix' from 'fixes' (classifier.py:44). SHOULD: add a regression test.",
}

R1_BLOCKED = {
    'h01-pytest-tmp': 'I updated `pager.py` and the new test, but `pytest` could not start: the temp directory is not writable in this environment. No tests ran.',
    'h02-push-dns': '`git push origin fix/pager` failed: Could not resolve host: github.com. The commit exists locally only.',
    'h03-install-needs-net': 'I stopped after the edit. Installing the dev dependencies needs network access, and this sandbox has none, so the lint and test steps are still open.',
    'h04-uv-pip-dns': 'Both `uv sync` and `pip install -e .` failed with `Temporary failure in name resolution`; I could not install the package, so nothing was tested.',
    'h05-bind-denied': 'The review is static only. The sandbox refused to start the test server (bind: operation not permitted), so none of the runtime checks ran.',
    'h06-worktree-ro': 'I could not create the worktree: `git worktree add` failed with `fatal: could not create leading directories` and `.git` is read-only here.',
    'h07-net-off-api': 'Network access is off. I could not call the release API, so the changelog entry was not verified against the published notes.',
    'h08-gh-run-watch': '`gh run watch` exited with `error connecting to api.github.com`; CI status is unknown and I did not merge.',
    'h09-fetch-base': 'Unable to fetch the base branch from this sandbox (no network). The diff below is against the local copy and may be stale.',
    'h10-commit-eperm': 'The task asked me to commit the fix. `git commit` fails with `error: unable to create temporary file: Operation not permitted`, so the working tree has the edit but no commit.',
    'h11-no-pr': 'I did not open the PR: the environment has no GitHub credentials or network access.',
    'h12-approval-docker': 'Approval is required to run `docker compose up`, and approvals are disabled in this run, so I skipped the integration tests.',
    'h13-ro-mount': 'Cannot write to `/var/log/router`: the filesystem is mounted read-only. The log rotation fix is described below but not applied.',
    'h14-curl-refused': '`curl http://localhost:8798/health` returned `Failed to connect to localhost port 8798: Connection refused`. The server could not be started in this sandbox, so the smoke test is incomplete.',
    'h15-runtime-blocked': 'Completed the code review of the diff. Runtime verification was blocked: tests require a writable temp dir and network, neither of which this run provides.',
    'h16-formatter-cache': 'I was unable to run the formatter because the sandbox blocks writing to `~/.cache`. Formatting is unverified.',
    'h17-enotfound': 'Dependency resolution failed (ENOTFOUND pypi.org), so the new constraint could not be validated.',
    'h18-etc-outside': 'The migration script needs to write to `/etc/router.d/`, which is outside the writable roots of this run. I left the change undone and documented the steps.',
    'h19-steps-not-performed': 'Verification steps 3 and 4 (integration run, PR creation) were not performed: no network and no write access to `.git`.',
    'h20-push-unreachable': 'I tried to push the branch, but the remote rejected the connection (network is unreachable). Push is still pending.',
}
R1_COMPLETE = {
    'c01-sandbox-flag': 'The proxy rejects requests when the sandbox flag is set, which is intended (gateway.py:210). The tests cover both paths. Verdict: no findings.',
    'c02-close-twice': 'SessionStore.close() cannot be called twice: the second call raises RuntimeError by design (store.py:77). Fix: none needed.',
    'c03-could-not-find': 'I could not find any caller that passes `timeout=None` (searched 14 call sites). Verdict: safe to merge.',
    'c04-conn-refused-cli': "The CLI reports 'connection refused' with exit code 7 when the daemon is down (cli.py:88) and tests assert it. Nothing to fix.",
    'c05-ro-replica': 'The read-only replica is never written to: all writers use the primary connection (db.py:31). Verified by reading the three writer paths.',
    'c06-fetch-remote': 'Finding (SHOULD): `fetch_remote()` fails to connect when the proxy URL has a trailing slash (net.py:140). Fix: strip it. No other findings.',
    'c07-lock-file': 'The hook is blocked from running twice by the lock file (hook.py:55); a second invocation exits 0 immediately. That matches the spec.',
    'c08-no-network-import': 'No network access is attempted at import time (checked with the offline test in tests/test_offline.py, which passes). Verdict: pass.',
    'c09-approval-policy': 'The deploy script requires approval from the release manager before step 4 (deploy.md:20); this is documented and enforced by `check_approval()`. Verdict: matches the policy.',
    'c10-token-rotation': "This session's token is rotated on login (auth.py:61), so replaying an old token fails. Tests pass in my run: 41 passed.",
    'c11-403-mapping': 'Permission denied errors are mapped to HTTP 403 in the handler (handlers.py:200), and the test asserts that mapping. No defects.',
    'c12-full-suite': 'I ran the full suite: 2,418 passed, 0 failed. The sandbox tests exercise the mount logic and pass.',
    'c13-enotfound-retry': 'The retry loop gives up after ENOTFOUND, as intended, and logs once (retry.py:33). I confirmed the logging with the added test.',
    'c14-connect-timeout': 'Cannot connect timeouts are configurable via `CONNECT_TIMEOUT`; the default of 5 s is too short for the gateway (config.py:12). SHOULD: raise to 10 s.',
    'c15-atomic-cache': 'The updater could not previously write the cache atomically (cache.py:90); the patch fixes it with a temp file and rename. I ran the tests, all pass.',
}

R2_BLOCKED = {
    'g01-pytest-cache-ro': "I could not run the suite. `pytest` exited with `PermissionError: [Errno 13] Permission denied: '.pytest_cache'` because the checkout is read-only.",
    'g02-patch-unapplied': 'The patch is ready but unapplied: this run cannot modify files outside the sandbox, and the target path is outside it.',
    'g03-pip-download': 'Blocked: no outbound network from the sandbox, so `pip download` for the wheel failed and the build was not attempted.',
    'g04-mutation-writable': 'I reviewed the diff statically. Running the mutation tests requires a writable worktree, which is unavailable here, so those results are missing.',
    'g05-gh-pr-view': '`gh pr view 812` failed: `HTTP request failed: dial tcp: lookup api.github.com: no such host`. I could not confirm the PR state.',
    'g06-erofs-report': "Step 2 failed because the filesystem is read-only: `OSError: [Errno 30] Read-only file system: 'out/report.json'`. The report was not generated.",
    'g07-ruff-install': "I'm unable to install `ruff`: the sandbox has no network, and the package is not cached. Lint was not run.",
    'g08-docker-socket': 'I attempted `docker build`, but the daemon socket is not accessible from this sandbox, so no image was built.',
    'g09-eai-again': 'Could not fetch dependencies — DNS resolution failed for registry.npmjs.org (EAI_AGAIN). Nothing was installed.',
    'g10-push-tags': 'The release step needs `git push --tags`; pushes are not possible without network access, so the tag exists only locally.',
    'g11-listen-eperm': 'My attempt to start the dev server failed with `listen EPERM: operation not permitted 0.0.0.0:3000`, so the browser checks did not run.',
    'g12-refs-not-writable': "I wasn't able to create the branch: `.git/refs` is not writable in this environment.",
    'g13-scan-rules': 'The security scan needs to download its rules database; with network disabled it could not start, so the scan is outstanding.',
    'g14-dynamic-server': 'Only the static analysis is complete. The dynamic tests were not executed because the sandbox does not allow spawning the required server.',
    'g15-commit-config': 'I could not commit: `git config user.email` is unset and the config file is read-only, so the commit step was skipped.',
    'g16-curl-health': "`curl -sS https://example.org/health` failed (Could not resolve host), meaning I couldn't validate the endpoint from here.",
    'g17-erofs-router': 'Writing the fix to `src/router.py` failed with `EROFS`. I have not changed any files.',
    'g18-model-weights': 'I have no network access in this run, so I could not download the model weights that the benchmark needs. The benchmark was not run.',
    'g19-uv-index': 'I tried `uv run pytest`, but uv could not reach the package index (network blocked), so the tests are unrun.',
    'g20-var-prohibited': 'The sandbox policy prohibits creating files under `/var`, so the log file was not created and the verification is incomplete.',
}
R2_COMPLETE = {
    'k01-retry-connection-error': 'The scheduler retries on `ConnectionError` three times, then raises (sched.py:44). That matches the spec and the test covers it. No findings.',
    'k02-cache-ro-mode': 'Writes to the cache are guarded by a lock; a read-only mode exists for the CLI (cache.py:12) and is covered by `test_cache_ro`. Verdict: pass.',
    'k03-no-regression': 'I could not find a regression: the three new tests fail on the old code and pass on the new. Verdict: ship.',
    'k04-updater-offline': 'The updater needs network access only in `check_for_updates()` (updater.py:30), and that call is skipped when `OFFLINE=1`. Verified by the test at tests/test_updater.py:55.',
    'k05-approval-cli': 'Approval is required for destructive actions in the CLI; `confirm()` enforces it (cli.py:101) and prompts by default. No defects.',
    'k06-sandbox-runner': 'The sandbox runner kills the child after the timeout and reports `timeout` (sandbox.py:210). Edge: a zero timeout is rejected. Verdict: correct.',
    'k07-ipv6-resolve': '`resolve_host()` fails to resolve IPv6 literals in brackets (net.py:77). SHOULD: strip the brackets before lookup. The rest of the module is fine.',
    'k08-session-cookie': 'This session cookie cannot be read by JavaScript because HttpOnly is set (session.py:19). Good.',
    'k09-permission-error-wrap': 'Permission denied from the OS is surfaced as `PermissionError` and wrapped in `StorageError` (storage.py:140); the wrapping test passes.',
    'k10-pytest-passed': 'I ran `pytest -q`: 311 passed in 41 s. The network-dependent tests are marked and were skipped by design, as documented in conftest.py.',
    'k11-irreversible-migration': 'The migration is not reversible by design (m0042.py:12); the downgrade raises `NotImplementedError`. Documented in the release notes. Verdict: acceptable.',
    'k12-push-not-invoked': '`git push` is intentionally not invoked by the script (release.sh:30); it prints the command for the operator. That is consistent with the runbook.',
    'k13-proxy-empty': 'The tool could not previously connect when the proxy variable was empty (client.py:61). The patch handles that case and adds a test; all 12 tests pass.',
    'k14-reports-ro-tree': 'The filesystem layer is read-only for the `reports/` tree (mount.py:88), which is why the exporter writes to `out/` instead. Correct.',
    'k15-unable-repro': 'Unable to reproduce the failure on the base commit, so the bisect is inconclusive: the flaky test passes 50 of 50 runs there (see the table). Verdict: not a regression.',
}

R3_BLOCKED = {
    'm01-partial-pytest-cache': "Result: partial. I edited `src/router.py` and added `tests/test_router_hold.py`, but I couldn't execute the tests here because pytest tries to write `.pytest_cache` and the directory is not writable for me.",
    'm02-no-token': 'Not done: the PR. `gh auth status` shows no usable token in this sandbox and there is no route to github.com.',
    'm03-bullets': 'What I could not do:\n- run the integration suite (needs the local Ollama daemon; sockets are denied)\n- push (no network)\nEverything else is below.',
    'm04-npm-ci': 'I stopped at the dependency step. `npm ci` fails with `getaddrinfo ENOTFOUND registry.npmjs.org`, and with no network there is no way to proceed.',
    'm05-git-ro-mount': 'The worktree could not be created because `.git` is mounted read-only in this run. I made no changes.',
    'm06-network-not-permitted': 'I did not run the full verification: execution of external commands that touch the network is not permitted here.',
    'm07-not-permitted-run': 'Review status: static findings only. I was not permitted to run the code, so every runtime claim below is unverified.',
    'm08-edits-described': 'All edits are described below rather than applied; the workspace denies writes to `src/`.',
    'm09-fetch-timeout': '`git fetch origin main` timed out: connection to github.com blocked by the sandbox. The comparison uses the local `main`, which may be behind.',
    'm10-postgres-port': "I couldn't validate the SQL migration because starting Postgres requires binding a port, and the sandbox forbids it.",
    'm11-image-pull': 'The container image could not be pulled (no registry access from this environment), so the e2e step is not completed.',
    'm12-mutation-temp': 'Mutation testing was not possible: it needs to copy the repo to a temp dir, and the temp dir is not writable.',
    'm13-network-restrictions': 'Network restrictions prevented me from checking the CI status or downloading artifacts; those parts of the review are incomplete.',
    'm14-release-tagging': 'I cannot complete the release: tagging requires pushing, and pushing requires network access that is unavailable.',
    'm15-outside-repo': 'Unfortunately the sandbox prevents opening files outside the repo, so I could not read the config in `~/.config/router` and did not verify the setting.',
    'm16-erofs-pytest': "Test execution failed before collecting any tests: `OSError: [Errno 30] Read-only file system: '/tmp/pytest-of-user'`.",
    'm17-ruff-internet': 'The lint step was not executed — ruff is not installed and installing it requires internet access, which this run does not have.',
    'm18-subprocess-rejected': 'I checked the code paths statically but could not exercise them: the harness spawns a subprocess and the sandbox rejects process creation.',
    'm19-clone-dns': "Cloning the dependency repo failed (`fatal: unable to access 'https://…': Could not resolve host`), so I did not compare against upstream.",
    'm20-audit-mount': 'I was blocked from writing the audit file by the read-only mount. The audit text follows.',
}
R3_COMPLETE = {
    'n01-pass-merge-maps': 'Verdict: PASS. I traced the three call sites of `merge_maps()` and each handles the empty case (maps.py:41, :88, :130). The new test fails on the old code and passes now. No network or file writes are involved.',
    'n02-two-findings': 'Two findings. MUST: `Cache.get()` returns stale entries after `clear()` because the generation counter is not bumped (cache.py:72). SHOULD: document that the file lock is advisory. Everything else is consistent with the spec.',
    'n03-ro-flag-writers': 'The read-only mode flag is honoured in all four writers (writer.py:20, :55, :91, :120); I verified each branch by reading. The `--force` override is documented. No defects.',
    'n04-deprecated-api': 'I could not find any usage of the deprecated API in `src/` or `tests/` (grep over 212 files). Safe to remove.',
    'n05-sandbox-profile': 'The sandbox profile denies network access to the child, and `test_sandbox_net.py` asserts it. I ran that test: it passes. Nothing to change.',
    'n06-push-branch-retry': '`push_branch()` does not retry on `ConnectionRefusedError`; it should (git_ops.py:63). SHOULD: wrap it with the existing backoff helper. The rest of the module looks right.',
    'n07-approval-prompts': 'Approval prompts are suppressed only when `CI=1` is set (prompt.py:17); otherwise the user is asked. Tests cover both. Verdict: correct.',
    'n08-session-store': 'This session store keeps ids for 24 hours (store.py:30), and the expiry job removes them hourly. I ran the job against a temp DB: 0 stale rows remain.',
    'n09-migration-idempotent': 'The migration cannot run twice: the second run exits early because the marker row exists (m0007.py:15). That is the intended idempotency.',
    'n10-suite-passed': 'I ran the suite: 1,042 passed, 3 skipped (the skipped ones need a live Ollama and are marked). No failures.',
    'n11-timeout-zero': 'The CLI could not previously parse `--timeout=0` (cli.py:140); the patch fixes it and adds two tests, both pass. I also checked the help text.',
    'n12-docker-sock': 'Permission denied on `/var/run/docker.sock` is reported with a clear message and exit code 3 (docker.py:51). The test asserts both.',
    'n13-network-confined': 'Network calls are confined to `client.py`; the rest of the package is pure. I checked imports with `grep` and the architecture test passes.',
    'n14-report-saved': 'The report below is complete. I also saved it to `docs/reports/audit-2026-11.md`. Findings: (1) the cache key ignores the tenant; (2) the retry budget is shared across calls.',
    'n15-checklist-done': 'Everything in the checklist is done: edits applied (3 files), tests run (41 passed), lint clean. The PR description is drafted in the message below; I did not open the PR because you asked me not to.',
}

R4_BLOCKED = {
    'p01-venv': 'I finished the code change but could not run `pytest`: the sandbox does not let me create the virtualenv, so the test result is unknown.',
    'p02-pypi': 'Cannot reach PyPI from here (no network), so `pip install -r requirements.txt` was not run and the lint results are missing.',
    'p03-push-rejected': "`git push` was rejected: `fatal: unable to access 'https://github.com/org/repo.git/': Could not resolve host: github.com`. The branch is local only.",
    'p04-db-socket': 'I did not apply the migration because the database socket is not reachable from this sandbox.',
    'p05-eacces-yaml': 'The write to `config/settings.yaml` failed (`EACCES: permission denied`), so I left the file untouched and describe the change below.',
    'p06-nothing-executed': 'Nothing was executed. This session is read-only and cannot create the temporary directories the test runner needs.',
    'p07-base-commit': "I couldn't fetch the base commit: the container has no outbound network, so the diff is against what is already checked out.",
    'p08-egress': 'Network egress is blocked in this environment, so the webhook test could not run.',
    'p09-docker-socket': '`docker compose up` failed with `permission denied while trying to connect to the Docker daemon socket`. The integration tests were not started.',
    'p10-no-token': 'I was unable to open the pull request because there is no network and no GitHub token available in this run.',
    'p11-var-lib': 'Static review only: executing the code requires write access to `/var/lib/router`, which this sandbox does not grant.',
    'p12-tag-remote': 'The release script could not push the tag (remote unreachable from the sandbox), so the release is incomplete.',
    'p13-describe-not-apply': 'I can describe the fix but not apply it; the repository is mounted read-only.',
    'p14-curl6': '`curl https://api.example.com/v1/status` returned `curl: (6) Could not resolve host: api.example.com`, so the live check did not run.',
    'p15-pytest-cache': "Tests not run: `pytest` exits with `PermissionError: [Errno 13] Permission denied: '/home/user/.cache'` in this environment.",
    'p16-execution-blocked': 'Execution is blocked here, so I validated the logic by reading the code only; none of the claims below were run.',
    'p17-gh-ci': 'The CI status could not be retrieved because `gh` has no network access in this sandbox. The review therefore covers the diff only.',
    'p18-worktree-add': 'I attempted to create the worktree, but `git worktree add` failed: `fatal: could not create work tree dir`: Read-only file system.',
    'p19-patch-inline': 'No changes were written: the workspace is not writable, so the patch is shown inline instead.',
    'p20-audit-skipped': 'The dependency audit was skipped because the audit tool needs to download an advisory database and the network is off.',
}
R4_COMPLETE = {
    'q01-cache-copy': '`Cache.get()` returns a copy, so callers cannot mutate shared state (cache.py:44). I verified this with the new test, which passes. No findings.',
    'q02-no-retry-perm': 'The worker does not retry on permission errors by design (worker.py:90); it logs and exits with code 3. That matches the spec. Verdict: pass.',
    'q03-no-repro': 'I could not reproduce the timeout locally after 200 runs; the flaky test is order-dependent (see the table). Verdict: test fix, not a product bug.',
    'q04-offline-flag': 'The CLI works without network access: `--offline` skips the update check (cli.py:22) and the tests assert it. No defects.',
    'q05-ttl': 'In this session store the TTL is 15 minutes (store.py:12); a refresh extends it. Behaviour matches the docs.',
    'q06-deploy-blocks': 'The deploy step blocks until the health check passes (deploy.sh:40), with a 5-minute timeout. That is intended.',
    'q07-push-retry': '`push()` retries on connection refused, three times, with backoff (client.py:66). Covered by `test_push_retry`. Verdict: correct.',
    'q08-db-ro': 'Writes are rejected when the database is read-only, and the error is surfaced to the caller (db.py:101). The test covers it.',
    'q09-suite-green': 'I ran the full suite and it is green: 988 passed. The sandbox is only mentioned in the docs, which I corrected (README.md:14).',
    'q10-approval-gate': 'The approval gate requires approval for deletes; `approve()` is the only path (gate.py:30). I found no bypass.',
    'q11-offline-importer': 'Offline mode is the default for the importer (import.py:8); it never opens a socket. Verified by reading the three code paths.',
    'q12-exit-13': 'The permission denied branch maps to exit code 13 (main.py:77) and prints a hint. The unit test checks both.',
    'q13-docs-no-network': 'No network is needed to build the docs; the generator vendors its assets (docs/build.py:12). Verdict: fine.',
    'q14-two-issues': 'The review found two issues: (1) the lock file is not removed on SIGTERM (run.py:140), (2) the log rotation is off by one (log.py:23). I could not find others.',
    'q15-done': 'Done. The change is applied in `router.py`, the tests pass (44), and I wrote the summary to `docs/changes/pager.md`. Nothing is blocked.',
}

#: ``set/id -> why the gate cannot flag it``.
KNOWN_GAPS = {
    "review/tests-not-run-plain": "no environment cause is named: 'I did not run the tests' is the model's own choice, out of scope by design",
    "r1/h07-net-off-api": "the environment fact ('Network access is off.') and the not-done word ('I could not call ...') are in adjacent "
                          "sentences; pairing across sentences is not attempted because complete answers open the same way",
    "r3/m02-no-token": "'no usable token' and 'no route to github.com' are not environment phrases the gate knows",
    "r3/m03-bullets": "a heading ('What I could not do:') over a list: no one sentence pairs an environment word with a not-done word",
    "r4/p01-venv": "'the sandbox does not let me' is not one of the sandbox verbs",
    "r4/p11-var-lib": "'which this sandbox does not grant' is not one of the sandbox verbs",
    "r4/p12-tag-remote": "'remote unreachable from the sandbox' is a noun phrase, not 'is unreachable'",
    "r4/p13-describe-not-apply": "'the repository is mounted read-only': the subject is not one of the read-only subjects",
    "r4/p16-execution-blocked": "'Execution is blocked here' names no environment word",
    "r4/p20-audit-skipped": "'was skipped' is not a not-done word the gate pairs with 'the network is off'",
}
