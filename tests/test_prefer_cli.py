"""P2.13-A: `llm-router prefer` and `llm_setup(action="prefer")`.

Round A stores and echoes only; no routing path reads the rules yet.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import sys
from pathlib import Path

import pytest

from llm_router import prefer_grammar, user_rules
from llm_router.commands.prefer import cmd_prefer
from llm_router.tools.setup import llm_setup

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "prefer_phrasings.json").read_text("utf-8"))
ORIGINAL = b"# mine\r\nenforce: smart\r\n"


class _Tty:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self):
        return self._tty

    def read(self, *a):
        return ""


@pytest.fixture
def path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    (tmp_path / ".llm-router").mkdir()
    p = tmp_path / ".llm-router" / "routing.yaml"
    p.write_bytes(ORIGINAL)
    return p


def _tty(monkeypatch, tty: bool, answer: str | None = None):
    monkeypatch.setattr(sys, "stdin", _Tty(tty))
    if answer is not None:
        monkeypatch.setattr(builtins, "input", lambda prompt="": answer)
    else:
        monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail("asked on a non-tty"))


def test_tty_confirm_yes_stores(path, monkeypatch, capsys):
    _tty(monkeypatch, True, "y")
    assert cmd_prefer(["codex before claude"]) == 0
    out = capsys.readouterr().out
    assert "Prefer codex before anthropic" in out and "Stored p1" in out
    assert [r["id"] for r in user_rules.read_rules(path)] == ["p1"]


def test_tty_confirm_no_writes_nothing(path, monkeypatch, capsys):
    _tty(monkeypatch, True, "n")
    assert cmd_prefer(["codex before claude"]) == 1
    assert path.read_bytes() == ORIGINAL
    assert "Not stored" in capsys.readouterr().out


def test_non_tty_without_yes_exits_3_and_writes_nothing(path, monkeypatch, capsys):
    _tty(monkeypatch, False)
    assert cmd_prefer(["codex before claude"]) == 3
    cap = capsys.readouterr()
    assert "--yes" in cap.err and "Prefer codex before anthropic" in cap.out
    assert path.read_bytes() == ORIGINAL


def test_yes_flag_stores_without_a_tty(path, monkeypatch):
    _tty(monkeypatch, False)
    assert cmd_prefer(["--yes", "בלי API בתשלום"]) == 0
    (rule,) = user_rules.read_rules(path)
    assert rule["kind"] == "no_paid_api" and rule["text"] == "בלי API בתשלום"


def test_multi_word_text_may_arrive_as_separate_args(path, monkeypatch):
    _tty(monkeypatch, False)
    assert cmd_prefer(["--yes", "codex", "before", "claude"]) == 0
    assert user_rules.read_rules(path)[0]["text"] == "codex before claude"


def test_dry_run_prints_the_echo_and_writes_nothing(path, monkeypatch, capsys):
    _tty(monkeypatch, False)
    assert cmd_prefer(["--dry-run", "cap codex 20/day"]) == 0
    assert "Cap codex at 20 calls per rolling 24 h" in capsys.readouterr().out
    assert path.read_bytes() == ORIGINAL


def test_list_remove_and_clear(path, monkeypatch, capsys):
    _tty(monkeypatch, False)
    assert cmd_prefer(["--list"]) == 0 and "No stored preferences" in capsys.readouterr().out
    cmd_prefer(["--yes", "codex before claude"])
    cmd_prefer(["--yes", "no openai"])
    capsys.readouterr()
    assert cmd_prefer(["--list"]) == 0
    listing = capsys.readouterr().out
    assert "p1" in listing and "p2" in listing and "no openai" in listing
    assert cmd_prefer(["--remove", "p1"]) == 3  # non-tty needs --yes
    assert len(user_rules.read_rules(path)) == 2
    assert cmd_prefer(["--yes", "--remove", "p1"]) == 0
    assert [r["id"] for r in user_rules.read_rules(path)] == ["p2"]
    assert cmd_prefer(["--yes", "--remove", "p9"]) == 2
    assert cmd_prefer(["--clear"]) == 3
    assert cmd_prefer(["--yes", "--clear"]) == 0
    assert user_rules.read_rules(path) == []
    assert path.read_bytes().startswith(ORIGINAL)
    assert cmd_prefer(["--remove"]) == 2


def test_conflicts_are_refused_with_exit_2_and_nothing_written(path, monkeypatch, capsys):
    _tty(monkeypatch, False)
    assert cmd_prefer(["--yes", "codex before claude"]) == 0
    before = path.read_bytes()
    assert cmd_prefer(["--yes", "claude before codex"]) == 2           # cycle
    assert cmd_prefer(["--yes", "no codex"]) == 2                      # ban of an order's first
    assert cmd_prefer(["--yes", "only local for code"]) == 0
    assert cmd_prefer(["--yes", "no local"]) == 2                      # only-local needs ollama
    err = capsys.readouterr().err
    assert "cycle" in err and "banned target first" in err and "only-local" in err
    assert path.read_bytes().count(b"p3") == 0 and b"no codex" not in path.read_bytes()
    assert path.read_bytes().startswith(before[: before.find(user_rules._BEGIN)])


def test_cap_range_is_refused_by_the_grammar(path, monkeypatch, capsys):
    _tty(monkeypatch, False)
    assert cmd_prefer(["--yes", "cap claude 150%"]) == 2
    assert "out of range" in capsys.readouterr().err
    assert path.read_bytes() == ORIGINAL


def test_outside_preferences_key_exits_2(path, monkeypatch, capsys):
    path.write_bytes(b"preferences: mine\n")
    _tty(monkeypatch, False)
    assert cmd_prefer(["--yes", "no openai"]) == 2
    assert "top-level `preferences:`" in capsys.readouterr().err
    assert path.read_bytes() == b"preferences: mine\n"


def test_duplicate_is_reported_not_added(path, monkeypatch, capsys):
    _tty(monkeypatch, False)
    cmd_prefer(["--yes", "no openai"])
    capsys.readouterr()
    assert cmd_prefer(["--yes", "no openai"]) == 0
    assert "Already stored" in capsys.readouterr().out
    assert len(user_rules.read_rules(path)) == 1


@pytest.mark.parametrize("text", FIXTURE["refused"])
def test_refused_phrasing_exits_2_with_examples_and_writes_nothing(path, monkeypatch, capsys, text):
    _tty(monkeypatch, False)
    assert cmd_prefer(["--yes", text]) == 2
    err = capsys.readouterr().err
    for example in prefer_grammar.EXAMPLES_EN + prefer_grammar.EXAMPLES_HE:
        assert example in err
    assert path.read_bytes() == ORIGINAL


def test_all_twenty_phrasings_store_through_the_cli(path, monkeypatch):
    _tty(monkeypatch, False)
    stored = 0
    for item in FIXTURE["phrasings"]:
        path.write_bytes(ORIGINAL)  # each phrasing alone: some pairs conflict by design
        assert cmd_prefer(["--yes", item["text"]]) == 0, item["id"]
        rules = user_rules.read_rules(path)
        assert [{k: v for k, v in r.items() if k not in ("id", "text")} for r in rules] == item["rules"]
        stored += 1
    assert stored == 20


def test_help_and_unknown_flag(path, capsys):
    assert cmd_prefer(["--help"]) == 0
    assert "llm-router prefer" in capsys.readouterr().out
    assert cmd_prefer(["--bogus"]) == 2


def test_cli_dispatch_and_help_listing(path, monkeypatch, capsys):
    from llm_router import cli

    monkeypatch.setattr(sys, "argv", ["llm-router", "prefer", "--dry-run", "codex לפני claude"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "Prefer codex before anthropic" in capsys.readouterr().out
    assert "prefer" in cli._KNOWN_SUBCOMMANDS and "prefer" in cli._OWN_HELP_ANYWHERE


# ── llm_setup(action="prefer") ───────────────────────────────────────────────

def test_llm_setup_two_step_confirm(path):
    echoed = asyncio.run(llm_setup(action="prefer", text="never claude opus"))
    assert "claude-opus*" in echoed and "confirm=true" in echoed
    assert path.read_bytes() == ORIGINAL                       # step 1 stores nothing
    stored = asyncio.run(llm_setup(action="prefer", text="never claude opus", confirm=True))
    assert "Stored p1" in stored
    assert [r["kind"] for r in user_rules.read_rules(path)] == ["ban"]
    again = asyncio.run(llm_setup(action="prefer", text="never claude opus", confirm=True))
    assert "Already stored" in again


def test_llm_setup_prefer_refusals_never_store(path):
    for text, confirm in (("make it faster", True), ("cap claude 150%", True), ("", True), (None, False)):
        out = asyncio.run(llm_setup(action="prefer", text=text, confirm=confirm))
        assert "Stored" not in out
    assert "Examples" in asyncio.run(llm_setup(action="prefer", text="make it faster"))
    assert path.read_bytes() == ORIGINAL
    asyncio.run(llm_setup(action="prefer", text="codex before claude", confirm=True))
    out = asyncio.run(llm_setup(action="prefer", text="claude before codex", confirm=True))
    assert "cycle" in out and len(user_rules.read_rules(path)) == 1


def test_llm_setup_unknown_action_lists_prefer():
    assert "prefer" in asyncio.run(llm_setup(action="nope"))


# ── mutant G4: the parser accepts an unknown phrase and writes ───────────────

def _refused_phrasings_write_nothing(path: Path) -> bool:
    path.write_bytes(ORIGINAL)
    codes = [cmd_prefer(["--yes", t]) for t in FIXTURE["refused"]]
    return all(c == 2 for c in codes) and path.read_bytes() == ORIGINAL


def test_mutant_g4_parser_accepting_unknown_phrases_turns_refusal_red(path, monkeypatch):
    _tty(monkeypatch, False)
    assert _refused_phrasings_write_nothing(path)

    def lenient(text):
        return prefer_grammar.ParseResult(rules=[{"kind": "no_paid_api"}], echo=["accepted anything"])

    monkeypatch.setattr(prefer_grammar, "parse", lenient)
    assert not _refused_phrasings_write_nothing(path), "mutant G4 survived"
