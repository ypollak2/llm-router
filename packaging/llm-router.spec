# PyInstaller spec for the standalone llm-router binary (First Forty, task 35).
#
# WHY ONEDIR AND NOT ONEFILE
#
# A onefile build unpacks its whole archive to a temp directory on every
# invocation. The dependency tree here is ~565 MB installed and ~313 MB
# collected, and the hooks run on EVERY prompt and EVERY tool call — auto-route
# on UserPromptSubmit, enforce-route on PreToolUse. Paying an unpack per
# invocation would make the router slower than the premium model it is routing
# away from.
#
# Measured on macOS, same machine, three runs each:
#
#     venv python      0.04 s
#     onedir binary    0.08 s
#
# 40 ms of bootloader overhead is affordable on a per-prompt path. Onefile is
# not, and no amount of tuning changes that: the cost is the unpack.
#
# WHY THE EXPLICIT COLLECTS
#
# cli.py imports litellm lazily, which is good design and defeats PyInstaller's
# static analysis completely — the first build produced a working binary with
# litellm entirely absent, discovered only by looking for its data file. litellm
# additionally loads model_prices_and_context_window_backup.json (1.3 MB) at
# runtime, so submodules alone are not enough; the data has to come too.
#
# tiktoken loads its encodings through a plugin namespace package
# (tiktoken_ext) that is imported by string, so it needs the same treatment.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

_ROOT = Path(SPECPATH).resolve().parent

_datas, _binaries, _hidden = [], [], []
for _pkg in ("litellm", "tiktoken", "tiktoken_ext"):
    d, b, h = collect_all(_pkg)
    _datas += d
    _binaries += b
    _hidden += h

# The hook scripts and rules ship INSIDE the package and are copied out at
# install time, so they must travel with the binary.
_datas += [
    (str(_ROOT / "src" / "llm_router" / "hooks"), "llm_router/hooks"),
    (str(_ROOT / "src" / "llm_router" / "rules"), "llm_router/rules"),
    (str(_ROOT / "src" / "llm_router" / "policies"), "llm_router/policies"),
    (str(_ROOT / "src" / "llm_router" / "data"), "llm_router/data"),
]
_hidden += collect_submodules("llm_router")

a = Analysis(
    [str(_ROOT / "src" / "llm_router" / "cli.py")],
    pathex=[str(_ROOT / "src")],
    binaries=_binaries,
    datas=_datas,
    hiddenimports=_hidden,
    hookspath=[],
    runtime_hooks=[],
    # Trimming what a CLI never needs. Each of these pulls in tens of MB.
    excludes=["tkinter", "matplotlib", "PIL", "pytest", "IPython", "notebook"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="llm-router",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX corrupts signed macOS binaries and trips Gatekeeper
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,   # set by CI per-runner; universal2 needs fat deps
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="llm-router",
)
