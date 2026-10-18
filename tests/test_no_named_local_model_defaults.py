"""D-205 / F50 + F43: code and bundled defaults never name a local model as a default.

A user's local models are THAT USER's configuration, discovered by llm-router
(model_discovery) and read through ``model_discovery.local_model()`` or the policy
placeholder ``ollama/@local``. Before F50 these paths guessed a name when nothing was
discovered (ensemble classifier, judge, rolling summary, ReAct, librarian, llm_local_task,
direct-executor ranking, standard.yaml BUDGET chains, config/signals.yaml chains).

Three parts:
  1. a source scan: no local-model tag literal in src/ or hooks/ code, no local model in a
     bundled policy chain, outside an explicit DATA allowlist (each entry says why it is
     descriptive data that never selects a model); stale allowlist entries fail;
  2. a no-family scan of the files that used to act on a name;
  3. behaviour: empty inventory + no user setting -> no model name from any acting path;
     a user setting or a discovered model -> that model.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "llm_router"

_FAMILY = (r"(?:qwen|(?<!o)llama|gemma|hermes|mistral|deepseek|phi\d|granite|smollm|lfm|"
           r"codellama|starcoder|nemotron|devstral|codestral)")
# A local-model TAG: ``family...:tag`` or ``ollama/family...``
TAG = re.compile(rf"(?i)(?:{_FAMILY}[\w.\-]*:[\w.\-]+|ollama/{_FAMILY})")
BARE_FAMILY = re.compile(rf"(?i){_FAMILY}")

# path (relative to repo root) -> why it may name a model. DESCRIPTIVE DATA ONLY: none of
# these chooses a model on its own; each is looked up BY a model the user already has.
ALLOWLIST: dict[str, str] = {
    "src/llm_router/model_registry.py":
        "bundled capability/price metadata row; looked up by id, never selects a model",
    "config/models.yaml":
        "capability/price metadata catalogue; looked up by id, never selects a model",
    "src/llm_router/token_budget.py":
        "context-window table keyed by model id; a lookup for a model already chosen",
    "src/llm_router/local_models.py":
        "num_ctx table keyed by model tag; a lookup for a model already chosen",
    "src/llm_router/router.py":
        "_SLOW_MODEL_MARKERS: a guard that keeps escalation OUT of slow models; selects nothing",
}

# Files that used to pick a model by name. They may not carry ANY family string literal.
ACTING_FILES = [
    "src/llm_router/ensemble.py",
    "src/llm_router/judge.py",
    "src/llm_router/context_summary.py",
    "src/llm_router/agentic/react.py",
    "src/llm_router/library/sealer.py",
    "src/llm_router/tools/local_task.py",
    "src/llm_router/agentic_registry.py",
    "src/llm_router/hooks/direct_executor.py",
    "src/llm_router/model_discovery.py",
    "src/llm_router/policy.py",
]


def _code_strings(path: Path) -> list[tuple[int, str]]:
    """String literals in code: not comments, not triple-quoted docstrings."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(path.read_text(errors="ignore")).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []
    out = []
    for t in toks:
        if t.type == tokenize.STRING and not t.string.lstrip("rbfuRBFU").startswith(('"""', "'''")):
            out.append((t.start[0], t.string))
    return out


def _yaml_lines(path: Path) -> list[tuple[int, str]]:
    """Non-comment YAML lines, skipping prose block scalars (description: |)."""
    out = []
    in_block = False
    for i, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.match(r"^\s*(description|notes?)\s*:\s*[|>]", line):
            in_block = True               # prose block scalar: documentation, not a chain
            continue
        if in_block:
            if line.startswith("  ") and not re.match(r"^\s*-\s", line):
                continue
            in_block = False
        out.append((i, stripped))
    return out


def scan_tag_literals(root: Path = ROOT) -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    py = [p for base in (root / "src", root / "hooks") if base.is_dir() for p in base.rglob("*.py")]
    for p in sorted(py):
        hits = [(n, s) for n, s in _code_strings(p) if TAG.search(s)]
        if hits:
            found[str(p.relative_to(root))] = hits
    yamls = list((root / "src" / "llm_router" / "policies").glob("*.yaml")) + \
        list((root / "config").glob("*.yaml"))
    for p in sorted(yamls):
        hits = [(n, s) for n, s in _yaml_lines(p) if TAG.search(s)]
        if hits:
            found[str(p.relative_to(root))] = hits
    return found


def test_no_local_model_tag_literal_outside_the_data_allowlist():
    found = scan_tag_literals()
    assert found, "the scan found nothing at all: it is not looking at the tree"
    bad = {k: v[:3] for k, v in found.items() if k not in ALLOWLIST}
    assert not bad, (
        "local-model names in acting code/defaults (D-205). Resolve through "
        f"model_discovery.local_model() or `ollama/@local`: {bad}")


def test_allowlist_has_no_stale_entries():
    found = scan_tag_literals()
    stale = [k for k in ALLOWLIST if k not in found]
    assert not stale, f"allowlist entries that match nothing (remove them): {stale}"


@pytest.mark.parametrize("rel", ACTING_FILES)
def test_acting_files_carry_no_model_family_literal(rel):
    strings = _code_strings(ROOT / rel)
    assert strings, f"{rel}: no string literals scanned"
    hits = [(n, s) for n, s in strings if BARE_FAMILY.search(s)]
    assert not hits, f"{rel} names a model family in code: {hits[:3]}"


def test_bundled_policy_chains_name_no_local_model():
    files = sorted((SRC / "policies").glob("*.yaml"))
    assert files
    seen = 0
    for p in files:
        for n, line in _yaml_lines(p):
            m = re.match(r"^-\s*(ollama/\S+)", line)
            if m:
                seen += 1
                assert m.group(1) == "ollama/@local", f"{p.name}:{n} names {m.group(1)}"
    assert seen, "no ollama chain entries scanned: the check is empty"


def test_the_scanner_flags_a_literal_and_ignores_comments_and_docstrings(tmp_path):
    f = tmp_path / "m.py"
    f.write_text('"""doc ollama/qwen3.5:latest"""\n# c = "gemma4:latest"\nx = "ollama/qwen3.5:latest"\n'
                 'def g():\n    """qwen3:8b"""\n')
    hits = _code_strings(f)
    assert [s for _, s in hits if TAG.search(s)] == ['"ollama/qwen3.5:latest"']


# ── behaviour ──────────────────────────────────────────────────────────────

@pytest.fixture
def quiet(monkeypatch):
    """No user setting, no env inventory, an EMPTY discovered inventory."""
    from llm_router import model_discovery as md
    for k in ("LLM_ROUTER_OLLAMA_MODEL", "LLM_ROUTER_ENSEMBLE_PRIMARY",
              "LLM_ROUTER_ENSEMBLE_SECONDARY", "LLM_ROUTER_SUMMARY_MODEL",
              "LLM_ROUTER_LIBRARIAN_MODEL", "OLLAMA_BUDGET_MODELS", "OLLAMA_MODELS"):
        monkeypatch.delenv(k, raising=False)
    state = {"inv": []}
    monkeypatch.setattr(md, "_inventory", lambda allow_probe=False: list(state["inv"]))
    monkeypatch.setattr(md, "available_ollama_models", lambda allow_probe=True: list(state["inv"]))
    return state


def test_empty_inventory_no_acting_path_returns_a_model_name(quiet, monkeypatch):
    from llm_router import context_summary, ensemble, judge, model_discovery as md
    from llm_router.agentic import react
    from llm_router.library import sealer

    assert md.local_model() is None
    assert ensemble.primary_model() == "" and ensemble.secondary_model() == ""
    assert react._default_local_model() == ""
    assert sealer._default_librarian_model() == ""
    assert context_summary.pick_model([]) is None
    monkeypatch.setattr("llm_router.discover.is_ollama_available", lambda: True, raising=False)
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models", lambda: [], raising=False)
    assert judge._select_judge_model("ollama/whatever") is None


def test_empty_inventory_local_task_is_blocked_not_guessed(quiet, tmp_path):
    from llm_router.tools import local_task as lt
    out = json.loads(asyncio.run(lt.llm_local_task(objective="x", workdir=str(tmp_path))))
    assert out["status"] == lt.BLOCKED and "no local model" in out["reason"]
    assert not hasattr(lt, "DEFAULT_MODEL")


def test_empty_inventory_policy_placeholder_is_dropped(quiet):
    from llm_router.policy import _resolve_chain
    assert _resolve_chain(["ollama/@local", "openai/gpt-4o-mini"]) == ["openai/gpt-4o-mini"]


def test_best_agentic_model_names_no_family_by_default():
    import inspect
    from llm_router import agentic_registry
    assert inspect.signature(agentic_registry.best_agentic_model).parameters["prefer"].default == ()


def test_user_setting_is_used_by_every_acting_path(quiet, monkeypatch):
    from llm_router import context_summary, ensemble, judge
    from llm_router.agentic import react
    from llm_router.library import sealer
    from llm_router.policy import _resolve_chain

    quiet["inv"] = ["first:1b", "mine:7b"]
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", "mine:7b")
    assert ensemble.primary_model() == "ollama/mine:7b"
    assert ensemble.secondary_model() == "ollama/first:1b"
    assert react._default_local_model() == "mine:7b"
    assert sealer._default_librarian_model() == "mine:7b"
    assert context_summary.pick_model(["first:1b", "mine:7b"]) == "mine:7b"
    assert _resolve_chain(["ollama/@local"]) == ["ollama/mine:7b"]
    monkeypatch.setattr("llm_router.discover.is_ollama_available", lambda: True, raising=False)
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models",
                        lambda: ["ollama/first:1b", "ollama/mine:7b"], raising=False)
    assert judge._select_judge_model("ollama/other") == "ollama/mine:7b"


def test_discovered_model_is_used_when_there_is_no_setting(quiet):
    from llm_router import ensemble
    from llm_router.agentic import react
    from llm_router.library import sealer
    from llm_router.policy import _resolve_chain

    quiet["inv"] = ["found:3b"]
    assert ensemble.primary_model() == "ollama/found:3b"
    assert react._default_local_model() == "found:3b"
    assert sealer._default_librarian_model() == "found:3b"
    assert _resolve_chain(["ollama/@local"]) == ["ollama/found:3b"]


def test_local_task_uses_the_user_model(quiet, tmp_path, monkeypatch):
    from llm_router.tools import local_task as lt
    seen = {}

    async def fake_run(objective, root, check, model, *a, **k):
        seen["model"] = model
        return json.dumps({"status": lt.PROPOSED})

    monkeypatch.setattr(lt, "_local_task_run", fake_run)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", "mine:7b")
    out = json.loads(asyncio.run(lt.llm_local_task(objective="x", workdir=str(tmp_path))))
    assert seen["model"] == "mine:7b" and out["status"] == lt.PROPOSED
