"""P10: on-disk store of valid chunk summaries for the P9 map-reduce summarizer.

Flag LLM_ROUTER_CHUNK_STORE, default off, and only consulted when LLM_ROUTER_PROXY_MR is also on. Without
it every over-cap turn re-maps all closed chunks; with it a closed chunk already summarized is a hit and
costs no model call.

Key (ported from the research SummaryCache idea, simplified): sha256 over the RAW chunk messages, the map
prompt version, a hash of the map prompt template, the model name, num_predict and the mode ("model").
Any change to one of them is a miss. The research keyed on (session, epoch, span); here the key is
content-only, so an identical chunk is shared across turns and sessions.

Storage: ONE JSON file ``chunk_summaries.json`` in the router state dir (``paths.state_path``, which follows
LLM_ROUTER_HOME), mode 0600, written to a temp file and ``os.replace``d. Size bound: the newest
LLM_ROUTER_CHUNK_STORE_MAX entries (default 500), least recently used evicted first. The file holds summary
text (that is its job); the ledger only ever sees counts.

Only summaries that passed ``mr_summary.valid_summary`` are put here; extractive fallbacks never are, so a
later model map can replace them. A corrupt or unreadable file is a miss plus ``failopen``; nothing raises.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path

from llm_router import failopen, paths

FILE_NAME = "chunk_summaries.json"
DEFAULT_MAX_ENTRIES = 500
SCHEMA = 1
MODE_MODEL = "model"


def enabled() -> bool:
    return os.environ.get("LLM_ROUTER_CHUNK_STORE", "").strip().lower() in ("1", "true", "yes", "on")


def max_entries() -> int:
    try:
        value = int(os.environ.get("LLM_ROUTER_CHUNK_STORE_MAX", "").strip())
    except ValueError:
        return DEFAULT_MAX_ENTRIES
    return value if value > 0 else DEFAULT_MAX_ENTRIES


def store_path() -> Path:
    return paths.state_path(FILE_NAME)


def chunk_key(chunk: list[dict], *, prompt_version: str, prompt_template: str, model: str,
              num_predict: int, mode: str = MODE_MODEL) -> str:
    raw = json.dumps(chunk, sort_keys=True, ensure_ascii=False, default=str)
    parts = [raw, prompt_version, hashlib.sha256(prompt_template.encode()).hexdigest(), model, str(num_predict), mode]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


class ChunkStore:
    """Load once, serve hits from memory, write once (``flush``) when something was added. Never raises."""

    def __init__(self, path: Path | None = None, cap: int | None = None):
        self.path = path or store_path()
        self.cap = cap or max_entries()
        self.entries: dict[str, str] = {}  # insertion order = recency (oldest first)
        self.hits = self.misses = self.writes = 0
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            ents = doc["entries"]
            if doc.get("schema") != SCHEMA or not isinstance(ents, list):
                raise ValueError("unrecognised chunk store layout")
            for k, body in ents:
                if isinstance(k, str) and isinstance(body, str):
                    self.entries[k] = body
        except Exception as exc:  # noqa: BLE001 - a broken store is a miss, never a request failure
            self.entries = {}
            failopen.record("LR-FO-PROXY-CHUNK-STORE", exc, detail="load")

    def get(self, key: str) -> str | None:
        body = self.entries.pop(key, None)
        if body is None:
            self.misses += 1
            return None
        self.entries[key] = body  # most recently used
        self.hits += 1
        self._dirty = True
        return body

    def put(self, key: str, body: str) -> None:
        self.entries.pop(key, None)
        self.entries[key] = body
        while len(self.entries) > self.cap:
            del self.entries[next(iter(self.entries))]
        self.writes += 1
        self._dirty = True

    def flush(self) -> None:
        """Persist (atomic, 0600) if anything was added or touched. Failure is recorded, not raised."""
        if not self._dirty:
            return
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=FILE_NAME + ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o600)
                json.dump({"schema": SCHEMA, "entries": list(self.entries.items())}, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
            tmp = None
            self._dirty = False
        except Exception as exc:  # noqa: BLE001
            failopen.record("LR-FO-PROXY-CHUNK-STORE", exc, detail="write")
        finally:
            if tmp:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def counts(self) -> dict:
        return {"store_hits": self.hits, "store_misses": self.misses, "store_writes": self.writes}
