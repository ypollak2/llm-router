"""The plan suite: §28-shaped tasks for Q15 (architecture/Q15_HARNESS_PLAN.md).

Each task is a small repository with a PLAN.md, and the prompt is literally
"Implement the plan." — the §28 scenario. Verified completion means:

  * HIDDEN acceptance tests pass. They are written into the sandbox only by the
    verifier, after the backend has finished, so no backend can read them.
  * the repository's own tests still pass.

Both arms of Q15 (one strong model, and later the graph) get exactly these
checks. REFERENCE holds a correct solution per task: `--self-test` proves every
verifier FAILS on the untouched fixture and PASSES on the reference, so a
verifier that accepts anything cannot hide in here.
"""
from __future__ import annotations

import os as _os

# Benchmark traffic, not usage (M-02).
_os.environ.setdefault("LLM_ROUTER_SYNTHETIC", "1")

EDIT = "edit"
PROMPT = "Implement the plan in PLAN.md. Run the tests when you are done."


def _verifier(hidden: str) -> str:
    """Write the hidden tests AFTER the run, then run them and the repo's own."""
    return (
        "import pathlib\n"
        "pathlib.Path('_acceptance').mkdir(exist_ok=True)\n"
        f"pathlib.Path('_acceptance/test_acceptance.py').write_text({hidden!r})\n"
        "ok, log = pytest_passes('_acceptance')\n"
        "assert ok, 'acceptance: ' + log\n"
        "ok, log = pytest_passes('tests')\n"
        "assert ok, 'own tests: ' + log\n"
    )


# ── plan-retry ────────────────────────────────────────────────────────────────

RETRY_FILES = {
    "src/__init__.py": "",
    "src/config.py": 'TIMEOUT_S = 5.0\n',
    "src/errors.py": (
        "class TransientError(Exception):\n"
        '    """A failure worth retrying (timeouts, 503s)."""\n\n\n'
        "class PermanentError(Exception):\n"
        '    """A failure that will not go away by retrying (404, auth)."""\n'
    ),
    "src/client.py": (
        "from dataclasses import dataclass\n\n"
        "from src import config\n\n\n"
        "@dataclass\n"
        "class Response:\n"
        "    status: int\n"
        "    body: str\n\n\n"
        "def fetch(url, transport):\n"
        '    """Fetch url through transport(url, timeout) -> (status, body)."""\n'
        "    status, body = transport(url, config.TIMEOUT_S)\n"
        "    return Response(status, body)\n"
    ),
    "tests/test_client.py": (
        "from src.client import fetch\n\n\n"
        "def test_fetch_returns_body():\n"
        "    r = fetch('u', lambda url, timeout: (200, 'hi'))\n"
        "    assert (r.status, r.body) == (200, 'hi')\n"
    ),
    "PLAN.md": (
        "# Plan: retries for fetch\n\n"
        "1. In `src/config.py` add `RETRY_MAX = 3` (total attempts, including the first)\n"
        "   and `RETRY_BASE_S = 0.1`.\n"
        "2. `fetch(url, transport, sleep=time.sleep)` retries when the transport raises\n"
        "   `TransientError`, up to `RETRY_MAX` attempts in total. Before retry *i*\n"
        "   (i = 0 for the first retry) it calls `sleep(RETRY_BASE_S * 2**i)`.\n"
        "3. `PermanentError` propagates immediately, with no retry and no sleep.\n"
        "4. When every attempt fails, the last `TransientError` propagates.\n"
        "5. `Response` gains `attempts: int`, the number of transport calls made.\n"
        "6. Read the config values at call time, so tests can change them.\n"
    ),
}

RETRY_HIDDEN = '''
import pytest
from src import config
from src.client import fetch
from src.errors import PermanentError, TransientError


def flaky(fails, exc=TransientError):
    calls = []
    def transport(url, timeout):
        calls.append(url)
        if len(calls) <= fails:
            raise exc("boom")
        return 200, "ok"
    return transport, calls


def test_config_defaults():
    assert config.RETRY_MAX == 3 and config.RETRY_BASE_S == 0.1


def test_retries_then_succeeds_with_backoff():
    t, calls = flaky(2)
    slept = []
    r = fetch("u", t, sleep=slept.append)
    assert r.body == "ok" and r.attempts == 3 and len(calls) == 3
    assert slept == pytest.approx([0.1, 0.2])


def test_permanent_error_is_not_retried():
    t, calls = flaky(5, PermanentError)
    slept = []
    with pytest.raises(PermanentError):
        fetch("u", t, sleep=slept.append)
    assert len(calls) == 1 and slept == []


def test_exhaustion_raises_the_transient_error():
    t, calls = flaky(99)
    with pytest.raises(TransientError):
        fetch("u", t, sleep=lambda s: None)
    assert len(calls) == 3


def test_config_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(config, "RETRY_MAX", 5)
    t, calls = flaky(4)
    assert fetch("u", t, sleep=lambda s: None).attempts == 5


def test_first_attempt_success_does_not_sleep():
    slept = []
    r = fetch("u", lambda url, timeout: (200, "x"), sleep=slept.append)
    assert r.attempts == 1 and slept == []
'''

RETRY_REFERENCE = {
    "src/config.py": "TIMEOUT_S = 5.0\nRETRY_MAX = 3\nRETRY_BASE_S = 0.1\n",
    "src/client.py": (
        "import time\n"
        "from dataclasses import dataclass\n\n"
        "from src import config\n"
        "from src.errors import TransientError\n\n\n"
        "@dataclass\n"
        "class Response:\n"
        "    status: int\n"
        "    body: str\n"
        "    attempts: int = 1\n\n\n"
        "def fetch(url, transport, sleep=time.sleep):\n"
        "    for i in range(config.RETRY_MAX):\n"
        "        try:\n"
        "            status, body = transport(url, config.TIMEOUT_S)\n"
        "            return Response(status, body, i + 1)\n"
        "        except TransientError:\n"
        "            if i + 1 >= config.RETRY_MAX:\n"
        "                raise\n"
        "            sleep(config.RETRY_BASE_S * 2 ** i)\n"
    ),
}

# ── plan-ttl-cache ────────────────────────────────────────────────────────────

CACHE_FILES = {
    "src/__init__.py": "",
    "src/store.py": (
        "class UserStore:\n"
        '    """Users by id, backed by a slow backend(user_id) -> dict."""\n\n'
        "    def __init__(self, backend):\n"
        "        self._backend = backend\n\n"
        "    def get(self, user_id):\n"
        "        return self._backend(user_id)\n\n"
        "    def update(self, user_id, **fields):\n"
        "        record = dict(self._backend(user_id))\n"
        "        record.update(fields)\n"
        "        return record\n"
    ),
    "src/service.py": (
        "from src.store import UserStore\n\n\n"
        "def display_name(store: UserStore, user_id):\n"
        "    u = store.get(user_id)\n"
        "    return f\"{u['name']} <{u['email']}>\"\n"
    ),
    "tests/test_service.py": (
        "from src.service import display_name\n"
        "from src.store import UserStore\n\n\n"
        "def test_display_name():\n"
        "    s = UserStore(lambda uid: {'name': 'Ann', 'email': 'a@x'})\n"
        "    assert display_name(s, 1) == 'Ann <a@x>'\n"
    ),
    "PLAN.md": (
        "# Plan: cache user lookups\n\n"
        "1. New module `src/cache.py` with `TTLCache(ttl_s, clock=time.monotonic)` and\n"
        "   methods `get(key)` (returns `None` when the key is missing or expired),\n"
        "   `set(key, value)` and `invalidate(key)`. An entry set at time t is valid\n"
        "   while `clock() - t < ttl_s`.\n"
        "2. `UserStore(backend, ttl_s=60.0, clock=time.monotonic)` caches `get()`\n"
        "   results, so the backend is called at most once per id per TTL window.\n"
        "3. `UserStore.update()` invalidates that id, so the next `get()` refetches.\n"
        "4. `src/service.py` keeps its API unchanged.\n"
    ),
}

CACHE_HIDDEN = '''
from src.cache import TTLCache
from src.store import UserStore


class Clock:
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        return self.t


def counting_backend():
    calls = []
    def backend(uid):
        calls.append(uid)
        return {"name": f"u{uid}", "email": "e"}
    return backend, calls


def test_ttlcache_expiry_boundary():
    c = Clock()
    cache = TTLCache(10, clock=c)
    cache.set("k", 1)
    c.t = 9.99
    assert cache.get("k") == 1
    c.t = 10.0
    assert cache.get("k") is None


def test_ttlcache_missing_and_invalidate():
    cache = TTLCache(10, clock=Clock())
    assert cache.get("nope") is None
    cache.set("k", 1)
    cache.invalidate("k")
    assert cache.get("k") is None


def test_store_calls_backend_once_per_window():
    c = Clock()
    backend, calls = counting_backend()
    s = UserStore(backend, ttl_s=30, clock=c)
    s.get(1); s.get(1); s.get(2)
    assert calls == [1, 2]
    c.t = 31
    s.get(1)
    assert calls == [1, 2, 1]


def test_update_invalidates():
    backend, calls = counting_backend()
    s = UserStore(backend, ttl_s=30, clock=Clock())
    s.get(1)
    s.update(1, name="new")
    s.get(1)
    assert calls.count(1) >= 3  # get, update's read, refetch after invalidation


def test_default_construction_still_works():
    s = UserStore(lambda uid: {"name": "n", "email": "e"})
    assert s.get(1)["name"] == "n"
'''

CACHE_REFERENCE = {
    "src/cache.py": (
        "import time\n\n\n"
        "class TTLCache:\n"
        "    def __init__(self, ttl_s, clock=time.monotonic):\n"
        "        self._ttl, self._clock, self._d = ttl_s, clock, {}\n\n"
        "    def get(self, key):\n"
        "        hit = self._d.get(key)\n"
        "        if hit is None or self._clock() - hit[0] >= self._ttl:\n"
        "            return None\n"
        "        return hit[1]\n\n"
        "    def set(self, key, value):\n"
        "        self._d[key] = (self._clock(), value)\n\n"
        "    def invalidate(self, key):\n"
        "        self._d.pop(key, None)\n"
    ),
    "src/store.py": (
        "import time\n\n"
        "from src.cache import TTLCache\n\n\n"
        "class UserStore:\n"
        "    def __init__(self, backend, ttl_s=60.0, clock=time.monotonic):\n"
        "        self._backend = backend\n"
        "        self._cache = TTLCache(ttl_s, clock=clock)\n\n"
        "    def get(self, user_id):\n"
        "        hit = self._cache.get(user_id)\n"
        "        if hit is None:\n"
        "            hit = self._backend(user_id)\n"
        "            self._cache.set(user_id, hit)\n"
        "        return hit\n\n"
        "    def update(self, user_id, **fields):\n"
        "        record = dict(self._backend(user_id))\n"
        "        record.update(fields)\n"
        "        self._cache.invalidate(user_id)\n"
        "        return record\n"
    ),
}

# ── plan-todo-cli ─────────────────────────────────────────────────────────────

TODO_FILES = {
    "src/__init__.py": "",
    "src/todo.py": (
        "import json\n"
        "from pathlib import Path\n\n\n"
        "def load(path):\n"
        "    p = Path(path)\n"
        "    return json.loads(p.read_text()) if p.exists() else []\n\n\n"
        "def save(path, items):\n"
        "    Path(path).write_text(json.dumps(items))\n\n\n"
        "def add(path, text):\n"
        "    items = load(path)\n"
        "    item = {'id': max((i['id'] for i in items), default=0) + 1, 'text': text}\n"
        "    items.append(item)\n"
        "    save(path, items)\n"
        "    return item\n"
    ),
    "src/cli.py": (
        "import argparse\n\n"
        "from src import todo\n\n\n"
        "def main(argv, out=print):\n"
        "    ap = argparse.ArgumentParser(prog='todo')\n"
        "    ap.add_argument('--file', required=True)\n"
        "    sub = ap.add_subparsers(dest='cmd', required=True)\n"
        "    a = sub.add_parser('add'); a.add_argument('text')\n"
        "    sub.add_parser('list')\n"
        "    args = ap.parse_args(argv)\n"
        "    if args.cmd == 'add':\n"
        "        item = todo.add(args.file, args.text)\n"
        "        out(f\"added {item['id']}\")\n"
        "    elif args.cmd == 'list':\n"
        "        for i in todo.load(args.file):\n"
        "            out(f\"{i['id']} {i['text']}\")\n"
        "    return 0\n"
    ),
    "tests/test_cli.py": (
        "from src.cli import main\n\n\n"
        "def test_add_then_list(tmp_path):\n"
        "    f = str(tmp_path / 't.json'); got = []\n"
        "    main(['--file', f, 'add', 'milk'], out=got.append)\n"
        "    main(['--file', f, 'list'], out=got.append)\n"
        "    assert got == ['added 1', '1 milk']\n"
    ),
    "PLAN.md": (
        "# Plan: completing todos\n\n"
        "1. Items carry `done: bool`. New items start with `done: false`. Files written\n"
        "   by the old version have no `done` key: treat a missing key as `false`\n"
        "   when loading, and do not crash.\n"
        "2. New subcommand `done <id>` marks that item done and prints `done <id>`.\n"
        "   An unknown id prints `no such item <id>` and returns exit code 1, without\n"
        "   changing the file.\n"
        "3. `list` prints `<id> [x] <text>` for done items and `<id> [ ] <text>`\n"
        "   otherwise. `list --pending` shows only the items that are not done.\n"
        "4. Keep `add` output unchanged. Update the existing test for the new `list` format.\n"
    ),
}

TODO_HIDDEN = '''
import json
from src.cli import main


def run(f, *argv):
    got = []
    rc = main(["--file", f, *argv], out=got.append)
    return rc, got


def test_done_and_list_format(tmp_path):
    f = str(tmp_path / "t.json")
    run(f, "add", "milk"); run(f, "add", "eggs")
    assert run(f, "done", "2") == (0, ["done 2"])
    assert run(f, "list")[1] == ["1 [ ] milk", "2 [x] eggs"]
    assert run(f, "list", "--pending")[1] == ["1 [ ] milk"]


def test_unknown_id_fails_without_writing(tmp_path):
    f = tmp_path / "t.json"
    run(str(f), "add", "milk")
    before = f.read_text()
    rc, got = run(str(f), "done", "9")
    assert rc == 1 and got == ["no such item 9"]
    assert f.read_text() == before


def test_old_files_without_done_key_load(tmp_path):
    f = tmp_path / "t.json"
    f.write_text(json.dumps([{"id": 1, "text": "old"}]))
    assert run(str(f), "list")[1] == ["1 [ ] old"]
    assert run(str(f), "done", "1")[0] == 0
    assert run(str(f), "list", "--pending")[1] == []


def test_new_items_persist_done_false(tmp_path):
    f = tmp_path / "t.json"
    run(str(f), "add", "x")
    assert json.loads(f.read_text())[0]["done"] is False
'''

TODO_REFERENCE = {
    "src/todo.py": (
        "import json\n"
        "from pathlib import Path\n\n\n"
        "def load(path):\n"
        "    p = Path(path)\n"
        "    items = json.loads(p.read_text()) if p.exists() else []\n"
        "    for i in items:\n"
        "        i.setdefault('done', False)\n"
        "    return items\n\n\n"
        "def save(path, items):\n"
        "    Path(path).write_text(json.dumps(items))\n\n\n"
        "def add(path, text):\n"
        "    items = load(path)\n"
        "    item = {'id': max((i['id'] for i in items), default=0) + 1, 'text': text,\n"
        "            'done': False}\n"
        "    items.append(item)\n"
        "    save(path, items)\n"
        "    return item\n\n\n"
        "def mark_done(path, item_id):\n"
        "    items = load(path)\n"
        "    for i in items:\n"
        "        if i['id'] == item_id:\n"
        "            i['done'] = True\n"
        "            save(path, items)\n"
        "            return True\n"
        "    return False\n"
    ),
    "src/cli.py": (
        "import argparse\n\n"
        "from src import todo\n\n\n"
        "def main(argv, out=print):\n"
        "    ap = argparse.ArgumentParser(prog='todo')\n"
        "    ap.add_argument('--file', required=True)\n"
        "    sub = ap.add_subparsers(dest='cmd', required=True)\n"
        "    a = sub.add_parser('add'); a.add_argument('text')\n"
        "    ls = sub.add_parser('list'); ls.add_argument('--pending', action='store_true')\n"
        "    d = sub.add_parser('done'); d.add_argument('id', type=int)\n"
        "    args = ap.parse_args(argv)\n"
        "    if args.cmd == 'add':\n"
        "        item = todo.add(args.file, args.text)\n"
        "        out(f\"added {item['id']}\")\n"
        "    elif args.cmd == 'done':\n"
        "        if not todo.mark_done(args.file, args.id):\n"
        "            out(f'no such item {args.id}')\n"
        "            return 1\n"
        "        out(f'done {args.id}')\n"
        "    elif args.cmd == 'list':\n"
        "        for i in todo.load(args.file):\n"
        "            if args.pending and i['done']:\n"
        "                continue\n"
        "            out(f\"{i['id']} [{'x' if i['done'] else ' '}] {i['text']}\")\n"
        "    return 0\n"
    ),
    "tests/test_cli.py": (
        "from src.cli import main\n\n\n"
        "def test_add_then_list(tmp_path):\n"
        "    f = str(tmp_path / 't.json'); got = []\n"
        "    main(['--file', f, 'add', 'milk'], out=got.append)\n"
        "    main(['--file', f, 'list'], out=got.append)\n"
        "    assert got == ['added 1', '1 [ ] milk']\n"
    ),
}

# ── Suite ─────────────────────────────────────────────────────────────────────
# Tasks carry their own fixture (6th element). `allowed` entries ending in "/"
# are prefixes, because a plan-implementing agent may reasonably add tests.

TASKS = [
    ("plan-retry", EDIT, PROMPT, ["src/", "tests/"], _verifier(RETRY_HIDDEN),
     {}, RETRY_FILES, RETRY_REFERENCE),
    ("plan-ttl-cache", EDIT, PROMPT, ["src/", "tests/"], _verifier(CACHE_HIDDEN),
     {}, CACHE_FILES, CACHE_REFERENCE),
    ("plan-todo-cli", EDIT, PROMPT, ["src/", "tests/"], _verifier(TODO_HIDDEN),
     {}, TODO_FILES, TODO_REFERENCE),
]
FILES: dict[str, str] = {}  # per-task fixtures; see TASKS[i][6]
