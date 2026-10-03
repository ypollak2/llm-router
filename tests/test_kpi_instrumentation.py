"""KPI instrumentation: session tags and the decision fields the scorecard needs.

Primary KPI: G3 (ledger completeness), which enables NS and D3. Each test pins
one thing the scorecard cannot be computed without:

* a session kind on every ledger that feeds a KPI (organic / research / harness /
  headless), tagged and never dropped;
* ``tier_proposed`` (pre-override), ``tier_policy_version`` and ``tier_retry``
  present on EVERY proxy row, null when not computed.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from llm_router import edit_ledger, paths, session_kind
from llm_router.proxy import server as ps
from llm_router.proxy import tiers as pt
from tests.test_proxy_tiers import (
    OPUS,
    SID,
    SONNET,
    Upstream,
    _app,
    _first,
    _post,
    _req,
    _rows,
)



@pytest.fixture
def moderate(monkeypatch):
    from llm_router.proxy import backends as pb

    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "code", "complexity": "moderate", "chain_head": [], "model": None}

    monkeypatch.setattr(pb, "choose_model", choose)


REPO = Path(__file__).resolve().parent.parent
HOME = "/Users/someone"


# ── session kind: pure classification ────────────────────────────────────────

@pytest.mark.parametrize("cwd,entry,override,expected", [
    ("/Users/someone/Projects/app", "cli", None, "organic"),
    ("/Users/someone/Projects/app", None, None, "organic"),
    ("/Users/someone/Projects/app", "claude-desktop", None, "organic"),  # desktop is not headless
    ("/Users/someone/Projects/app", "sdk-cli", None, "headless"),
    ("/Users/someone/Projects/app", "sdk-ts", None, "headless"),
    ("/Users/someone/.rsi/targets/x", "cli", None, "research"),
    ("/Users/someone/work/scratchpad/p1", "cli", None, "research"),
    ("/private/tmp/claude-501/-Users-someone/abc/scratchpad/wt", "cli", None, "research"),
    ("/private/tmp/bq-probe", "cli", None, "harness"),
    ("/var/folders/ab/xyz/T/sandbox", "sdk-cli", None, "harness"),  # sandbox beats headless
    ("/Users/someone/.rsi/x", "sdk-cli", None, "research"),         # research beats headless
    ("/Users/someone/Projects/app", "sdk-cli", "organic", "organic"),  # override beats everything
    ("/private/tmp/bq", "cli", "research", "research"),
    ("/Users/someone/Projects/app", "cli", "bogus", "organic"),     # bad override is ignored
    (None, None, None, "organic"),
])
def test_classify_precedence(cwd, entry, override, expected):
    assert session_kind.classify(cwd=cwd, entrypoint=entry, override=override, home=HOME) == expected


def test_a_directory_merely_named_like_a_marker_is_not_research():
    # "scratchpad-notes" and "/tmp-archive" are not path parts / prefixes.
    assert session_kind.classify(cwd="/Users/someone/scratchpad-notes", home=HOME) == "organic"
    assert session_kind.classify(cwd="/Users/someone/tmp/app", home=HOME) == "organic"
    assert session_kind.classify(cwd="/Users/someone/.rsi-old/x", home=HOME) == "organic"


def test_classify_returns_only_declared_kinds():
    kinds = {session_kind.classify(cwd=c, entrypoint=e, home=HOME)
             for c in (None, "/tmp/x", "/Users/someone/.rsi/x", "/Users/someone/p")
             for e in (None, "cli", "sdk-cli")}
    assert kinds == set(session_kind.VALID_KINDS)  # finds something, and all four


# ── session kind: persistence ────────────────────────────────────────────────

def test_tag_persists_and_reads_back_by_session_id():
    env = {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}
    assert session_kind.tag_session("sess-1", "/Users/someone/p", env=env) == "headless"
    assert session_kind.kind_of("sess-1") == "headless"
    tag = json.loads((paths.state_path("session_kind_sess-1.json")).read_text())
    assert (tag["kind"], tag["cwd"], tag["entrypoint"]) == ("headless", "/Users/someone/p", "sdk-cli")


def test_env_override_wins_at_tag_time():
    env = {"LLM_ROUTER_SESSION_KIND": "research", "CLAUDE_CODE_ENTRYPOINT": "cli"}
    assert session_kind.tag_session("sess-2", "/Users/someone/p", env=env) == "research"


def test_an_untagged_session_is_none_not_organic():
    assert session_kind.kind_of("never-tagged") is None
    assert session_kind.kind_of(None) is None
    assert session_kind.tag_session(None, "/x", env={}) is None


def test_a_failed_tag_write_is_counted_not_silent(monkeypatch):
    """T-14: the tag write is a persistence site; a failure must leave a counter."""
    from llm_router import failopen

    failopen.reset_cache()

    real = Path.write_text

    def boom(self, *a, **k):
        if self.name.startswith("session_kind_"):
            raise OSError("disk full")
        return real(self, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(Path, "write_text", boom)
        assert session_kind.tag_session("sess-fail", "/Users/someone/p", env={}) == "organic"
    failopen.reset_cache()
    assert failopen.snapshot().by_code.get("CHZ-FO-SESSION-KIND-TAG") == 1
    assert session_kind.kind_of("sess-fail") is None  # untagged, never silently organic


def test_a_hostile_session_id_cannot_escape_the_state_dir():
    session_kind.tag_session("../../etc/evil", "/x", env={})
    written = list(paths.state_path("").glob("session_kind_*.json"))
    assert len(written) == 1 and written[0].parent == paths.state_path("")
    assert session_kind.kind_of("../../etc/evil") == "organic"


def test_a_corrupt_tag_reads_as_untagged():
    paths.state_path("session_kind_bad.json").parent.mkdir(parents=True, exist_ok=True)
    paths.state_path("session_kind_bad.json").write_text("{not json")
    assert session_kind.kind_of("bad") is None
    paths.state_path("session_kind_odd.json").write_text(json.dumps({"kind": "weird"}))
    assert session_kind.kind_of("odd") is None


def test_edit_outcome_row_carries_the_session_kind(monkeypatch):
    monkeypatch.setattr(edit_ledger, "_resolve_session_id", lambda: "sess-edit")
    session_kind.tag_session("sess-edit", "/private/tmp/bq", env={})
    edit_ledger.record_edit_outcome(file="a.py", model="ollama/x", applied=True)
    row = json.loads(paths.state_path(edit_ledger.LEDGER_FILENAME).read_text().splitlines()[-1])
    assert row["session_kind"] == "harness" and row["session_id"] == "sess-edit"


def test_untagged_edit_outcome_row_has_the_key_with_null(monkeypatch):
    monkeypatch.setattr(edit_ledger, "_resolve_session_id", lambda: "sess-untagged")
    edit_ledger.record_edit_outcome(file="a.py", model="ollama/x", applied=False)
    row = json.loads(paths.state_path(edit_ledger.LEDGER_FILENAME).read_text().splitlines()[-1])
    assert "session_kind" in row and row["session_kind"] is None


# ── hooks: wiring and the byte-identical mirror ──────────────────────────────

@pytest.mark.parametrize("name", ["session-start.py", "agent-route.py"])
def test_hooks_mirror_is_byte_identical(name):
    assert (REPO / "hooks" / name).read_bytes() == (REPO / "src/llm_router/hooks" / name).read_bytes()


def _calls(tree: ast.AST, attr: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == attr]


def test_session_start_hook_tags_the_session():
    tree = ast.parse((REPO / "src/llm_router/hooks/session-start.py").read_text())
    calls = _calls(tree, "tag_session")
    assert len(calls) == 1  # found something to check
    main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
    assert calls[0] in list(ast.walk(main))
    # fail-open: the call sits inside a try whose handler swallows
    tries = [n for n in ast.walk(main) if isinstance(n, ast.Try) and calls[0] in list(ast.walk(n))]
    assert tries and tries[-1].handlers


def test_agent_route_ledgers_stamp_the_session_kind():
    tree = ast.parse((REPO / "src/llm_router/hooks/agent-route.py").read_text())
    keys = [k.value for n in ast.walk(tree) if isinstance(n, ast.Dict)
            for k in n.keys if isinstance(k, ast.Constant)]
    assert keys.count("session_kind") == 2  # agent_calls entry + north_star_units entry


def test_session_start_hook_end_to_end(monkeypatch, tmp_path):
    """Run the hook's tagging block the way main() does, from its own payload."""
    payload = {"session_id": "sess-hook", "cwd": str(Path.home() / ".rsi" / "t")}
    monkeypatch.delenv("LLM_ROUTER_SESSION_KIND", raising=False)
    session_kind.tag_session(payload["session_id"], payload["cwd"])
    assert session_kind.kind_of("sess-hook") == "research"


# ── proxy rows: tier_proposed / tier_policy_version / tier_retry / session_kind ─

NEW_KEYS = ("session_kind", "tier_proposed", "tier_policy_version", "tier_retry")


async def test_every_forwarded_row_has_all_four_keys_even_with_tiers_off(tmp_path, moderate):
    up = Upstream()
    await _post(_app(tmp_path, up, tiers=ps.TIERS_OFF), _req())
    (row,) = _rows(tmp_path)
    assert all(k in row for k in NEW_KEYS)
    assert row["tier_proposed"] is None and row["tier_policy_version"] is None
    assert row["tier_retry"] is None


async def test_tier_proposed_is_the_policy_table_answer_before_overrides(tmp_path, moderate):
    # code/moderate -> sonnet in the bundled policy; the call asked for opus.
    up = Upstream()
    app = _app(tmp_path, up)
    await _post(app, _first())
    await _post(app, _req())
    first, second = _rows(tmp_path)
    assert first["tier_reason"] == "first_call" and first["tier_proposed"] is None  # classifier never ran
    assert second["tier_proposed"] == "sonnet" and second["tier"] == "sonnet"
    assert all(k in first and k in second for k in NEW_KEYS)


async def test_proposed_differs_from_served_when_an_override_moves_it(tmp_path, moderate):
    # The table says opus for moderate; the request is sonnet, so the no-upgrade
    # clamp holds it at sonnet. The ledger must keep BOTH answers.
    up = Upstream()
    app = _app(tmp_path, up, policy_overrides={"route": {"default": {"simple": "haiku", "moderate": "opus",
                                                                    "complex": "opus"}}})
    await _post(app, _first(SONNET))
    await _post(app, _req(SONNET))
    second = _rows(tmp_path)[1]
    assert second["tier_proposed"] == "opus"          # what the table said
    assert second["tier"] == "sonnet"                 # the no-upgrade clamp moved it


async def test_rejected_rewrite_records_tier_retry_and_keeps_the_proposal(tmp_path, moderate):
    err = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "nope"}}).encode()
    from tests.test_proxy_tiers import _sse

    up = Upstream(responses=[(200, _sse(OPUS)), (400, err)])
    app = _app(tmp_path, up)
    await _post(app, _first())
    await _post(app, _req())
    row = _rows(tmp_path)[-1]
    assert row["tier_retry"]["status"] == 400
    assert row["tier_proposed"] == "sonnet"


async def test_decision_error_row_still_has_every_key(tmp_path, monkeypatch):
    from llm_router.proxy import backends as pb

    async def boom(text, pinned, *, anthropic=False):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(pb, "choose_model", boom)
    up = Upstream()
    app = _app(tmp_path, up)
    await _post(app, _first())
    await _post(app, _req())
    row = _rows(tmp_path)[-1]
    assert row["tier_reason"] == pt.REASON_DECISION_ERROR
    assert all(k in row for k in NEW_KEYS)
    assert row["tier_proposed"] is None
    assert len(row["tier_policy_version"]) == 12


async def test_row_carries_the_persisted_session_kind(tmp_path, moderate):
    session_kind.tag_session(SID, "/private/tmp/claude-501/x/scratchpad/y", env={})
    up = Upstream()
    await _post(_app(tmp_path, up), _req())
    (row,) = _rows(tmp_path)
    assert row["session_kind"] == "research" and row["session_id"] == SID


async def test_untagged_session_is_null_not_organic(tmp_path, moderate):
    up = Upstream()
    await _post(_app(tmp_path, up), _req())
    (row,) = _rows(tmp_path)
    assert "session_kind" in row and row["session_kind"] is None


# ── policy version ───────────────────────────────────────────────────────────

def test_policy_version_follows_the_yaml_bytes_and_the_router_version(tmp_path, monkeypatch):
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    a.write_text("tiers: []\n")
    b.write_text("tiers: []\n# edited\n")
    va, vb = pt.policy_version(a), pt.policy_version(b)
    assert len(va) == 12 and va != vb
    assert pt.policy_version(a) == va  # deterministic
    import llm_router

    monkeypatch.setattr(llm_router, "__version__", "99.0.0")
    assert pt.policy_version(a) != va  # a release changes it too


def test_policy_version_of_a_missing_file_is_a_label_not_a_crash(tmp_path):
    assert pt.policy_version(tmp_path / "nope.yaml") == "unreadable"


def test_loaded_policy_carries_its_version_and_a_dict_policy_does_not(tmp_path):
    assert pt.ClaudeTierPolicy.load().policy_version == pt.policy_version(pt.DEFAULT_POLICY_PATH)
    from tests.test_proxy_tiers import _raw_policy

    assert pt.ClaudeTierPolicy.from_dict(_raw_policy()).policy_version is None


async def test_rows_in_one_run_share_one_version_and_it_changes_with_the_file(tmp_path, moderate):
    up = Upstream()
    app = _app(tmp_path, up)
    await _post(app, _first())
    await _post(app, _req())
    v1 = {r["tier_policy_version"] for r in _rows(tmp_path)}
    assert len(v1) == 1 and None not in v1
    other = tmp_path / "other"
    other.mkdir()
    app2 = _app(other, Upstream(), policy_overrides={"allow_upgrade": True})
    await _post(app2, _req())
    assert {r["tier_policy_version"] for r in _rows(other)} != v1
