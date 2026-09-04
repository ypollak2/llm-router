"""RED2-8-01/RED2-9-*: install/uninstall artifact manifest.

The multi-host installer writes to a large, growing set of surfaces (Claude Code,
Claude Desktop, claw-code, codex, cursor, gemini-cli, vscode, copilot-cli,
opencode, openclaw, trae, …). Uninstall was assembled per-host and repeatedly
missed subsets — every audit round found another gap. The structural fix is a
**manifest**: every write records what it did here; uninstall replays the records
in reverse. New surfaces are covered automatically as long as their write goes
through a recording helper, so the coverage can no longer silently drift.

Record kinds:
- ``json_mcp``     {path, root_key, server}  → remove root_key[server] from a JSON file
- ``toml_table``   {path, header}            → remove a ``[header]`` TOML table
- ``text_block``   {path, block}             → remove an exact appended text block
- ``created_file`` {path}                    → delete a whole file llm_router created
- ``file``         {path}                     → delete a copied file (e.g. a hook script)
- ``dir``          {path}                     → recursively delete a llm_router-created dir
- ``json_key``     {path, key, had_key, previous}
                                              → RESTORE a JSON key to the value it
                                                held before install, or delete it if
                                                there was none

The last kind is different in shape from the others and the difference matters.
Every other record answers "delete this thing llm_router created". ``json_key``
answers "put back the thing llm_router *replaced*" — needed the moment the installer
overwrites a key it does not own, which is what RED4-01 found it doing to a
user's ``statusLine``. Removal-only records cannot express that: deleting the key
would leave the user with nothing where their own config used to be, which is the
same data loss with a tidier name.

All operations are best-effort and defensive: a manifest write must never break
install, and a single removal failure must never abort uninstall.
"""
from __future__ import annotations

import json
import os
import pathlib
from typing import Any


def _manifest_path() -> pathlib.Path:
    return pathlib.Path.home() / ".llm-router" / "install-manifest.json"


def _load() -> list[dict[str, Any]]:
    p = _manifest_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record(kind: str, path: Any, **meta: Any) -> None:
    """Append an artifact record. Never raises — a manifest hiccup must not break
    install. De-duplicates identical records so repeated installs don't bloat it.

    RED2-10-01: the path is stored ABSOLUTE (resolved against the install-time
    cwd). A relative path (e.g. Trae's project-scoped ``.rules``) would otherwise
    be re-resolved against the DIFFERENT cwd of a later ``llm_router uninstall``,
    deleting an unrelated file there and orphaning the real one — confirmed data
    loss. Resolving here binds the record to the file that was actually written.
    """
    try:
        abs_path = pathlib.Path(path)
        try:
            abs_path = abs_path.resolve() if abs_path.exists() else abs_path.absolute()
        except OSError:
            abs_path = abs_path.absolute()
        entry = {"kind": kind, "path": str(abs_path), **meta}
        records = _load()
        if entry in records:
            return
        records.append(entry)
        p = _manifest_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(records, indent=2))
    except Exception:
        pass  # best-effort; install must proceed regardless


def find(kind: str, path: Any, **match: Any) -> dict[str, Any] | None:
    """First record of ``kind`` for ``path`` also matching every key in ``match``.

    Exists so a *restore* record can be written exactly once. Install is expected
    to be re-run, and the second run sees llm_router's own value sitting in the key —
    so a blind re-record would overwrite the user's captured original with
    llm_router's replacement, destroying the very thing the record exists to protect.
    Callers check here first and skip if a capture already exists.
    """
    try:
        target = str(pathlib.Path(path).absolute())
        for rec in _load():
            if not isinstance(rec, dict) or rec.get("kind") != kind:
                continue
            if rec.get("path") not in (target, str(path)):
                continue
            if all(rec.get(k) == v for k, v in match.items()):
                return rec
    except Exception:  # noqa: BLE001 — a lookup failure must not break install
        return None
    return None


def _restore_json_key(
    path: pathlib.Path, key: str, had_key: bool, previous: Any
) -> list[str]:
    """Put ``key`` back to its pre-install value, or remove it if there was none."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        return [f"  restore skipped ({path}): {e}"]
    if not isinstance(data, dict):
        return [f"  restore skipped ({path}): top level is not an object"]

    if had_key:
        if data.get(key) == previous:
            return []
        data[key] = previous
        verb = f"✓ Restored {key} in {path} to its pre-install value"
    else:
        if key not in data:
            return []
        del data[key]
        verb = f"✓ Removed {key} from {path} (absent before install)"

    try:
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as e:
        return [f"  restore skipped ({path}): {e}"]
    return [verb]


def clear() -> None:
    """Delete the manifest (after a successful uninstall replay)."""
    try:
        _manifest_path().unlink(missing_ok=True)
    except OSError:
        pass


def apply_uninstall() -> list[str]:
    """Replay the manifest in reverse, removing every recorded artifact.

    Returns human-readable action strings. Records are processed newest-first and
    each removal is independently guarded, so one failure never aborts the rest.
    On completion the manifest is cleared.
    """
    import shutil as _shutil

    actions: list[str] = []
    records = _load()
    for rec in reversed(records):
        # RED1-10-01: a malformed (non-dict) record must be skipped safely — the
        # except handler below must never itself raise (calling rec.get on a
        # non-dict aborted the whole replay, orphaning later records' files).
        if not isinstance(rec, dict):
            actions.append(f"  manifest removal skipped (malformed record: {rec!r})")
            continue
        try:
            kind = rec.get("kind")
            path = pathlib.Path(rec["path"])
            if kind == "json_mcp":
                actions += _remove_json_key(path, rec.get("root_key", "mcpServers"), rec.get("server", "llm_router"))
            elif kind == "toml_table":
                actions += _remove_toml_table(path, rec.get("header", ""))
            elif kind == "text_block":
                actions += _remove_text_block(path, rec.get("block", ""))
            elif kind == "created_file":
                # RED1-10-02: llm_router created this file, but a user may have appended
                # their own content afterward. If we recorded the exact text we
                # wrote, strip ONLY that (deleting the file only if nothing else
                # remains) — never unconditionally unlink and destroy user content.
                block = rec.get("block")
                if block:
                    actions += _remove_text_block(path, block)
                elif path.exists():
                    path.unlink()
                    actions.append(f"✓ Removed {path}")
            elif kind == "file":
                # A llm_router-authored script copy (e.g. a host hook script) — whole
                # file is llm_router's, removal on uninstall is correct.
                if path.exists():
                    path.unlink()
                    actions.append(f"✓ Removed {path}")
            elif kind == "dir":
                if path.exists():
                    _shutil.rmtree(path, ignore_errors=True)
                    actions.append(f"✓ Removed {path}")
            elif kind == "json_key":
                actions += _restore_json_key(
                    path, rec.get("key", ""), bool(rec.get("had_key")), rec.get("previous")
                )
            elif kind == "codex_hooks":
                actions += _remove_codex_hooks(path)
            elif kind == "codex_agents_block":
                actions += _remove_codex_agents_block(path)
            elif kind == "claude_link":
                actions += _remove_claude_link(pathlib.Path(rec.get("link", "")), path)
        except Exception as e:  # noqa: BLE001 — one bad record must not abort the rest
            _p = rec.get("path", "?")
            actions.append(f"  manifest removal skipped ({_p}): {e}")
    clear()
    return actions


def _remove_json_key(path: pathlib.Path, root_key: str, server: str) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    servers = data.get(root_key)
    if isinstance(servers, dict) and server in servers:
        del servers[server]
        path.write_text(json.dumps(data, indent=2))
        return [f"✓ Removed {server} from {path}"]
    return []


def _remove_toml_table(path: pathlib.Path, header: str) -> list[str]:
    import re
    if not path.exists() or not header:
        return []
    text = path.read_text()
    # RED1-9-02: body stops at the next '[table]' line (^-anchored, MULTILINE) so
    # adjacent tables not separated by a blank line are NOT swallowed.
    pattern = re.compile(
        rf'(?m)^\[{re.escape(header)}\][^\n]*\n(?:(?!\[).*(?:\n|$))*'
    )
    updated = pattern.sub("", text, count=1)
    if updated != text:
        # RED2-10-05: no persistent .llm_router-bak — uninstall must leave nothing
        # llm_router-authored. The removal regex is ^-anchored and regression-tested
        # (RED1-9-02), so it strips only the target table.
        path.write_text(updated)
        return [f"✓ Removed [{header}] from {path}"]
    return []


def _remove_text_block(path: pathlib.Path, block: str) -> list[str]:
    if not path.exists() or not block:
        return []
    text = path.read_text()
    if block in text:
        updated = text.replace(block, "", 1)
        # If the file is now empty/whitespace-only, remove it entirely.
        if updated.strip():
            path.write_text(updated)
        else:
            path.unlink()
        return [f"✓ Removed llm_router block from {path}"]
    return []


def _remove_codex_hooks(path: pathlib.Path) -> list[str]:
    """Drop the hook groups whose command points into ~/.llm-router/hooks.
    Everything else in the user's hooks.json is untouched."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    ours = str(pathlib.Path.home() / ".llm-router" / "hooks")
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return []
    removed = 0
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            handlers = (g.get("hooks") or []) if isinstance(g, dict) else []
            if any(isinstance(h, dict) and ours in str(h.get("command", "")) for h in handlers):
                removed += 1
            else:
                kept.append(g)
        hooks[event] = kept
    if removed:
        path.write_text(json.dumps(data, indent=2) + "\n")
        return [f"✓ Removed {removed} llm_router hook group(s) from {path}"]
    return []


def _remove_codex_agents_block(path: pathlib.Path) -> list[str]:
    if not path.exists():
        return []
    from llm_router import codex_host
    text = path.read_text()
    updated = codex_host.remove_marked_block(text)
    if updated == text:
        return []
    if updated.strip():
        path.write_text(updated)
    else:
        path.unlink()
    return [f"✓ Removed llm_router block from {path}"]


def _remove_claude_link(link: pathlib.Path, target: pathlib.Path) -> list[str]:
    """Remove the CLAUDE.md symlink we created, only while it still points at
    the AGENTS.md we recorded. A file or a re-pointed link is the user's."""
    if not str(link) or not link.is_symlink():
        return []
    try:
        points_at = (link.parent / os.readlink(link)).resolve()
    except OSError:
        return []
    if points_at != pathlib.Path(target).resolve():
        return []
    link.unlink()
    return [f"✓ Removed link {link}"]
