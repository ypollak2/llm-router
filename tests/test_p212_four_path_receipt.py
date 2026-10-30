"""P2.12-a1 (P212-5, FABLE W2-4): the four install paths leave the same receipt.

Each path runs on its own twin fixture HOME with the same detected hosts:

* ``cli``    -- ``llm-router install --auto``: ``install.cmd_install(["--auto"])``.
* ``npm``    -- the real ``npm/install.js`` ``runInstaller`` under node, with ``bin`` a shim
  that runs ``llm_router.cli:main`` (the entry point the shipped binary is built from) in a
  child Python under the fixture HOME. No download, no npx: the shim stands in for the
  binary the postinstall already fetched, so the argv npm passes is the argv tested.
* ``plugin`` -- the plugin's SessionStart hook (``hooks/plugin-install.py``, the file the
  plugin runs) with consent given and a synchronous spawner that runs the argv it would
  hand to npx (``install --auto --via-plugin``) through ``install.cmd_install``.
* ``nl``     -- ``llm_router_admin(action="install")``: plan, then confirm with the token.

"Same receipt" is the one in the P212 scope (§2): an identical digest (package_version and
each host's status and hook_version), the same host rows, and per-host fields equal except
``via``, which may differ only for claude-code on the plugin path, where it must be
``plugin`` (the plugin provides those hooks) instead of ``settings``. ``ts`` is outside the
rows and may differ. Each HOME also passes the P2.12-c undeclared-change check on its state
dir, and settings.json never registers an llm-router hook twice.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import llm_router
from llm_router import host_detect, seats
from llm_router import install_hooks as ih
from llm_router.commands import install
from llm_router.tools import consolidated
from tests.fixtures.dyn import homes
from tests.test_npm_wrapper import _NO_NET, _node
from tests.test_p212_preservation_floor import _tree, undeclared_changes

REPO = Path(__file__).resolve().parent.parent
PATHS = ("cli", "npm", "plugin", "nl")
#: Detected on every twin HOME. Three are wired with hook scripts; the other three cover
#: each not_wired branch of run_full: a snippet host (cursor), a host that cannot be wired
#: globally (windsurf), one wired with an MCP config only (pi, P212-6 / D-315) and an
#: unsupported one (kimi).
HOSTS = ("claude-code", "codex", "gemini-cli", "cursor", "windsurf", "pi", "kimi")
#: pi's expected receipt status: wired (MCP config only, P212-6). Its row has hook_version None
#: and an extra `smoke` field (``skipped``: install._pi_binary is stubbed in _twin and in the
#: npm child _BOOT, so a machine with a real pi never runs it).
PI_STATUS = "wired"
WIRED = {"claude-code", "codex", "gemini-cli"}  # wired WITH hook scripts (hook_version set)
MCP_ONLY = {"pi"}  # wired, hook_version None
ROW_FIELDS = ("status", "reason", "via", "hook_version")
PI_SMOKE_SKIPPED = {"status": "skipped", "detail": "pi binary not on PATH"}


def _fields(host: str) -> tuple[str, ...]:
    return ROW_FIELDS + ("smoke",) if host in MCP_ONLY else ROW_FIELDS
#: §2's only allowed per-row difference: {(path, host): {field: (expected on that path, elsewhere)}}
ALLOWED = {("plugin", "claude-code"): {"via": ("plugin", "settings")}}


# ── comparison (§2) ────────────────────────────────────────────────────────────

def receipt_differences(receipts: dict[str, dict | None], base: str = "cli") -> list[str]:
    """One line per way a path's receipt is not §2's same receipt as ``base``'s, each line
    naming the path. [] when all are the same."""
    out: list[str] = []
    ref = receipts.get(base)
    if not ref:
        return [f"{base}: no receipt"]
    for name, got in receipts.items():
        if not got:
            out.append(f"{name}: no receipt")
            continue
        out += [f"{name}: {f}" for f in install.receipt_failures(got)]
        for key in ("schema", "package_version", "digest"):
            if got.get(key) != ref.get(key):
                out.append(f"{name}: {key} {got.get(key)!r} != {base} {ref.get(key)!r}")
        if not got.get("ts"):
            out.append(f"{name}: no ts")
        if set(got.get("hosts") or {}) != set(ref.get("hosts") or {}):
            out.append(f"{name}: hosts {sorted(got.get('hosts') or {})} != {base} {sorted(ref['hosts'])}")
        for host in sorted(set(got.get("hosts") or {}) & set(ref.get("hosts") or {})):
            row, ref_row = got["hosts"][host], ref["hosts"][host]
            if set(row) != set(_fields(host)):
                out.append(f"{name}: hosts[{host}] fields {sorted(row)} != {sorted(_fields(host))}")
            allowed = ALLOWED.get((name, host), {})
            for field in _fields(host):
                if field in allowed:
                    want_here, want_base = allowed[field]
                    if (row.get(field), ref_row.get(field)) != (want_here, want_base):
                        out.append(f"{name}: hosts[{host}].{field} {row.get(field)!r} (want {want_here!r}), "
                                   f"{base} {ref_row.get(field)!r} (want {want_base!r})")
                elif row.get(field) != ref_row.get(field):
                    out.append(f"{name}: hosts[{host}].{field} {row.get(field)!r} != {base} {ref_row.get(field)!r}")
        for (path, host), fields in ALLOWED.items():  # the difference §2 allows must be there
            if name == path and host not in (got.get("hosts") or {}):
                out.append(f"{name}: hosts[{host}] missing, so {sorted(fields)} could not be checked")
    return out


# ── twin HOMEs ─────────────────────────────────────────────────────────────────

def _twin(root: Path, monkeypatch) -> dict:
    """A HOME with HOSTS present and a 15.4.0-shaped state dir (the P2.12-c ``upgrade``
    fixture: routing.yaml, profile.yaml, .env, ... that the user owns). Points the process at
    it: HOME, ``Path.home``, ``LLM_ROUTER_HOME``, the cwd; clears the variables that would
    reach a real host dir or a plugin; pins host detection to the HOME (not the developer's
    PATH) and replaces the seat probe."""
    root.mkdir(parents=True)
    state = homes.make_home(root, "upgrade")
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
    for var in ("LLM_ROUTER_CLAUDE_DIR", "XDG_CONFIG_HOME", "CLAUDE_PLUGIN_ROOT",
                "LLM_ROUTER_NO_AUTO_INSTALL", "npm_command"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(root)
    detect = host_detect.detect_hosts
    monkeypatch.setattr(host_detect, "detect_hosts",
                        lambda **k: detect(**{"home": root, "which": lambda b: None, **k}))
    monkeypatch.setattr(seats, "refresh_seats",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no seat probe in tests")))
    monkeypatch.setattr(install, "_pi_binary", lambda: None)  # the pi smoke never runs a real pi
    for host in HOSTS:
        d = host_detect._config_dir(host_detect._HOSTS[host][1], root, sys.platform, os.environ)
        d.mkdir(parents=True, exist_ok=True)
        (d / "host-own-state.json").write_text("{}")
    owned = homes.user_owned_files(state)
    return {"root": root, "state": state, "owned": {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in owned},
            "tree": _tree(state)}


#: Runs in the child Python the npm shim starts: the same conditions as the in-process paths,
#: then the binary's entry point with the argv npm passed. The in-process paths get the
#: conftest's ``_hermetic_reachability`` (:11434 refused) and so never start the agentic
#: registry probe; this child has no conftest, so it gets the same block here, plus
#: ``agentic_registry.populate_in_background`` stubbed to False and a refusal for any
#: ``agentic_registry`` subprocess. An audit hook records any :11434 connect that gets past
#: the block. Without them, on a machine running Ollama the child's
#: ``install_hooks.install()`` started a detached ``python -m llm_router.agentic_registry``
#: that outlived the test and ran live inference (review of #796). Every refusal is written
#: to $P212_5_EVENTS so the test can assert on it.
_BOOT = """\
import json, os, socket, subprocess, sys
from pathlib import Path

def _event(kind, detail=""):
    with open(os.environ["P212_5_EVENTS"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": kind, "detail": str(detail)}) + "\\n")

def _audit(event, args):  # independent of the block below: a :11434 connect that got through
    if event == "socket.connect" and isinstance(args[1], tuple) and len(args[1]) >= 2 and args[1][1] == 11434:
        _event("ollama_connected", args[1])
sys.addaudithook(_audit)

_real_connect = socket.socket.connect
def _connect(self, address):
    try:
        port = int(address[1]) if isinstance(address, tuple) and len(address) >= 2 else None
    except (TypeError, ValueError):
        port = None
    if port == 11434:
        _event("ollama_blocked", address)
        raise OSError("live Ollama connection blocked in tests (localhost:11434)")
    return _real_connect(self, address)
socket.socket.connect = _connect

_RealPopen = subprocess.Popen
class _Popen(_RealPopen):
    def __init__(self, args, *a, **k):
        if "agentic_registry" in " ".join(map(str, args if isinstance(args, (list, tuple)) else [args])):
            _event("agentic_registry_spawn_refused", args)
            raise OSError("agentic_registry subprocess refused in tests")
        super().__init__(args, *a, **k)
subprocess.Popen = _Popen

from llm_router import agentic_registry
def _no_registry_probe():
    _event("populate_in_background_stubbed")
    return False
agentic_registry.populate_in_background = _no_registry_probe

from llm_router import host_detect, seats
_home = Path.home()
_detect = host_detect.detect_hosts
host_detect.detect_hosts = lambda **k: _detect(**{"home": _home, "which": lambda b: None, **k})
def _no_probe(*a, **k):
    raise OSError("no seat probe in tests")
seats.refresh_seats = _no_probe
from llm_router.commands import install as _install
_install._pi_binary = lambda: None  # the pi smoke never runs a real pi
from tests import _real_home_guard  # the suite's real-HOME tripwire, in this process too
_real_home_guard.install()
from llm_router.cli import main
sys.argv = ["llm-router", *sys.argv[1:]]
try:
    main()
finally:
    if _real_home_guard.VIOLATIONS:
        print(f"touched the real HOME: {_real_home_guard.VIOLATIONS}", file=sys.stderr)
        sys.exit(3)
"""


def _receipt_in(home: dict) -> dict | None:
    """The receipt in this HOME's configured state dir, read by path and not through the
    process env: a path that dropped LLM_ROUTER_HOME on the way would write it elsewhere."""
    return install.read_receipt(home["state"] / install.RECEIPT_NAME)


def _run_cli(home: dict, monkeypatch, tmp: Path) -> dict | None:
    assert install.cmd_install(["--auto"]) == 0
    return _receipt_in(home)


def _child_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _run_npm(home: dict, monkeypatch, tmp: Path) -> dict | None:
    node = _node()
    tmp.mkdir(exist_ok=True)
    pkg = tmp / "pkg"  # npm runs a postinstall in the package dir; a copy keeps the repo's npm/ clean
    pkg.mkdir()
    for name in ("install.js", "package.json"):
        (pkg / name).write_bytes((REPO / "npm" / name).read_bytes())
    events = tmp / "child-events.jsonl"
    boot = tmp / "npm-boot.py"
    boot.write_text(_BOOT)
    shim = tmp / "llm-router"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{boot}" "$@"\n')
    shim.chmod(0o755)
    (tmp / "no_net.js").write_text(_NO_NET)
    pkg_src = str(Path(llm_router.__file__).resolve().parent.parent)
    env = {**os.environ, "npm_command": "install", "P212_5_EVENTS": str(events),  # `npm i -g`, not npx
           "PYTHONPATH": os.pathsep.join(p for p in (pkg_src, str(REPO), os.environ.get("PYTHONPATH", "")) if p)}
    code = "process.exitCode = require(process.argv[1]).runInstaller(process.argv[2], process.env);"
    res = subprocess.run([node, "-r", str(tmp / "no_net.js"), "-e", code, str(pkg / "install.js"), str(shim)],
                         cwd=pkg, env=env, capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stdout + res.stderr  # postinstall exits 0 even when wiring fails,
    assert "WARNING" not in res.stderr, res.stderr       # so the installer's own failure shows here
    kinds = [e["kind"] for e in _child_events(events)]
    # the install reached the registry probe's spawn point and the stub stopped it ...
    assert "populate_in_background_stubbed" in kinds, kinds
    # ... nothing else started the probe process, and no :11434 connect got through
    assert "agentic_registry_spawn_refused" not in kinds, _child_events(events)
    assert "ollama_connected" not in kinds, _child_events(events)
    assert set(kinds) <= {"populate_in_background_stubbed", "ollama_blocked"}, kinds
    receipt = _receipt_in(home)
    if receipt is not None:  # the postinstall echoes the line of the receipt it left
        assert f"llm-routing: receipt: {receipt['digest'][:8]} " in res.stdout, res.stdout + res.stderr
    return receipt


def _plugin_hook():
    spec = importlib.util.spec_from_file_location("p212_5_plugin_install", REPO / "hooks" / "plugin-install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_plugin(home: dict, monkeypatch, tmp: Path) -> dict | None:
    """Claude Code sets CLAUDE_PLUGIN_ROOT for plugin hooks; the plugin is this repo (its
    hooks/ ships the bundled hook copies). Consent was given earlier (``/llm-router:setup``)."""
    version = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())["version"]
    calls = []

    def spawn_now(argv, env, cwd):
        calls.append(argv)
        assert argv == ["npx", "-y", f"llm-routing@{version}", "install", "--auto", "--via-plugin"], argv
        for k in set(os.environ) - set(env):  # run with exactly the env the hook passes
            monkeypatch.delenv(k)
        for k, v in env.items():
            if os.environ.get(k) != v:
                monkeypatch.setenv(k, v)
        monkeypatch.chdir(cwd)
        # npx runs the npm postinstall first; under npx (npm_command=exec) it wires no host
        # (tests/test_npm_wrapper.py), then the binary, whose cli.main hands
        # cmd_install the arguments after `install`.
        assert install.cmd_install(argv[4:]) == 0, argv

    out = io.StringIO()
    assert _plugin_hook().run({**os.environ, "CLAUDE_PLUGIN_ROOT": str(REPO)}, spawn_now, out) == 0
    assert len(calls) == 1 and out.getvalue() == "", (calls, out.getvalue())
    return _receipt_in(home)


def _run_nl(home: dict, monkeypatch, tmp: Path) -> dict | None:
    plan = asyncio.run(consolidated.llm_router_admin("install"))
    assert _receipt_in(home) is None, "step 1 wrote a receipt"
    token = json.loads(re.search(r"value='(\{.*\})'", plan).group(1))["confirm"]
    asyncio.run(consolidated.llm_router_admin("install", json.dumps({"confirm": token})))
    return _receipt_in(home)


RUNNERS = {"cli": _run_cli, "npm": _run_npm, "plugin": _run_plugin, "nl": _run_nl}


def _settings_hook_commands() -> list[tuple[str, str]]:
    """(event, command) for every llm-router hook settings.json registers. One script under
    two events (session-end.py on Stop and SessionEnd) is by design; twice under one event
    would fire twice."""
    if not ih.settings_path().is_file():
        return []
    hooks = json.loads(ih.settings_path().read_text()).get("hooks") or {}
    return [(event, s) for event, groups in sorted(hooks.items()) for s in install._json_strings(groups)
            if "llm_router" in s or "llm-router" in s]


# ── the test ───────────────────────────────────────────────────────────────────

def test_four_install_paths_leave_the_same_receipt(tmp_path, monkeypatch):
    receipts: dict[str, dict | None] = {}
    problems: list[str] = []
    for name in PATHS:
        home = _twin(tmp_path / name, monkeypatch)
        if name == "plugin":  # consent given earlier (/llm-router:setup); not a change by this run
            install.consent_path().write_text('{"consent": true}\n')
            home["tree"] = _tree(home["state"])
        receipts[name] = RUNNERS[name](home, monkeypatch, tmp_path / f"{name}-tmp" if name == "npm" else tmp_path)
        changed = [str(p) for p, sha in home["owned"].items() if hashlib.sha256(p.read_bytes()).hexdigest() != sha]
        problems += [f"{name}: user-owned file changed: {p}" for p in changed]
        problems += [f"{name}: undeclared change in the state dir: {n}"
                     for n in undeclared_changes(home["tree"], _tree(home["state"]), "upgrade")]
        cmds = _settings_hook_commands()
        problems += [f"{name}: settings.json registers {c!r} more than once under {e}"
                     for e, c in sorted(set(cmds)) if cmds.count((e, c)) > 1]
        if name == "plugin" and cmds:
            problems.append(f"{name}: the plugin provides the hooks, yet settings.json registers {len(cmds)}")
        if name != "plugin" and not cmds:
            problems.append(f"{name}: settings.json registers no llm-router hook")

    # non-vacuity: four receipts, every detected host has a row, three wired with versions
    assert set(receipts) == set(PATHS) and all(receipts.values()), {k: bool(v) for k, v in receipts.items()}
    cli = receipts["cli"]
    assert sorted(cli["hosts"]) == sorted(HOSTS), cli["hosts"]
    assert {h for h, r in cli["hosts"].items() if r["status"] == "wired" and r["hook_version"]} == WIRED, cli["hosts"]
    assert cli["hosts"]["pi"] == {"status": PI_STATUS, "reason": None, "via": "files", "hook_version": None,
                                  "smoke": PI_SMOKE_SKIPPED}, cli["hosts"]["pi"]
    assert all(cli["hosts"][h]["reason"] for h in set(HOSTS) - WIRED - MCP_ONLY), cli["hosts"]
    assert cli["hosts"]["kimi"]["status"] == "not_wired", cli["hosts"]["kimi"]
    assert len(owned := homes.user_owned_files(tmp_path / "cli" / "upgrade")) >= 5, owned

    problems += receipt_differences(receipts)
    assert problems == [], "\n".join(problems)
    assert len({r["digest"] for r in receipts.values()}) == 1


# ── the comparison goes red, and names the path ────────────────────────────────

def _row(via: str | None = "settings", status: str = "wired", version: int | None = 7, reason=None) -> dict:
    return {"status": status, "reason": reason, "via": via,
            "hook_version": {"auto-route.py": version} if version is not None else None}


def _receipts(**override: dict) -> dict[str, dict]:
    base = {"claude-code": _row(), "codex": _row(via="files"), "kimi": _row(None, "not_wired", None, "unsupported"),
            "pi": {**_row("files", "wired", None), "smoke": dict(PI_SMOKE_SKIPPED)}}
    rows = {name: json.loads(json.dumps(base)) for name in PATHS}
    rows["plugin"]["claude-code"]["via"] = "plugin"
    for name, hosts in override.items():
        rows[name].update(hosts)
    return {name: install.build_receipt(r, package_version="9.9.9") for name, r in rows.items()}


def test_comparison_accepts_the_one_difference_section_2_allows():
    assert receipt_differences(_receipts()) == []


@pytest.mark.parametrize("override,expect", [
    ({"nl": {"codex": _row(None, "not_wired", None, "skipped: --no-hosts")}}, "nl: digest"),
    ({"npm": {"codex": _row(via="settings")}}, "npm: hosts[codex].via"),
    ({"npm": {"kimi": _row(None, "not_wired", None, "other reason")}}, "npm: hosts[kimi].reason"),
    ({"npm": {"pi": {**_row("files", "wired", None), "smoke": {"status": "passed", "detail": "x"}}}},
     "npm: hosts[pi].smoke"),
    ({"nl": {"pi": _row("files", "wired", None)}}, "nl: hosts[pi] fields"),
    ({"plugin": {"claude-code": _row(via="settings")}}, "plugin: hosts[claude-code].via 'settings' (want 'plugin')"),
    ({"cli": {"claude-code": _row(via="plugin")}}, "plugin: hosts[claude-code].via 'plugin' (want 'plugin'), cli 'plugin'"),
    ({"nl": {"claude-code": _row(via="plugin")}}, "nl: hosts[claude-code].via 'plugin' != cli 'settings'"),
    ({"plugin": {"cursor": _row(None, "not_wired", None, "x")}}, "plugin: hosts ["),
])
def test_comparison_is_red_on_each_difference_and_names_the_path(override, expect):
    diffs = receipt_differences(_receipts(**override))
    assert any(d.startswith(expect) for d in diffs), diffs


def test_comparison_is_red_on_a_missing_receipt():
    receipts = _receipts()
    receipts["npm"] = None
    assert receipt_differences(receipts) == ["npm: no receipt"]
