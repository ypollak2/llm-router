"""Regression: RED2-03 — scripts/install.sh must back up a malformed settings.json.

The legacy shell installer overwrote ~/.claude/settings.json with a plain
json.dump and crashed on a malformed file (json.load), unlike the maintained
install_hooks.py which backs up + writes atomically. This drives the exact
inline-python snippet the script uses against a malformed file and asserts a
backup is created (parity with install_hooks.py CHZ-PKG-008).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "scripts" / "install.sh"


def test_shell_installer_snippet_backs_up_malformed_settings(tmp_path, monkeypatch):
    # Extract the first settings.json python snippet from the shell script and
    # run it against a malformed file under an isolated HOME.
    text = INSTALL_SH.read_text()
    m = re.search(r"python3 -c \"\nimport json, os\n(.*?)\n\"", text, re.DOTALL)
    assert m, "could not find the settings.json python snippet"
    snippet = "import json, os\n" + m.group(1)
    # The snippet references $HOOK_DST (shell var) — substitute a dummy.
    snippet = snippet.replace("$HOOK_DST", "/tmp/dummy-hook.py")

    home = tmp_path
    (home / ".claude").mkdir(parents=True)
    settings = home / ".claude" / "settings.json"
    settings.write_text("{ this is : not valid json,,,")
    original = settings.read_text()

    monkeypatch.setenv("HOME", str(home))
    subprocess.run([sys.executable, "-c", snippet], check=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"})

    baks = list((home / ".claude").glob("settings.json.corrupt.*.bak"))
    assert baks, "RED2-03: malformed settings.json not backed up by shell installer"
    assert baks[0].read_text() == original
    # And the new settings.json is valid JSON with the hook installed.
    import json
    data = json.loads(settings.read_text())
    assert "hooks" in data
