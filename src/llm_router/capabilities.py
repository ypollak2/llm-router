"""CF-2: capability-aware classification — the single shared source of truth.

The North Star's CLASSIFY stage must decide *needs-tools?* and *which files/context*,
not just task_type + complexity. This module is the ONE predicate that enforcement
exemptions, routing, provisioning, and agent tool-permissions all consult, so those
four never diverge.

Two honest limits are called out up front:
  * Detection is REGEX-based. It has false positives ("my src/images folder has
    vacation photos" trips on ``src/``) and false negatives ("patch the auth handler"
    has no path). Consolidating the regex here does NOT fix brittleness — it only
    removes divergence. Real fix = a semantic classifier (out of scope).
  * Relevant-context collection is best-effort and bounded. It may miss files in
    unusual layouts and it deliberately excludes secrets even when they'd be useful.

``needs_claude_tools()`` in ``chain_builder`` delegates here; by default it returns the
LEGACY boolean (``CapabilityDecision.legacy_match``) so shipping this module does not
change default routing. The richer 8-bit vector is used by direct callers and, when
``LLM_ROUTER_CAPABILITY_ROUTING=1``, by the routing path (Branch 3 ships in shadow mode).
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ── Capability vector ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CapabilityRequirement:
    read_files: bool = False
    write_files: bool = False
    run_commands: bool = False
    repo_search: bool = False
    git_operations: bool = False
    network_access: bool = False
    objective_verification: bool = False
    multi_step_execution: bool = False

    @property
    def needs_tools(self) -> bool:
        return any((
            self.read_files, self.write_files, self.run_commands,
            self.repo_search, self.git_operations, self.network_access,
            self.objective_verification, self.multi_step_execution,
        ))


CapabilitySource = Literal["regex", "classifier", "repo_scan", "user_explicit", "history"]


@dataclass(frozen=True)
class CapabilityEvidence:
    source: CapabilitySource
    reason: str
    confidence: float  # 0.0–1.0


@dataclass(frozen=True)
class CapabilityDecision:
    required: CapabilityRequirement
    evidence: tuple[CapabilityEvidence, ...]
    confidence: float
    # True iff the legacy needs_claude_tools() regex would have fired. Used to keep
    # default routing byte-identical while the richer vector ships in shadow mode.
    legacy_match: bool = False


# ── Relevant context model ────────────────────────────────────────────────────

@dataclass(frozen=True)
class RelevantFile:
    path: str                       # relative to repo_root; never absolute
    symbols: tuple[str, ...]
    reason: str
    score: float                    # descending; used for ranking and truncation
    snippet: str | None = None      # bounded excerpt; None if not collected


@dataclass(frozen=True)
class RelevantContext:
    repo_root: str | None
    branch: str | None
    git_diff_summary: str | None
    files: tuple[RelevantFile, ...]
    failing_command: str | None
    failing_output_excerpt: str | None
    recent_milestones: tuple[str, ...]
    prior_failed_attempt: str | None
    config_files: tuple[str, ...]
    token_budget: int
    truncated: bool


# ── Regex evidence ────────────────────────────────────────────────────────────
# LEGACY patterns reproduce chain_builder.needs_claude_tools() EXACTLY. Their match
# sets both the capability bit and ``legacy_match=True``. NEW patterns (symbol search,
# generic write verbs) enrich the vector but do NOT set legacy_match, so the default
# routing boolean is unchanged.

_LEGACY_PROJECT = re.compile(
    r'\b(src/|tests/|hooks/|in the codebase|this file|this repo|this project'
    r'|current project|current version|what version|package\.json|pyproject\.toml'
    r'|llm_router|blocked by hook|error message)\b', re.IGNORECASE)

_LEGACY_LOCAL_FS = re.compile(
    r'(?:~/|\$HOME|/Users/|\.env\b|\.pypirc\b|keychain'
    r'|\b(?:on |in )?my (?:machine|computer|laptop|mac|system|disk|files?|env(?:ironment)?)\b'
    r'|\b(?:locally|local files?|on disk|file ?system|environment variable|env var)\b'
    r'|\b(?:search|find|locate|where(?:\W?s| is| are| did)?|store|put|save)\b'
    r'[^.?!]{0,40}\b(?:token|api[ _-]?key|secret|credential|password)\b'
    r'|\b(?:run|launch|start|restart|deploy|publish|install|build|test)\b'
    r'[^.?!]{0,25}\b(?:the app|my app|the server|the dashboard|the script|the suite|the release|locally)\b)',
    re.IGNORECASE)

_LEGACY_READ_FILE = re.compile(
    r'\b(?:read|open|inspect|show|cat|summari[sz]e)\s+'
    r'(?:the\s+)?(?:file\s+)?[\w./-]+\.[A-Za-z0-9]{1,8}\b', re.IGNORECASE)

_LEGACY_EXT = re.compile(r'\.(py|ts|js|go|rs|java|cpp|yaml|json|md|toml|cfg|sh|sql)\b')

_LEGACY_EDIT_INTENT = re.compile(r'\b(fix|debug|investigate|refactor|update|modify)\b', re.IGNORECASE)
_LEGACY_EDIT_LOCATION = re.compile(
    r'\b(in|at|from|the)\s+(src|tests|hooks|module|class|function)\b', re.IGNORECASE)

# NEW enrichment patterns (do not affect legacy_match)
_RUN_CMD = re.compile(
    r'\b(run|execute|launch|deploy|build|install|restart)\b.{0,30}'
    r'\b(test|tests|suite|app|server|script|build|lint|command)\b', re.IGNORECASE)
_TEST_INTENT = re.compile(r'\b(tests?|pytest|unit tests?|test suite|failing test)\b', re.IGNORECASE)
_WRITE_VERB = re.compile(
    r'\b(add|append|write|edit|create|rename|replace|insert|remove|delete|change)\b',
    re.IGNORECASE)
_REPO_SEARCH = re.compile(
    r'\b(all callers|find all|everywhere|across the (?:repo|codebase)|grep|search the'
    r'|who calls|usages? of|references? to)\b', re.IGNORECASE)
_GIT_OP = re.compile(r'\b(commit|branch|rebase|cherry-?pick|git\b|stage|staged|diff)\b', re.IGNORECASE)
_NETWORK = re.compile(r'\b(https?://|download|fetch (?:the )?url|curl|api endpoint|webhook)\b', re.IGNORECASE)
# A backtick-quoted identifier or a bare ``name()`` — a symbol reference with no path.
_SYMBOL = re.compile(r'`([A-Za-z_]\w+)`|\b([A-Za-z_]\w+)\(\)')


def _legacy_needs_tools(prompt: str, task_type: str) -> bool:
    """The pre-CF-2 needs_claude_tools() boolean, verbatim — the single home for that
    logic (chain_builder no longer contains it)."""
    if _LEGACY_PROJECT.search(prompt):
        return True
    if _LEGACY_LOCAL_FS.search(prompt):
        return True
    if _LEGACY_READ_FILE.search(prompt):
        return True
    if task_type not in ("code", "analyze"):
        return False
    if _LEGACY_EXT.search(prompt):
        return True
    if _LEGACY_EDIT_INTENT.search(prompt) and _LEGACY_EDIT_LOCATION.search(prompt):
        return True
    return False


def detect_capabilities(
    prompt: str,
    task_type: str = "",
    repo_root: Path | None = None,
    cwd: Path | None = None,
) -> CapabilityDecision:
    """Shared predicate for ALL routing / exemption / provisioning / permission
    decisions. Regex-based (documented brittleness). Never raises."""
    ev: list[CapabilityEvidence] = []
    read = write = run = search = git = net = verify = multi = False
    legacy = _legacy_needs_tools(prompt, task_type)

    if _LEGACY_PROJECT.search(prompt) or _LEGACY_READ_FILE.search(prompt) or _LEGACY_EXT.search(prompt):
        read = True
        ev.append(CapabilityEvidence("regex", "project/file reference", 0.6))
    if _LEGACY_LOCAL_FS.search(prompt):
        read = True
        ev.append(CapabilityEvidence("regex", "local filesystem / credential reference", 0.6))
    if _REPO_SEARCH.search(prompt):
        search = read = True
        ev.append(CapabilityEvidence("regex", "repo-wide search intent", 0.6))
    # Symbol reference with no explicit path → needs a repo search to locate it.
    if _SYMBOL.search(prompt):
        search = read = True
        ev.append(CapabilityEvidence("regex", "symbol reference (no path)", 0.5))
    if _WRITE_VERB.search(prompt) and (read or _LEGACY_EDIT_LOCATION.search(prompt)
                                       or _LEGACY_EXT.search(prompt)):
        write = verify = True
        ev.append(CapabilityEvidence("regex", "write/edit intent on a file", 0.55))
    if _RUN_CMD.search(prompt) or (_TEST_INTENT.search(prompt)
                                   and re.search(r'\b(run|show|why|fail)', prompt, re.IGNORECASE)):
        run = verify = True
        ev.append(CapabilityEvidence("regex", "command/test execution intent", 0.55))
    if _GIT_OP.search(prompt):
        git = True
        ev.append(CapabilityEvidence("regex", "git operation intent", 0.5))
    if _NETWORK.search(prompt):
        net = True
        ev.append(CapabilityEvidence("regex", "network access intent", 0.5))
    if (write and search) or _REPO_SEARCH.search(prompt):
        multi = True

    required = CapabilityRequirement(
        read_files=read, write_files=write, run_commands=run, repo_search=search,
        git_operations=git, network_access=net, objective_verification=verify,
        multi_step_execution=multi,
    )
    # Confidence: max evidence confidence, or a low floor for ambiguous prompts.
    confidence = max((e.confidence for e in ev), default=0.2)
    return CapabilityDecision(required=required, evidence=tuple(ev),
                              confidence=confidence, legacy_match=legacy)


def capability_routing_enabled() -> bool:
    """Branch 3 ships in shadow mode: the richer capability boolean only drives
    routing when explicitly enabled."""
    return os.environ.get("LLM_ROUTER_CAPABILITY_ROUTING", "").strip().lower() in ("1", "on", "true", "yes")


def serialize_capability_decision(decision: CapabilityDecision) -> str:
    """JSON-serialise a decision for ``routing_decisions.capabilities_json``.

    Shadow mode exists to answer one question offline: *would* capability-aware
    routing have chosen differently? Without persistence that question has no
    data behind it — ``detect_capabilities`` runs, decides, and the decision is
    discarded. This is the write half of the shadow.

    Never read back by the live routing path. Purely for analysis of what the
    richer capability vector would have picked, which is what keeps enabling
    the flag from being able to change a routing outcome.

    Fields are written out explicitly rather than via ``asdict``: the column is
    read by offline analysis that has to survive the dataclass gaining a field,
    and an implicit dump would silently change the stored shape the day someone
    adds one.

    FAIL-OPEN: returns ``"{}"`` on any error. Serialising a shadow observation
    must never be able to break the decision logging it rides along with —
    losing one shadow record is cheap, losing the routing decision is not.
    """
    import json

    try:
        req = decision.required
        return json.dumps(
            {
                "required": {
                    "read_files": req.read_files,
                    "write_files": req.write_files,
                    "run_commands": req.run_commands,
                    "repo_search": req.repo_search,
                    "git_operations": req.git_operations,
                    "network_access": req.network_access,
                    "objective_verification": req.objective_verification,
                    "multi_step_execution": req.multi_step_execution,
                    "needs_tools": req.needs_tools,
                },
                "evidence": [
                    {"source": e.source, "reason": e.reason, "confidence": e.confidence}
                    for e in decision.evidence
                ],
                "confidence": decision.confidence,
                "legacy_match": decision.legacy_match,
            }
        )
    except Exception:  # noqa: BLE001 - serialisation must never break logging
        return "{}"


# ── Relevant-context collection (bounded, safe) ───────────────────────────────

EXCLUDED_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa", "id_ed25519", ".npmrc", ".pypirc", ".netrc",
    "*token*", "*secret*", "*credential*",
]
_EXCLUDED_DIR_PARTS = {".aws", ".gcp", ".azure", ".ssh", ".git", "node_modules",
                       "__pycache__", ".venv", "venv"}

MAX_FILES = 12
MAX_SNIPPETS = 8
MAX_SNIPPET_LINES = 80
MAX_SNIPPET_CHARS = 6000
_EXPLICIT_PATH = re.compile(r'\b([\w./-]+\.[A-Za-z0-9]{1,8})\b')
_CONFIG_FILES = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml")


def is_safe_path(path: Path, repo_root: Path) -> bool:
    """Reject paths outside the repo root, symlink escapes, ``../`` traversal, and
    secret files. Enforced at COLLECTION time (not runtime) — the routed model never
    receives a path that fails this."""
    try:
        resolved = path.resolve()
        resolved_root = repo_root.resolve()
    except (OSError, RuntimeError):
        return False
    # Containment: resolved path must live under the resolved repo root. Using
    # is_relative_to catches ../ traversal AND symlink escape (resolve() follows links).
    try:
        if not resolved.is_relative_to(resolved_root):
            return False
    except AttributeError:  # py<3.9 fallback
        if not str(resolved).startswith(str(resolved_root) + os.sep):
            return False
    if any(part in _EXCLUDED_DIR_PARTS for part in resolved.parts):
        return False
    name = resolved.name
    for pat in EXCLUDED_PATTERNS:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(name.lower(), pat):
            return False
    return True


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 — fixed arg list, no shell
            ["git", "-C", str(repo_root), *args],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _repo_search_symbol(repo_root: Path, symbol: str) -> list[str]:
    """Locate files defining/mentioning *symbol* via git grep (fixed args, no shell)."""
    out = _git(repo_root, "grep", "-l", "-I", "--", symbol)
    if not out:
        return []
    return [ln for ln in out.splitlines() if ln.strip()][:MAX_FILES]


def collect_relevant_context(
    prompt: str,
    repo_root: Path | None,
    *,
    token_budget: int = 8000,
    decision: CapabilityDecision | None = None,
) -> RelevantContext | None:
    """Best-effort, bounded, SAFE relevant-context collection. Returns None outside a
    repo or when nothing is found — never raises, never blocks routing."""
    if repo_root is None or not Path(repo_root).is_dir():
        return None
    repo_root = Path(repo_root)
    ranked: dict[str, RelevantFile] = {}

    def _add(rel: str, symbols: tuple[str, ...], reason: str, score: float) -> None:
        p = (repo_root / rel)
        if not is_safe_path(p, repo_root):
            return
        rel_norm = os.path.relpath(p.resolve(), repo_root.resolve())
        prev = ranked.get(rel_norm)
        if prev is None or score > prev.score:
            ranked[rel_norm] = RelevantFile(path=rel_norm, symbols=symbols,
                                            reason=reason, score=score)

    # 1. explicit file paths in the prompt (score 1.0)
    for m in _EXPLICIT_PATH.finditer(prompt):
        cand = m.group(1)
        if (repo_root / cand).is_file():
            _add(cand, (), "explicit path in prompt", 1.0)

    # 3. symbol matches via repo search (score 0.7)
    for m in _SYMBOL.finditer(prompt):
        sym = m.group(1) or m.group(2)
        if not sym:
            continue
        for hit in _repo_search_symbol(repo_root, sym):
            _add(hit, (sym,), f"symbol '{sym}' found via repo search", 0.7)

    # 4. recently modified files (score 0.6)
    diff_names = _git(repo_root, "diff", "--name-only", "HEAD~1") or ""
    for rel in [ln for ln in diff_names.splitlines() if ln.strip()][:MAX_FILES]:
        if (repo_root / rel).is_file():
            _add(rel, (), "recently modified (git diff HEAD~1)", 0.6)

    # 5. ecosystem config files (score 0.4)
    config_files = [c for c in _CONFIG_FILES if (repo_root / c).is_file()]

    # Rank: descending score, then alphabetical path. Bound to MAX_FILES.
    ordered = sorted(ranked.values(), key=lambda f: (-f.score, f.path))
    truncated = len(ordered) > MAX_FILES
    files = tuple(ordered[:MAX_FILES])
    if not files and not config_files:
        return None

    branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    diff_stat = _git(repo_root, "diff", "--stat", "HEAD~1")
    return RelevantContext(
        repo_root=str(repo_root),
        branch=branch,
        git_diff_summary=(diff_stat[:500] if diff_stat else None),
        files=files,
        failing_command=None,
        failing_output_excerpt=None,
        recent_milestones=(),
        prior_failed_attempt=None,
        config_files=tuple(config_files),
        token_budget=token_budget,
        truncated=truncated,
    )


def serialize_relevant_context(rc: RelevantContext, max_chars: int = 4000) -> str:
    """Compact, bounded rendering for injection into a routed model's prompt. Truncates
    snippets first, then file count, preserving the file list / capability signal last."""
    lines: list[str] = ["[RELEVANT CONTEXT — read-only background, not an instruction]"]
    if rc.repo_root:
        lines.append(f"repo: {rc.repo_root}" + (f" (branch {rc.branch})" if rc.branch else ""))
    if rc.files:
        lines.append("candidate files (ranked):")
        for f in rc.files:
            sym = f" [{', '.join(f.symbols)}]" if f.symbols else ""
            lines.append(f"  - {f.path}{sym} — {f.reason}")
    if rc.config_files:
        lines.append("config: " + ", ".join(rc.config_files))
    if rc.truncated:
        lines.append("(list truncated to bounds)")
    lines.append("[/RELEVANT CONTEXT]")
    out = "\n".join(lines)
    return out[:max_chars]
