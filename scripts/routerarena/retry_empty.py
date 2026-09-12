#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Re-run the rows a generation pass left empty or errored.

``run_single_model.py`` retries exceptions, which covers HTTP 429 and timeouts but *not* the
case where the provider returns 200 with an empty ``content``. Under heavy throttling that
turns out to be the dominant failure: a full-split run against a free endpoint came back with
64 exceptions and 463 empty-but-successful rows -- 5.6% of the split, every one of them scored
as a wrong answer. That is a measurement artifact of the rate limiter, not of the model, and
it cost 4.3 accuracy points.

So an empty body is treated here as a retryable condition in its own right. Rows that come
back non-empty replace the originals; rows that stay empty after every attempt are left as
they were, because a model that genuinely declines to answer should still be scored on it.

Usage::

    OPENROUTER_API_KEY=... python scripts/routerarena/retry_empty.py \\
        --model stealth/ox-alpha --file /tmp/ox_full.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--file", required=True, help="generation jsonl, rewritten in place")
    ap.add_argument("--prompts", default="/tmp/full_prompts.json")
    ap.add_argument("--key", default="main")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--attempts", type=int, default=8)
    args = ap.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set")

    prompts = json.loads(Path(args.prompts).read_text())[args.key]
    path = Path(args.file)
    rows = [json.loads(line) for line in path.open()]
    by_gi = {r["gi"]: r for r in rows}

    def is_bad(r: dict) -> bool:
        return bool(r.get("error")) or not (r.get("text") or "").strip()

    todo = [gi for gi, r in by_gi.items() if is_bad(r)]
    print(f"{len(rows)} rows | {len(todo)} empty or errored -> retrying", flush=True)
    if not todo:
        return 0

    lock = threading.Lock()
    fixed = [0]
    n = [0]
    t0 = time.time()

    def call(gi: str) -> tuple[str, dict | None]:
        prompt = prompts[gi]
        delay = 4.0
        for attempt in range(args.attempts):
            try:
                req = urllib.request.Request(
                    ENDPOINT,
                    data=json.dumps({
                        "model": args.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": args.max_tokens,
                    }).encode(),
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=300) as resp:
                    d = json.loads(resp.read())
                text = d["choices"][0]["message"]["content"] or ""
                u = d.get("usage") or {}
                if text.strip():
                    return gi, {"gi": gi, "model": args.model, "text": text,
                                "usage": {"input_tokens": u.get("prompt_tokens", 0),
                                          "output_tokens": u.get("completion_tokens", 0)}}
                # 200 with an empty body: the throttle case this script exists for.
            except Exception:  # noqa: BLE001
                pass
            time.sleep(delay)
            delay = min(delay * 1.7, 90.0)
        return gi, None

    # Recoveries land in a sidecar as they happen. Against a throttled free endpoint this
    # loop can run for hours, and holding every repaired row in memory until the end means a
    # Ctrl-C -- or a decision to cut it short -- throws away everything already bought.
    side = path.with_suffix(path.suffix + ".recovered")
    for line in side.open() if side.exists() else []:
        try:
            r = json.loads(line)
            by_gi[r["gi"]] = r
            fixed[0] += 1
        except Exception:
            pass
    if fixed[0]:
        print(f"  resumed {fixed[0]} recoveries from a previous pass", flush=True)
        todo = [gi for gi in todo if is_bad(by_gi[gi])]

    with side.open("a") as sf, ThreadPoolExecutor(max_workers=args.workers) as pool:
        for gi, res in pool.map(call, todo):
            with lock:
                n[0] += 1
                if res is not None:
                    by_gi[gi] = res
                    fixed[0] += 1
                    sf.write(json.dumps(res) + "\n")
                    sf.flush()
                if n[0] % 25 == 0:
                    rate = n[0] / max(time.time() - t0, 1)
                    print(f"  {n[0]}/{len(todo)}  recovered={fixed[0]}  {rate:.2f}/s", flush=True)

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(by_gi[r["gi"]]) + "\n")
    tmp.replace(path)

    still = sum(1 for r in by_gi.values() if is_bad(r))
    print(f"\nrecovered {fixed[0]} of {len(todo)} | still empty/errored: {still}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
