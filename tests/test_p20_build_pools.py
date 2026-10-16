import hashlib
import importlib.util
import json
import re
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MARKER = "unique-private-prompt-marker-p20"
PIN_NAMES = (
    "rx_packets", "rx_results_p2b", "rx_grades_grader1", "rx_corpus",
    "rx_real_corpus", "rx_edit_fixtures_tree", "gt_split", "gt_manifest",
    "noncloud_task_ids", "census_py",
)
SOURCE_NAMES = set(PIN_NAMES) | {
    "rx_fixtures_manifest", "gt_items", "noncloud_tasks_meta", "proxy_calls_copy",
    "exec_sessions", "secret_patterns",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def put(path, value, lines=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if lines:
        text = "".join(json.dumps(row) + "\n" for row in value)
    else:
        text = value if isinstance(value, str) else json.dumps(value)
    path.write_text(text)


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True,
        stdin=subprocess.DEVNULL, timeout=30,
    ).stdout.decode().strip()


def entry(role, content, identity=None, **extra):
    return dict(type=role, message=dict(role=role, content=content, id=identity), **extra)


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router"))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location(
        "_test_p20_builder", ROOT / "scripts/p20_build_pools.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    from llm_router.proxy import tool_classes
    from llm_router.proxy import backends
    from llm_router.qa_policy import QA_TASK_TYPES
    from llm_router.secret_scrubber import SECRET_PATTERNS

    qa_type = sorted(QA_TASK_TYPES)[0]
    monkeypatch.setattr(
        backends, "_classify_class",
        lambda prompt, capabilities=False: (
            "not-a-qa-class" if prompt == "not qa" else qa_type, None
        ),
    )
    monkeypatch.setattr(module, "REPO", ROOT)
    monkeypatch.setitem(SECRET_PATTERNS, "test_p20", re.compile("TEST_SECRET_P20"))
    fake = types.ModuleType("_p20_fake_census")
    fake.BASH_UNRESOLVED = "unresolved-bash"
    fake.step_kind_of = lambda row: (row.get("kind", "continuation"), row.get("recorded", True))
    fake.offline_tool_class = lambda tools: tool_classes.TECHNICAL_OP
    monkeypatch.setitem(sys.modules, "_p20_fake_census", fake)
    rx, gt, nc = (tmp_path / name for name in ("rx", "gt", "nc"))
    paths = {
        "rx_packets": rx / "p2b_work/packets.jsonl",
        "rx_results_p2b": rx / "results_p2b_qwen38.md",
        "rx_grades_grader1": rx / "p2b_work/grades_grader1.jsonl",
        "rx_corpus": rx / "corpus.jsonl",
        "rx_real_corpus": rx / "real_corpus.jsonl",
        "gt_split": gt / "split.json", "gt_manifest": gt / "manifest.json",
        "noncloud_task_ids": nc / "task_ids.json",
        "census_py": tmp_path / "census.py",
        "rx_fixtures_manifest": rx / "fixtures/manifest.json",
        "gt_items": gt / "items.jsonl", "noncloud_tasks_meta": nc / "tasks_meta.jsonl",
        "proxy_calls_copy": tmp_path / "calls.jsonl",
        "exec_sessions": tmp_path / "exec_sessions.txt",
        "prereg": tmp_path / "prereg.md",
    }
    packet = dict(packet_id="p1", kind="main", task_type=qa_type, ref_id="r1",
                  source="real", complexity="deep_reasoning")
    put(paths["rx_packets"], [packet, dict(packet, packet_id="ungraded")], True)
    put(paths["rx_results_p2b"], "not parsed: " + MARKER)
    put(paths["rx_grades_grader1"], [dict(packet_id="p1", verdict=MARKER, reason=MARKER)], True)
    put(paths["rx_corpus"], [dict(
        id="r1", text=MARKER, prior_turns=[
            dict(role="user", text="before"), dict(type="assistant", content=[
                dict(type="text", text="reply")
            ]), "raw",
        ],
    )], True)
    put(paths["rx_real_corpus"], [dict(id="r1", _orig_session_id="original")], True)
    put(paths["gt_split"], {"heldout": ["g1", "g2"]})
    put(paths["gt_manifest"], [
        dict(id="g1", split="heldout", kind="organic", tier_opus="sonnet"),
        dict(id="g2", split="heldout", kind="organic", tier_opus="haiku"),
        dict(id="unused", split="train", kind="organic"),
        dict(id="unused2", split="heldout", kind="synthetic"),
    ])
    put(paths["gt_items"], [
        dict(id="g1", prompt="tiny", context="context", session_id="gt-session"),
        dict(id="g2", prompt="not qa", session_id="gt-session"),
    ], True)
    mirror = nc / "mirror.git"
    mirror.mkdir(parents=True)
    git(mirror, "init")
    git(mirror, "config", "user.email", "test@example.invalid")
    git(mirror, "config", "user.name", "P20 Test")
    put(mirror / "file", "fixture")
    git(mirror, "add", "file")
    git(mirror, "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
    commit = git(mirror, "rev-parse", "HEAD")
    put(paths["noncloud_task_ids"], {"tasks": [
        dict(task_id="ok", claude_truth="opus"), dict(task_id="missing", claude_truth="unknown"),
    ]})
    put(paths["noncloud_tasks_meta"], [
        dict(task_id="ok", commit_sha=commit, parent_sha=commit),
        dict(task_id="missing", commit_sha=commit, parent_sha="0" * 40),
    ], True)
    put(paths["rx_fixtures_manifest"], [
        dict(name="edit-one", path="original/path", test="pytest", idx=7),
        dict(name="edit-missing", path="missing/path", test="pytest", idx=8),
    ])
    put(rx / "fixtures/edit-one/file.txt", "fixture")
    put(rx / "fixtures/edit-two/file.txt", "second fixture")
    put(paths["census_py"],
        "from _p20_fake_census import BASH_UNRESOLVED, step_kind_of, offline_tool_class\n")
    put(paths["exec_sessions"], "exec\n")
    transcript_root = tmp_path / "transcripts"
    row = dict(ts="2026-10-01T00:00:00Z", session_id="good", msg_id="target",
               session_kind="organic", prev_tool_class=tool_classes.TECHNICAL_OP,
               tier_complexity="moderate", is_main_thread=True)
    put(paths["proxy_calls_copy"], [row], True)
    transcript_path = transcript_root / "project/good.jsonl"
    put(transcript_path, [
        entry("user", "old ask"),
        entry("assistant", [dict(type="tool_use", name="Custom", input={"x": 1})]),
        entry("user", [dict(type="tool_result", content=[dict(type="text", text="result")])]),
        entry("user", [dict(type="text", text="latest " + MARKER)]),
        entry("user", "sidechain", isSidechain=True),
        entry("assistant", "STOP", "target"),
        entry("user", "AFTER"),
    ], True)
    transcript_path.write_text("invalid json\n" + transcript_path.read_text())

    def pin():
        text = (ROOT / "docs/prereg/PREREG-P20-requal.md").read_text()
        for name, constant in module.PINS.items():
            value = (module.tree_digest(rx / "fixtures") if name == "rx_edit_fixtures_tree"
                     else sha(paths[name].read_bytes()))
            text, count = re.subn(
                rf"(?m)^({constant}\s*=\s*).*$", lambda m: m[1] + value, text
            )
            assert count == 1
        put(paths["prereg"], text)

    pin()
    args = [
        "--out", str(tmp_path / "out"), "--rx-dir", str(rx), "--gt-dir", str(gt),
        "--noncloud-dir", str(nc), "--census-py", str(paths["census_py"]),
        "--proxy-calls", str(paths["proxy_calls_copy"]),
        "--exec-sessions", str(paths["exec_sessions"]),
        "--transcripts-root", str(transcript_root), "--prereg", str(paths["prereg"]),
        "--until", "2026-10-02T00:00:00Z",
    ]
    return types.SimpleNamespace(
        module=module, args=args, out=tmp_path / "out", paths=paths, rx=rx, pin=pin,
        row=row, transcripts=transcript_root, transcript_path=transcript_path,
    )


def read_pool(case, name):
    return [json.loads(line) for line in (case.out / "pools" / f"{name}.jsonl").read_text()
            .splitlines()]


def test_build(case, capsys):
    assert case.module.main(case.args) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    summary = json.loads(captured.out)
    raw = (case.out / "pools/build_manifest.json").read_bytes()
    manifest = json.loads(raw)
    assert MARKER not in captured.out + raw.decode()
    assert set(manifest) == {
        "built_utc", "since", "until", "repo_git_sha", "sources", "transcripts",
        "counts", "rx_membership", "pools",
    }
    assert set(manifest["sources"]) == SOURCE_NAMES
    assert manifest["built_utc"] == manifest["until"] == "2026-10-02T00:00:00Z"
    assert manifest["since"] == case.module.SINCE
    assert manifest["repo_git_sha"] == git(ROOT, "rev-parse", "HEAD")
    assert summary == dict(counts=manifest["counts"], pools=manifest["pools"],
                           manifest_sha256=sha(raw))
    for name, path in case.paths.items():
        if name != "prereg":
            assert manifest["sources"][name] == sha(path.read_bytes())
    from llm_router.secret_scrubber import SECRET_PATTERNS

    assert manifest["sources"]["secret_patterns"] == sha(json.dumps(sorted(
        (k, p.pattern if hasattr(p, "pattern") else str(p)) for k, p in SECRET_PATTERNS.items()
    )).encode())
    assert manifest["transcripts"] == {
        "project/good.jsonl": sha(case.transcript_path.read_bytes())
    }
    assert manifest["rx_membership"] == {
        "main_qa_packets": 2, "main_qa_graded": 1, "difference": 1,
    }
    qa = read_pool(case, "qa")
    assert [(r["id"], r["session"], r["tier"]) for r in qa] == [
        ("rx-p1", "original", "complex"), ("g1", "gt-session", "moderate"),
    ]
    assert qa[0]["context"] == "user: before\nassistant: reply\nraw"
    assert qa[1]["request"] == "tiny"
    tech, = read_pool(case, "tech_op")
    assert tech["request"] == "latest " + MARKER
    assert tech["id"] == "tech-" + sha(b"good:target")[:16]
    assert tech["messages"][2]["content"] == "[tool_result] result"
    assert tech["messages"][1]["content"] == '[tool_use Custom] {"x": 1}'
    assert len(tech["messages"]) == 4
    assert [t["name"] for t in tech["tools"]] == sorted(
        ["Read", "Grep", "Glob", "Bash", "Edit", "Write", "Custom"]
    )
    code = read_pool(case, "code")
    assert [r["id"] for r in code] == ["nc-ok", "rx-edit-edit-one"]
    assert code[0]["tier"] == "complex"
    assert code[1]["task_ref"] == dict(
        stratum="rx_edit", name="edit-one", path="original/path", test="pytest", idx=7
    )
    assert manifest["counts"]["code"]["excluded"] == {
        "checkout_unavailable": 1, "fixture_missing": 1,
    }
    assert manifest["counts"]["qa"]["excluded"] == {"not_qa": 1}
    for name in case.module.SETS:
        path = case.out / "pools" / f"{name}.jsonl"
        assert manifest["pools"][name] == sha(path.read_bytes())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((case.out / "pools/build_manifest.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(case.out.stat().st_mode) == 0o700
    assert stat.S_IMODE((case.out / "pools").stat().st_mode) == 0o700


@pytest.mark.parametrize("name", PIN_NAMES)
def test_pin_mismatch(case, name, capsys, monkeypatch):
    original = case.module.load_module

    def guarded(module_name, path):
        assert module_name != "_p20_pool_census"
        return original(module_name, path)

    monkeypatch.setattr(case.module, "load_module", guarded)
    if name == "rx_edit_fixtures_tree":
        put(case.rx / "fixtures/edit-one/file.txt", "mutated")
    else:
        with case.paths[name].open("ab") as stream:
            stream.write(b"\n")
    assert case.module.main(case.args) == 2
    assert not case.out.exists()
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("pool build refused")


@pytest.mark.parametrize("target", [
    "pools/qa.jsonl", "pools/tech_op.jsonl", "pools/code.jsonl", "pools/build_manifest.json",
])
@pytest.mark.parametrize("symlink", [False, True])
def test_existing_output(case, target, symlink, capsys):
    path = case.out / target
    path.parent.mkdir(parents=True)
    if symlink:
        path.symlink_to(case.out / "absent")
    else:
        path.write_text("keep")
    assert case.module.main(case.args) == 2
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("pool build refused")
    assert path.is_symlink() if symlink else path.read_text() == "keep"


def test_tree_hash(case):
    root = case.rx / "fixtures"
    expected = sha(b"".join(
        name.encode() + b"\0" + sha(content).encode() + b"\n"
        for name, content in sorted([
            ("edit-one/file.txt", b"fixture"),
            ("edit-two/file.txt", b"second fixture"),
        ])
    ))
    assert case.module.tree_digest(root) == expected
    for name in (".hidden/a", "__pycache__/a", "ignored.pyc"):
        put(root / "edit-one" / name, "ignored")
    assert case.module.tree_digest(root) == expected
    put(root / "edit-one/file.txt", "changed")
    assert case.module.tree_digest(root) != expected


def test_duplicate_refusal(case, capsys):
    packets = [json.loads(line) for line in case.paths["rx_packets"].read_text().splitlines()]
    put(case.paths["rx_packets"], packets + [packets[0]], True)
    case.pin()
    assert case.module.main(case.args) == 2
    assert not case.out.exists()
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("pool build refused")


def test_all_tech_exclusions(case, capsys):
    variants = {
        "not_organic": dict(session_kind="generated"),
        "exec_session": dict(session_id="exec"),
        "not_continuation": dict(kind="initial"),
        "bash_unresolved": dict(prev_tool_class="unresolved-bash"),
        "not_technical_op": dict(prev_tool_class="other"),
        "not_main_thread": dict(is_main_thread=False),
        "transcript_missing": {},
        "msg_not_found": {},
        "no_human_ask": {},
        "secret_pattern": {},
    }
    ledger = []
    for reason, changes in variants.items():
        row = dict(case.row, session_id=reason)
        row.update(changes)
        ledger.append(row)
        messages = [entry("user", "ask"), entry("assistant", "", "target")]
        if reason == "msg_not_found":
            messages = [entry("user", "ask")]
        elif reason == "no_human_ask":
            messages = [entry("assistant", "", "target")]
        elif reason == "secret_pattern":
            messages[0] = entry("user", "TEST_SECRET_P20")
        if reason != "transcript_missing":
            put(case.transcripts / "project" / f"{row['session_id']}.jsonl", messages, True)
    ledger.extend([
        dict(case.row, ts="2026-09-27T23:59:59Z"),
        dict(case.row, ts="2026-10-02T00:00:00Z"),
    ])
    put(case.paths["proxy_calls_copy"], ledger, True)
    assert case.module.main(case.args) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["counts"]["tech_op"] == {
        "rows": 0, "by_stratum": {}, "excluded": {reason: 1 for reason in variants},
    }


def test_output_symlink_escape(case, tmp_path, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    case.out.symlink_to(elsewhere, target_is_directory=True)
    assert case.module.main(case.args) == 2
    assert list(elsewhere.iterdir()) == []
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("pool build refused")

def test_rx_membership_sessions_and_tiers(case, capsys):
    path = case.paths["rx_packets"]
    base = json.loads(path.read_text().splitlines()[0])
    variants = [
        dict(base, packet_id="generated", source="gen", complexity="simple"),
        dict(base, packet_id="moderate", complexity="moderate", ref_id="other"),
        dict(base, packet_id="complex", complexity="complex"),
        dict(base, packet_id="unknown", complexity="unknown"),
        dict(base, packet_id="not-main", kind="plant"),
        dict(base, packet_id="not-qa", task_type="code"),
        dict(base, packet_id="missing", ref_id="absent"),
    ]
    originals = [json.loads(line) for line in path.read_text().splitlines()]
    put(path, originals + variants, True)
    put(case.paths["rx_grades_grader1"],
        [dict(packet_id=p["packet_id"]) for p in [base] + variants], True)
    corpus = [json.loads(line) for line in case.paths["rx_corpus"].read_text().splitlines()]
    put(case.paths["rx_corpus"], corpus + [dict(id="other", text="another ask")], True)
    case.pin()
    assert case.module.main(case.args) == 0
    summary = json.loads(capsys.readouterr().out)
    rx_rows = [row for row in read_pool(case, "qa") if row["stratum"] == "rx"]
    assert [(row["id"], row["session"], row["tier"]) for row in rx_rows] == [
        ("rx-p1", "original", "complex"),
        ("rx-generated", "rx-gen", "simple"),
        ("rx-moderate", "rx-real", "moderate"),
        ("rx-complex", "original", "complex"),
        ("rx-unknown", "original", ""),
    ]
    assert summary["counts"]["qa"]["excluded"] == {"ref_missing": 1, "not_qa": 1}
    manifest = json.loads((case.out / "pools/build_manifest.json").read_text())
    assert manifest["rx_membership"]["difference"] == 1


@pytest.mark.parametrize("location", ["request", "context", "message"])
def test_secret_locations(case, location, capsys):
    if location in ("request", "context"):
        put(case.paths["rx_corpus"], [dict(
            id="r1", text="TEST_SECRET_P20" if location == "request" else MARKER,
            prior_turns=[dict(role="user", content="TEST_SECRET_P20")]
            if location == "context" else [],
        )], True)
        case.pin()
    else:
        put(case.transcript_path, [
            entry("user", "TEST_SECRET_P20"),
            entry("user", "latest safe request"),
            entry("assistant", "STOP", "target"),
        ], True)
    assert case.module.main(case.args) == 0
    summary = json.loads(capsys.readouterr().out)
    group = "tech_op" if location == "message" else "qa"
    assert summary["counts"][group]["excluded"]["secret_pattern"] == 1


def test_offline_class_and_transcript_boundary(case, capsys):
    put(case.paths["proxy_calls_copy"], [
        dict(case.row, recorded=False, prev_tool_class="ignored"),
    ], True)
    put(case.transcript_path, [
        entry("user", "human request"),
        entry("user", [
            dict(type="text", text="not a human ask"),
            dict(type="tool_result", content="tool output"),
        ]),
        entry("progress", "ignored", "target"),
        entry("assistant", [
            dict(type="tool_use", name="FutureTool", input={"secret": MARKER}),
        ], "target"),
        entry("user", "later ask"),
    ], True)
    assert case.module.main(case.args) == 0
    capsys.readouterr()
    row, = read_pool(case, "tech_op")
    assert row["request"] == "human request"
    assert len(row["messages"]) == 2
    assert row["messages"][1]["content"] == "not a human ask\n[tool_result] tool output"
    assert "FutureTool" not in {tool["name"] for tool in row["tools"]}


@pytest.mark.parametrize("label,expected", [
    ("haiku", "simple"), ("sonnet", "moderate"), ("opus", "complex"), ("other", ""),
])
def test_gt_tier_provenance(case, label, expected, capsys):
    manifest = json.loads(case.paths["gt_manifest"].read_text())
    manifest[0]["tier_opus"] = label
    put(case.paths["gt_manifest"], manifest)
    case.pin()
    assert case.module.main(case.args) == 0
    capsys.readouterr()
    row, = [row for row in read_pool(case, "qa") if row["stratum"] == "gt"]
    assert row["tier"] == expected


def test_epoch_ts_and_missing_msg_id(case, capsys):
    """The live proxy ledger stores ts as a float epoch; rows without msg_id are excluded."""
    epoch_row = dict(case.row, ts=1790812800.0)  # 2026-10-01T00:00:00Z, same step as case.row
    no_msg = dict(case.row, session_id="nomsg", msg_id=None)
    bad_ts = dict(case.row, session_id="badts", ts=True)
    put(case.paths["proxy_calls_copy"], [epoch_row, no_msg, bad_ts], True)
    assert case.module.main(case.args) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["counts"]["tech_op"]["rows"] == 1
    assert summary["counts"]["tech_op"]["excluded"] == {"no_msg_id": 1, "bad_ts": 1}


def test_refusal_reason_is_safe(case, capsys):
    case.paths["prereg"].write_text(case.paths["prereg"].read_text().replace(
        "RX_CORPUS_SHA256=", "RX_CORPUS_SHA256=0", 1))
    assert case.module.main(case.args) == 2
    err = capsys.readouterr().err
    assert err.startswith("pool build refused: ValueError pin mismatch line ")
    assert MARKER not in err
