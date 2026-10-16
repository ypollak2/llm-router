"""P2.13-A: the managed `preferences:` block in routing.yaml.

routing.yaml is user-edited (L26): every byte outside the block must survive a
write, including CRLF, comments and a missing trailing newline.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

from llm_router import failopen, user_rules
from llm_router import prefer_grammar as g

A = {"providers": ["anthropic"], "model": None}
C = {"providers": ["codex"], "model": None}
O = {"providers": ["ollama"], "model": None}  # noqa: E741
ORDER = {"kind": "order", "first": C, "then": A}
BAN = {"kind": "ban", "target": {"providers": ["openai"], "model": None}}

USER_LF = b"# my routing\nenforce: smart   # keep\nblock_providers:\n  - openai\n\n# trailing comment\n"
USER_CRLF = b"# my routing\r\nenforce: smart\r\nblock_providers:\r\n  - openai\r\n"
USER_NO_NL = b"enforce: hard"
USER_ODD = b"enforce: smart\n# caf\xe9 \xff not utf-8\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    (tmp_path / ".llm-router").mkdir()
    return tmp_path / ".llm-router"


@pytest.fixture
def path(home):
    return home / "routing.yaml"


def _outside(data: bytes) -> bytes:
    begin = data.find(user_rules._BEGIN)
    end = data.find(user_rules._END) + len(user_rules._END)
    return data[:begin] + data[end:]


@pytest.mark.parametrize("original", [USER_LF, USER_CRLF, USER_NO_NL, USER_ODD, b""],
                         ids=["lf", "crlf", "no-trailing-newline", "non-utf8", "empty"])
def test_bytes_outside_the_block_are_preserved(path, original):
    path.write_bytes(original)
    user_rules.add_rules([ORDER], "codex before claude")
    first = path.read_bytes()
    assert first.startswith(original)
    assert user_rules.MARK_BEGIN.encode() in first and user_rules.MARK_END.encode() in first
    user_rules.add_rules([BAN], "no openai")
    second = path.read_bytes()
    assert second.startswith(original)  # the appended block is the only addition
    assert _outside(second) == _outside(first)
    assert [r["id"] for r in user_rules.read_rules(path)] == ["p1", "p2"]


def test_user_edits_around_an_existing_block_survive_a_replace(path):
    path.write_bytes(USER_LF)
    user_rules.add_rules([ORDER], "codex before claude")
    data = path.read_bytes()
    edited = b"# added by hand above\nenforce: hard\n" + data + b"\nextra: 1\n"
    path.write_bytes(edited)
    before = _outside(edited)
    user_rules.add_rules([BAN], "no openai")
    after = path.read_bytes()
    assert _outside(after) == before
    assert len(user_rules.read_rules(path)) == 2


def test_crlf_file_gets_a_crlf_block(path):
    path.write_bytes(USER_CRLF)
    user_rules.add_rules([ORDER, BAN], "x")
    data = path.read_bytes()
    assert b"\n" not in data.replace(b"\r\n", b"")  # no bare LF anywhere
    assert len(user_rules.read_rules(path)) == 2


def test_block_is_valid_yaml_and_equal_to_the_json(path):
    path.write_bytes(USER_LF)
    he = g.parse("הגבל את claude ל-80%")
    user_rules.add_rules(he.rules, "הגבל את claude ל-80%")
    user_rules.add_rules([ORDER, BAN], "codex before claude")
    whole = yaml.safe_load(path.read_text("utf-8"))
    assert whole["enforce"] == "smart" and whole["block_providers"] == ["openai"]
    assert whole["preferences"] == user_rules.read_rules(path)
    assert whole["preferences"][0]["text"] == "הגבל את claude ל-80%"
    data = path.read_bytes()
    assert data[data.find(user_rules._BEGIN):].isascii()  # Hebrew is \u-escaped


def test_clear_leaves_an_empty_valid_block(path):
    path.write_bytes(USER_LF)
    user_rules.add_rules([ORDER], "t")
    user_rules.write_rules([])
    assert yaml.safe_load(path.read_text("utf-8"))["preferences"] == []
    assert user_rules.read_rules(path) == []
    assert path.read_bytes().startswith(USER_LF)


def test_first_write_backs_up_once_and_a_missing_file_needs_none(path, home):
    user_rules.add_rules([ORDER], "t")
    assert [p.name for p in home.iterdir() if "pre-prefer" in p.name] == []
    path.unlink()
    path.write_bytes(USER_LF)
    user_rules.add_rules([ORDER], "t")
    backups = [p for p in home.iterdir() if ".pre-prefer-" in p.name]
    assert len(backups) == 1 and backups[0].read_bytes() == USER_LF
    assert backups[0].name.startswith("routing.yaml.pre-prefer-") and backups[0].name.endswith("Z")
    user_rules.add_rules([BAN], "t2")
    assert len([p for p in home.iterdir() if ".pre-prefer-" in p.name]) == 1


def test_outside_preferences_key_refuses_and_changes_nothing(path, home):
    original = b"enforce: smart\npreferences:\n  - mine\n"
    path.write_bytes(original)
    with pytest.raises(user_rules.StoreError, match="top-level `preferences:`"):
        user_rules.add_rules([ORDER], "t")
    assert path.read_bytes() == original
    assert not [p for p in home.iterdir() if "pre-prefer" in p.name]


def test_damaged_markers_refuse_to_write_and_read_as_empty(path, monkeypatch):
    path.write_bytes((user_rules.MARK_BEGIN + "\npreferences: []\n").encode())
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: seen.append(code))
    assert user_rules.read_rules(path) == []
    assert seen == ["CHZ-FO-USER-RULES-READ"]
    with pytest.raises(user_rules.StoreError, match="damaged"):
        user_rules.add_rules([ORDER], "t")


def test_corrupt_block_json_reads_empty_and_is_recorded(path, monkeypatch):
    path.write_bytes(f"{user_rules.MARK_BEGIN}\npreferences: [ {{oops\n{user_rules.MARK_END}\n".encode())
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: seen.append(code))
    assert user_rules.read_rules(path) == []
    assert seen == ["CHZ-FO-USER-RULES-READ"]
    assert user_rules.read_rules(path.with_name("nope.yaml")) == []


def test_failed_replace_leaves_the_original_and_no_temp_file(path, home, monkeypatch):
    path.write_bytes(USER_LF)
    user_rules.add_rules([ORDER], "t")
    snapshot = path.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        user_rules.add_rules([BAN], "t2")
    assert path.read_bytes() == snapshot
    assert not [p for p in home.iterdir() if p.name.startswith(".routing.yaml.")]


def test_file_mode_is_kept(path):
    path.write_bytes(USER_LF)
    os.chmod(path, 0o600)
    user_rules.add_rules([ORDER], "t")
    assert path.stat().st_mode & 0o777 == 0o600


def test_concurrent_writers_serialise(path):
    path.write_bytes(USER_LF)
    targets = ["codex", "ollama", "openai", "gemini_cli", "groq", "xai", "cohere", "mistral"]
    errors: list[BaseException] = []

    def add(name):
        try:
            rule = {"kind": "ban", "target": {"providers": [name], "model": None}}
            user_rules.add_rules([rule], f"no {name}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(t,)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    rules = user_rules.read_rules(path)
    assert len(rules) == 8
    assert sorted(r["id"] for r in rules) == sorted(f"p{i}" for i in range(1, 9))
    assert {r["target"]["providers"][0] for r in rules} == set(targets)


def test_ids_continue_after_a_removal_and_duplicates_are_not_added(path):
    user_rules.add_rules([ORDER], "a")
    user_rules.add_rules([BAN], "b")
    user_rules.remove_rule("p1")
    _all, added = user_rules.add_rules([{"kind": "no_paid_api"}], "c")
    assert [r["id"] for r in added] == ["p3"]
    _all, again = user_rules.add_rules([BAN], "b again")
    assert again == []
    assert [r["id"] for r in user_rules.read_rules(path)] == ["p2", "p3"]
    with pytest.raises(user_rules.StoreError, match="no stored preference"):
        user_rules.remove_rule("p99")


@pytest.mark.parametrize("existing, new, needle", [
    ([ORDER], [{"kind": "order", "first": A, "then": C}], "cycle"),
    ([ORDER, {"kind": "order", "first": A, "then": O}], [{"kind": "order", "first": O, "then": C}], "cycle"),
    ([BAN], [{"kind": "order", "first": BAN["target"], "then": A}], "banned target first"),
    ([{"kind": "order", "first": BAN["target"], "then": A}], [BAN], "banned target first"),
    ([], [{"kind": "ban", "target": {"providers": list(g.KNOWN_PROVIDERS), "model": None}}],
     "every known provider"),
    ([{"kind": "local_only", "task": "code"}], [{"kind": "ban", "target": O}], "only-local"),
    ([], [{"kind": "cap", "target": A, "unit": "pct", "n": 150}], "out of range"),
    ([], [{"kind": "cap", "target": A, "unit": "day", "n": 0}], "out of range"),
])
def test_conflicts_are_reported(existing, new, needle):
    assert needle in (user_rules.check_conflicts(existing, new) or "")


def test_compatible_rules_have_no_conflict_and_a_model_ban_is_not_a_provider_ban():
    opus = {"kind": "ban", "target": {"providers": ["anthropic"], "model": "claude-opus*"}}
    assert user_rules.check_conflicts([opus], [ORDER]) is None
    assert user_rules.check_conflicts([], [ORDER, BAN, {"kind": "no_paid_api"},
                                           {"kind": "cap", "target": A, "unit": "pct", "n": 100}]) is None


def test_token_tracks_the_file(path):
    assert user_rules.token() is None
    user_rules.add_rules([ORDER], "t")
    first = user_rules.token()
    assert isinstance(first, int)
    os.utime(path, ns=(first + 10**9, first + 10**9))
    assert user_rules.token() != first


def test_hook_safe_import_without_pyyaml_or_the_router(path, tmp_path):
    user_rules.add_rules([ORDER, BAN], "t")
    src = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys; sys.modules['yaml'] = None\n"
        "from llm_router import prefer_grammar, user_rules\n"
        "import llm_router\n"
        "assert llm_router.__file__.startswith(sys.argv[1]), llm_router.__file__\n"
        "assert prefer_grammar.parse('codex לפני claude').rules\n"
        "rules = user_rules.read_rules()\n"
        "assert [r['id'] for r in rules] == ['p1', 'p2'], rules\n"
        "assert 'llm_router.router' not in sys.modules and sys.modules['yaml'] is None\n"
        "print(len(rules))\n"
    )
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PYTHONPATH": str(src),
           "LLM_ROUTER_HOME": str(path.parent), "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, "-c", code, str(src)], env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "2"


# ── mutant G5: the writer rewrites the whole file with yaml.dump ─────────────

def _store_preserves_bytes(path: Path) -> bool:
    path.write_bytes(USER_LF)
    user_rules.add_rules([ORDER], "t")
    return path.read_bytes().startswith(USER_LF)


def test_mutant_g5_whole_file_yaml_dump_turns_byte_preservation_red(path, monkeypatch):
    assert _store_preserves_bytes(path)

    def dumping_writer(change, p=None):
        target = Path(p) if p else user_rules.routing_path()
        loaded = (yaml.safe_load(target.read_text("utf-8")) if target.exists() else {}) or {}
        loaded["preferences"] = change(list(loaded.get("preferences", [])))
        target.write_text(yaml.dump(loaded), "utf-8")
        return loaded["preferences"]

    monkeypatch.setattr(user_rules, "mutate_rules", dumping_writer)
    assert not _store_preserves_bytes(path), "mutant G5 survived: byte preservation not checked"


def test_stored_rule_json_shape(path):
    user_rules.add_rules([ORDER], "codex before claude")
    (rule,) = user_rules.read_rules(path)
    assert rule == {"id": "p1", **ORDER, "text": "codex before claude"}
    assert json.loads(json.dumps(rule)) == rule
