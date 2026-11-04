---
id: PROXY-START-1
status: fixed in this change (the walk); the 2026-11-04 slow start itself is unexplained
---
## PROXY-START-1. The proxy walked the whole state directory before it opened its port

- **Symptom.** After the #824 deploy on 2026-11-04 the proxy on 8797 was not answering after about
  50 s (`~/.rsi/research/primary-plan/v16/coord/deploy-pr824-run-20261104T120527Z.log`); the comment
  on line 189 of `coord/deploy-pr.sh` records about 85 s at load 8.6 and blames a startup scan of a
  large directory. Four other logged kickstarts that day (n=4, `deploy-w7-run-20261104T103735Z.log`,
  `deploy-pr822-run2-20261104T150959Z.log`, `deploy-pr823-run-20261104T170349Z.log`,
  `deploy-pr825-run-20261104T181440Z.log`) answered inside whole deploy runs of 22 to 39 s.
- **Cause.** Part proven, part not. Proven: `cmd_proxy` called `state_files.harden_state()` before
  `uvicorn.run`. That walk visits every inode under `~/.llm-router`, 43,644 files in 17,823
  directories on 2026-11-04 (a read-only `os.scandir` count, one pass), not the top-level files its
  docstring and `docs/RETENTION.md` measured (3,000 files in 30 directories). Its cost grows with
  the tree: with `scripts/time_proxy_start.py` the port opened at a median of 0.857 s (n=8) on a
  tree of the live size and 2.289 s (n=5) on a tree three times that. Not proven: that this walk
  caused the 50 to 85 s. It costs under 1 s here, and an 85 s walk of 61k inodes would be 1.4 ms per
  inode where a first pass over `~/.rsi/runs` (21,522 entries, n=1) cost 19.9 microseconds. The
  deploy's own probe (`curl --max-time 3 http://127.0.0.1:8797/`) is forwarded by the proxy to the
  Anthropic API, so it also waits for one upstream round trip (0.16 s here:
  `first_http_s` minus `listen_s` in the script's output), and it cannot tell "not listening" from
  "listening but upstream slow".
- **Fix.** `state_files.harden_state_in_background()` runs the same repair on a daemon thread, with
  the root resolved in the caller. `cmd_proxy` calls it in place of the inline walk. The port opens
  at a median of 0.204 s (n=8) on the live-size tree and 0.209 s (n=5) on the 3x tree
  (`scripts/time_proxy_start.py`, 2026-11-04). The MCP
  server and `doctor` still walk inline.
- **Not done.** The cause of the 2026-11-04 slow start. Next time: take the old and new pids and
  `launchctl print` before and after the kickstart, and probe the port with a TCP connect
  (`nc -z 127.0.0.1 8797`) separately from the HTTP request, so "listening" and "upstream
  reachable" are two numbers. The MCP server's inline walk is the same cost at every session start
  and is left alone here (unmeasured: how long a session waits for it).
- **Rule.** Nothing whose cost grows with the state directory runs before a server's port opens.
  A cost figure for a recursive walk names the number of inodes it covers, not the top-level count.
- **Test.** `test_proxy_start_reaches_uvicorn_while_the_state_walk_is_still_running`,
  `test_harden_state_in_background_returns_before_the_walk_finishes`,
  `test_harden_state_in_background_binds_the_root_at_call_time`,
  `test_harden_state_in_background_repairs_like_the_synchronous_call` and
  `test_server_main_and_proxy_start_call_harden_state` in `tests/test_w04_state_file_modes.py`
  (the first fails on a727cb26: the walk had finished when `uvicorn.run` was reached);
  `tests/test_time_proxy_start_script.py` for the measuring script's guards.
