"""Permission-check capture (D-532): store the proxy's D-525 side calls for an offline replay.

Flag: ``LLM_ROUTER_PERMCHECK_CAPTURE=<N>`` (default off). Unset, empty, ``0`` or a non-integer
means OFF: nothing here runs on the request path beyond one :func:`limit` call, and no file or
directory is created. ON, at most N checks per proxy process are appended, one JSON line each, to
``$LLM_ROUTER_HOME/permcheck_capture/<UTC date>.jsonl`` (dir 0700, file 0600).

WHAT IS STORED: the full request body exactly as forwarded upstream and Claude's reply content,
i.e. full transcript text. That is sensitive. See docs/proxy.md ("Permission-check capture").

Which calls: exactly the rows main already labels ``reason == "not_eligible"`` with
``step_class == "side_call"`` (``proxy/server.py``, the ``step_class(...) is None`` branch), minus
streaming and tool-bearing requests. No new heuristic.

The ledger row gets ``permcheck_capture``: ``written`` | ``cap`` | ``error`` (an enum, never text),
only when the flag is on and the call was a candidate.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
import time
from pathlib import Path

from llm_router import failopen
from llm_router.paths import llm_router_home
from llm_router.proxy.steps import STEP_SIDE_CALL

ENV = "LLM_ROUTER_PERMCHECK_CAPTURE"
DIR_NAME = "permcheck_capture"
MAX_DIR_BYTES = 200 * 1024 * 1024
WRITTEN, CAP, ERROR = "written", "cap", "error"
ROW_KEY = "permcheck_capture"

_lock = threading.Lock()
_written = 0  # records written by this process


def limit() -> int:
    """N from the flag; 0 means OFF (unset, empty, 0, negative or not an integer)."""
    raw = os.environ.get("LLM_ROUTER_PERMCHECK_CAPTURE", "").strip()
    try:
        return max(int(raw), 0) if raw else 0
    except ValueError:
        return 0


def is_candidate(body: dict, row: dict) -> bool:
    """The D-525 bucket as the proxy's own row labels it, non-streaming and tool-free."""
    return (row.get("reason") == "not_eligible" and row.get("step_class") == STEP_SIDE_CALL
            and not body.get("stream") and not body.get("tools"))


_tasks: set[asyncio.Task] = set()


def spawn(coro) -> None:
    """Run ``coro`` as a background task (strong ref kept until done); the caller does not wait for it."""
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Wait for every spawned capture (tests and the proxy lifespan teardown; never called on the request path)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def reset_for_tests() -> None:
    global _written
    with _lock:
        _written = 0


def _capture_dir() -> Path:
    """The capture dir, created 0700. Refuses a symlink or a dir that is not owner-only."""
    d = llm_router_home() / DIR_NAME
    d.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PermissionError("capture dir is a symlink or not a directory")
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise PermissionError("capture dir is not owner-only")
    return d


def _dir_bytes(d: Path) -> int:
    return sum(e.stat().st_size for e in os.scandir(d) if e.is_file(follow_symlinks=False))


def _record(*, row: dict, key: str, raw: bytes, status: int, reply: bytes, latency_s) -> dict:
    data = json.loads(reply or b"{}")
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    return {
        "ts": row.get("ts"), "conversation": key,
        "request_body": raw.decode("utf-8"),
        "reply_content": data.get("content"),
        "upstream_model": data.get("model"),
        "usage": data.get("usage"),
        "upstream_latency_ms": None if latency_s is None else int(round(latency_s * 1000)),
        "upstream_status": status,
    }


def capture(row: dict, *, key: str, raw: bytes, status: int, reply: bytes, latency_s) -> str:
    """Append one record. Returns ``written`` / ``cap`` / ``error``; never raises."""
    global _written
    try:
        n = limit()
        with _lock:
            if n <= 0 or _written >= n:
                return CAP
            d = _capture_dir()
            line = (json.dumps(_record(row=row, key=key, raw=raw, status=status, reply=reply,
                                       latency_s=latency_s), separators=(",", ":")) + "\n").encode()
            if _dir_bytes(d) + len(line) > MAX_DIR_BYTES:
                return CAP
            day = time.strftime("%Y-%m-%d", time.gmtime())
            fd = os.open(d / f"{day}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fst = os.fstat(fd)
                if not stat.S_ISREG(fst.st_mode) or fst.st_uid != os.getuid() or fst.st_mode & 0o077:
                    raise PermissionError("capture file is not an owner-only regular file")
                os.write(fd, line)
            finally:
                os.close(fd)
            _written += 1
            return WRITTEN
    except Exception as exc:  # noqa: BLE001 -- a capture failure must never fail a request
        failopen.record("LR-FO-PROXY-PERMCHECK-CAPTURE", exc)
        return ERROR
