"""Run result: patch + verdict + `used` flag, and the private-file writers.

`used` is `True` only when the verifier (V1: a test run) passed. It is `None`
when no verifier ran (unknown), never `False`-by-default and never `0`: an
unmeasured run must not look like a failed one, nor a passed one.
"""
from __future__ import annotations

import difflib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from llm_router import paths
from llm_router.paths import private_opener

_NOISE_PARTS = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".hypothesis")
_MAX_DIFF_FILE_BYTES = 1_000_000


def runs_dir() -> Path:
    return paths.state_path("toolkit", "runs")


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def write_private_text(path: Path, text: str) -> None:
    """Create/replace a 0600 file. Failure RAISES: a patch that was not saved must
    not be reported as returned."""
    _ensure_private_dir(path.parent)
    with open(path, "w", encoding="utf-8", opener=private_opener) as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def append_private_jsonl(path: Path, row: dict) -> bool:
    """Append one JSON row at 0600. Returns False (and counts) on failure: audit
    logging must not crash a run, but it must not fail silently either."""
    global _dropped_rows
    try:
        _ensure_private_dir(path.parent)
        with open(path, "a", encoding="utf-8", opener=private_opener) as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        return True
    except OSError:
        _dropped_rows += 1
        return False


_dropped_rows = 0


def dropped_rows() -> int:
    return _dropped_rows


# ── patch ────────────────────────────────────────────────────────────────────


def _files(root: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _NOISE_PARTS]
        for name in filenames:
            p = Path(dirpath) / name
            if name.endswith(".pyc") or p.is_symlink():
                continue
            out[os.path.relpath(p, root)] = p
    return out


def _read(p: Path | None) -> str | None:
    if p is None:
        return ""
    try:
        if p.stat().st_size > _MAX_DIFF_FILE_BYTES:
            return None
        data = p.read_bytes()
        if b"\0" in data:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def make_patch(baseline: Path, workspace: Path) -> tuple[str, list[str]]:
    """A `git apply`-able unified diff of workspace vs baseline, and the list of
    changed paths. Binary/oversized changes are listed in the header comment."""
    a, b = _files(baseline), _files(workspace)
    chunks: list[str] = []
    changed: list[str] = []
    for rel in sorted(set(a) | set(b)):
        pa, pb = a.get(rel), b.get(rel)
        ta, tb = _read(pa), _read(pb)
        if ta is None or tb is None:
            if (pa is None) != (pb is None) or (pa and pb and pa.read_bytes() != pb.read_bytes()):
                changed.append(rel)
                chunks.append(f"# binary or oversized change not included: {rel}\n")
            continue
        if ta == tb and (pa is None) == (pb is None):
            continue
        changed.append(rel)
        head = f"diff --git a/{rel} b/{rel}\n"
        if pa is None:
            head += "new file mode 100644\n"
            fromfile = "/dev/null"
        else:
            fromfile = f"a/{rel}"
        if pb is None:
            head += "deleted file mode 100644\n"
            tofile = "/dev/null"
        else:
            tofile = f"b/{rel}"
        body = "".join(difflib.unified_diff(ta.splitlines(keepends=True), tb.splitlines(keepends=True),
                                            fromfile=fromfile, tofile=tofile, n=3))
        if body and not body.endswith("\n"):
            body += "\n\\ No newline at end of file\n"
        chunks.append(head + body)
    return "".join(chunks), changed


# ── result ───────────────────────────────────────────────────────────────────


@dataclass
class RunResult:
    status: str                         # done | blocked | budget | kill | refused | error
    summary: str = ""
    patch_path: str | None = None
    patch_text: str = ""
    changed_files: list[str] = field(default_factory=list)
    verify: dict | None = None          # None = no verifier ran (unknown)
    used: bool | None = None            # True only on a passing test run (V1)
    verified_by: str | None = None      # "V1" or None
    steps: int = 0
    elapsed_s: float = 0.0
    tokens_in: int | None = None
    tokens_out: int | None = None
    stop_reason: str = ""
    denials: int = 0
    bash_enabled: bool = False
    bash_reason: str = ""
    run_id: str = ""
    model: str = ""
    workspace: str = ""
    ledger_path: str | None = None
    source_modified: bool | None = None   # the caller's tree changed during the run (must stay False)
    safety_flags: list[str] = field(default_factory=list)
    tool_calls: int = 0

    def verdict_line(self) -> str:
        if self.used is True:
            return "VERIFIED: the supplied test run passed (used=true, V1)"
        v = self.verify
        if v is None:
            return "UNVERIFIED: no verifier ran (used=unknown)"
        return f"NOT USED: {v.get('reason', 'verify failed')}"

    def to_json(self) -> str:
        d = asdict(self)
        d["patch_text"] = f"<{len(self.patch_text)} chars>"
        return json.dumps(d, indent=2, default=str)


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
