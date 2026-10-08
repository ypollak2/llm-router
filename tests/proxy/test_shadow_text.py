"""P1.7-d: the shadow text sidecar (``proxy/shadow_text.py``, called from ``llm_shadow``).

Rules under test: off by default (nothing hashed, written or created); a sampled turn leaves
exactly ``{text_sha, session_id, ts, context, prompt}`` in ``shadow_text.jsonl`` (0600) and
nowhere else; the sample is a pure function of ``text_sha`` and the UTC day; at most 20
entries a UTC day, counted from the file; router banners are cut out; a text that matches
a secret pattern is skipped whole; a held lock or a broken file never costs the shadow record.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path

import pytest

from llm_router import prompt_key
from llm_router import local_classifier as lc
from llm_router.proxy import shadow_text as st
from tests.proxy.test_llm_classifier_shadow import (
    LOG, SID, FakeClassifier, _post, _records, _shadow_app, _turn, simple,  # noqa: F401 - simple is a fixture
)

SIDE = st.SIDECAR_NAME
DAY0 = 1_791_000_000.0  # a fixed instant; DAY0 + 86400 is the next UTC day


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("LLM_ROUTER_LOCAL_CLASSIFIER", "LLM_ROUTER_SHADOW_TEXT_SAMPLE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", "http://127.0.0.1:9")
    lc._reset_state()
    yield
    lc._reset_state()


def _body(text: str) -> dict:
    return {"messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}


def _fields(text: str, *, ts: float = DAY0, sid: str = SID) -> dict:
    return {"ts": ts, "session_id": sid, "text_sha": prompt_key.key(text)}


def _entries(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _record(path: Path, text: str, *, context: str = "Working directory: /w", **kw) -> str:
    return st.maybe_record(path, _body(text), context, _fields(text, **kw))


# --- the env key ----------------------------------------------------------------------------


@pytest.mark.parametrize("raw,want", [
    (None, 0.0), ("", 0.0), ("off", 0.0), ("OFF", 0.0), ("0", 0.0), ("0.0", 0.0), ("-1", 0.0), ("1.5", 0.0),
    ("nope", 0.0), ("nan", 0.0), ("on", st.RATE_ON), (" On ", st.RATE_ON), ("0.25", 0.25), ("1", 1.0),
])
def test_rate_parsing(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv(st.ENV, raising=False)
    else:
        monkeypatch.setenv(st.ENV, raw)
    assert st.rate() == want


def test_the_rate_on_is_the_amended_value():
    assert st.RATE_ON == 0.3 and st.CAP_PER_DAY == 20 and st.PROMPT_STORE_CAP == 20000


# --- default off: nothing is written, nothing is created -----------------------------------


@pytest.mark.parametrize("value", [None, "", "off", "0", "garbage"])
def test_off_writes_nothing_and_creates_nothing(tmp_path, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv(st.ENV, value)
    path = tmp_path / "state" / SIDE
    assert _record(path, "fix the parser") == st.OFF
    assert not st.wanted(prompt_key.key("fix the parser"), DAY0)
    assert not (tmp_path / "state").exists()  # not even the directory or the lock file


async def test_off_through_the_proxy_keeps_the_shadow_record_and_no_sidecar(tmp_path, monkeypatch, simple):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    FakeClassifier(monkeypatch)
    app = _shadow_app(tmp_path)
    assert (await _post(app, _turn("rename the module"))).status_code == 200
    await app.state.cls_shadow.drain()
    assert len(_records(tmp_path)) == 1
    assert sorted(p.name for p in tmp_path.iterdir() if "shadow_text" in p.name) == []


# --- on: one entry, 0600, the right keys, the text only there -------------------------------


async def test_a_sampled_turn_leaves_one_0600_entry_and_the_text_nowhere_else(tmp_path, monkeypatch, simple):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    monkeypatch.setenv(st.ENV, "1")
    FakeClassifier(monkeypatch)
    marker = "ZZ-SIDECAR-MARKER-ZZ"
    app = _shadow_app(tmp_path)
    body = _turn(f"refactor the router {marker}")
    body["system"] = "Primary working directory: /work/proj"
    assert (await _post(app, body)).status_code == 200
    await app.state.cls_shadow.drain()
    (rec,) = _records(tmp_path)
    side = tmp_path / SIDE
    assert stat.S_IMODE(side.stat().st_mode) == 0o600
    (entry,) = _entries(side)
    assert list(entry) == list(st.KEYS)
    assert (entry["text_sha"], entry["session_id"], entry["ts"]) == (rec["text_sha"], SID, rec["ts"])
    assert entry["prompt"] == f"refactor the router {marker}"
    assert "Working directory: /work/proj" in entry["context"]
    # the text is in the sidecar and in no other file the proxy wrote
    for p in tmp_path.iterdir():
        if p.is_file() and p.name not in (SIDE,):
            assert marker not in p.read_text(errors="ignore"), p.name


async def test_a_turn_without_a_shadow_record_leaves_no_entry(tmp_path, monkeypatch, simple):  # noqa: F811
    """The sidecar is written only beside a shadow record: a continuation never schedules."""
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    monkeypatch.setenv(st.ENV, "1")
    fake = FakeClassifier(monkeypatch)
    app = _shadow_app(tmp_path)
    body = _turn("first")
    body["messages"] += [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}]
    assert (await _post(app, body)).status_code == 200
    await app.state.cls_shadow.drain()
    assert fake.calls == [] and not (tmp_path / SIDE).exists()


async def test_a_broken_sidecar_never_costs_the_shadow_record(tmp_path, monkeypatch, simple):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    monkeypatch.setenv(st.ENV, "1")
    FakeClassifier(monkeypatch)
    (tmp_path / SIDE).mkdir()  # a directory where the file should be: every open fails
    app = _shadow_app(tmp_path)
    assert (await _post(app, _turn("anything"))).status_code == 200
    await app.state.cls_shadow.drain()
    assert len(_records(tmp_path)) == 1


# --- the sample is deterministic -------------------------------------------------------------


def test_the_sample_is_a_pure_function_of_sha_and_day():
    shas = [prompt_key.key(f"turn {i}") for i in range(4000)]
    a = [st.sampled(s, "2026-10-08", 0.3) for s in shas]
    assert a == [st.sampled(s, "2026-10-08", 0.3) for s in shas]
    assert abs(sum(a) / len(a) - 0.3) < 0.03
    assert a != [st.sampled(s, "2026-10-09", 0.3) for s in shas]  # the daily seed changes the pick
    assert not any(st.sampled(s, "2026-10-08", 0.0) for s in shas) and all(st.sampled(s, "2026-10-08", 1.0) for s in shas)
    assert st.sampled(shas[0], "d", 0.99) or not st.sampled(shas[0], "d", 0.0)


def test_a_turn_not_in_the_sample_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv(st.ENV, "0.3")
    path = tmp_path / SIDE
    texts = [f"prompt number {i}" for i in range(60)]
    got = [_record(path, t) for t in texts]
    day = st.day_of(DAY0)
    want = [st.sampled(prompt_key.key(t), day, 0.3) for t in texts]
    assert [g == st.WRITTEN for g in got] == want and 0 < sum(want) < len(want)
    assert [g for g, w in zip(got, want) if not w] == [st.NOT_SAMPLED] * (len(want) - sum(want))
    assert len(_entries(path)) == sum(want)


# --- the cap --------------------------------------------------------------------------------


def test_the_daily_cap_is_enforced_from_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv(st.ENV, "1")
    path = tmp_path / SIDE
    got = [_record(path, f"turn {i}") for i in range(st.CAP_PER_DAY + 7)]
    assert got.count(st.WRITTEN) == st.CAP_PER_DAY and got.count(st.SKIP_CAP) == 7
    assert len(_entries(path)) == st.CAP_PER_DAY
    # a restart has no memory but the file: the cap still holds
    assert _record(path, "after restart") == st.SKIP_CAP
    # the next UTC day starts a fresh count
    assert _record(path, "next day", ts=DAY0 + 86400) == st.WRITTEN
    assert len(_entries(path)) == st.CAP_PER_DAY + 1


def test_the_same_turn_is_one_entry(tmp_path, monkeypatch):
    monkeypatch.setenv(st.ENV, "1")
    path = tmp_path / SIDE
    assert [_record(path, "same text"), _record(path, "same text")] == [st.WRITTEN, st.SKIP_DUP]
    assert _record(path, "same text", sid="99999999-2222-3333-4444-555555555555") == st.WRITTEN  # another session
    assert len(_entries(path)) == 2


# --- the file mode ----------------------------------------------------------------------------


def test_the_file_is_0600_even_when_the_umask_is_open(tmp_path, monkeypatch):
    monkeypatch.setenv(st.ENV, "1")
    old = os.umask(0o000)
    try:
        assert _record(tmp_path / SIDE, "mode check") == st.WRITTEN
        assert _record(tmp_path / SIDE, "mode check two") == st.WRITTEN
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / SIDE).stat().st_mode) == 0o600


def test_an_existing_loose_file_is_tightened_on_the_next_write(tmp_path, monkeypatch):
    monkeypatch.setenv(st.ENV, "1")
    path = tmp_path / SIDE
    path.write_text("")
    path.chmod(0o644)
    assert _record(path, "tighten me") == st.WRITTEN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_state_dir_is_0700_created_and_tightened(tmp_path, monkeypatch):
    monkeypatch.setenv(st.ENV, "1")
    old = os.umask(0o000)
    try:
        fresh = tmp_path / "fresh" / "state"
        assert _record(fresh / SIDE, "dir check") == st.WRITTEN
        assert stat.S_IMODE(fresh.stat().st_mode) == 0o700
        loose = tmp_path / "loose"
        loose.mkdir()
        loose.chmod(0o755)
        assert _record(loose / SIDE, "dir check two") == st.WRITTEN
        assert stat.S_IMODE(loose.stat().st_mode) == 0o700
    finally:
        os.umask(old)


def test_the_lock_file_is_0600_created_and_tightened(tmp_path, monkeypatch):
    monkeypatch.setenv(st.ENV, "1")
    lock = tmp_path / (SIDE + st.LOCK_SUFFIX)
    old = os.umask(0o000)
    try:
        assert _record(tmp_path / SIDE, "lock check") == st.WRITTEN
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        lock.chmod(0o644)
        assert _record(tmp_path / SIDE, "lock check two") == st.WRITTEN
    finally:
        os.umask(old)
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


# --- banners ----------------------------------------------------------------------------------

BANNERS = [
    '⚡ ROUTE: query/simple → try llm(task="query", tier="fast") [via heuristic]',
    "⚡ MANDATORY ROUTE: code/complex → call llm_code(complexity=\"complex\") [via api]",
    "ROUTE: research/moderate",
    "[llm_router] Routing context for this agent:\nPressure: session=35% | LOW",
    "[llm-router] The image was NOT loaded into your context.",
    "📊 Router saved $0.01",
    "SUBSCRIPTION OVERRIDE: code/simple → /model haiku",
]


@pytest.mark.parametrize("banner", BANNERS)
def test_banners_are_cut_from_context_and_prompt(tmp_path, monkeypatch, banner):
    monkeypatch.setenv(st.ENV, "1")
    path = tmp_path / SIDE
    text = f"please fix the parser\n{banner}\nand add a test"
    ctx = f"Working directory: /w\n\nAssistant's last message before this prompt (tail):\nok {banner}"
    assert st.maybe_record(path, _body(text), ctx, _fields(text)) == st.WRITTEN
    (e,) = _entries(path)
    for field in (e["context"], e["prompt"]):
        for marker in st.MARKERS:
            assert marker not in field, (marker, field)
    assert "please fix the parser" in e["prompt"] and "Working directory: /w" in e["context"]
    assert st.SCRUB_SUFFIX in e["prompt"] and st.SCRUB_SUFFIX in e["context"]


def test_scrub_keeps_a_clean_text_byte_for_byte():
    clean = "Working directory: /w\n\nEarlier user prompt 1/1:\nfix it\n\nroute 66 is a road; ROUTES are fine"
    assert st.scrub(clean) == clean  # "ROUTE:" with the colon is the marker, not the word


# --- the secret guard ----------------------------------------------------------------------------


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-" + "Ab1_" * 8, "AKIA" + "ABCDEFGHIJKLMNOP", "ghp_" + "a1B2" * 6,
    "password = hunter2hunter2", "OPENAI_API_KEY=abc123", "Bearer abc.def.ghi-jkl",
    "-----BEGIN RSA PRIVATE KEY-----\nxx\n-----END RSA PRIVATE KEY-----",
    "postgres://user:s3cretpw@db.example.com/app",
])
def test_a_secret_in_the_prompt_or_the_context_skips_the_turn(tmp_path, monkeypatch, secret):
    monkeypatch.setenv(st.ENV, "1")
    path = tmp_path / SIDE
    in_prompt = f"deploy with {secret} now"
    assert _record(path, in_prompt) == st.SKIP_SECRET
    clean = "a harmless prompt"
    assert st.maybe_record(path, _body(clean), f"Working directory: /w\n\nEarlier:\n{secret}", _fields(clean)) == st.SKIP_SECRET
    assert _entries(path) == []  # nothing, and not a redacted copy either


async def test_a_secret_turn_leaves_a_shadow_record_but_no_text(tmp_path, monkeypatch, simple):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    monkeypatch.setenv(st.ENV, "1")
    FakeClassifier(monkeypatch)
    app = _shadow_app(tmp_path)
    assert (await _post(app, _turn("use sk-ant-api03-" + "Zx9_" * 8))).status_code == 200
    await app.state.cls_shadow.drain()
    assert len(_records(tmp_path)) == 1 and not (tmp_path / SIDE).exists()


# --- other guards ------------------------------------------------------------------------------


def test_a_text_that_is_not_the_one_the_key_names_is_never_stored(tmp_path, monkeypatch):
    monkeypatch.setenv(st.ENV, "1")
    f = _fields("the text the key names")
    assert st.maybe_record(tmp_path / SIDE, _body("a different text"), "ctx", f) == st.SKIP_MISMATCH
    assert not (tmp_path / SIDE).exists()


def test_an_empty_prompt_and_a_missing_identity_write_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv(st.ENV, "1")
    p = tmp_path / SIDE
    assert st.maybe_record(p, {"messages": []}, "", {"ts": DAY0, "session_id": SID, "text_sha": prompt_key.key("x")}) == st.SKIP_EMPTY
    assert st.maybe_record(p, _body("x"), "", {"ts": DAY0, "session_id": None, "text_sha": prompt_key.key("x")}) == st.NOT_SAMPLED
    assert st.maybe_record(p, _body("x"), "", {"ts": DAY0, "session_id": SID, "text_sha": None}) == st.NOT_SAMPLED
    assert not p.exists()


def test_a_held_lock_drops_the_sample_and_never_waits(tmp_path, monkeypatch):
    monkeypatch.setenv(st.ENV, "1")
    path = tmp_path / SIDE
    assert _record(path, "first") == st.WRITTEN
    fd = os.open(str(path) + st.LOCK_SUFFIX, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # the labeller is pruning
        assert _record(path, "second") == st.SKIP_LOCKED
    finally:
        os.close(fd)
    assert [e["prompt"] for e in _entries(path)] == ["first"]
    assert _record(path, "second") == st.WRITTEN  # released: the next sample goes through


def test_a_very_long_prompt_is_kept_head_and_tail(tmp_path, monkeypatch):
    monkeypatch.setenv(st.ENV, "1")
    path = tmp_path / SIDE
    text = "H" * 12000 + "M" * 20000 + "T" * 12000
    assert _record(path, text) == st.WRITTEN
    (e,) = _entries(path)
    assert e["prompt"] == "H" * 10000 + st.OMITTED + "T" * 10000
    assert len(e["prompt"]) <= st.PROMPT_STORE_CAP + len(st.OMITTED)


def test_the_module_never_logs_or_prints_text(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(st.ENV, "1")
    _record(tmp_path / SIDE, "ZZ-NEVER-PRINT-ZZ")
    out = capsys.readouterr()
    assert "ZZ-NEVER-PRINT-ZZ" not in out.out + out.err
    src = Path(st.__file__).read_text()
    assert "print(" not in src and "logger" not in src and "structlog" not in src


PINNED_MARKERS = ("⚡", "ROUTE:", "[llm_router]", "[llm-router]", "📊", "Routing context for this agent",
                  "SUBSCRIPTION OVERRIDE")  # AMEND1-shadowlab F: the labeller scrubs the same list


def test_the_marker_list_is_the_pinned_one():
    assert st.MARKERS == PINNED_MARKERS and st.SCRUB_SUFFIX == " [router text removed]"


@pytest.mark.parametrize("marker", PINNED_MARKERS)
def test_each_marker_alone_is_cut(marker):
    assert st.scrub(f"keep this {marker} drop this") == "keep this" + st.SCRUB_SUFFIX
    assert st.scrub(f"line one\n{marker} only\nline three") == f"line one\n{st.SCRUB_SUFFIX}\nline three"


@pytest.mark.parametrize("kind,written", [("harness", False), ("headless", False), ("organic", True), ("research", True),
                                           (None, True)])
def test_harness_and_headless_turns_leave_no_text(tmp_path, monkeypatch, kind, written):
    monkeypatch.setenv(st.ENV, "1")
    f = {**_fields("a typed prompt"), "session_kind": kind}
    got = st.maybe_record(tmp_path / SIDE, _body("a typed prompt"), "Working directory: /w", f)
    assert got == (st.WRITTEN if written else st.SKIP_KIND)
    assert (tmp_path / SIDE).exists() is written


def test_the_skipped_kinds_are_the_labellers_excluded_ones():
    assert st.SKIP_SESSION_KINDS == ("harness", "headless")  # AMEND2 SIDE_SKIP_KINDS pin
