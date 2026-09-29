"""Phase 0.1 import boundary: the release-time outcome audit must never become
part of the end-user code path.

Two directions, both enforced:

1. Nothing under ``src/llm_router/`` imports ``outcome_audit`` or
   ``outcome_gate`` — not by ``import``, not by ``importlib`` path loading
   (path loading is a pattern this repo uses, so a plain ``import`` scan
   would miss it).
2. The audit and the gate do not import ``llm_router`` and do not put
   ``src/`` on ``sys.path``. A release check that runs the product's own
   readers inherits the product's own bugs (northstar's dispatch-time
   ``used``, file-mtime windowing) instead of checking them. The one
   exception is ``SHARED_RULE_MODULES``: the two stdlib-only rule owners the
   audit always reused, which moved from ``scripts/`` into ``src/`` in Phase
   0.2d. They are path-loaded (never imported as ``llm_router``), and
   ``test_shared_rule_modules_stay_stdlib_leaves`` pins that they pull in
   nothing from the product.

The detector is itself checked against a synthetic violating tree, because a
scan that finds nothing has not been shown to work.
"""
from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "llm_router"
AUDIT_FILES = [
    REPO / "scripts" / "release" / "outcome_audit.py",
    REPO / "scripts" / "release" / "outcome_gate.py",
]
FORBIDDEN_NAMES = ("outcome_audit", "outcome_gate")
#: src/ files the audit may path-load. Adding one needs the same argument as
#: these: a stdlib-only rule owner, not a product reader.
SHARED_RULE_MODULES = frozenset({
    "src/llm_router/groundtruth_sources.py",
    "src/llm_router/edit_survival.py",
})


def _violations_in_src(root: pathlib.Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = [node.value]
            for n in names:
                if any(f in n for f in FORBIDDEN_NAMES):
                    found.append(f"{path.relative_to(root.parent)}:{node.lineno}: {n!r}")
    return found


def _product_imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] == "llm_router"]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "llm_router":
                found.append(node.module)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # a sys.path.insert(..., ".../src") or a path-load of a src/ module
            v = node.value.strip("/")
            if v in SHARED_RULE_MODULES:
                continue
            if v == "src" or v.startswith("src/llm_router"):
                found.append(f"string {node.value!r}")
    return found


def test_src_never_imports_the_outcome_audit():
    assert SRC.is_dir()
    assert _violations_in_src(SRC) == []


def test_the_audit_and_gate_exist_and_never_import_the_product():
    for f in AUDIT_FILES:
        assert f.is_file(), f"{f} is missing"
        assert _product_imports(f) == [], f"{f.name} reaches into src/"


def test_src_detector_fires_on_a_violating_tree(tmp_path):
    pkg = tmp_path / "llm_router"
    pkg.mkdir()
    (pkg / "ok.py").write_text("import json\n")
    (pkg / "bad_import.py").write_text("from scripts.release import outcome_audit\n")
    (pkg / "bad_pathload.py").write_text(
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('x', 'scripts/release/outcome_gate.py')\n"
    )
    hits = _violations_in_src(pkg)
    assert len(hits) == 2
    assert any("bad_import.py" in h for h in hits)
    assert any("bad_pathload.py" in h for h in hits)


def test_product_import_detector_fires(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("import sys\nsys.path.insert(0, 'src')\nfrom llm_router import northstar\n")
    assert len(_product_imports(bad)) == 2


def test_shared_rule_modules_stay_stdlib_leaves():
    """The SHARED_RULE_MODULES exemption is only safe while path-loading one
    of them cannot drag product code into the audit process."""
    for rel in sorted(SHARED_RULE_MODULES):
        path = REPO / rel
        assert path.is_file(), f"{rel} is missing"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{rel}:{node.lineno}: relative import"
                assert (node.module or "").split(".")[0] != "llm_router", (
                    f"{rel}:{node.lineno}: imports llm_router")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name.split(".")[0] != "llm_router", (
                        f"{rel}:{node.lineno}: imports llm_router")


def test_product_import_detector_still_fires_on_other_src_paths(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("p = 'src/llm_router/northstar.py'\nq = 'src/llm_router/edit_survival.py'\n")
    assert _product_imports(bad) == ["string 'src/llm_router/northstar.py'"]
