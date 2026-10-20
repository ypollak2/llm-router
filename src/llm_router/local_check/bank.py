"""Item bank for ``llm-router local-check`` (LC-1).

The bank is public text (diffs and commit/PR messages from allow-listed open-source
repos) shipped as ``data/bank-v1.jsonl.gz`` next to ``data/manifest.json``. Every load
re-hashes the gz bytes and refuses on a mismatch with ``manifest["bank_sha256"]``:
a bank that does not match its manifest is never partly used.

Records are ``{"type": "item" | "plant", ...}``. Items are the tasks a local model is
asked to do (``job`` = commit_message | pr_description). Plants are judge-validity
cases (good / swapped / mutated message texts) drawn from a pool disjoint from every
item. ``Bank.draw(1)`` and ``Bank.draw(2)`` are the two disjoint 70-item draws,
35 per job, ordered by a hash seeded with the bank sha so they are reproducible.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
MANIFEST_NAME = "manifest.json"
JOBS = ("commit_message", "pr_description")
PLANT_KINDS = ("good", "swapped", "mutated")
ALLOWED_LICENCES = frozenset({"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "Unlicense"})
DRAW_PER_JOB = 35          # 35 + 35 = 70 items per draw
DRAWS = 2


class BankError(Exception):
    """The bank or its manifest is missing, malformed or inconsistent."""


class BankIntegrityError(BankError):
    """The bank bytes do not hash to the sha256 recorded in the manifest."""


def licence_allowed(expr: str) -> bool:
    """SPDX OR-expression is usable when at least one alternative is on the allow-list."""
    return any(p.strip() in ALLOWED_LICENCES for p in expr.split(" OR "))


@dataclass(frozen=True)
class Item:
    id: str
    job: str
    repo: str
    sha: str
    licence: str
    url: str
    date: str
    task: str            # the complete prompt shown to the model (includes the diff)
    diff: str
    files: tuple[str, ...]
    size: str            # strata: small | medium | large
    lang: str            # strata: py | other

    @property
    def stratum(self) -> str:
        return f"{self.size}/{self.lang}"


@dataclass(frozen=True)
class Plant:
    id: str
    kind: str            # good | swapped | mutated
    job: str
    repo: str
    sha: str
    licence: str
    url: str
    task: str
    diff: str
    files: tuple[str, ...]
    text: str            # the candidate message the judge must accept (good) or reject
    source: dict | None  # swapped: repo/sha/licence/url of the message's real commit


_ITEM_KEYS = ("id", "job", "repo", "sha", "licence", "url", "date", "task", "diff", "files", "strata")
_PLANT_KEYS = ("id", "kind", "job", "repo", "sha", "licence", "url", "task", "diff", "files", "text")


def _need(rec: dict, keys: tuple[str, ...]) -> None:
    missing = [k for k in keys if k not in rec]
    if missing:
        raise BankError(f"record {rec.get('id', '?')} is missing {missing}")


def _item(rec: dict) -> Item:
    _need(rec, _ITEM_KEYS)
    if rec["job"] not in JOBS:
        raise BankError(f"item {rec['id']}: unknown job {rec['job']!r}")
    st = rec["strata"]
    return Item(rec["id"], rec["job"], rec["repo"], rec["sha"], rec["licence"], rec["url"], rec["date"],
                rec["task"], rec["diff"], tuple(rec["files"]), st["size"], st["lang"])


def _plant(rec: dict) -> Plant:
    _need(rec, _PLANT_KEYS)
    if rec["kind"] not in PLANT_KINDS or rec["job"] not in JOBS:
        raise BankError(f"plant {rec['id']}: bad kind/job {rec['kind']!r}/{rec['job']!r}")
    if rec["kind"] == "swapped" and "source" not in rec:
        raise BankError(f"plant {rec['id']}: swapped plant has no source")
    return Plant(rec["id"], rec["kind"], rec["job"], rec["repo"], rec["sha"], rec["licence"], rec["url"],
                 rec["task"], rec["diff"], tuple(rec["files"]), rec["text"], rec.get("source"))


class Bank:
    def __init__(self, manifest: dict, items: tuple[Item, ...], plants: tuple[Plant, ...]):
        self.manifest = manifest
        self.sha256: str = manifest["bank_sha256"]
        self.version: int = manifest["bank_version"]
        self._items = items
        self._plants = plants

    # ---- items
    def items(self, job: str | None = None, stratum: str | None = None) -> list[Item]:
        """Items filtered by job and/or stratum ("small/py")."""
        return [i for i in self._items if (job is None or i.job == job) and (stratum is None or i.stratum == stratum)]

    def strata(self, job: str | None = None) -> dict[str, int]:
        out: dict[str, int] = {}
        for i in self.items(job):
            out[i.stratum] = out.get(i.stratum, 0) + 1
        return dict(sorted(out.items()))

    def draw(self, n: int) -> list[Item]:
        """Draw ``n`` (1 or 2): 35 items per job, disjoint from the other draw, same every time for this bank."""
        if n not in range(1, DRAWS + 1):
            raise ValueError(f"draw must be 1..{DRAWS}, got {n}")
        out: list[Item] = []
        for job in JOBS:
            ordered = sorted(self.items(job), key=lambda i: hashlib.sha256(f"{self.sha256}|{job}|{i.id}".encode()).hexdigest())
            if len(ordered) < DRAW_PER_JOB * DRAWS:
                raise BankError(f"bank has {len(ordered)} {job} items, need {DRAW_PER_JOB * DRAWS}")
            out.extend(ordered[(n - 1) * DRAW_PER_JOB:n * DRAW_PER_JOB])
        return out

    # ---- plants
    def plants(self, kind: str | None = None, job: str | None = None) -> list[Plant]:
        return [p for p in self._plants if (kind is None or p.kind == kind) and (job is None or p.job == job)]


def load_bank(data_dir: Path | str | None = None) -> Bank:
    """Load and verify the bank. Re-hashes on every call; raises BankIntegrityError on mismatch."""
    d = Path(data_dir) if data_dir is not None else DATA_DIR
    try:
        manifest = json.loads((d / MANIFEST_NAME).read_text())
        gz_path = d / manifest["bank_file"]
        raw = gz_path.read_bytes()
    except (OSError, KeyError, ValueError) as e:
        raise BankError(f"cannot read bank in {d}: {e}") from e
    actual = hashlib.sha256(raw).hexdigest()
    if actual != manifest.get("bank_sha256"):
        raise BankIntegrityError(f"{gz_path.name}: sha256 {actual} != manifest {manifest.get('bank_sha256')}")
    try:
        recs = [json.loads(x) for x in gzip.decompress(raw).decode().splitlines() if x]
    except (OSError, ValueError, EOFError) as e:
        raise BankError(f"{gz_path.name} is not valid gzip JSONL: {e}") from e
    items = tuple(_item(r) for r in recs if r.get("type") == "item")
    plants = tuple(_plant(r) for r in recs if r.get("type") == "plant")
    if len(items) + len(plants) != len(recs):
        raise BankError("bank has records that are neither item nor plant")
    for r in (*items, *plants):
        if not licence_allowed(r.licence):
            raise BankError(f"{r.id}: licence {r.licence!r} is not on the allow-list")
    counts = manifest.get("counts", {})
    if counts.get("items") != len(items) or counts.get("plants") != len(plants):
        raise BankError("manifest counts do not match the bank")
    return Bank(manifest, items, plants)
