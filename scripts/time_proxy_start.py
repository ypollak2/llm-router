"""Time a proxy start in a throwaway HOME (source of the PROXY-START-HARDEN numbers).

Usage: ``python scripts/time_proxy_start.py [--runs N] [--files F] [--dirs D] [--http] [--python PY]``
(defaults: 5 runs, 3000 files in 30 directories, no HTTP request, the interpreter running this script).

Builds F empty files spread over D directories under a fresh temporary state directory, starts
``python -m llm_router.cli proxy`` on a free port with HOME, LLM_ROUTER_HOME and CODEX_HOME set to that
temporary directory, and prints one JSON line: for each run, the seconds from process start until the
port accepts a TCP connection (``listen_s``) and, with ``--http``, until ``GET /`` is answered
(``first_http_s``). ``GET /`` is forwarded upstream by the proxy, so ``first_http_s`` includes one
round trip to the Anthropic API, sent without credentials; it is what a deploy's ``curl`` sees.

Safe by construction: it never reads or writes the real state directory, refuses to run when the temporary
directory is not under the system temp dir, and uses a free port (never 8787 or 8797, the live proxy and its
shim). It does not call Ollama.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

LIVE_PORTS = (8787, 8797)
GIVE_UP_S = 170.0


def free_port() -> int:
    """A free loopback port that is not one of the live ones."""
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in LIVE_PORTS:
            return port


def under_system_temp(path: Path) -> bool:
    """True when ``path`` resolves inside the system temp directory (the only place this script writes)."""
    try:
        path.resolve().relative_to(Path(tempfile.gettempdir()).resolve())
        return True
    except ValueError:
        return False


def build_tree(state: Path, files: int, dirs: int) -> None:
    """``files`` empty 0600 files spread over ``dirs`` directories (0700) under ``state``."""
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    made = []
    for i in range(max(dirs, 1)):
        d = state / f"d{i}"
        d.mkdir(mode=0o700, exist_ok=True)
        made.append(d)
    for i in range(files):
        os.close(os.open(made[i % len(made)] / f"f{i}.jsonl", os.O_WRONLY | os.O_CREAT, 0o600))


def one_run(python: str, home: Path, http: bool) -> dict:
    port = free_port()
    assert port not in LIVE_PORTS
    env = dict(os.environ, HOME=str(home), LLM_ROUTER_HOME=str(home / ".llm-router"),
               CODEX_HOME=str(home / ".codex"))
    cmd = [python, "-m", "llm_router.cli", "proxy", "--port", str(port), "--steps", "off",
           "--tiers", "conversation"]
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=home, env=env,
                            start_new_session=True)
    listen_s = http_s = None
    try:
        while time.monotonic() - t0 < GIVE_UP_S and proc.poll() is None:
            if listen_s is None:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    listen_s = time.monotonic() - t0
                except OSError:
                    time.sleep(0.02)
                    continue
            if not http:
                break
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10).close()
                http_s = time.monotonic() - t0
                break
            except urllib.error.HTTPError:
                http_s = time.monotonic() - t0  # any HTTP answer counts, as in the deploy's curl
                break
            except Exception:  # noqa: BLE001 -- not answering yet
                time.sleep(0.02)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    return {"listen_s": None if listen_s is None else round(listen_s, 3),
            "first_http_s": None if http_s is None else round(http_s, 3)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--files", type=int, default=3000)
    ap.add_argument("--dirs", type=int, default=30)
    ap.add_argument("--http", action="store_true", help="also time the first HTTP answer (one upstream round trip)")
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args(argv[1:])
    with tempfile.TemporaryDirectory(prefix="proxy-start-") as tmp:
        home = Path(tmp)
        if not under_system_temp(home):
            print("refusing: the throwaway HOME is not under the system temp dir", file=sys.stderr)
            return 2
        build_tree(home / ".llm-router", a.files, a.dirs)
        runs = [one_run(a.python, home, a.http) for _ in range(a.runs)]
    listen = [r["listen_s"] for r in runs if r["listen_s"] is not None]
    out = {"runs": a.runs, "files": a.files, "dirs": a.dirs, "machine": f"{platform.platform()} {platform.machine()}",
           "python": platform.python_version(), "per_run": runs,
           "listen_median_s": round(statistics.median(listen), 3) if listen else None}
    if a.http:
        got = [r["first_http_s"] for r in runs if r["first_http_s"] is not None]
        out["first_http_median_s"] = round(statistics.median(got), 3) if got else None
    print(json.dumps(out))
    return 0 if listen else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
