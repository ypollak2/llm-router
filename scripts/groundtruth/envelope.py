"""Everything needed to re-run a task later — by reference where possible.

The envelope is what turns "we saved the prompt" into "we can run this again".
It is captured at task time because that is the only time the state exists.

Two rules shape every field below:

* **Reference, do not duplicate.** A commit SHA is an immutable reference to a
  whole tree; copying the tree per task would be gigabytes for no added
  ability to reproduce. Files are recorded by content hash, and only the files
  the task actually names.
* **Capture only what reproduction needs.** Dependency lockfile hashes, yes —
  they decide whether a test can run. Full environment dumps, no.

`completeness()` is the load-bearing part. An envelope that is missing state
the task requires must say so, because the failure mode this whole phase
exists to prevent is a task marked eligible whose replay state was never
actually captured.

A dirty working tree is recorded, not rejected. Developers usually have one,
so rejecting them would reject most real coding traffic. The tree is made
reconstructable by storing the patch (scrubbed, and capped), not merely its
hash — a hash proves you have the right tree and cannot produce it.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from groundtruth.scrub import scrub  # noqa: E402

SCHEMA_VERSION = 1

# Files whose hash decides whether a test command can even run.
_LOCKFILES = ("uv.lock", "poetry.lock", "requirements.txt", "package-lock.json",
              "pnpm-lock.yaml", "yarn.lock", "Cargo.lock", "go.sum")


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=str(cwd) if cwd else None)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — envelope capture never breaks routing
        return ""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


@dataclass
class RepoState:
    """An immutable reference to a tree, plus what makes it not immutable."""

    root: str | None = None
    remote: str | None = None
    commit: str | None = None
    branch: str | None = None
    dirty: bool = False
    dirty_file_count: int = 0
    diff_sha256: str | None = None      # identity of the uncommitted diff
    diff: str | None = None             # the patch itself, when small enough
    diff_truncated: bool = False
    lockfile_hashes: dict[str, str] = field(default_factory=dict)
    file_hashes: dict[str, str] = field(default_factory=dict)  # only named files

    @property
    def reconstructable(self) -> bool:
        """Can this tree be rebuilt later, not merely recognised?

        A clean commit: check it out. A dirty tree: check out the commit and
        apply the stored patch — which requires the PATCH, not its hash. An
        earlier version kept only `diff_sha256`, which proves you have the
        right tree and cannot produce it, so every dirty-tree task was rejected
        as unreplayable. Developers usually have dirty trees, so that rejected
        most real coding traffic.

        A diff too large to store keeps its hash and stays non-reconstructable:
        the honest answer when the patch was not preserved.
        """
        if not self.commit:
            return False
        if not self.dirty:
            return True
        return bool(self.diff) and not self.diff_truncated


@dataclass
class ExternalEvidence:
    """Frozen external data with the provenance needed to trust it later."""

    source: str                      # URL, API endpoint, package name
    retrieved_at: float
    content_sha256: str
    content_ref: str | None = None   # where the frozen bytes live, if stored
    version: str | None = None       # commit, package version, ETag
    media_type: str | None = None
    note: str = ""


@dataclass
class ReplayEnvelope:
    schema_version: int = SCHEMA_VERSION
    route_id: str | None = None
    session_id: str | None = None
    prompt_sha256: str | None = None
    task_type: str | None = None
    captured_at: float = 0.0

    prompt: str | None = None        # scrubbed
    context_slice: str | None = None  # scrubbed; only what the task referenced

    repo: RepoState | None = None
    external: list[ExternalEvidence] = field(default_factory=list)

    tool_names: list[str] = field(default_factory=list)
    tool_inputs_sha256: str | None = None
    test_command: str | None = None
    build_command: str | None = None
    expected_schema: str | None = None

    model_config: dict = field(default_factory=dict)
    toolchain: dict = field(default_factory=dict)

    required_state: list[str] = field(default_factory=list)
    missing_state: list[str] = field(default_factory=list)

    def completeness(self) -> tuple[bool, list[str]]:
        """(complete, missing). The conservative half of the design.

        Each required state is checked against what was actually captured. A
        requirement with nothing behind it is reported, never assumed
        satisfiable later.
        """
        missing: list[str] = []
        for need in self.required_state:
            if need == "repo":
                if not (self.repo and self.repo.commit):
                    missing.append("repo-commit")
                elif self.repo.dirty and not self.repo.diff_sha256:
                    missing.append("repo-dirty-diff")
            elif need == "external-evidence":
                if not self.external:
                    missing.append("external-evidence")
            elif need == "tool":
                if not self.tool_names and not self.test_command:
                    missing.append("tool-or-test-command")
            elif need == "session":
                # Intentionally unsatisfiable. Capturing the transcript does
                # not make "continue what we were doing" well-defined, so this
                # requirement is never marked satisfied.
                missing.append("session-state-not-reconstructable")
            elif need == "machine":
                missing.append("machine-state-not-captured")
        if self.prompt is None:
            missing.append("prompt-text")
        self.missing_state = missing
        return (not missing), missing

    def to_json(self) -> dict:
        d = asdict(self)
        d["complete"] = self.completeness()[0]
        return d


MAX_DIFF_CHARS = 200_000


def capture_repo_state(cwd: Path | None = None, *,
                       named_files: list[str] | None = None,
                       max_diff_chars: int = MAX_DIFF_CHARS) -> RepoState | None:
    """Immutable reference first; the messy parts recorded, not hidden."""
    cwd = Path(cwd or os.getcwd())
    root = _run(["git", "rev-parse", "--show-toplevel"], cwd)
    if not root:
        return None
    rp = Path(root)
    status = _run(["git", "status", "--porcelain"], rp)
    dirty_files = [ln for ln in status.splitlines() if ln.strip()]
    state = RepoState(
        root=rp.name,                       # name only: the path is identity
        remote=_run(["git", "config", "--get", "remote.origin.url"], rp) or None,
        commit=_run(["git", "rev-parse", "HEAD"], rp) or None,
        branch=_run(["git", "rev-parse", "--abbrev-ref", "HEAD"], rp) or None,
        dirty=bool(dirty_files),
        dirty_file_count=len(dirty_files),
    )
    if state.dirty:
        diff = _run(["git", "diff", "HEAD"], rp, timeout=20)
        if diff:
            state.diff_sha256 = sha256_text(diff)
            # The patch is stored scrubbed, so a secret in an uncommitted file
            # does not reach the envelope. Capped: a huge diff usually means
            # the tree diverged so far that "replay" is not meaningful anyway.
            if len(diff) <= max_diff_chars:
                state.diff, _ = scrub(diff)
            else:
                state.diff_truncated = True
    for name in _LOCKFILES:
        p = rp / name
        if p.exists():
            try:
                state.lockfile_hashes[name] = hashlib.sha256(p.read_bytes()).hexdigest()[:32]
            except OSError:
                pass
    for rel in (named_files or []):
        p = rp / rel
        if p.exists() and p.is_file():
            try:
                state.file_hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()[:32]
            except OSError:
                pass
    return state


def freeze_external(source: str, content: str, *, version: str | None = None,
                    media_type: str | None = None,
                    store_dir: Path | None = None) -> ExternalEvidence:
    """Freeze external content with provenance, scrubbed before it is stored.

    The hash is over the SCRUBBED bytes, so it matches what was actually kept.
    A hash of the original would claim to verify something that was never
    written.
    """
    clean, _ = scrub(content)
    ev = ExternalEvidence(
        source=source,
        retrieved_at=time.time(),
        content_sha256=sha256_text(clean),
        version=version,
        media_type=media_type,
    )
    if store_dir:
        store_dir.mkdir(parents=True, exist_ok=True)
        path = store_dir / f"{ev.content_sha256[:32]}.txt"
        if not path.exists():
            path.write_text(clean, encoding="utf-8")
        ev.content_ref = f"external:{ev.content_sha256[:32]}"
    return ev


def toolchain_metadata() -> dict:
    """Only what decides whether a captured command can run again."""
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


def build(
    *,
    prompt: str,
    required_state: list[str],
    route_id: str | None = None,
    session_id: str | None = None,
    prompt_sha256: str | None = None,
    task_type: str | None = None,
    cwd: Path | None = None,
    named_files: list[str] | None = None,
    external: list[ExternalEvidence] | None = None,
    tool_names: list[str] | None = None,
    tool_inputs: dict | None = None,
    test_command: str | None = None,
    model_config: dict | None = None,
    context_slice: str | None = None,
) -> ReplayEnvelope:
    """Assemble an envelope. Repo state is only touched when the task needs it."""
    clean_prompt, _ = scrub(prompt)
    env = ReplayEnvelope(
        route_id=route_id,
        session_id=session_id,
        prompt_sha256=prompt_sha256,
        task_type=task_type,
        captured_at=time.time(),
        prompt=clean_prompt,
        required_state=list(required_state),
        external=list(external or []),
        tool_names=list(tool_names or []),
        test_command=test_command,
        model_config=dict(model_config or {}),
        toolchain=toolchain_metadata(),
    )
    if context_slice:
        env.context_slice, _ = scrub(context_slice)
    if "repo" in required_state:
        env.repo = capture_repo_state(cwd, named_files=named_files)
    if tool_inputs:
        env.tool_inputs_sha256 = sha256_text(
            json.dumps(tool_inputs, sort_keys=True, default=str))
    env.completeness()
    return env
