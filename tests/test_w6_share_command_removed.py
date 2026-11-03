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
* nothing in the roots listed in `_ROOTS` asks the user to promote the project: not the card's wording
  ("saved me", "Open-source MCP router"), not the star emoji, "Star on GitHub" or a star-the-repo
  phrasing, not a share-intent URL (X/Twitter, LinkedIn, Reddit, Hacker News, Bluesky, Telegram,
  Facebook), so the card cannot come back under another module name. The dead leftovers that are
  still there are named, with the reason, in `_KNOWN_LEFTOVERS`.
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


# ── no promotional prompt may live anywhere the product's text lives ──────────────

# The star emoji, or how Python or JSON source would spell it: a literal star in a hook is one print away.
_BS = chr(92)
_STAR_SPELLINGS = (chr(0x2B50), _BS + "u2b50", _BS + "U00002b50", _BS + "N{WHITE MEDIUM STAR}")

# One label per thing the removed card did, so an exemption can name a (file, label) pair and the
# file is still scanned for every other label.
_PROMO = {
    "saved me": re.compile(r"\bsaved me\b"),
    "tagline": re.compile(r"Open-source MCP router", re.IGNORECASE),
    "star emoji": re.compile("|".join(re.escape(x) for x in _STAR_SPELLINGS), re.IGNORECASE),
    "star the repo": re.compile(
        r"Star on GitHub|\bstar(?:ring)? (?:us|me)\b|\bstar(?:ring)? (?:the|this|our|my) (?:repo|repository|project)"
        r"|\b(?:give|leave|drop) (?:it |us |me )?a star",
        re.IGNORECASE,
    ),
    "share-intent URL": re.compile(
        r"\b(?:twitter|x)\.com/intent|intent/tweet|\blinkedin\.com/(?:sharing|shareArticle)|\breddit\.com/submit"
        r"|\bnews\.ycombinator\.com/submitlink|\bbsky\.app/intent/compose|\b(?:t|telegram)\.me/share"
        r"|\bfacebook\.com/sharer",
        re.IGNORECASE,
    ),
    "star-CTA code": re.compile(r"star[-_ ]?cta", re.IGNORECASE),
}

# Where the product's user-facing text lives: the package, the hook scripts, the plugin manifests, the
# slash commands, the skills, the host integrations and the npm launcher. assets/ is here only so the
# two README star images below are decisions you can see; drop it from this tuple when they go.
_ROOTS = ("src", "hooks", "commands", "skills", ".claude-plugin", ".codex-plugin", ".factory-plugin",
          "integrations", "npm", "assets")
_TEXT_SUFFIXES = {".py", ".md", ".sh", ".json", ".html", ".yaml", ".yml", ".toml", ".txt",
                  ".mjs", ".js", ".ts", ".tsx", ".svg"}

# Known dead or non-shipped leftovers, each a decision the owner can see and reverse. The exemption is a
# (file, label) pair, not the file: a star prompt that comes back inside one of these files still
# fails. test_every_known_leftover_still_matches fails once one is deleted, so the entry goes with it.
_KNOWN_LEFTOVERS = {
    # Dead code in the claw-code Stop hook: `_should_show_star_cta`, its threshold and its marker file
    # have no caller, and the file holds no star text, so it prints nothing. Removing it means a hook
    # version bump (`# llm_router-hook-version`), so it is a change of its own.
    ("src/llm_router/hooks/session-end-clawcode.py", "star-CTA code"),
    # README art, "Star on GitHub if llm-router saves your quota". README.md and the docs do not
    # reference it and the wheel and sdist do not carry assets/. It goes with its two generators
    # (scripts/generate-readme-svgs.py and scripts/dev/generate-readme-svgs.py), in that same change.
    ("assets/readme/star-cta-dark.svg", "star the repo"),
    ("assets/readme/star-cta-light.svg", "star the repo"),
}


def _scan(base: Path, roots: tuple[str, ...]) -> tuple[dict[str, int], set[tuple[str, str]]]:
    """({root: text files read}, {(path relative to base, label)}). os.walk: this reads text, it is
    not a code walk."""
    counts: dict[str, int] = {}
    hits: set[tuple[str, str]] = set()
    for root in roots:
        counts[root] = 0
        for dirpath, dirnames, filenames in os.walk(base / root):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "node_modules")]
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix not in _TEXT_SUFFIXES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                counts[root] += 1
                rel = path.relative_to(base).as_posix()
                hits |= {(rel, label) for label, rx in _PROMO.items() if rx.search(text)}
    return counts, hits


@pytest.fixture(scope="module")
def tree_scan():
    return _scan(REPO, _ROOTS)


def test_no_shipped_file_carries_a_promotional_prompt(tree_scan):
    counts, hits = tree_scan
    assert counts["src"] + counts["hooks"] > 300, f"the scan read {counts}; it is not looking at the tree"
    assert all(counts.values()), f"a root held no text file, so nothing in it was scanned: {counts}"
    unexpected = sorted(hits - _KNOWN_LEFTOVERS)
    assert not unexpected, "promotional text is back:\n" + "\n".join(f"{p}: {label}" for p, label in unexpected)


def test_every_known_leftover_still_matches(tree_scan):
    """An exemption that matches nothing is dead config: delete it together with what it excused."""
    _, hits = tree_scan
    assert not _KNOWN_LEFTOVERS - hits, sorted(_KNOWN_LEFTOVERS - hits)


# Positive control: no hits proves nothing unless every label can fire.
_SAMPLES = {
    "saved me": ['print("llm_router saved me est. $5.00 so far")'],
    "tagline": ["Open-source MCP router for Claude Code"],
    "star emoji": [f"{s} github.com/ypollak2/llm_router" for s in _STAR_SPELLINGS],
    "star the repo": ["Star on GitHub", "please star the repo", "give us a star", "star us on GitHub"],
    "share-intent URL": [
        "https://twitter.com/intent/tweet?text=x", "https://x.com/intent/post?text=x",
        "https://www.linkedin.com/sharing/share-offsite/?url=x", "https://www.reddit.com/submit?url=x",
        "https://news.ycombinator.com/submitlink?u=x", "https://bsky.app/intent/compose?text=x",
        "https://t.me/share/url?url=x", "https://www.facebook.com/sharer/sharer.php?u=x",
    ],
    "star-CTA code": ["def _should_show_star_cta(session_saved):", "STAR_CTA_THRESHOLD_USD = 0.50"],
}
# Near misses that must stay clean (a looser pattern would flag real text in the tree).
_NEAR_MISSES = [
    "Quota-saved metric", "restart the project", "start the repo", "the stargazers badge",
    "https://www.linux.com/intent", "https://git.me/share", "starting a session",
]


def test_the_scan_can_fail_on_every_label(tmp_path):
    assert set(_SAMPLES) == set(_PROMO), "every label needs a positive control"
    (tmp_path / "root").mkdir()
    expected = set()
    for label, texts in _SAMPLES.items():
        for text in texts:
            name = f"{len(expected)}.md"
            (tmp_path / "root" / name).write_text(text + "\n")
            expected.add((f"root/{name}", label))
    counts, hits = _scan(tmp_path, ("root",))
    assert counts["root"] == len(expected)
    assert expected <= hits, sorted(expected - hits)


def test_the_scan_leaves_near_misses_alone(tmp_path):
    (tmp_path / "root").mkdir()
    for i, text in enumerate(_NEAR_MISSES):
        (tmp_path / "root" / f"{i}.md").write_text(text + "\n")
    counts, hits = _scan(tmp_path, ("root",))
    assert counts["root"] == len(_NEAR_MISSES)
    assert not hits, sorted(hits)
