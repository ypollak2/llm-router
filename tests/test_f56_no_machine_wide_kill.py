"""F56 guard: tests and the scripts they use must never kill by process name or pattern.

`pkill -f <pattern>` / `killall` / `kill $(pgrep ...)` match machine-wide and can kill the
user's unrelated processes (found in test_statusline_tick.py: `pkill -f "time.sleep(30)"`).
A test terminates only a pid it started (Popen object or a pid file its child wrote).
No allowlist.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    re.compile(r"\bpkill\b"),
    re.compile(r"\bkillall\b"),
    re.compile(r"\btaskkill\b", re.I),
    re.compile(r"\bkill\b[^\n]*\$\(\s*pgrep"),
    re.compile(r"\bpgrep\b[^\n]*\|\s*xargs\s+kill"),
)


def _files() -> list[Path]:
    me = Path(__file__).resolve()
    return sorted(p for d in ("tests", "scripts") for p in (ROOT / d).rglob("*.py") if p.resolve() != me)


def test_no_machine_wide_kill_in_tests_or_scripts():
    files = _files()
    assert len(files) > 50, "the scan must find the test tree (non-vacuous)"
    hits = [f"{p.relative_to(ROOT)}:{n}: {line.strip()}"
            for p in files
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
            if any(pat.search(line) for pat in PATTERNS)]
    assert not hits, "machine-wide process kill found; kill only a pid you started:\n" + "\n".join(hits)


def test_patterns_detect_the_known_bad_forms():
    samples = (
        'subprocess.run(["p' + 'kill", "-f", "x"])',
        "kill" + "all python",
        "task" + "kill /im x.exe",
        "kill -9 $(" + "pgrep -f x)",
        "pgrep -f x | xargs " + "kill",
    )
    for s in samples:
        assert any(p.search(s) for p in PATTERNS), s
