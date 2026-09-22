"""Library store — OKF document read/write for .llm_router/context/.

All timestamps are UTC ISO-8601 and emitted by *this code*, never by a
model — a hallucinating librarian cannot backdate a memory.

Frontmatter contract (superset of okf.py's parser, so every Library doc
is a valid OKF concept):
  written_at       when the prose in the doc was generated
  sealed_at        (chapters) the git event that triggered the seal
  covers           (chapters) {from, to} span of harvested events
  covers_since     (working memory) everything after last seal
  last_updated     (biography) last Book-close merge
  source_sealed_at (abridgements) inherited from source Chapter
"""

from __future__ import annotations

import json
import os
import stat
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Location + time
# ---------------------------------------------------------------------------

CONTEXT_DIRNAME = ".llm_router/context"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_root(cwd: Path | None = None) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd or Path.cwd()), capture_output=True, text=True, timeout=5,
        )
        return Path(out.stdout.strip()) if out.returncode == 0 else None
    except Exception:
        return None


def relative_age(iso_ts: str, now: datetime | None = None) -> str:
    """'4m ago' / '2h ago' / '3d ago' / '5w ago' — for index.md front desks."""
    try:
        then = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "?"
    delta = (now or datetime.now(timezone.utc)) - then
    s = int(delta.total_seconds())
    if s < 0:
        return "future?"  # clock skew — surface it, don't hide it
    if s < 3600:
        return f"{max(s // 60, 0)}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    if s < 7 * 86400:
        return f"{s // 86400}d ago"
    return f"{s // (7 * 86400)}w ago"


# ---------------------------------------------------------------------------
# Secrets scrub — at write time, before anything touches disk
# ---------------------------------------------------------------------------

def scrub_secrets(text: str) -> str:
    """Delegate to the canonical scrubber. Fails CLOSED.

    T-04 / S-01. This used to carry its own six-pattern list, and the 2026-09-22
    audit measured what that cost: it missed **Slack tokens, JWTs and Google API
    keys** that `secret_scrubber` covers, and it never called the canonical
    scrubber at all.

    That mattered more here than at most call sites. `library-harvest` runs this
    on `tool_input["command"]` for every Bash and Edit call, and `pack.py` later
    re-injects the stored text verbatim as `additionalContext` into a future
    prompt — so an unscrubbed secret does not merely rest on disk, it is replayed
    to whichever model answers next.

    The previous fix round made `secret_scrubber` a genuine superset of every
    rival table and wrote a test asserting exactly that. The test never called
    THIS function, so a live caller kept using a weaker copy for another day.
    Hence the rule this round: the gate asserts the call site, not the definition.

    Fails closed: if the canonical scrubber cannot be imported or raises, the
    body is withheld rather than written raw. A memory is not worth a credential.
    """
    if not text:
        return text
    try:
        from llm_router.secret_scrubber import scrub_text

        return scrub_text(text)
    except Exception:  # noqa: BLE001 — see the docstring; never write raw
        return "[SCRUB-FAILED: content withheld]"


# ---------------------------------------------------------------------------
# Frontmatter emit/parse
# ---------------------------------------------------------------------------

def _yaml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if re.search(r"[:#{}\[\],&*?|>'\"%@`]", s) or s != s.strip():
        return json.dumps(s)
    return s


def emit_frontmatter(meta: dict[str, Any]) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(_yaml_scalar(i) for i in v)}]")
        elif isinstance(v, dict):
            inner = ", ".join(f"{ik}: {_yaml_scalar(iv)}" for ik, iv in v.items())
            lines.append(f"{k}: {{{inner}}}")
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    lines.append("---")
    return "\n".join(lines)


_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _coerce(v: str) -> Any:
    """Un-stringify scalars emitted by emit_frontmatter (int/float/bool)."""
    if v.startswith('"') and v.endswith('"') and len(v) >= 2:
        return v[1:-1]
    if v in ("true", "True"):
        return True
    if v in ("false", "False"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def parse_doc(text: str) -> tuple[dict[str, Any], str]:
    """Minimal YAML-subset parser matching emit_frontmatter's output."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, Any] = {}
    for raw in m.group(1).splitlines():
        if ":" not in raw:
            continue
        k, _, v = raw.partition(":")
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            items = [_coerce(i.strip()) for i in v[1:-1].split(",") if i.strip()]
            meta[k.strip()] = items
        elif v.startswith("{") and v.endswith("}"):
            d: dict[str, Any] = {}
            for pair in v[1:-1].split(","):
                if ":" in pair:
                    ik, _, iv = pair.partition(":")
                    d[ik.strip()] = _coerce(iv.strip())
            meta[k.strip()] = d
        else:
            meta[k.strip()] = _coerce(v)
    return meta, text[m.end():]


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

@dataclass
class LibraryDoc:
    path: Path
    meta: dict[str, Any]
    body: str

    @property
    def type(self) -> str:
        return str(self.meta.get("type", ""))


@dataclass
class LibraryStore:
    root: Path  # .../<repo>/.llm-router/context

    # -- construction -------------------------------------------------------
    @classmethod
    def for_repo(cls, cwd: Path | None = None) -> "LibraryStore | None":
        top = repo_root(cwd)
        if top is None:
            return None
        return cls(root=top / CONTEXT_DIRNAME)

    def ensure_layout(self) -> None:
        for sub in ("biography", "biography/decisions", "books",
                    "working-memory", "abridgements"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        gi = self.root.parent / ".gitignore"  # .llm_router/.gitignore
        marker = "context/"
        if not gi.exists() or marker not in gi.read_text():
            with gi.open("a") as f:
                f.write(f"{marker}\n")

    # -- doc IO --------------------------------------------------------------
    def write_doc(self, rel: str, meta: dict[str, Any], body: str) -> Path:
        meta = dict(meta)
        meta.setdefault("written_at", now_utc())
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        text = emit_frontmatter(meta) + "\n\n" + scrub_secrets(body).strip() + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        # T-04 / S-01: created 0600, not created-then-tightened. These documents
        # hold scrubbed conversation content, and `write_text` makes the file with
        # the umask default (0644 here), so it was world-readable for the whole
        # write and stayed that way — permissions are checked at open time, so a
        # later chmod does not close a handle already taken.
        try:
            from llm_router.paths import private_opener

            with open(tmp, "w", encoding="utf-8", opener=private_opener) as fh:
                fh.write(text)
        except Exception:  # noqa: BLE001 — a hook must not fail over a mode
            tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)  # atomic — a crashed writer never leaves half a memory
        # repair a file an older version created at 0644
        try:
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    def read_doc(self, rel: str) -> LibraryDoc | None:
        path = self.root / rel
        if not path.exists():
            return None
        meta, body = parse_doc(path.read_text(encoding="utf-8"))
        return LibraryDoc(path=path, meta=meta, body=body)

    def list_books(self) -> list[str]:
        books = self.root / "books"
        if not books.exists():
            return []
        return sorted((p.name for p in books.iterdir() if p.is_dir()), reverse=True)

    def list_chapters(self, book: str) -> list[LibraryDoc]:
        out: list[LibraryDoc] = []
        chdir = self.root / "books" / book / "chapters"
        if chdir.exists():
            for p in sorted(chdir.glob("ch*.md")):
                meta, body = parse_doc(p.read_text(encoding="utf-8"))
                out.append(LibraryDoc(path=p, meta=meta, body=body))
        return out

    # -- front desks ---------------------------------------------------------
    def regen_indexes(self) -> None:
        """Auto-generate index.md at root, books/, and each book (progressive
        disclosure: one level per hop, with relative ages)."""
        self._regen_root_index()
        self._regen_shelf_index()
        for book in self.list_books():
            self._regen_book_index(book)

    def _line(self, title: str, rel: str, ts: str, extra: str = "") -> str:
        age = relative_age(ts) if ts else "?"
        suffix = f" — {extra}" if extra else ""
        return f"- [{title}]({rel}) ({age}){suffix}"

    def _regen_root_index(self) -> None:
        bio = self.read_doc("biography/biography.md")
        lines = ["# Library — front desk", ""]
        if bio:
            ts = str(bio.meta.get("last_updated") or bio.meta.get("written_at", ""))
            lines.append(self._line("Biography", "biography/biography.md", ts))
        lines.append(self._line("Bookshelf", "books/index.md",
                                now_utc(), f"{len(self.list_books())} books"))
        wm = self.read_doc("working-memory/delta.md")
        if wm:
            lines.append(self._line("Working Memory", "working-memory/delta.md",
                                    str(wm.meta.get("written_at", ""))))
        self._write_index("index.md", lines)

    def _regen_shelf_index(self) -> None:
        lines = ["# Bookshelf — newest first", ""]
        for book in self.list_books():
            doc = self.read_doc(f"books/{book}/book.md")
            status = "closed" if doc else "open"
            ts = str(doc.meta.get("written_at", "")) if doc else ""
            n = len(self.list_chapters(book))
            lines.append(self._line(book, f"{book}/index.md", ts,
                                    f"{status}, {n} chapters"))
        self._write_index("books/index.md", lines)

    def _regen_book_index(self, book: str) -> None:
        lines = [f"# Book: {book}", ""]
        doc = self.read_doc(f"books/{book}/book.md")
        if doc:
            lines.append(self._line("Closing summary", "book.md",
                                    str(doc.meta.get("written_at", ""))))
        for ch in self.list_chapters(book):
            title = ch.meta.get("title", ch.path.stem)
            sha = str(ch.meta.get("sealed_at_sha", ""))[:7]
            lines.append(self._line(str(title), f"chapters/{ch.path.name}",
                                    str(ch.meta.get("sealed_at", "")), sha))
        self._write_index(f"books/{book}/index.md", lines)

    def _write_index(self, rel: str, lines: list[str]) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
