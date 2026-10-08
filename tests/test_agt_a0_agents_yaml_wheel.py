"""AGT A.0 (A.0-a, clause 1): agents.yaml is found from an INSTALLED wheel.

The bug: ``tools/agents._default_config_path`` fell back to
``<site-packages>/../../config/agents.yaml``, a repo-root file that no wheel
contains. From a source checkout every test passed; for every installed user the
registry was empty and ``llm_router_agent_start_session("code-reviewer")``
returned ``agent_not_found``.

Only building the package and installing it shows this, so the test does exactly
that: ``uv build`` (sdist, then the wheel FROM the sdist, as a release does),
install the wheel into a fresh venv, and call the real tool from a cwd outside
the repo with an isolated HOME. Dependencies come from the running interpreter's
site-packages through a ``.pth`` line, which sys.path appends AFTER the venv's own
site-packages, so ``llm_router`` itself is imported from the wheel; the test
asserts that too, otherwise it would prove nothing.
"""
from __future__ import annotations

import json
import os
import shutil
import site
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

_PROBE = r"""
import asyncio, json, llm_router
from llm_router.tools import agents
res = asyncio.run(agents.llm_router_agent_start_session("code-reviewer"))
print(json.dumps({
    "pkg_file": llm_router.__file__,
    "config_path": str(agents._default_config_path()),
    "ids": agents.get_registry().list_ids(),
    "start": res,
}))
"""


def _run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=600, **kw)


@pytest.mark.timeout(900)
def test_agents_yaml_found_from_installed_wheel(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available to build the wheel")

    dist = tmp_path / "dist"
    build = _run([uv, "build", "--out-dir", str(dist)], cwd=str(REPO))
    assert build.returncode == 0, build.stderr[-2000:]
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"

    venv = tmp_path / "venv"
    mk = _run([uv, "venv", "--python", sys.executable, str(venv)])
    assert mk.returncode == 0, mk.stderr[-2000:]
    vpy = venv / "bin" / "python"
    inst = _run([uv, "pip", "install", "--python", str(vpy), "--no-deps", str(wheels[0])])
    assert inst.returncode == 0, inst.stderr[-2000:]

    # Dependencies (yaml, aiosqlite, ...) from this interpreter, AFTER the venv.
    vsite = Path(_run([str(vpy), "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"]
                      ).stdout.strip())
    dep_dirs = {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"],
                *site.getsitepackages()}
    (vsite / "zz_test_deps.pth").write_text("\n".join(sorted(dep_dirs)) + "\n")

    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "LLM_ROUTER_AGENTS_CONFIG", "VIRTUAL_ENV")}
    env["HOME"] = str(home)
    probe = _run([str(vpy), "-c", _PROBE], cwd=str(work), env=env)
    assert probe.returncode == 0, probe.stderr[-3000:]
    out = json.loads(probe.stdout.strip().splitlines()[-1])
    print(f"\nA.0-a wheel={wheels[0].name} pkg={out['pkg_file']} "
          f"config={out['config_path']} ids={out['ids']} start={out['start']}")

    assert Path(out["pkg_file"]).resolve().is_relative_to(venv.resolve()), (
        "llm_router was not imported from the installed wheel; the check is void")
    assert Path(out["config_path"]).resolve().is_relative_to(venv.resolve())
    assert {"code-reviewer", "trend-researcher", "tdd-guide"} <= set(out["ids"])
    assert out["start"].get("error") is None, out["start"]
    assert out["start"]["agent_id"] == "code-reviewer"
