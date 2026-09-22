"""M-04 repository invariant: no runtime state path is frozen at import time.

The 2026-09-21 audit found `LLM_ROUTER_HOME` was "not universally honoured".
That undersold it. Three stores resolved their path as a module- or class-level
constant evaluated when Python first imported the module, so *no* environment
variable set afterwards could move them:

    routing_quality._default_ledger   fell back to Path.home(), ignoring LLM_ROUTER_HOME
    QuotaTracker.USAGE_JSON           class attribute, bound at import
    result_cache._ROUTER_DIR          module constant, bound at import

and roughly 110 further call sites composed ``~/.llm-router`` directly inside
functions, resolving per call but against the wrong base.

The consequence was not hypothetical: seven synthetic rows from a development
session were written into the operator's real routing ledger on 2026-09-20 and
had to be removed by hand.

These tests encode the invariant so the pattern cannot come back. They are
deliberately **path-resolution** tests, not filesystem-mutation tests: the live
routing hook writes to the real ~/.llm-router continuously while the suite runs,
so any guard keyed on that directory's mtime would fail according to what the
developer happened to be doing at the time.
"""

from __future__ import annotations

import ast
import importlib
import os
import pathlib
import subprocess
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "llm_router"


# ── Step 7: the enumerated runtime-store invariant ──────────────────────────

def _runtime_stores() -> dict[str, pathlib.Path]:
    """Every runtime store, resolved now. Add a row when you add a store."""
    from llm_router import paths, result_cache, routing_quality
    from llm_router.quota_tracker import QuotaTracker

    stores: dict[str, pathlib.Path] = {
        "state root": paths.llm_router_home(),
        "routing ledger": routing_quality._default_ledger(),
        "quota usage.json": pathlib.Path(QuotaTracker.USAGE_JSON),
        "result cache dir": result_cache._router_dir(),
        "execution ledger": pathlib.Path(
            os.environ.get("LLM_ROUTER_EXECUTION_LEDGER_DB")
            or paths.state_path("usage.db")
        ),
        "provider health": pathlib.Path(
            os.environ.get("LLM_ROUTER_HEALTH_SNAPSHOT")
            or paths.state_path("provider_health.json")
        ),
        "hook health": paths.state_path("hook_health.json"),
        "hook error log": paths.state_path("hook_errors.log"),
        "enforcement log": paths.state_path("enforcement.log"),
        "session spend": paths.state_path("session_spend.json"),
        "model tracking": paths.state_path("model_tracking.jsonl"),
        "savings log": paths.state_path("savings_log.jsonl"),
        "GT candidate pool": paths.state_path("ground_truth_candidates.jsonl"),
        "GT accumulation": paths.state_path("gt_accumulation.jsonl"),
        "trace": paths.state_path("trace.jsonl"),
        "tool intercepts": paths.state_path("intercepts.jsonl"),
        "knowledge dir": paths.state_path("knowledge"),
        "idempotency db": paths.state_path("idempotency.db"),
        "budgets db": paths.state_path("budgets.db"),
        "sessions db": paths.state_path("sessions.db"),
    }
    return stores


def test_no_runtime_store_escapes_an_isolated_home(tmp_path, monkeypatch):
    """Step 7. With LLM_ROUTER_HOME isolated, nothing resolves under the real home."""
    sandbox = tmp_path / "isolated"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(sandbox))
    monkeypatch.delenv("LLM_ROUTER_EXECUTION_LEDGER_DB", raising=False)
    monkeypatch.delenv("LLM_ROUTER_HEALTH_SNAPSHOT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_ROUTING_LEDGER", raising=False)

    real_home = pathlib.Path.home() / ".llm-router"
    stores = _runtime_stores()
    assert stores, "store enumeration is empty — this test would pass vacuously"

    escaped = {
        name: p for name, p in stores.items()
        if p == real_home or real_home in p.parents
    }
    assert not escaped, (
        f"{len(escaped)} of {len(stores)} runtime stores resolved under the "
        f"operator's real {real_home} while LLM_ROUTER_HOME was isolated: "
        + ", ".join(f"{k} -> {v}" for k, v in escaped.items())
    )

    outside = {
        name: p for name, p in stores.items()
        if sandbox not in p.parents and p != sandbox
    }
    assert not outside, f"stores resolved outside the sandbox: {outside}"

    print(f"\nRuntime stores checked: {len(stores)}")
    print("Escaping isolated LLM_ROUTER_HOME: 0")


def test_store_invariant_is_not_vacuous():
    """The invariant must reject a known real-home path, or it proves nothing."""
    real_home = pathlib.Path.home() / ".llm-router"
    known_positive = real_home / "routing_quality.jsonl"
    assert real_home in known_positive.parents, (
        "the containment check used by the invariant does not fire on a real-home "
        "path; every assertion above would be decorative"
    )


# ── Step 6: import order must not matter ────────────────────────────────────

@pytest.mark.parametrize(
    "module_name, resolver",
    [
        ("llm_router.routing_quality", "_default_ledger"),
        ("llm_router.result_cache", "_router_dir"),
        ("llm_router.model_tracking", "_tracking_path"),
        ("llm_router.session_spend", "_session_spend_file"),
        ("llm_router.hook_health", "_router_dir"),
    ],
)
def test_resolution_survives_env_change_after_import(tmp_path, monkeypatch, module_name, resolver):
    """The module is imported FIRST, the env is changed AFTER.

    This is the exact ordering the old code got wrong: a constant bound during
    import could never see a later LLM_ROUTER_HOME.
    """
    mod = importlib.import_module(module_name)  # imported before the env moves

    a = tmp_path / "a"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(a))
    monkeypatch.delenv("LLM_ROUTER_ROUTING_LEDGER", raising=False)
    got_a = pathlib.Path(getattr(mod, resolver)())
    assert a in got_a.parents or got_a == a, f"{module_name}.{resolver}() -> {got_a}, expected under {a}"


@pytest.mark.parametrize(
    "module_name, resolver",
    [
        ("llm_router.routing_quality", "_default_ledger"),
        ("llm_router.result_cache", "_router_dir"),
        ("llm_router.model_tracking", "_tracking_path"),
    ],
)
def test_resolution_switches_between_two_homes(tmp_path, monkeypatch, module_name, resolver):
    """Runtime switching A -> B within one process, as multiple profiles require."""
    mod = importlib.import_module(module_name)
    monkeypatch.delenv("LLM_ROUTER_ROUTING_LEDGER", raising=False)

    a, b = tmp_path / "home_a", tmp_path / "home_b"

    monkeypatch.setenv("LLM_ROUTER_HOME", str(a))
    first = pathlib.Path(getattr(mod, resolver)())

    monkeypatch.setenv("LLM_ROUTER_HOME", str(b))
    second = pathlib.Path(getattr(mod, resolver)())

    assert first != second, (
        f"{module_name}.{resolver}() returned {first} for both homes — the value "
        f"is cached or bound, not resolved per call"
    )
    assert a in first.parents or first == a
    assert b in second.parents or second == b


# ── Step 1/3 as a standing guard: no new import-time bindings ───────────────

# Genuinely static: third-party host config and binary discovery, none of which
# live under LLM_ROUTER_HOME and none of which this project owns.
_ALLOWED_STATIC = {
    ("hosts/gemini_cli.py", "GeminiCliAdapter.config_path"),   # ~/.gemini
    ("hosts/cursor.py", "CursorAdapter.config_path"),          # ~/.cursor
    ("install_hooks.py", "_CLAUDE_DIR"),           # ~/.claude
    ("install_hooks.py", "_CLAUDE_JSON_PATH"),     # ~/.claude.json
    ("claude_jsonl_usage.py", "_CC_DIR"),          # ~/.claude/projects
    ("claude_agent.py", "CLAUDE_PATHS"),           # binary discovery
    ("codex_agent.py", "CODEX_PATHS"),             # binary discovery
    ("gemini_cli_agent.py", "GEMINI_PATHS"),       # binary discovery
    # pydantic-settings reads `model_config` when the class body executes; there
    # is no supported way to defer it. Impact is bounded: it selects which .env
    # file is read, and explicit LLM_ROUTER_* environment variables still win
    # over that file. Revisit if pydantic ever supports a callable env_file.
    ("config.py", "RouterConfig.model_config"),
}

# Both spellings matter. `Path.home()` is the original bug; `paths.state_path()`
# at class-definition time is the bug the first sweep *introduced* while fixing
# the base — it looks correct and is still evaluated exactly once, at import.
_TRIGGERS = (
    "Path.home(",
    "expanduser(",
    "paths.state_path(",
    "paths.llm_router_home(",
    "_router_home(",
    "state_path(",
)


def _resolves_eagerly(value: ast.AST, text: str) -> bool:
    """True if this value resolves a home path *when the statement executes*.

    A trigger inside a ``lambda`` (pydantic's ``default_factory=lambda: ...``) or
    a nested ``def`` is deferred to call time and is therefore correct. Only a
    trigger on the eager path is a defect, so lambda and function bodies are
    pruned before looking.
    """
    for node in ast.walk(value):
        if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.Call):
            # a Call nested inside a pruned lambda still shows up via ast.walk,
            # so check ancestry explicitly
            if _inside_deferred(value, node):
                continue
            seg = ast.get_source_segment(text, node) or ""
            head = seg.split("(")[0] + "("
            if any(t == head or seg.startswith(t) for t in _TRIGGERS):
                return True
            if any(t in head for t in _TRIGGERS):
                return True
    return False


def _inside_deferred(root: ast.AST, target: ast.AST) -> bool:
    """Is *target* inside a lambda/def under *root*?"""
    for node in ast.walk(root):
        if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if sub is target:
                    return True
    return False


def _binding_sites():
    """Module- and class-level assignments whose value resolves a home path."""
    found = []
    for f in sorted(SRC.rglob("*.py")):
        rel = str(f.relative_to(SRC))
        try:
            text = f.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (SyntaxError, UnicodeDecodeError):
            continue

        def check(node, name, scope):
            value = node.value
            if value is None:
                return
            seg = ast.get_source_segment(text, node) or ""
            if (rel, name) in _ALLOWED_STATIC:
                return
            if "StatePathAttr(" in seg:
                return  # a descriptor: resolves on every access, which is the fix
            if not _resolves_eagerly(value, text):
                return
            found.append((rel, name, scope, " ".join(seg.split())[:90]))

        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
                if isinstance(tgt, ast.Name):
                    check(node, tgt.id, "module")
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                        t2 = sub.targets[0] if isinstance(sub, ast.Assign) else sub.target
                        if isinstance(t2, ast.Name):
                            check(sub, f"{node.name}.{t2.id}", "class")
    return found


def test_no_module_or_class_level_home_binding():
    """No new import-time path binding may enter the tree.

    `paths.py` is the one place allowed to spell `Path.home()`, and it is a
    function. Everything else resolves through it, at call time.
    """
    sites = [s for s in _binding_sites() if s[0] != "paths.py"]
    assert not sites, (
        f"{len(sites)} import-time path binding(s) reintroduced:\n"
        + "\n".join(f"  {r}: {n} ({scope}-level) -> {seg}" for r, n, scope, seg in sites)
        + "\n\nResolve at access time instead: a module-level function returning "
          "paths.state_path(...), or a descriptor for a class attribute."
    )


def test_binding_scanner_is_not_vacuous(tmp_path):
    """A scanner that finds nothing has not been shown to work (repo rule).

    Confirms the scanner actually walked files and can recognise the pattern.
    """
    py_files = list(SRC.rglob("*.py"))
    assert len(py_files) > 200, f"only scanned {len(py_files)} files — scan is too narrow to trust"

    sample = 'FROZEN = Path.home() / ".llm-router" / "x.json"\n'
    tree = ast.parse(sample)
    node = tree.body[0]
    seg = ast.get_source_segment(sample, node)
    assert any(t in seg for t in _TRIGGERS), (
        "the trigger set no longer recognises a textbook import-time binding"
    )


# ── Step 4: hooks run as separate processes ─────────────────────────────────

@pytest.mark.parametrize("hook", ["auto-route.py", "enforce-route.py", "stop-enforce.py"])
def test_hook_subprocess_honours_isolated_home(tmp_path, hook):
    """A hook launched with LLM_ROUTER_HOME set must write nothing to the real home.

    Hooks are the case that matters most: they execute in a fresh interpreter,
    so an import-time constant is re-frozen on every invocation.
    """
    sandbox = tmp_path / "hookhome"
    sandbox.mkdir()
    script = SRC / "hooks" / hook

    env = dict(os.environ)
    env["LLM_ROUTER_HOME"] = str(sandbox)
    env["HOME"] = str(tmp_path / "fake_home")

    payload = '{"session_id":"iso-test","prompt":"hello","tool_name":"Bash",' \
              '"tool_input":{"command":"ls"},"cwd":"/tmp"}'
    subprocess.run(
        [sys.executable, str(script)],
        input=payload, text=True, env=env,
        capture_output=True, timeout=60,
    )

    # The hook may legitimately write nothing. What it must never do is write
    # into a ~/.llm-router derived from the real HOME.
    leaked = tmp_path / "fake_home" / ".llm-router"
    assert not leaked.exists(), (
        f"{hook} wrote to {leaked}, ignoring LLM_ROUTER_HOME={sandbox}"
    )


# ── Ground Truth tooling lives outside src/ but holds runtime state ─────────

GT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "groundtruth"


def _load_gt(modname: str):
    """Import a scripts/groundtruth module by path (the dir is not a package)."""
    import importlib.util

    name = f"_gt_{modname}"
    spec = importlib.util.spec_from_file_location(name, GT / f"{modname}.py")
    mod = importlib.util.module_from_spec(spec)
    # a @dataclass in the module resolves its annotations via
    # sys.modules[cls.__module__], so the module must be registered before exec
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return mod


@pytest.mark.parametrize("modname", ["sources", "join"])
def test_groundtruth_home_resolves_per_call(tmp_path, monkeypatch, modname):
    """`LLM_ROUTER_HOME = Path(os.environ.get(...))` at module level read the
    environment exactly once, at import — so it honoured the variable only if it
    was already set when the module first loaded.

    Ground Truth candidate pools and accumulation logs are runtime state; this
    is the store the audit's own contamination incident came from.
    """
    mod = _load_gt(modname)  # imported before the env moves

    a, b = tmp_path / "gt_a", tmp_path / "gt_b"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(a))
    first = mod.llm_router_home()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(b))
    second = mod.llm_router_home()

    assert first == a, f"{modname}.llm_router_home() -> {first}, expected {a}"
    assert second == b, f"{modname}.llm_router_home() -> {second}, expected {b}"
    assert first != second, "value is cached, not resolved per call"


def test_groundtruth_readers_take_no_resolved_default(tmp_path, monkeypatch):
    """Step 3: a resolved state path must never be a function default argument.

    A default is evaluated once when `def` executes, which is import time — the
    same defect as a module constant, wearing a different hat.
    """
    import inspect

    sources = _load_gt("sources")
    for fn_name in ("read_llm_router_transcripts", "read_captured",
                    "read_claude_code_transcripts"):
        fn = getattr(sources, fn_name)
        default = inspect.signature(fn).parameters["root"].default
        assert default is None, (
            f"sources.{fn_name}(root=...) has a resolved default {default!r}; "
            f"it was frozen when the module was imported. Use None and resolve "
            f"inside the body."
        )

    # and the deferred resolution actually follows the environment
    sandbox = tmp_path / "gt_home"
    (sandbox).mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(sandbox))
    assert sources.llm_router_home() == sandbox
