"""Shared fixtures for the tool-layer tests (a real workspace, real subprocesses)."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

CANARY = "CANARY-7f3a91c2-do-not-read"
ENV_CANARY = "ENVCANARY-55e1-do-not-leak"
PY_DIR = os.path.dirname(os.path.abspath(sys.executable))


def digest(root: Path) -> str:
    """Hash of every file (path, mode, bytes) under root; symlinks hashed by target."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]):
            p = os.path.join(dirpath, name)
            h.update(os.path.relpath(p, root).encode())
            if os.path.islink(p):
                h.update(os.readlink(p).encode())
            else:
                try:
                    h.update(open(p, "rb").read())
                except OSError:
                    pass
    return h.hexdigest()


def make_source(root: Path) -> Path:
    """A tiny project with tests, plus secrets the owner might have lying around."""
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "pkg.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "tests" / "test_pkg.py").write_text(
        "from pkg import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
        "def test_add_zero():\n    assert add(0, 0) == 0\n")
    (root / "README.md").write_text("# demo\n")
    (root / "notes.txt").write_text("plain notes\n")
    for name in (".env", ".env.local", "id_rsa", "server.pem", "api.key", ".npmrc", ".netrc",
                 "credentials.json"):
        (root / name).write_text(f"{CANARY}\n")
    (root / ".aws").mkdir()
    (root / ".aws" / "credentials").write_text(f"{CANARY}\n")
    (root / ".ssh").mkdir()
    (root / ".ssh" / "config").write_text(f"{CANARY}\n")
    return root
