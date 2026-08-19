"""SEC-002 regression: filesystem tools must be opt-in and sandboxed.

The pre-remediation ``llm_fs_*`` family read user files into model prompts
with no sandbox; ``llm_fs_edit_many(glob_pattern="~/.ssh/**")`` was a
one-call exfiltration vector.

After SEC-002, two independent gates apply:

1. **Opt-in env (``LLM_ROUTER_FS_TOOLS=on``).** Without it, ``register()``
   exposes ZERO ``llm_fs_*`` tools to MCP clients.
2. **``project_root`` sandbox.** File-reading tools require it; any path
   that escapes the root (via ``..``, absolute, or symlinks) is rejected
   after ``Path.resolve()`` symlink resolution. ``project_root='/'`` is
   refused outright.

See: Docs/audit/HIGH_PRIORITY_WORK_PLAN.md F-SEC-002
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from llm_router.tools import fs


# ── Gate 1: env opt-in registration ───────────────────────────────────────────


def _fake_mcp() -> MagicMock:
    """Minimal MCP stand-in that records every tool() registration."""
    mcp = MagicMock()
    mcp.registered = []

    def tool_factory():
        def decorator(func):
            mcp.registered.append(func.__name__)
            return func
        return decorator

    mcp.tool = tool_factory
    return mcp


def test_fs_tools_not_registered_without_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-002 gate 1: LLM_ROUTER_FS_TOOLS unset → zero llm_fs_* tools registered."""
    monkeypatch.delenv("LLM_ROUTER_FS_TOOLS", raising=False)
    mcp = _fake_mcp()
    fs.register(mcp)
    assert mcp.registered == [], (
        "Filesystem tools must NOT be registered without LLM_ROUTER_FS_TOOLS=on. "
        f"Got: {mcp.registered}"
    )


@pytest.mark.parametrize("value", ["on", "1", "true", "TRUE", "Yes"])
def test_fs_tools_registered_when_opted_in(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """SEC-002 gate 1: with opt-in, all four llm_fs_* tools register."""
    monkeypatch.setenv("LLM_ROUTER_FS_TOOLS", value)
    mcp = _fake_mcp()
    fs.register(mcp)
    assert set(mcp.registered) == {
        "llm_fs_find",
        "llm_fs_rename",
        "llm_fs_edit_many",
        "llm_fs_analyze_context",
    }, f"Expected all four llm_fs_* tools registered; got: {mcp.registered}"


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "", "  "])
def test_fs_tools_falsy_env_values_treated_as_opt_out(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """SEC-002 gate 1: non-affirmative env values must NOT opt in."""
    monkeypatch.setenv("LLM_ROUTER_FS_TOOLS", value)
    mcp = _fake_mcp()
    fs.register(mcp)
    assert mcp.registered == [], (
        f"LLM_ROUTER_FS_TOOLS={value!r} must be treated as opt-out, "
        f"got registrations: {mcp.registered}"
    )


# ── Gate 2: project_root validation ──────────────────────────────────────────


def test_resolve_root_rejects_empty() -> None:
    with pytest.raises(fs.FsSandboxError, match="non-empty"):
        fs._resolve_root("")
    with pytest.raises(fs.FsSandboxError, match="non-empty"):
        fs._resolve_root("   ")


def test_resolve_root_rejects_filesystem_root() -> None:
    """SEC-002: project_root='/' is not a sandbox; refuse it."""
    with pytest.raises(fs.FsSandboxError, match="not a sandbox"):
        fs._resolve_root("/")


def test_resolve_root_rejects_nonexistent(tmp_path: Path) -> None:
    bogus = tmp_path / "does_not_exist"
    with pytest.raises(fs.FsSandboxError, match="does not exist"):
        fs._resolve_root(str(bogus))


def test_resolve_root_rejects_file_instead_of_dir(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(fs.FsSandboxError, match="not a directory"):
        fs._resolve_root(str(f))


def test_resolve_root_accepts_valid_directory(tmp_path: Path) -> None:
    root = fs._resolve_root(str(tmp_path))
    assert root == tmp_path.resolve()


def test_assert_under_root_accepts_child(tmp_path: Path) -> None:
    child = tmp_path / "src" / "app.py"
    child.parent.mkdir()
    child.write_text("pass")
    resolved = fs._assert_under_root(str(child), tmp_path.resolve())
    assert resolved == child.resolve()


def test_assert_under_root_rejects_parent_escape(tmp_path: Path) -> None:
    """SEC-002: a path using .. to escape the sandbox must be rejected."""
    sub = tmp_path / "sub"
    sub.mkdir()
    escape = sub / ".." / ".." / "etc" / "passwd"
    with pytest.raises(fs.FsSandboxError, match="escapes project_root"):
        fs._assert_under_root(str(escape), sub.resolve())


def test_assert_under_root_rejects_absolute_outside(tmp_path: Path) -> None:
    """SEC-002: an absolute path outside the root must be rejected."""
    with pytest.raises(fs.FsSandboxError, match="escapes project_root"):
        fs._assert_under_root("/etc/passwd", tmp_path.resolve())


def test_assert_under_root_resolves_symlinks(tmp_path: Path) -> None:
    """SEC-002: symlink that points outside the sandbox must be rejected.

    Without symlink resolution, an attacker who can create a symlink inside
    the sandbox pointing to /etc could read arbitrary files. Path.resolve()
    closes that hole.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("classified")

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    link = sandbox / "leak"
    link.symlink_to(secret)

    with pytest.raises(fs.FsSandboxError, match="escapes project_root"):
        fs._assert_under_root(str(link), sandbox.resolve())


def test_filter_files_under_root_splits_allowed_and_rejected(tmp_path: Path) -> None:
    inside = tmp_path / "inside.py"
    inside.write_text("x")
    outside = tmp_path.parent / "outside.py"

    allowed, rejected = fs._filter_files_under_root(
        [str(inside), str(outside), "/etc/hosts"],
        tmp_path.resolve(),
    )
    assert allowed == [str(inside)]
    assert str(outside) in rejected
    assert "/etc/hosts" in rejected


# ── End-to-end: llm_fs_edit_many surface contract ────────────────────────────


@pytest.mark.asyncio
async def test_edit_many_rejects_missing_project_root() -> None:
    """SEC-002: empty project_root short-circuits with a validation error."""
    result = await fs.llm_fs_edit_many(
        task="noop", project_root="", ctx=None, glob_pattern="**/*.py",
    )
    assert "invalid project_root" in result


@pytest.mark.asyncio
async def test_edit_many_rejects_filesystem_root() -> None:
    """SEC-002: project_root='/' is refused at the validation layer."""
    result = await fs.llm_fs_edit_many(
        task="noop", project_root="/", ctx=None, glob_pattern="**/*.py",
    )
    assert "not a sandbox" in result


@pytest.mark.asyncio
async def test_edit_many_rejects_files_outside_root(tmp_path: Path) -> None:
    """SEC-002: explicit `files` list with paths outside root → all-escaped error."""
    outside = tmp_path.parent / "leak.py"
    result = await fs.llm_fs_edit_many(
        task="noop",
        project_root=str(tmp_path),
        ctx=None,
        files=["/etc/passwd", str(outside)],
    )
    assert "escaped project_root" in result
    # And critically: no spend was incurred — the function returned before
    # any route_and_call. (Implicit: no mock for route_and_call needed.)


@pytest.mark.asyncio
async def test_edit_many_rejects_absolute_glob_outside_root(tmp_path: Path) -> None:
    """SEC-002: an absolute glob like '/etc/**' yields no files inside root."""
    result = await fs.llm_fs_edit_many(
        task="noop",
        project_root=str(tmp_path),
        ctx=None,
        glob_pattern="/etc/**",
    )
    # _glob with root_dir treats absolute paths as outside root → no matches.
    assert "No files to process" in result or "escaped project_root" in result
