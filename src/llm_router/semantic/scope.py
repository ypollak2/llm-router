"""Which project is this? Asked once, answered once.

Four modules answered it independently and did not agree:

    okf.project_root           $LLM_ROUTER_PROJECT_ROOT, else nearest-.git walk
    semantic_cache             sha256($LLM_ROUTER_PROJECT_DIR or os.getcwd())
    result_cache._get_db_path  sha256(whatever string the caller passed)
    gateway                    header, then body, then okf.project_root

Two environment variable names and two fallbacks, which is two ways to be
wrong. The consequences are not symmetrical:

  * Run anything from `src/` or `tests/` and OKF walks up to the repo root
    while the caches key on the subdirectory. One project, two cache
    namespaces, and nothing looks broken — a cache miss looks like a cache
    miss.
  * `result_cache` hashes into a FILE PATH rather than a column in a TTL'd
    table. A divergent spelling there does not age out over a day; it orphans a
    database file that is never reopened, so it is never purged.
  * router.py is about to hand `result_cache` the MCP client's reported root,
    which is whatever the client said, with no `.git` walk at all.

So: `resolve_scope()` produces the canonical root, `scope_key()` the one hash
of it, and everything else calls these.

WHY NOT AN ENVIRONMENT VARIABLE, FOR THE NEXT PERSON WHO REACHES FOR ONE

`gateway.py` currently sets `$LLM_ROUTER_PROJECT_ROOT` for the duration of a
request and restores it in a `finally`, because the function it needs to scope
does not take a scope argument. That works for one request at a time and races
the moment two arrive together — process environment is global and a request is
not. Scope is a value. Pass it.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

# Spelled out rather than referenced through a constant: `test_env_registry.py`
# scans the source for literal names, and a variable read only via an alias is
# reported as a phantom entry — a registry that has stopped describing reality.
# The literal at the read site is also what someone greps for.
#
# `LLM_ROUTER_PROJECT_ROOT` is canonical. `LLM_ROUTER_PROJECT_DIR` was the
# semantic cache's own spelling; anyone who set it has a working configuration
# and keeps one.
_ENV_NAMES = ("LLM_ROUTER_PROJECT_ROOT", "LLM_ROUTER_PROJECT_DIR")


def _walk_to_repo_root(start: Path) -> Path:
    """Nearest ancestor containing `.git`, else *start* itself.

    Git root rather than raw cwd, so scope follows the PROJECT and not whichever
    subdirectory a command ran from — otherwise `src/` and `tests/` accumulate
    two disjoint stores for one codebase.
    """
    here = start.resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return here


def resolve_scope(hint: "str | Path | None" = None) -> Path:
    """The canonical root for *hint*, or for this process if none is given.

    Precedence:

      1. an explicit *hint*, walked to its repo root
      2. ``$LLM_ROUTER_PROJECT_ROOT``
      3. ``$LLM_ROUTER_PROJECT_DIR`` (legacy; the semantic cache's spelling)
      4. the cwd, walked to its repo root

    An explicit hint beats the environment on purpose. The MCP server is a
    long-lived process whose cwd is wherever the host editor was launched — in
    the field that is `$HOME`, which has no `.git`, so every project collapsed
    into one bucket named after the home directory and cross-injected into every
    other. A caller that named a project meant that project, and that is the
    only signal that survives a process whose cwd is meaningless.

    Both environment values are walked to a repo root too. `$..._DIR` was
    previously used raw, so pointing it at a subdirectory silently created a
    second namespace for the same project.
    """
    if hint is not None:
        try:
            return _walk_to_repo_root(Path(hint).expanduser())
        except (TypeError, ValueError, OSError, RuntimeError):
            pass  # unusable hint → fall through to the environment

    raw_root = os.environ.get("LLM_ROUTER_PROJECT_ROOT", "").strip()
    raw_dir = os.environ.get("LLM_ROUTER_PROJECT_DIR", "").strip()
    for raw in (raw_root, raw_dir):
        if not raw:
            continue
        try:
            return _walk_to_repo_root(Path(raw).expanduser())
        except (ValueError, OSError, RuntimeError):
            continue  # e.g. expanduser("~baduser") → try the next source

    return _walk_to_repo_root(Path.cwd())


def resolve_scope_or_none(hint: "str | Path | None" = None) -> Path | None:
    """`resolve_scope`, but None when nothing actually identifies a project.

    `resolve_scope` always answers, falling back to the cwd itself when no `.git`
    is found. That is right for a store that must be scoped SOMEWHERE. It is
    wrong for a caller that would rather have no context than the wrong context —
    the long-lived server, whose cwd is `$HOME`, where "the cwd" is not a project
    but a confidently wrong bucket shared by every project on the machine.

    So: an explicit hint or environment override answers; a cwd inside a repo
    answers; a cwd that is not in a repo answers None.
    """
    if hint is not None or any(
        os.environ.get(n, "").strip() for n in _ENV_NAMES
    ):
        return resolve_scope(hint)
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def scope_key(hint: "str | Path | None" = None, length: int = 16) -> str:
    """A stable hash of the resolved root — the same project, the same key.

    The raw path is never stored: a cache key travels further than the machine
    it was computed on, and a project path is a user's directory layout.

    *length* exists only because the two caches chose different truncations
    (16 and 12) and the stored values are live. The INPUT is now identical,
    which is what made them disagree; the width is cosmetic.
    """
    return hashlib.sha256(
        str(resolve_scope(hint)).encode("utf-8")
    ).hexdigest()[:length]
