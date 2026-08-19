"""Chapter sealer — freezes Working Memory into immutable Chapters.

Seal triggers (detected from harvested events, or called explicitly):
  commit / push / release-tag / loop-end

Invariants:
  * Chapters are immutable once written; a re-seal writes a new chapter file.
  * Chapter prose is distilled by the local librarian ($0 ollama) from raw
    events only — never from another summary.
  * provenance (event ids) is mandatory; an unsourced memory is a bug and
    the seal hard-fails (falls back to a mechanical, non-prose chapter).
  * All timestamps emitted by code; the librarian's output is prose only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from llm_router.library.store import LibraryStore, now_utc, scrub_secrets

def _validated_ollama_env_url(raw: str) -> str:
    """CHZ-SEC-06: never hand an unvalidated env URL to urlopen.

    Imported, not reimplemented — three earlier copies of this reader diverged
    and bypassed the fix. Fails CLOSED: an unavailable validator falls back to
    localhost rather than honouring an unchecked URL.
    """
    default = "http://localhost:11434"
    try:
        from llm_router.config import validate_ollama_url
    except Exception:
        return raw if raw == default else default
    return validate_ollama_url(raw) or default


LIBRARIAN_MODEL = os.environ.get("LLM_ROUTER_LIBRARIAN_MODEL", "qwen2.5-coder:7b")
OLLAMA_BASE_URL = _validated_ollama_env_url(
    os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
)

_GIT_SEAL_RE = re.compile(
    r"\bgit\b.*\b(commit|push|tag)\b|\bgh\s+release\b", re.IGNORECASE)


def is_seal_event(event: dict[str, Any]) -> str | None:
    """Return trigger name if this harvested event should seal a chapter."""
    if event.get("tool") != "Bash" or event.get("outcome") != "ok":
        return None
    m = _GIT_SEAL_RE.search(str(event.get("cmd") or event.get("desc", "")))
    if not m:
        return None
    return {"commit": "commit", "push": "push", "tag": "release"}.get(
        (m.group(1) or "release").lower(), "release")


def _head_sha(cwd: Path) -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                       cwd=str(cwd), capture_output=True, text=True, timeout=5)
    return r.stdout.strip() if r.returncode == 0 else ""


def _events_since_last_seal(store: LibraryStore, book: str) -> list[dict]:
    raw = store.root / "books" / book / "raw" / "events.jsonl"
    if not raw.exists():
        return []
    marker = store.root / "working-memory" / "last_seal.json"
    last_id = 0
    if marker.exists():
        try:
            last_id = int(json.loads(marker.read_text()).get("last_event_id", 0))
        except Exception:
            last_id = 0
    events = []
    for line in raw.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if int(e.get("id", 0)) > last_id:
            events.append(e)
    return events


def _librarian(prompt: str, timeout: int = 60) -> str | None:
    """Call the local $0 librarian. None on any failure (mechanical fallback)."""
    try:
        req = urllib.request.Request(
            f"{OLLAMA_BASE_URL}/api/generate",
            data=json.dumps({"model": LIBRARIAN_MODEL, "prompt": prompt,
                             "stream": False,
                             "options": {"temperature": 0.2, "num_predict": 700},
                             }).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()).get("response", "").strip() or None
    except Exception:
        return None


def _mechanical_prose(events: list[dict]) -> str:
    """Fallback chapter body when the librarian is unreachable: grouped facts,
    no interpretation. Boring but never wrong."""
    lines = ["(mechanical seal — librarian unavailable)"]
    for e in events[-30:]:
        files = ",".join(e.get("files", [])[:3])
        lines.append(f"- evt-{e['id']} {e['ts']} {e['tool']} {e['outcome']}"
                     + (f" [{files}]" if files else "")
                     + (f": {e['desc'][:80]}" if e.get("desc") else ""))
    return "\n".join(lines)


def _distill(events: list[dict], trigger: str) -> tuple[str, str, str]:
    """Returns (title, prose, distilled_by)."""
    facts = "\n".join(
        f"evt-{e['id']} | {e['ts']} | {e['tool']} | {e['outcome']}"
        f" | files={','.join(e.get('files', []))} | {e.get('desc', '')[:120]}"
        for e in events)
    prompt = (
        "You are a project librarian. Write a concise chapter (<=200 words) "
        "summarizing this work session slice for future reference. Every claim "
        "MUST cite event ids inline like (evt-3). Start with a single-line "
        f"title prefixed 'TITLE: '. Seal trigger: {trigger}.\n\nEVENTS:\n{facts}\n")
    out = _librarian(prompt)
    if out:
        cited = set(re.findall(r"evt-(\d+)", out))
        have = {str(e["id"]) for e in events}
        if cited and cited <= have:  # provenance must be real and non-empty
            m = re.match(r"\s*\**\s*TITLE:?\**\s*(.+)", out)
            title = (m.group(1).strip() if m else f"{trigger} seal")[:80]
            prose = out[m.end():].strip() if m else out
            return title, prose, LIBRARIAN_MODEL
    return f"{trigger} seal", _mechanical_prose(events), "mechanical"


def seal_chapter(store: LibraryStore, book: str, trigger: str,
                 cwd: Path | None = None) -> Path | None:
    """Seal everything since the last seal into a new immutable Chapter."""
    events = _events_since_last_seal(store, book)
    if not events:
        return None
    cwd = cwd or store.root.parent.parent
    sha = _head_sha(cwd)
    branch = events[-1].get("branch", "")
    n = len(store.list_chapters(book)) + 1
    title, prose, distilled_by = _distill(events, trigger)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or trigger
    rel = f"books/{book}/chapters/ch{n:02d}--{slug}.md"

    target = store.root / rel
    if target.exists():  # immutability guard — never overwrite a memory
        rel = f"books/{book}/chapters/ch{n:02d}b--{slug}.md"

    path = store.write_doc(rel, {
        "type": "chapter",
        "title": title,
        "book": book,
        "seal_trigger": trigger,
        "sealed_at": now_utc(),
        "sealed_at_sha": sha,
        "branch": branch,
        "covers": {"from": events[0]["ts"], "to": events[-1]["ts"]},
        "entities": sorted({f for e in events for f in e.get("files", [])})[:32],
        "provenance": [f"evt-{e['id']}" for e in events][:64],
        "distilled_by": distilled_by,
        "written_at": now_utc(),
    }, scrub_secrets(prose))

    (store.root / "working-memory" / "last_seal.json").write_text(json.dumps({
        "book": book, "chapter": path.name, "sealed_at": now_utc(),
        "sealed_at_sha": sha, "last_event_id": events[-1]["id"]}))
    store.regen_indexes()
    return path


def maybe_seal_on_event(store: LibraryStore, book: str, event: dict,
                        cwd: Path | None = None) -> Path | None:
    trigger = is_seal_event(event)
    if trigger is None:
        return None
    return seal_chapter(store, book, trigger, cwd=cwd)
