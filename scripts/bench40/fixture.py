"""A realistic multi-file project + deterministic screenshots for the benchmark.

Ground truth is constructed, so every check is mechanical: a string that must
appear, or file content that must match exactly. No model scores another.
"""
from pathlib import Path
import shutil

ROOT = Path(__file__).parent / "proj"

FILES = {
"src/config.py": '''"""Runtime configuration."""

MAX_RETRIES = 3
TIMEOUT_SECONDS = 30
CACHE_SIZE = 128
DEBUG = False


def timeout_ms():
    return TIMEOUT_SECONDS * 1000
''',
"src/cache.py": '''from src.config import CACHE_SIZE


class LRUCache:
    """A tiny LRU cache."""

    def __init__(self, size=CACHE_SIZE):
        self.size = size
        self._items = {}

    def get(self, key):
        return self._items.get(key)

    def put(self, key, value):
        if len(self._items) >= self.size:
            self._items.pop(next(iter(self._items)))
        self._items[key] = value

    def clear(self):
        self._items = {}
''',
"src/client.py": '''import time

from src.cache import LRUCache
from src.config import MAX_RETRIES, TIMEOUT_SECONDS


class ApiClient:
    def __init__(self):
        self.cache = LRUCache()
        self.calls = 0

    def fetch(self, url):
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        for attempt in range(MAX_RETRIES):
            self.calls += 1
            result = self._request(url)
            if result is not None:
                self.cache.put(url, result)
                return result
            time.sleep(1)
        return None

    def _request(self, url):
        return {"url": url, "ok": True}
''',
"src/util.py": '''def slugify(text):
    return text.lower().replace(" ", "-")


def truncate(text, limit=80):
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def parse_bool(value):
    return str(value).lower() in ("1", "true", "yes")
''',
"tests/test_cache.py": '''from src.cache import LRUCache


def test_put_and_get():
    c = LRUCache(size=2)
    c.put("a", 1)
    assert c.get("a") == 1


def test_evicts_when_full():
    c = LRUCache(size=2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)
    assert c.get("a") is None
''',
"tests/test_util.py": '''from src.util import slugify, truncate, parse_bool


def test_slugify():
    assert slugify("Hello World") == "hello-world"


def test_truncate():
    assert truncate("abc", 10) == "abc"


def test_parse_bool():
    assert parse_bool("YES") is True
''',
"README.md": '''# Demo Service

A small API client with an LRU cache.

## Install

    pip install -e .

## Configuration

Set `TIMEOUT_SECONDS` in `src/config.py`.
''',
"pyproject.toml": '''[project]
name = "demo-service"
version = "0.4.2"
requires-python = ">=3.11"
''',
"CHANGELOG.md": '''# Changelog

## 0.4.2
- Fix cache eviction order

## 0.4.1
- Add retry logic
''',
}


def build():
    shutil.rmtree(ROOT, ignore_errors=True)
    for rel, body in FILES.items():
        p = ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return ROOT


def read(rel):
    try:
        return (ROOT / rel).read_text()
    except OSError:
        return ""
