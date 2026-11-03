"""`llm-router share` is REMOVED (owner decision, 2026-11-03).

The command printed a savings "card" meant to be pasted into social posts, copied it to the
clipboard and opened a browser tab on a tweet-intent URL. Its tweet text read "llm_router saved me
est. $X so far ... Open-source MCP router for Claude Code ... github.com/ypollak2/llm_router". The
owner called that promotion abusive and ordered the feature out. Every other savings and statistics
surface (`summary`, `gain`, `savings-report`, `stats`, `status`, `dashboard`, the statusline) stays.

This file pins the removal:

* `llm-router share` (bare, with `--svg [PATH]`, or with `--help`) is an unknown command: exit 2,
  one line on stderr, nothing on stdout, no file written, no browser opened, no process started;
* the module is gone, and the command is in neither the known-subcommand table nor `--help`;
* no file under `src/` or `hooks/` carries the card's wording or a tweet-intent URL, so the card
  cannot come back under another module name.
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import webbrowser
from pathlib import Path

import pytest

from llm_router import cli

REPO = Path(__file__).resolve().parents[1]
_TIMEOUT_S = 30

# Every form the old command accepted, plus --help (which used to print usage).
_FORMS = [
    ["share"],
    ["share", "--svg"],
    ["share", "--svg", "{tmp}/card.svg"],
    ["share", "--help"],
]


@pytest.mark.parametrize("form", _FORMS, ids=["bare", "svg", "svg-with-path", "help"])
def test_share_is_an_unknown_command_and_does_nothing(form, tmp_path, monkeypatch, capsys):
    launched: list[tuple] = []
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: launched.append(("webbrowser.open", a)))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: launched.append(("subprocess.run", a)))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: launched.append(("subprocess.Popen", a)))
    monkeypatch.setattr(sys, "argv", ["llm-router", *(a.format(tmp=tmp_path) for a in form)])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    out, err = capsys.readouterr()
    assert exc.value.code == 2
    assert "unknown command 'share'" in err
    assert out == "", "the removed command printed to stdout"
    assert launched == [], "the removed command started a browser or a process"
    assert not (tmp_path / "card.svg").exists()
    assert not (Path(os.environ["LLM_ROUTER_HOME"]) / "savings-card.svg").exists()


def test_share_exits_nonzero_in_a_real_process(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    env = {**os.environ, "HOME": str(tmp_path), "LLM_ROUTER_HOME": str(state), "NO_COLOR": "1"}
    r = subprocess.run(
        [sys.executable, "-m", "llm_router.cli", "share"],
        capture_output=True, text=True, timeout=_TIMEOUT_S, env=env, stdin=subprocess.DEVNULL,
    )
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "unknown command 'share'" in r.stderr
    assert r.stdout == ""
    assert "Traceback" not in r.stderr
    assert list(tmp_path.rglob("*.svg")) == [], "the removed command wrote a card"


def test_the_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("llm_router.commands.share")


def test_share_is_in_neither_the_subcommand_table_nor_help():
    assert "share" not in cli._KNOWN_SUBCOMMANDS
    assert not re.search(r"llm-router\s+share\b", cli.__doc__ or "")


@pytest.mark.parametrize("name", ["summary", "gain", "savings-report", "stats", "status", "dashboard"])
def test_the_other_savings_surfaces_survive(name):
    """The removal took the card and nothing else."""
    assert name in cli._KNOWN_SUBCOMMANDS
    assert re.search(rf"llm-router\s+{re.escape(name)}\b", cli.__doc__ or ""), name


# ── the card's wording must not live anywhere else ────────────────────────────────

_CARD_TEXT = (
    re.compile(r"\bsaved me\b"),
    re.compile(r"Open-source MCP router", re.IGNORECASE),
    re.compile(r"(?:twitter|x)\.com/intent|intent/tweet"),
)
_TEXT_SUFFIXES = {".py", ".md", ".sh", ".json", ".html", ".yaml", ".yml", ".toml", ".txt",
                  ".mjs", ".js", ".ts", ".tsx"}


def _scan(roots: list[Path]) -> tuple[int, list[str]]:
    """(text files read, one 'path: pattern' per hit). os.walk: this reads text, it is not a code walk."""
    seen, hits = 0, []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix not in _TEXT_SUFFIXES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                seen += 1
                hits += [f"{path}: {p.pattern}" for p in _CARD_TEXT if p.search(text)]
    return seen, hits


def test_no_source_file_carries_the_promotional_card():
    seen, hits = _scan([REPO / "src", REPO / "hooks"])
    assert seen > 300, f"the scan read {seen} files; it is not looking at the tree"
    assert not hits, "promotional savings-card text is back:\n" + "\n".join(hits)


def test_the_scan_can_fail(tmp_path):
    """Positive control: no hits proves nothing unless the scanner can hit, and `saved metric` is
    not `saved me`."""
    (tmp_path / "card.py").write_text('print("llm_router saved me est. $5.00 so far")\n')
    (tmp_path / "tweet.py").write_text('URL = "https://twitter.com/intent/tweet?text=x"\n')
    (tmp_path / "blurb.md").write_text("Open-source MCP router for Claude Code\n")
    (tmp_path / "quota.py").write_text('print("Quota-saved metric")\n')
    seen, hits = _scan([tmp_path])
    assert seen == 4
    assert sorted({Path(h.split(": ")[0]).name for h in hits}) == ["blurb.md", "card.py", "tweet.py"]
