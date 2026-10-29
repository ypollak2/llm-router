"""Install command — global hook and MCP server installation."""

from __future__ import annotations

import os
import platform
import shutil
import sys
import time
from pathlib import Path
from llm_router.tool_surface import route_tool  # CHZ-SURF-01: never print a raw tool name

from llm_router import paths


# ── ANSI helpers (respect NO_COLOR / non-tty) ─────────────────────────────────

def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _color_enabled() else s


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


def _ok(label: str) -> str:
    return f"  {_green('✓')}  {label}"


def _warn(label: str) -> str:
    return f"  {_yellow('⚠')}  {label}"


def _fail(label: str, fix: str | None = None) -> str:
    line = f"  {_red('✗')}  {label}"
    if fix:
        line += f"\n       {_yellow('→')} {fix}"
    return line


# ── Subscription mode auto-enable ───────────────────────────────────────────────
#
# North Star #2 (flat-rate subscribers first, zero workflow change): a machine
# that is already logged in to Claude Code should not need to hand-set
# LLM_ROUTER_CLAUDE_SUBSCRIPTION=true (read by discover.get_available_providers())
# before subscription mode takes effect. The installer sets it the first time it
# sees a logged-in Claude seat, but only ever fills an UNSET value — an explicit
# value, true or false, set either in the process environment or already
# persisted to the state .env, is never touched. Detection that fails or comes
# back unknown is not treated as a subscription: unknown must not be the
# favourable answer.
_SUBSCRIPTION_ENV_VAR = "LLM_ROUTER_CLAUDE_SUBSCRIPTION"


def _maybe_enable_claude_subscription(found) -> None:
    """``found`` is the detected ``Seats`` (see ``llm_router.seats``), or ``None``
    when detection itself failed. Writes ``LLM_ROUTER_CLAUDE_SUBSCRIPTION=true``
    to the state ``.env`` (the same file ``llm-router setup``/``onboard`` manage)
    exactly once, only when a Claude seat is present and the variable has never
    been set anywhere. Safe to call on every install — re-running is a no-op
    once the variable exists, which is what makes the upgrade path idempotent.
    """
    if found is None:
        print(_yellow(
            f"  Claude subscription status unknown (detection failed) — "
            f"{_SUBSCRIPTION_ENV_VAR} left unset"
        ))
        return

    if not found.claude.present:
        return  # no seat: nothing to enable

    env_path = paths.state_path(".env")
    existing: set[str] = set()
    if env_path.exists():
        # Hand-rolled on purpose, matching `commands/setup.py`'s existing .env
        # parsing — no new third-party dependency for one key lookup.
        for line in env_path.read_text().splitlines():
            line = line.strip()
            # A shell-style `export VAR=value` line still counts as the user
            # having set VAR. Without stripping this, `export
            # LLM_ROUTER_CLAUDE_SUBSCRIPTION=false` parsed to a key of
            # "export LLM_ROUTER_CLAUDE_SUBSCRIPTION" — not the real
            # variable name — so this check never saw it as set, the
            # installer appended a contradictory `LLM_ROUTER_CLAUDE_SUBSCRIPTION=true`
            # line right below it, and the hook's own dotenv loader
            # (`hooks/auto-route.py::_load_dotenv`, same "export" blind spot)
            # then read that second, unprefixed line as the real value —
            # silently overriding the user's explicit false. Reproduced live
            # by review on PR #151.
            if line.startswith("export ") or line.startswith("export\t"):
                line = line[len("export"):].lstrip()
            if line and not line.startswith("#") and "=" in line:
                existing.add(line.partition("=")[0].strip())

    # Never override a value the user set explicitly, in the process
    # environment or already persisted to the state .env — an explicit
    # LLM_ROUTER_CLAUDE_SUBSCRIPTION=false must stay false.
    if _SUBSCRIPTION_ENV_VAR in os.environ or _SUBSCRIPTION_ENV_VAR in existing:
        return

    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        with open(env_path, "a") as f:
            f.write(f"\n{_SUBSCRIPTION_ENV_VAR}=true\n")
    except OSError as exc:
        from llm_router import failopen
        failopen.record("CHZ-FO-INSTALL-SUB-ENV-WRITE", exc)
        print(_yellow(
            f"  could not write {_SUBSCRIPTION_ENV_VAR}=true to {env_path} — "
            "subscription mode not enabled automatically"
        ))
        return
    print(_green(
        f"  ✓ Claude subscription seat detected — {_SUBSCRIPTION_ENV_VAR}=true "
        f"(set {_SUBSCRIPTION_ENV_VAR}=false to turn off)"
    ))


# ── Command entry point ────────────────────────────────────────────────────────

_INSTALL_HELP = """\
llm-router install — install LLM Router routing into a host

Usage:
  llm-router install                     Install for Claude Code (hooks + rules + MCP)
  llm-router install --host <name>       Install/print config for a specific host
                                     (claude-code, claude-desktop, cursor, copilot,
                                     windsurf, gemini-cli, codex, …)
  llm-router install --mode <mode>       Install mode (auto | gateway)
  llm-router install --no-hosts          Claude Code only; skip other detected hosts (Codex, Gemini CLI)
  llm-router install --auto              Full install with no prompts (for npm postinstall, plugins,
                                     scripts). Writes install_receipt.json in the state dir, prints
                                     one `receipt:` line, exits 1 if any host failed to install
  llm-router install --auto --via-plugin The Claude Code plugin already provides the hooks and MCP
                                     entry: wire the other hosts, leave settings.json hooks alone
                                     (the plugin's SessionStart hook runs this, after consent;
                                     the installer also does this by itself when it finds the plugin)
  llm-router install --no-plugin         With --auto: wire Claude Code hooks from the CLI even if the
                                     plugin is installed (you then have them twice; for plugin removal)
  llm-router install --project           Write AGENTS.md + CLAUDE.md (link) into the current
                                     repository so both agents read one set of rules
  llm-router install --proxy-default     Make the per-call proxy the DEFAULT ANTHROPIC_BASE_URL
                                     for every Claude Code session (docs/proxy.md).
                                     Verifies the proxy answers before touching settings.json.
  llm-router install --proxy-default off Revert it (same as `llm-router uninstall`)
  llm-router install --authority route|advise
                                     Install the proxy default (with its fail-open shim) AND
                                     set the routing authority (`llm-router mode`). Plain
                                     `install` leaves routing authority off.
  llm-router install --no-authority      Never turn the proxy default on from this install
  llm-router install --schedule-daily    Also install the daily KPI job (asked [Y/n] on a terminal;
                                     headless installs write it only with this flag)
  llm-router install --no-schedule       Never install the daily KPI job from this install
  llm-router install --help, -h          Show this help and exit (no changes made)
"""


# Owner switch (D-104, PLAN-v16 P2.2): when True, a FRESH plain `llm-router install`
# (no proxy-default sentinel, no ANTHROPIC_BASE_URL in settings.json) also installs the
# proxy default with authority "route". It ships False: merging or deploying this code
# changes nothing on a live install until the owner runs `install --authority` (or
# flips this constant in a separate, owner-approved change).
PROXY_DEFAULT_ON_INSTALL = False

_AUTHORITY_HINT = (
    "Routing authority is OFF by default; run `llm-router install --authority route` "
    "(or advise) to enable it."
)


def _install_authority(mode: str) -> int:
    """The explicit proxy-default install (``cmd_proxy_default``: fail-open shim included;
    settings.json is touched only by that existing path, and only once the proxy answers),
    then the mode. A refused install returns its non-zero code and sets no mode."""
    from llm_router import authority
    from llm_router.commands.proxy_default import cmd_proxy_default

    rc = cmd_proxy_default("on")
    if rc != 0:
        print(f"  {_yellow('The routing authority was not set.')}\n")
        return rc
    authority.set_mode(mode)
    print(_ok(f"routing authority: {mode} (change it any time with `llm-router mode advise|route`)"))
    return 0


def _fresh_for_proxy_default() -> bool:
    """No sentinel, and settings.json (absent, or parseable) names no ANTHROPIC_BASE_URL."""
    import json

    from llm_router import proxy_default as pd
    from llm_router.install_hooks import settings_path

    if pd.read_sentinel() is not None:
        return False
    path = settings_path()
    if not path.exists():
        return True
    try:
        env = json.loads(path.read_text()).get("env")
    except (OSError, ValueError, AttributeError):
        return False  # unreadable: never treat as fresh
    return not (isinstance(env, dict) and env.get("ANTHROPIC_BASE_URL"))


def _flag_value(args: list[str], flag: str) -> str | None:
    idx = args.index(flag)
    return args[idx + 1].strip().lower() if idx + 1 < len(args) and not args[idx + 1].startswith("--") else None


def cmd_install(args: list[str]) -> int:
    """Entry point for install command."""
    # CHZ-PKG-007: `--help`/`-h` must be inert — previously it fell straight
    # through to a real install (file modifications) instead of printing usage.
    if any(a in ("--help", "-h", "help") for a in args):
        print(_INSTALL_HELP)
        return 0
    if "--auto" in args or "--via-plugin" in args:
        return _run_auto(args)
    if "--proxy-default" in args:
        # Handled here, not inside `_run_install`, so a refused install (the
        # proxy never answered — see commands/proxy_default.py) can actually
        # exit non-zero; `_run_install` returns None throughout and every
        # other path here discards it.
        idx = args.index("--proxy-default")
        sub = args[idx + 1].strip().lower() if idx + 1 < len(args) and not args[idx + 1].startswith("--") else "on"
        from llm_router.commands.proxy_default import cmd_proxy_default
        return cmd_proxy_default(sub)
    if "--authority" in args:
        from llm_router import authority

        value = _flag_value(args, "--authority")
        if value not in authority.MODES:
            print("llm-router install: --authority takes route or advise", file=sys.stderr)
            return 2
        if "--no-authority" in args:
            print("llm-router install: --authority and --no-authority contradict each other", file=sys.stderr)
            return 2
        return _install_authority(value)
    result = _run_install(args)
    rc = _after_plain_install(args)
    if "--check" not in args and "--project" not in args:
        _local_check_nudge()
    receipt = getattr(result, "receipt", None)
    if rc == 0 and receipt is not None and receipt_failures(receipt):
        return 1  # P2.12: a host that failed to install is an error row, not a silent 0
    return rc


#: Flags that select a different install than the full one ``--auto`` runs.
_AUTO_CONFLICTS = ("--host", "--project", "--check", "--headless", "--proxy-default", "--authority")


def _run_auto(args: list[str]) -> int:
    """``install --auto`` (P2.12): ``run_full(interactive=False)``, exit code from the receipt.

    No prompt (``input`` is never called), no daily-job question, no authority step and no
    local-check nudge: npm postinstall, the plugin and the MCP door run exactly this, so all
    of them leave the same receipt. 0 when every host row is ``wired`` or ``not_wired``,
    1 when any row is ``error``, 2 for a flag that selects a different install."""
    clash = [a for a in args if a in _AUTO_CONFLICTS]
    if clash:
        print(f"llm-router install: --auto runs the full install; it cannot be combined with {clash[0]}",
              file=sys.stderr)
        return 2
    result = run_full(interactive=False, flags=[a for a in args if a != "--auto"])
    return 1 if receipt_failures(result.receipt) else 0


def _local_check_nudge() -> None:
    """LC-7: one dim line when a local model is installed and nothing is measured (once per bank version)."""
    try:
        from llm_router.local_check.display import maybe_nudge

        line = maybe_nudge()
        if line:
            print(_dim(f"  {line}"))
    except Exception:  # noqa: BLE001 -- a nudge must never fail an install
        pass


def _after_plain_install(args: list[str]) -> int:
    """The default-safe authority step of a plain install (see PROXY_DEFAULT_ON_INSTALL)."""
    if "--no-authority" in args or "--check" in args or "--project" in args:
        return 0
    if "--host" in args and _flag_value(args, "--host") not in (None, "claude-code"):
        return 0
    if PROXY_DEFAULT_ON_INSTALL and _fresh_for_proxy_default():
        return _install_authority("route")
    if not PROXY_DEFAULT_ON_INSTALL:
        print(_dim(f"  {_AUTHORITY_HINT}"))
    return 0


# ── Main install logic ──────────────────────────────────────────────────────────

# README headline uses `--host claude-code` / `claude-desktop`, neither of
# which is a distinct snippet: claude-code IS the default LLM Router install
# target, and claude-desktop maps onto the existing `desktop` snippet.
_HOST_ALIASES = {"claude-desktop": "desktop", "claude_desktop": "desktop"}

# Hosts the README lists as planned: `--host <id>` reports the reason and exits
# 2, and auto-detect reports them instead of skipping them silently (v16 P0.4).
# Kimi is planned in 16.1 (D-27); Pi is wired, MCP config only (D-315).
_UNSUPPORTED_HOSTS = {
    "kimi": "not wired into `llm-router install` yet",
}


def _run_install(flags: list[str]) -> InstalledHosts | None:
    # --host <name> is handled before the regular install path — it prints
    # config snippets only (no file modifications to external tools).
    if "--host" in flags:
        idx = flags.index("--host")
        raw_host = (flags[idx + 1] if idx + 1 < len(flags) else "all").strip().lower()
        mode = "auto"
        if "--mode" in flags:
            mode_idx = flags.index("--mode")
            mode = (flags[mode_idx + 1] if mode_idx + 1 < len(flags) else mode).strip().lower()
        host = _HOST_ALIASES.get(raw_host, raw_host)
        # claude-code = the default install (hooks + rules + MCP for Claude
        # Code), so route it through the full install path below rather than
        # the snippet printer — this makes the documented headline command work.
        if host != "claude-code":
            _install_host(host, mode=mode)
            return
        flags = [f for i, f in enumerate(flags) if i not in (idx, idx + 1)]

    if "--project" in flags:
        import pathlib
        for a in _install_project_files(pathlib.Path.cwd()):
            print(f"  {a}")
        return

    check_only = "--check" in flags

    from llm_router.install_hooks import (
        _HOOKS_DST, _HOOKS_SRC, _HOOK_DEFS,
        _RULES_DST, check_api_keys, claude_desktop_config_path,
    )

    if check_only:
        print(f"\n{_bold('[llm_router] Install check')}  (no changes made)\n")

        print(_bold("  Hooks & rules"))
        all_ok = True
        for src_name, dst_name, event, _ in _HOOK_DEFS:
            src = _HOOKS_SRC / src_name
            dst = _HOOKS_DST / dst_name
            if not src.exists():
                print(_fail(f"{src_name}  {_yellow('(source missing in package)')}"))
                all_ok = False
            elif dst.exists():
                print(_ok(f"{dst_name}  ({event})"))
            else:
                print(_fail(
                    f"{dst_name}  ({event})  — not installed",
                    fix="llm-router install",
                ))
                all_ok = False

        rules_dst = _RULES_DST / "llm_router.md"
        if rules_dst.exists():
            print(_ok("llm_router.md  (routing rules)"))
        else:
            print(_fail("llm_router.md  (routing rules)  — not installed",
                        fix="llm-router install"))
            all_ok = False

        print(f"\n{_bold('  Claude Desktop')}")
        desktop_path = claude_desktop_config_path()
        if desktop_path is None:
            print(_warn("unsupported platform"))
        else:
            import json as _json
            registered = False
            if desktop_path.exists():
                try:
                    cfg = _json.loads(desktop_path.read_text())
                    registered = "llm_router" in cfg.get("mcpServers", {})
                except Exception:
                    pass
            if registered:
                print(_ok(f"registered  ({desktop_path})"))
            elif desktop_path.exists():
                print(_fail(
                    f"not registered  ({desktop_path})",
                    fix="llm-router install",
                ))
                all_ok = False
            else:
                print(_warn(f"config not found  ({desktop_path})"))

        print(f"\n{_bold('  Provider keys')}")
        for line in check_api_keys():
            print(f"  {line}")

        print()
        if all_ok:
            print(_green("  Everything looks good."))
        else:
            print(_yellow("  Run `llm-router install` to fix the issues above."))
        print()
        return

    headless  = "--headless"  in flags

    if headless:
        _run_install_headless(flags)
        return

    return run_full(interactive=True, flags=flags)


#: Detected hosts whose `--host` installer cannot wire them from a global
#: install, so the "wire one with --host" hint would be wrong for them.
_NOT_GLOBALLY_WIRABLE = {
    "windsurf": "its config is per project; run `llm-router install --host windsurf` inside each project",
    "factory": "install the plugin with `factory plugin install ypollak2/llm_router`",
}


def _daily_schedule_step(*, interactive: bool, flags: list[str], schedule: dict | None = None,
                         ask=input, stdin_tty: bool | None = None) -> bool:
    """D-220: install the daily KPI scheduler entry only with explicit consent.

    ``--no-schedule`` skips; ``--schedule-daily`` consents; otherwise an interactive terminal is asked
    [Y/n] and anything else (headless, piped, CI) writes nothing. ``schedule`` holds keyword arguments for
    ``daily_schedule.install`` (home, system, runner), so tests never reach the real home or a real
    scheduler. Returns True when an entry was installed."""
    if "--no-schedule" in flags:
        return False
    from llm_router import daily_schedule
    kw = dict(schedule or {})
    if "--schedule-daily" not in flags:
        tty = sys.stdin.isatty() if stdin_tty is None else stdin_tty
        if not (interactive and tty):
            return False
        plan = daily_schedule.build_plan(
            system=kw.get("system") or platform.system(), home=kw.get("home") or Path.home(),
            state_dir=paths.llm_router_home(), python=sys.executable, now=time.time(),
            home_override=daily_schedule.install_home_override(kw.get("home") or Path.home()))
        print(f"\n{_bold('  Daily KPI job')}")
        for line in daily_schedule.plan_lines(plan, plan["system"]):
            print(f"  {line}")
        print("  The job reads only your local ledgers and writes one summary file per day under the state dir.")
        try:
            answer = ask("  Install the daily KPI job? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer not in ("", "y", "yes"):
            print(_dim("  daily KPI job not installed (`llm-router kpi --daily-install` adds it later)"))
            return False
    try:
        r = daily_schedule.install(**kw)
    except Exception as exc:  # noqa: BLE001 -- install must not fail on an optional job
        from llm_router import failopen
        failopen.record("P33-3-DAILY-SCHEDULE-INSTALL", exc)
        print(_yellow(f"  daily KPI job not installed: {exc}"))
        return False
    ok = r["status"] == "installed"
    detail = f": {r['detail']}" if r.get("detail") else ""
    print(f"  {_green('✓') if ok else _yellow('⚠')}  daily KPI job {r['status']}{detail}")
    if r.get("manual"):
        print(f"     run it from your own scheduler: {r['manual']}")
    return ok


# ── Install receipt (P2.12) ────────────────────────────────────────────────────
#
# One row per host: every host ``host_detect`` finds, plus claude-code (always wired by
# ``install()``) and claw-code when run_full wires it. Each row has ``status`` (wired |
# not_wired | error), ``reason`` (set unless wired), ``via`` (settings | plugin | files, or
# None when nothing was wired) and ``hook_version`` ({source hook name: version read from
# the INSTALLED file}, or None). ``digest`` hashes only ``package_version`` and each host's
# (status, hook_version): ``ts``, ``via``, ``reason`` and paths are left out, so the CLI,
# npm, plugin and MCP install paths can be compared by digest alone.

RECEIPT_NAME = "install_receipt.json"
RECEIPT_SCHEMA = 1
RECEIPT_STATUSES = ("wired", "not_wired", "error")


class InstalledHosts(list):
    """What ``run_full`` always returned (labels of the hosts it wired), plus ``.receipt``."""

    def __init__(self, labels: list[str], receipt: dict):
        super().__init__(labels)
        self.receipt = receipt


def receipt_path() -> Path:
    return paths.state_path(RECEIPT_NAME)


def _receipt_hook_files(host: str) -> dict[str, Path]:
    """Source hook name -> where run_full's installer for ``host`` puts it. Hosts that run_full
    does not wire with hook scripts map to {}. A source the package does not ship is left out,
    as the installers skip it too."""
    from llm_router import install_hooks as ih

    state_hooks = paths.state_path("hooks")
    if host == "claude-code":
        files = {src: ih.hooks_dst() / dst for src, dst, _e, _m in ih._HOOK_DEFS}
    elif host == "codex":
        files = {"auto-route.py": state_hooks / "codex-auto-route.py",
                 "codex-post-tool.py": state_hooks / "codex-post-tool.py",
                 "codex-stop.py": state_hooks / "codex-stop.py"}
    elif host == "gemini-cli":
        files = {n: state_hooks / n for n in ("gemini-cli-post-tool.py", "gemini-cli-auto-route.py",
                                              "status-bar.py", "gemini-cli-session-end.py",
                                              "session-start.py")}
    elif host == "claw-code":
        cc_dir = ih._claw_code_dir()
        files = ({src: cc_dir / "hooks" / dst for src, dst, _e, _m in ih._CLAW_CODE_HOOK_DEFS}
                 if cc_dir is not None else {})
    else:
        files = {}
    return {src: dst for src, dst in files.items() if (ih._HOOKS_SRC / src).exists()}


def _receipt_registry(host: str) -> Path | None:
    """The JSON file in which run_full's installer for ``host`` registers its hook commands."""
    from llm_router import install_hooks as ih

    if host == "claude-code":
        return ih.settings_path()
    if host == "codex":
        return Path.home() / ".codex" / "hooks.json"
    if host == "gemini-cli":
        return Path.home() / ".gemini" / "extensions" / "llm_router" / "hooks" / "hooks.json"
    if host == "claw-code":
        cc_dir = ih._claw_code_dir()
        return cc_dir / "settings.json" if cc_dir is not None else None
    if host == "pi":
        return pi_mcp_path()  # registers the MCP server only; Pi has no hooks (D-315)
    return None


def _json_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _json_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _json_strings(v)


def _unregistered(files: dict[str, Path], registry: Path) -> list[str]:
    """Source names whose installed path appears in no string of the registry JSON (every
    installer registers a command that contains the hook's installed path)."""
    import json

    try:
        strings = list(_json_strings(json.loads(registry.read_text(encoding="utf-8"))))
    except (OSError, ValueError):
        strings = []
    return sorted(src for src, dst in files.items() if not any(str(dst) in s for s in strings))


_DEFAULT_REGISTRY = object()


def wired_row(host: str, via: str, hook_files: dict[str, Path] | None = None,
              registry: Path | None | object = _DEFAULT_REGISTRY) -> dict:
    """Row for a host whose installer returned.

    ``wired`` means every hook file is installed (its version is read back into
    ``hook_version``) AND registered: its installed path appears in a command in the host's
    registry JSON (Claude Code / claw-code settings.json, ~/.codex/hooks.json, the Gemini CLI
    extension hooks.json). A missing or unregistered hook makes the row ``error``.

    ``hook_files`` (source name -> installed path) defaults to where run_full's installer
    writes, and ``registry`` to that installer's registry file. A caller that wires hooks
    elsewhere (the plugin, P212-3) passes its own ``hook_files`` and, if nothing to check,
    ``registry=None``. Key ``hook_files`` by the SOURCE names in ``install_hooks._HOOK_DEFS``
    (e.g. ``auto-route.py``, not ``llm_router-auto-route.py``): the digest compares
    ``hook_version`` by key, so other keys never match the CLI install."""
    from llm_router.install_hooks import _hook_version

    files = _receipt_hook_files(host) if hook_files is None else hook_files
    if registry is _DEFAULT_REGISTRY:
        registry = _receipt_registry(host) if hook_files is None else None
    versions = {src: _hook_version(dst) for src, dst in sorted(files.items()) if dst.is_file()}
    missing = sorted(set(files) - set(versions))
    row = {"status": "wired", "reason": None, "via": via, "hook_version": versions or None}
    if missing:
        return {**row, "status": "error", "reason": f"hook missing after install: {', '.join(missing)}"}
    unregistered = _unregistered(files, registry) if files and registry is not None else []
    if unregistered:
        return {**row, "status": "error",
                "reason": f"hook not registered in {registry}: {', '.join(unregistered)}"}
    return row


def error_row(via: str | None, exc: BaseException) -> dict:
    return {"status": "error", "reason": f"{type(exc).__name__}: {exc}", "via": via, "hook_version": None}


def not_wired_row(reason: str) -> dict:
    return {"status": "not_wired", "reason": reason, "via": None, "hook_version": None}


def receipt_digest(receipt: dict) -> str:
    """sha256 over the canonical JSON of package_version and {host: [status, hook_version]}."""
    import hashlib
    import json

    canon = {"package_version": receipt.get("package_version"),
             "hosts": {h: [row.get("status"), row.get("hook_version")]
                       for h, row in (receipt.get("hosts") or {}).items()}}
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def build_receipt(hosts: dict[str, dict], *, package_version: str | None = None) -> dict:
    from datetime import datetime, timezone

    if package_version is None:
        from llm_router import __version__
        package_version = __version__
    receipt = {"schema": RECEIPT_SCHEMA, "package_version": package_version,
               "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "hosts": dict(sorted(hosts.items()))}
    receipt["digest"] = receipt_digest(receipt)
    return receipt


def write_receipt(receipt: dict, path: Path | None = None) -> Path:
    """Atomic write, file 0600 (``state_files.open_private``). Raises OSError on failure."""
    import json

    from llm_router import state_files

    path = path or receipt_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        with state_files.open_private(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        if tmp.exists():
            tmp.unlink()  # a failure here propagates as the OSError the caller records
        raise
    return path


def read_receipt(path: Path | None = None) -> dict | None:
    """The last receipt written, or None when it is absent or not valid JSON."""
    import json

    try:
        data = json.loads((path or receipt_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def receipt_failures(receipt: dict | None) -> list[str]:
    """Host-matrix check: one line per problem, [] when the receipt passes. A missing receipt,
    no host rows, a digest that does not match the rows, an ``error`` row, an unknown status
    and a ``not_wired`` row without a reason each fail."""
    if not receipt:
        return ["no install receipt"]
    hosts = receipt.get("hosts")
    if not isinstance(hosts, dict) or not hosts:
        return ["install receipt has no host rows"]
    out = []
    if receipt.get("digest") != receipt_digest(receipt):
        out.append("install receipt digest does not match its rows")
    for host, row in sorted(hosts.items()):
        status = row.get("status") if isinstance(row, dict) else None
        if status not in RECEIPT_STATUSES:
            out.append(f"{host}: unknown status {status!r}")
        elif status == "error":
            out.append(f"{host}: error: {row.get('reason') or 'no reason given'}")
        elif status == "not_wired" and not (row.get("reason") or "").strip():
            out.append(f"{host}: not_wired without a reason")
    return out


def receipt_line(receipt: dict) -> str:
    hosts = receipt.get("hosts") or {}
    wired = sum(1 for row in hosts.values() if row.get("status") == "wired")
    return (f"receipt: {str(receipt.get('digest', ''))[:8]} hosts={wired}/{len(hosts)} "
            f"version={receipt.get('package_version')}")


CONSENT_NAME = "plugin_install_consent.json"


def consent_path() -> Path:
    return paths.state_path(CONSENT_NAME)


def claude_plugin_root() -> Path | None:
    """Root of the llm-router Claude Code plugin that is active on this machine, or None.

    ``CLAUDE_PLUGIN_ROOT`` (set by Claude Code for plugin hooks, and passed on to the detached
    ``--via-plugin`` run) wins; otherwise ``<claude dir>/plugins/installed_plugins.json`` is read
    for an ``llm-router@...`` entry that counts for this machine: user scope, or project/local
    scope whose ``projectPath`` is the current directory; its install path ships
    ``hooks/hooks.json``; and ``enabledPlugins`` (user settings, then the project's) says True. The plugin provides the Claude Code
    hooks and MCP entry, so a second copy in settings.json would fire every hook twice."""
    import json

    from llm_router import install_hooks as ih

    def _usable(p: object) -> Path | None:
        root = Path(p).expanduser() if isinstance(p, str) and p else None
        return root if root is not None and (root / "hooks" / "hooks.json").is_file() else None

    env_root = _usable(os.environ.get("CLAUDE_PLUGIN_ROOT"))
    if env_root is not None:
        return env_root
    cwd = Path.cwd().resolve()

    def _read(path: Path) -> dict:
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # enabledPlugins: a user-scope entry is judged ONLY on the user's own settings.json (a
    # repository must not be able to switch off the user's global wiring); project settings
    # count only for project/local-scope entries of this very directory. Absent = not enabled.
    def _flags(*files: Path) -> dict:
        merged: dict = {}
        for f in files:
            value = _read(f).get("enabledPlugins")
            if isinstance(value, dict):
                merged.update(value)
        return merged

    user_enabled = _flags(ih.settings_path())
    project_enabled = _flags(ih.settings_path(), cwd / ".claude" / "settings.json",
                             cwd / ".claude" / "settings.local.json")
    plugins = _read(ih.claude_dir() / "plugins" / "installed_plugins.json").get("plugins")
    for key, entries in sorted((plugins if isinstance(plugins, dict) else {}).items()):
        if not str(key).startswith("llm-router@"):
            continue
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            scope = entry.get("scope")
            if scope == "user":
                if user_enabled.get(key) is not True:
                    continue
            else:
                project = entry.get("projectPath")
                if scope not in ("project", "local") or not isinstance(project, str) or not project:
                    continue
                if Path(project).expanduser().resolve() != cwd:
                    continue  # another project's plugin entry
                if project_enabled.get(key) is not True:
                    continue
            root = _usable(entry.get("installPath"))
            if root is not None:
                return root
    return None


def _plugin_hook_files(root: Path) -> dict[str, Path]:
    """Source hook name -> the copy the plugin bundle ships (same keys as the CLI install, so
    the receipt digest compares the two)."""
    from llm_router import install_hooks as ih

    return {src: root / "hooks" / src for src, _dst, _e, _m in ih._HOOK_DEFS if (ih._HOOKS_SRC / src).exists()}


def _settings_hooks_registered() -> bool:
    """True when settings.json already registers llm-router hook commands (a CLI install)."""
    import json

    from llm_router import install_hooks as ih

    try:
        hooks = json.loads(ih.settings_path().read_text("utf-8")).get("hooks") or {}
    except (OSError, ValueError, AttributeError):
        return False
    return any("llm_router" in s or "llm-router" in s for s in _json_strings(hooks))


def record_plugin_consent() -> None:
    """Running the installer through the plugin (``/llm-router:setup`` or the detached run) is
    the consent the SessionStart hook waits for. Atomic, 0600; failure is recorded, not fatal."""
    import json

    from llm_router import failopen, state_files

    path = consent_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        with state_files.open_private(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"consent": True}, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        failopen.record("P212-PLUGIN-CONSENT-WRITE", exc)


def _host_failed(host: str, label: str, via: str | None, exc: BaseException) -> dict:
    """An installer that raised: record it (failopen), say so on stderr (npm and MCP callers
    keep stdout for the receipt line), and return its ``error`` row."""
    from llm_router import failopen

    failopen.record("P212-INSTALL-HOST-FAILED", exc, detail=host)
    print(_fail(f"{label}: install failed: {type(exc).__name__}: {exc}"), file=sys.stderr)
    return error_row(via, exc)


def run_full(interactive: bool = True, flags: list[str] | None = None, *,
             schedule: dict | None = None) -> InstalledHosts:
    """Install globally and return the labels of hosts wired by this run (a list), with the
    install receipt (see ``build_receipt``) on ``.receipt``; the receipt is also written to
    ``receipt_path()`` and summarised in one printed ``receipt:`` line."""
    from llm_router.install_hooks import check_api_keys, install

    flags = flags if flags is not None else []
    claw_code = "--claw-code" in flags

    if "--force" in flags:
        from llm_router.install_hooks import _load_settings, _save_settings
        settings = _load_settings()
        settings.get("mcpServers", {}).pop("llm_router", None)
        _save_settings(settings)

    print(f"\n{_bold('╔══════════════════════════════════════════╗')}")
    print(f"{_bold('║   LLM Router — One-Command Install        ║')}")
    print(f"{_bold('╚══════════════════════════════════════════╝')}\n")

    rows: dict[str, dict] = {}
    # P212-3: when the Claude Code plugin is active (asked for with --via-plugin, or found on
    # this machine), it provides the hooks and the MCP entry; writing them to settings.json
    # too would fire every hook twice. Every other host is wired as usual.
    plugin_root = None if ("--no-plugin" in flags and "--via-plugin" not in flags) else claude_plugin_root()
    via_plugin = plugin_root is not None
    if via_plugin and "--via-plugin" not in flags:
        print(_dim(f"  Claude Code plugin found at {plugin_root}: it provides the hooks and MCP entry, so "
                   "none are written to settings.json. To wire them from the CLI instead, run "
                   "`llm-router install --auto --no-plugin`."))
    if "--via-plugin" in flags and not via_plugin:
        rows["claude-code"] = error_row("plugin", RuntimeError(
            "--via-plugin: no llm-router plugin found (CLAUDE_PLUGIN_ROOT unset and none in installed_plugins.json)"))
        print(_fail(f"Claude Code: {rows['claude-code']['reason']}"), file=sys.stderr)
        actions = []
    else:
        try:
            actions = install(via_plugin=True) if via_plugin else install()
        except Exception as exc:  # noqa: BLE001 -- P2.12: a failing host is an error row, not an abort
            actions = []
            rows["claude-code"] = _host_failed("claude-code", "Claude Code", "plugin" if via_plugin else "settings", exc)
        else:
            if via_plugin:
                rows["claude-code"] = wired_row("claude-code", "plugin", hook_files=_plugin_hook_files(plugin_root))
                if _settings_hooks_registered():
                    print(_warn("settings.json still registers llm-router hooks from an earlier install; "
                                "with the plugin every hook fires twice. Run `llm-router uninstall` to remove them."))
                if "--via-plugin" in flags:
                    record_plugin_consent()
            else:
                rows["claude-code"] = wired_row("claude-code", "settings")
    for a in actions:
        print(f"  {_green('✓')}  {a}")

    # ── claw-code (explicit flag or auto-detect) ──────────────────────────
    from llm_router.install_hooks import install_claw_code, claw_code_settings_path
    cc_detected = claw_code_settings_path() is not None
    if claw_code or cc_detected:
        if cc_detected and not claw_code:
            print(f"\n{_bold('  claw-code detected — installing hooks...')}")
        try:
            cc_actions = install_claw_code()
        except Exception as exc:  # noqa: BLE001 -- see claude-code above
            cc_actions = []
            rows["claw-code"] = _host_failed("claw-code", "claw-code", "settings", exc)
        else:
            skipped = bool(cc_actions) and cc_actions[0].startswith("SKIP claw-code")
            rows["claw-code"] = (not_wired_row(cc_actions[0][len("SKIP "):]) if skipped
                                 else wired_row("claw-code", "settings"))
        for a in cc_actions:
            ok = not a.startswith("SKIP")
            marker = _green('✓') if ok else _yellow('⚠')
            print(f"  {marker}  {a}")

    # ── Other hosts on this machine (auto-detect) ──────────────────────────
    # A user with Claude Code AND Codex wants both wired, both ways, from the
    # one command. --host still targets a single host; --no-hosts skips this.
    # Every detected host is either wired here or reported with a reason
    # (v16 P0.4): before, gemini-cli was detected and then silently skipped.
    installed_hosts = ["Claude Code"] if rows["claude-code"]["status"] == "wired" else []
    from llm_router import host_detect
    detected = [h for h, info in host_detect.detect_hosts().items() if info.present]
    if "--no-hosts" in flags:
        for host_id in detected:
            if host_id != "claude-code":
                rows[host_id] = not_wired_row("skipped: --no-hosts")
    else:
        _auto_wire = {
            "codex": ("Codex CLI", _install_codex_files),
            "gemini-cli": ("Gemini CLI", _install_gemini_cli_files),
            "pi": ("Pi", _install_pi_files),
        }
        _unwired: list[str] = []
        for host_id in detected:
            if host_id == "claude-code":
                continue  # already wired by install() above
            if host_id in _auto_wire:
                label, install_fn = _auto_wire[host_id]
                print(f"\n{_bold(f'  {label} detected — installing...')}")
                try:
                    host_actions = install_fn()
                except Exception as exc:  # noqa: BLE001 -- see claude-code above
                    rows[host_id] = _host_failed(host_id, label, "files", exc)
                    continue
                if host_actions and host_actions[0].startswith("SKIP "):
                    rows[host_id] = not_wired_row(host_actions[0][len("SKIP "):])
                    print(f"  {host_id}: detected, not wired: {rows[host_id]['reason']}")
                    continue
                for a in host_actions:
                    ok = a.lstrip().startswith("✓")
                    print(f"  {_green('✓') if ok else _dim('·')}  {a.lstrip('✓ ').strip()}")
                try:
                    rows[host_id] = pi_row() if host_id == "pi" else wired_row(host_id, "files")
                except Exception as exc:  # noqa: BLE001 -- see claude-code above
                    rows[host_id] = _host_failed(host_id, label, "files", exc)
                    continue
                if "smoke" in rows[host_id]:
                    smoke = rows[host_id]["smoke"]
                    print(f"  pi mcp list: {smoke['status']}: {smoke['detail']}")
                if rows[host_id]["status"] == "wired":
                    installed_hosts.append(label)
                else:
                    print(_fail(f"{label}: {rows[host_id]['reason']}"))
            elif host_id in _NOT_GLOBALLY_WIRABLE:
                rows[host_id] = not_wired_row(_NOT_GLOBALLY_WIRABLE[host_id])
                print(f"  {host_id}: detected, not wired: {_NOT_GLOBALLY_WIRABLE[host_id]}")
            elif host_id in _HOST_SNIPPETS:
                rows[host_id] = not_wired_row(f"not auto-wired; run `llm-router install --host {host_id}`")
                _unwired.append(host_id)
            else:
                reason = _UNSUPPORTED_HOSTS.get(host_id, "no installer for this host")
                rows[host_id] = not_wired_row(reason)
                print(f"  {host_id}: detected, not wired: {reason}")
        if _unwired:
            # One line, not one per host: vscode and cursor are on most dev machines.
            print(
                f"  also detected, not auto-wired: {', '.join(sorted(_unwired))} "
                "(wire one with `llm-router install --host <name>`)"
            )

    # ── Seats: which subscriptions are logged in (drives the free bucket) ──
    # Three steps, each with its own label, so a failure names what failed (MAP-FIX-2f).
    # None of them may fail install: say it, as doctor does, then go on.
    _found = None
    try:
        from llm_router import seats as _seats
        _carried: dict = {}
        _found = _seats.refresh_seats(carried=_carried)
    except Exception as e:  # noqa: BLE001 -- install must not fail on a probe
        print(_warn(f"seat detection failed: {e}"))
    if _found is not None:
        try:
            print(f"\n{_bold('  Seats')}  {_found.summary_line(carried=_carried)}")
            _bucket = sorted(_found.free_bucket())
            if _bucket:
                print(f"  free bucket: {_seats.mark_carried(_bucket, _carried)}")
            else:
                print(_yellow("  no seat found — routed calls will bill an API key; log in to Claude Code, Codex, or start Ollama"))
        except Exception as e:  # noqa: BLE001 -- see above
            print(_warn(f"seat summary failed: {e}"))
    try:
        _maybe_enable_claude_subscription(_found)
    except Exception as e:  # noqa: BLE001 -- see above
        print(_warn(f"Claude subscription setup failed: {e}"))

    _daily_schedule_step(interactive=interactive, flags=flags, schedule=schedule)

    failed = sorted(h for h, row in rows.items() if row["status"] == "error")
    if failed:
        print(f"\n{_red('✗')} {_bold('LLM Router install finished with errors:')} {', '.join(failed)}")
    else:
        print(f"\n{_green('✓')} {_bold('LLM Router installed globally.')}")
    auto_routed = [h for h in installed_hosts if h != "Pi"]  # Pi: MCP tools only, no hooks (D-315)
    if auto_routed:
        print(f"  Every {' and '.join(auto_routed)} session will now auto-route tasks.")
    if "Pi" in installed_hosts:
        print("  Pi sessions get the llm-router MCP tools; Pi has no hooks, so prompts are not auto-routed.")
    print("  Restart Claude Code (and Claude Desktop if installed) to activate.\n")

    print(_bold("  Provider keys (optional — router works without any):"))
    for line in check_api_keys():
        print(f"  {line}")

    receipt = build_receipt(rows)
    try:
        write_receipt(receipt)
    except OSError as exc:
        from llm_router import failopen
        failopen.record("P212-INSTALL-RECEIPT-WRITE", exc)
        print(_warn(f"install receipt not written to {receipt_path()}: {exc}"))
    print(f"\n  {receipt_line(receipt)}")
    result = InstalledHosts(installed_hosts, receipt)

    if not interactive:
        return result

    print(f"\n{_bold('  Try it:')}")
    print('    In Claude Code, ask: "What does os.path.join do?"')
    print("    You'll see: ⚡ ROUTE → Haiku (simple query)\n")

    print(_bold("  Subcommands:"))
    print("    llm-router doctor               — verify everything is wired up")
    print("    llm-router status               — today's cost & savings")
    print("    llm-router dashboard            — web dashboard (localhost:7337)")
    print("    llm-router install --check      — preview install state")
    print("    llm-router install --force      — reinstall / update paths")
    print("    llm-router install --claw-code  — also install into claw-code")
    print("    llm-router uninstall            — remove\n")
    return result


# ── MCP install door (P2.12, P212-4) ───────────────────────────────────────────

#: Seconds a confirm token stays valid after the plan was shown.
INSTALL_TOKEN_TTL_S = 600


def _install_plan() -> dict:
    """What ``run_full(interactive=False)`` would touch on this machine. Reads only."""
    from llm_router import host_detect

    hosts = {"claude-code"}
    hosts.update(h for h, info in host_detect.detect_hosts().items() if info.present)
    plan: dict[str, dict] = {}
    for host_id in sorted(hosts):
        files = sorted(str(p) for p in _receipt_hook_files(host_id).values())
        registry = _receipt_registry(host_id)
        plan[host_id] = {"hook_files": files, "registry": str(registry) if registry else None}
    return plan


def _install_token(plan: dict, issued: int) -> str:
    import hashlib
    import json

    blob = json.dumps({"plan": plan, "home": str(paths.llm_router_home())}, sort_keys=True)
    return f"{issued}.{hashlib.sha256(f'{issued}:{blob}'.encode()).hexdigest()[:16]}"


def _token_valid(token: object, plan: dict, now: float) -> bool:
    import hmac

    if not isinstance(token, str) or "." not in token:
        return False
    stamp = token.split(".", 1)[0]
    if not stamp.isdigit() or not 0 <= now - int(stamp) <= INSTALL_TOKEN_TTL_S:
        return False
    return hmac.compare_digest(token, _install_token(plan, int(stamp)))


def admin_install(value: str = "") -> str:
    """``llm_router_admin(action="install")``: two steps, nothing is printed to the process stdout.

    Step 1 (``value`` empty) lists the hosts and files the install would write and returns a
    confirm token; it writes nothing. Step 2 (``value`` = JSON ``{"confirm": token}``) runs
    ``run_full(interactive=False)`` -- the same call as ``install --auto``, so the receipt and its
    digest match the CLI's -- with stdout and stderr captured, because stdout belongs to the MCP
    stdio stream. It never changes the proxy default or the routing authority."""
    import contextlib
    import io
    import json
    import time

    token = None
    if (value or "").strip():
        try:
            body = json.loads(value)
            token = body["confirm"]
        except (ValueError, KeyError, TypeError):
            return 'Bad value: leave it empty to see the plan, or pass JSON {"confirm": "<token>"}.'
    plan = _install_plan()
    now = time.time()
    if token is None:
        lines = [f"  {h}: {', '.join(d['hook_files']) or 'no hook scripts'}"
                 + (f" (registered in {d['registry']})" if d["registry"] else "") for h, d in plan.items()]
        confirm = json.dumps({"confirm": _install_token(plan, int(now))})
        return ("Nothing installed yet. `install` would wire these hosts:\n" + "\n".join(lines)
                + "\nIt does not change the proxy default or the routing authority."
                + f"\nTo install, call again with value='{confirm}' (valid {INSTALL_TOKEN_TTL_S // 60} minutes).")
    if not _token_valid(token, plan, now):
        return ("Not installed: the confirm token is wrong, expired, or the detected hosts changed; "
                "start again with an empty value.")
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        receipt = run_full(interactive=False).receipt
    failures = receipt_failures(receipt)
    rows = "\n".join(f"  {h}: {r['status']}" + (f" ({r['reason']})" if r.get("reason") else "")
                      for h, r in receipt["hosts"].items())
    head = "Install finished with errors." if failures else "Installed."
    return (f"{head}\n{rows}\n{receipt_line(receipt)}\ndigest: {receipt['digest']}"
            + ("".join(f"\n  problem: {f}" for f in failures)) + "\nRestart the host to activate.")


# ── install --headless ─────────────────────────────────────────────────────────

def _run_install_headless(flags: list[str] | None = None, *, schedule: dict | None = None) -> None:
    """Install for Docker / CI / agent environments (API-key mode, no OAuth).

    Runs the standard hook + MCP install, then prints a Dockerfile snippet showing
    the complete wiring needed for a Claude Code agent container.
    """
    from llm_router.install_hooks import install

    print(f"\n{_bold('╔══════════════════════════════════════════════╗')}")
    print(f"{_bold('║   LLM Router — Headless / Agent Install       ║')}")
    print(f"{_bold('╚══════════════════════════════════════════════╝')}\n")
    print(_dim("  API-key mode — no subscription, no OAuth, routes directly to external providers.\n"))

    actions = install()
    for a in actions:
        print(f"  {_green('✓')}  {a}")

    _daily_schedule_step(interactive=False, flags=flags or [], schedule=schedule)

    print(f"\n{_green('✓')} {_bold('Hooks and MCP server installed.')}\n")

    print(_bold("  Dockerfile snippet (bake hooks into agent image):"))
    print(_dim("  ─────────────────────────────────────────────────────────────"))
    snippet = """\
  FROM python:3.12-slim

  # Install llm_router and wire in hooks
  RUN pip install llm-routing && llm-router install

  # Route to API providers — no Anthropic subscription in CI
  ENV LLM_ROUTER_CLAUDE_SUBSCRIPTION=false

  # At least one provider key required for the fallback chain
  # (pass at runtime via --env or K8s secret)
  ENV GEMINI_API_KEY=""
  ENV OPENAI_API_KEY=""
  ENV GROQ_API_KEY=""
  ENV DEEPSEEK_API_KEY=""
"""
    print(snippet)

    print(_bold("  .claude/settings.json — MCP + hooks merge example:"))
    print(_dim("  (llm-router install does this automatically; shown for reference)"))
    import json as _json
    example = {
        "mcpServers": {"llm_router": {"command": "llm-router", "args": []}},
        "hooks": {
            "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/llm_router-auto-route.py"}]}],
            "Stop":             [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/llm_router-session-end.py"}]}],
        }
    }
    print("  " + _dim(_json.dumps(example, indent=2).replace("\n", "\n  ")))

    print(f"\n{_bold('  Verification (run inside the container after a job):')}")
    print("    grep ROUTE /proc/1/fd/1            # look for routing log lines")
    print("    cat ~/.llm-router/usage.json        # routing stats")
    print("    sqlite3 ~/.llm-router/usage.db \\")
    print('      "SELECT model, COUNT(*) FROM usage GROUP BY model"\n')


# ── install --host (print config snippets for non-Claude Code hosts) ──────────

# CHZ-SURF-01: snippets are .format() templates, so tool names are placeholders
# resolved against the live tier rather than baked in as literals.
_SNIPPET_TOOLS = {
    "savings_tool": route_tool("llm_savings"),
    "research_tool": route_tool("llm_research"),
    "query_tool": route_tool("llm_query"),
}

_HOST_SNIPPETS: dict[str, str] = {
    "codex": """\
{bold}Codex CLI{reset}  (MCP server + push-routing hook + AGENTS.md rules)
──────────────────────────────────────────────────────────────────
Writes, in ~/.codex:
  config.toml  [mcp_servers.llm_router] and the hook trust records
  hooks.json   UserPromptSubmit -> auto-route, PostToolUse -> telemetry, Stop -> savings
  AGENTS.md    a marked block of routing rules (replaced on re-run)
Legacy config.yaml / config.json / rules/llm_router.md entries are removed: Codex never read them.
Restart Codex, then run {savings_tool} to verify the DB is shared.
""",

    "desktop": """\
{bold}Claude Desktop{reset}  (capability extension — no cost-routing)
──────────────────────────────────────────────────────────────────
Edit ~/Library/Application Support/Claude/claude_desktop_config.json
(Linux: ~/.config/Claude/claude_desktop_config.json)
(Windows: %APPDATA%\\Claude\\claude_desktop_config.json)

Add inside the top-level object:

  "mcpServers": {{
    "llm_router": {{
      "command": "llm-router",
      "args": [],
      "env": {{
        "LLM_ROUTER_PROFILE": "balanced"
      }}
    }}
  }}

Restart Claude Desktop. Run {savings_tool} to confirm DB is shared.
Note: cost-routing is not available in Desktop (no hook system).
""",

    "copilot": """\
{bold}GitHub Copilot (VS Code){reset}  (capability extension — no cost-routing)
──────────────────────────────────────────────────────────────────
1. Create or edit .vscode/mcp.json in your workspace:

   {{
     "servers": {{
       "llm_router": {{
         "command": "llm-router",
         "args": []
       }}
     }}
   }}

2. Optionally add routing guidance to .github/copilot-instructions.md:

   When a task requires live web search, call the {research_tool} MCP tool.
   When a task requires image generation, call the llm_image MCP tool.
   For auto-routing with savings tracking, call llm_auto.

3. Enable MCP in VS Code settings (Copilot > MCP: Enable).
   Restart VS Code. Run @llm_router {savings_tool} to verify.
Note: cost-routing is not available in Copilot (no hook system).
""",

    "opencode": """\
{bold}OpenCode{reset}  — writing config files…
""",

    "gemini-cli": """\
{bold}Gemini CLI{reset}  — writing config files…
""",

    "copilot-cli": """\
{bold}GitHub Copilot CLI{reset}  — writing config files…
""",

    "openclaw": """\
{bold}OpenClaw{reset}  — writing config files…
""",

    "trae": """\
{bold}Trae IDE{reset}  — writing config files…
""",

    "factory": """\
{bold}Factory Droid{reset}  — writing config files…
""",

    "vscode": """\
{bold}VS Code (MCP native){reset}  — writing config files…
""",

    "cursor": """\
{bold}Cursor IDE{reset}  — writing config files…
""",

    "windsurf": """\
{bold}Windsurf (MCP native){reset}  — writing config files…
""",

    "pi": """\
{bold}Pi coding agent (pi.dev){reset}  (MCP server only)
Adds mcpServers.llm_router to ~/.pi/agent/mcp.json; other servers are kept.
The end-of-turn line on Pi is planned in 16.1.
""",
}


# ── Host-specific install functions ────────────────────────────────────────────

def _replace_or_insert_toml_scalar(text: str, key: str, value: str) -> str:
    """Replace a top-level TOML scalar, or insert it before the first table."""
    import re

    line = f'{key} = "{value}"'
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    table_match = re.search(r"^\[", text, flags=re.MULTILINE)
    if table_match:
        return text[:table_match.start()] + line + "\n" + text[table_match.start():]
    return (text.rstrip() + "\n" + line + "\n") if text.strip() else line + "\n"


def _ensure_toml_table_block(text: str, header: str, block: str) -> str:
    """Append a TOML table block if the table is not already present."""
    if f"[{header}]" in text:
        return text
    sep = "" if text.endswith("\n") or not text else "\n"
    return f"{text}{sep}\n[{header}]\n{block.rstrip()}\n"


def _remove_toml_scalar_if_equals(text: str, key: str, value: str) -> str:
    """Remove a top-level TOML scalar line, but only if it currently equals
    `value` — used to self-heal a setting a previous llm-router install forced,
    without touching a value the user (or another tool) set independently."""
    import re

    pattern = re.compile(
        rf'^{re.escape(key)}\s*=\s*"{re.escape(value)}"\s*\n?', re.MULTILINE
    )
    return pattern.sub("", text, count=1)


def _install_codex_gateway_config(codex_dir) -> list[str]:
    """Register LLM Router as an available Codex model provider (opt-in).

    Earlier versions of this installer also force-set `model = "auto"` and
    `model_provider = "llm_router"` as Codex's GLOBAL defaults, silently looping
    every Codex call — including Codex's own interactive/agentic use, and
    LLM Router's own dispatch of Codex from its routing chain — through the local
    LLM Router gateway. The gateway does not yet speak the exact OpenAI
    "responses" wire shape Codex's client expects, so every such call failed
    with an undecodable-stream error. Concretely: this broke Codex CLI itself
    for any user who ran `llm-router install --host codex`, independent of
    whether they ever touched LLM Router's own routing.
    We now only REGISTER the provider (so it exists for future opt-in use,
    e.g. `codex -c model_provider=llm_router` once wire compatibility lands) and
    never force it as the default. If an earlier llm-router install already
    forced it, this run reverts those two lines so Codex falls back to its
    own built-in default — self-healing existing broken installs the next
    time this function runs (e.g. via `llm-router install --host codex --mode
    gateway` or `llm-router doctor --fix`).
    """
    import pathlib
    import re
    import shutil as _shutil

    from llm_router import presets

    actions: list[str] = []
    config_toml = pathlib.Path(codex_dir) / "config.toml"
    config_toml.parent.mkdir(parents=True, exist_ok=True)
    original = config_toml.read_text() if config_toml.exists() else ""
    updated = original

    # Self-heal: undo a previous install's forced global default, if present.
    had_forced_default = (
        re.search(r'^model_provider\s*=\s*"llm_router"\s*$', updated, re.MULTILINE) is not None
    )
    updated = _remove_toml_scalar_if_equals(updated, "model", "auto")
    updated = _remove_toml_scalar_if_equals(updated, "model_provider", "llm_router")

    gateway = presets.gateway_url()
    updated = _ensure_toml_table_block(
        updated,
        "model_providers.llm_router",
        "\n".join([
            'name = "LLM Router"',
            f'base_url = "{gateway}"',
            'env_key = "LLM_ROUTER_API_KEY"',
            'wire_api = "responses"',
        ]),
    )

    if updated != original:
        try:
            if config_toml.exists() and not (config_toml.parent / "config.toml.llm_router-bak").exists():
                _shutil.copy2(config_toml, config_toml.parent / "config.toml.llm_router-bak")
                actions.append(f"✓ Backed up Codex config to {config_toml.parent / 'config.toml.llm_router-bak'}")
            config_toml.write_text(updated)
            try:
                from llm_router import install_manifest
                install_manifest.record("toml_table", config_toml, header="model_providers.llm_router")
            except Exception:
                pass
        except OSError as e:
            actions.append(f"  Could not update Codex gateway config at {config_toml}: {e}")
            return actions
        if had_forced_default:
            actions.append(
                f"✓ Reverted Codex's default model_provider (was force-set to 'llm_router' by an "
                f"earlier install, which broke Codex CLI — see {config_toml})"
            )
        actions.append(f"✓ Registered LLM Router as an available Codex model provider in {config_toml}")
        actions.append(
            f"  Not set as default (gateway doesn't yet support Codex's wire format) — "
            f"opt in per-call with -c model_provider=llm_router ({gateway})"
        )
    else:
        actions.append(f"  Codex gateway provider already configured in {config_toml} (skipped)")
    return actions


# ── Codex CLI ───────────────────────────────────────────────────────────────
#
# One writer, targeting what Codex actually reads (verified against Codex
# 0.153, 2026-09-04 -- see llm_router.codex_host):
#
#   ~/.codex/config.toml   [mcp_servers.llm_router]         the MCP server
#                          [hooks.state."…"] trusted_hash   without this Codex
#                                                           silently skips a hook
#   ~/.codex/hooks.json    UserPromptSubmit -> auto-route   push routing, same
#                          PostToolUse      -> telemetry    hint Claude Code gets
#   ~/.codex/AGENTS.md     marked block of routing rules    Codex reads AGENTS.md
#
# Earlier versions wrote config.yaml, config.json, rules/llm_router.md and
# instructions.md. Codex reads none of them, so Codex -> llm-router never
# worked for anyone. Those are cleaned up below when they are ours.

_CODEX_LEGACY_YAML_BLOCK = (
    "\nmcp:\n"
    "  servers:\n"
    "    llm_router:\n"
    "      command: llm-router\n"
    "      args: []\n"
)
def _codex_hook_command(dst) -> str:
    from llm_router.install_hooks import _python_exe
    return f"{_python_exe()} {dst}"


def _clear_codex_orphan_tables(text: str, config_toml) -> tuple[str, list[str]]:
    """Strip ``[mcp_servers.llm_router...]`` entries that have no command or url.

    Codex refuses to start on them ("invalid transport"); Codex writes
    ``[mcp_servers.llm_router.tools.<x>]`` itself on "always allow", so they
    outlive any removal that only knows what we recorded. Returns the text to
    write and the action lines. The success line is printed only when a
    re-check finds the server gone and the file still parses; otherwise the
    text is returned unchanged with a warning naming what is left.
    """
    import tomllib
    from llm_router import codex_host

    if not codex_host.has_orphan_mcp_tables(text):
        return text, []
    updated = codex_host.remove_toml_subtree(text, codex_host.MCP_TABLE)
    try:
        tomllib.loads(updated)
        cleared = codex_host.read_mcp_server(updated) is None
    except tomllib.TOMLDecodeError:
        cleared = False
    if cleared:
        return updated, [f"✓ Removed orphaned [{codex_host.MCP_TABLE}.*] tables from {config_toml}"]
    return text, [
        f"⚠ {config_toml} still has [{codex_host.MCP_TABLE}...] entries with no command or url "
        "that could not be removed safely; Codex will not start until you delete them by hand"
    ]


def _install_codex_files(mode: str = "mcp") -> list[str]:
    """Write Codex-specific config files and return a list of actions taken.

    ``mode``: "mcp" (default) registers the MCP server, hooks and rules.
    "gateway" additionally registers the opt-in model provider table; it is
    never the default because the gateway does not yet speak Codex's wire
    format (see _install_codex_gateway_config).
    """
    import json as _json
    import pathlib
    import shutil as _shutil

    from llm_router import codex_host, install_manifest
    from llm_router.install_hooks import _build_mcp_entry, _localized_rules_text

    actions: list[str] = []
    home = pathlib.Path.home()
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    config_toml = codex_dir / "config.toml"
    hooks_json = codex_dir / "hooks.json"
    agents_md = codex_dir / "AGENTS.md"

    original = config_toml.read_text() if config_toml.exists() else ""
    text = original

    # 1. MCP server ----------------------------------------------------------
    # Surgical TOML insert, not `codex mcp add`: the CLI rewrites the whole
    # file (re-orders the user's tables, drops blank lines and `args = []`).
    # Codex users hand-edit config.toml; we own one table and the per-tool
    # approval tables under it, nothing else.
    entry, warnings = _build_mcp_entry()
    if entry is None:
        actions.extend(f"  {w}" for w in warnings)
        # Tool tables with no server table: Codex will not start at all.
        text, cleanup = _clear_codex_orphan_tables(text, config_toml)
        actions.extend(cleanup)
    else:
        command, args = entry["command"], list(entry.get("args") or [])
        text = codex_host.upsert_toml_table(
            text, codex_host.MCP_TABLE, codex_host.mcp_table_body(command, args),
        )
        install_manifest.record("toml_table", config_toml, header=codex_host.MCP_TABLE)
        # `codex exec` runs with approval policy "never": an MCP tool without
        # approval_mode = "approve" fails with "MCP tool call requires
        # approval" instead of routing. Approve the routing doors only; the
        # admin / agent tools still prompt.
        for tool in codex_host.approved_tools():
            header = codex_host.tool_table(tool)
            text = codex_host.upsert_toml_table(text, header, 'approval_mode = "approve"')
            install_manifest.record("toml_table", config_toml, header=header)
        actions.append(f"✓ Registered llm_router MCP server in {config_toml} ({command})")

    # 2. Hooks ---------------------------------------------------------------
    hooks_dir = paths.state_path("hooks")
    hooks_dir.mkdir(parents=True, exist_ok=True)
    pkg_hooks = pathlib.Path(__file__).parent.parent / "hooks"
    our_commands: set[str] = set()

    route_dst = hooks_dir / "codex-auto-route.py"
    route_src = pkg_hooks / "auto-route.py"
    if route_src.exists():
        _shutil.copy2(route_src, route_dst)
        route_dst.chmod(0o755)
        install_manifest.record("file", route_dst)
        our_commands.add(_codex_hook_command(route_dst))
        actions.append(f"✓ Installed auto-route hook to {route_dst}")
    # The support module the hook falls back to when llm_router is not importable.
    support_src = pathlib.Path(__file__).parent.parent / "tool_surface.py"
    if support_src.exists():
        _shutil.copy2(support_src, hooks_dir / "llm_router_tool_surface.py")
        install_manifest.record("file", hooks_dir / "llm_router_tool_surface.py")

    post_dst = hooks_dir / "codex-post-tool.py"
    post_src = pkg_hooks / "codex-post-tool.py"
    if post_src.exists():
        _shutil.copy2(post_src, post_dst)
        post_dst.chmod(0o755)
        install_manifest.record("file", post_dst)
        our_commands.add(str(post_dst))
        actions.append(f"✓ Installed telemetry hook to {post_dst}")

    stop_dst = hooks_dir / "codex-stop.py"
    stop_src = pkg_hooks / "codex-stop.py"
    if stop_src.exists():
        _shutil.copy2(stop_src, stop_dst)
        stop_dst.chmod(0o755)
        install_manifest.record("file", stop_dst)
        our_commands.add(_codex_hook_command(stop_dst))
        actions.append(f"✓ Installed per-turn savings hook to {stop_dst}")

    doc: dict = {}
    if hooks_json.exists():
        try:
            doc = _json.loads(hooks_json.read_text())
        except (OSError, ValueError):
            actions.append(f"  {hooks_json} is not valid JSON -- leaving it alone; hooks not installed")
            doc = None
    if doc is not None:
        hooks = doc.setdefault("hooks", {})
        changed = False

        def _ensure(event: str, matcher: str | None, cmd: str) -> None:
            nonlocal changed
            groups = hooks.setdefault(event, [])
            for g in groups:
                if any(h.get("command") == cmd for h in (g.get("hooks") or []) if isinstance(h, dict)):
                    return
            group: dict = {"hooks": [{"type": "command", "command": cmd}]}
            if matcher is not None:
                group["matcher"] = matcher
            groups.append(group)
            changed = True

        if route_src.exists():
            _ensure("UserPromptSubmit", None, _codex_hook_command(route_dst))
        if post_src.exists():
            _ensure("PostToolUse", "Bash", str(post_dst))
        if stop_src.exists():
            _ensure("Stop", None, _codex_hook_command(stop_dst))
        if changed or not hooks_json.exists():
            hooks_json.write_text(_json.dumps(doc, indent=2) + "\n")
            install_manifest.record("codex_hooks", hooks_json)
            actions.append(f"✓ Registered hooks in {hooks_json}")
        else:
            actions.append(f"  Hooks already in {hooks_json} (skipped)")

        # 3. Trust records -- the part every earlier version missed ---------
        for key, digest in codex_host.trust_records(hooks_json, doc, only_commands=our_commands).items():
            text = codex_host.upsert_toml_table(
                text, codex_host.hook_state_table(key), f'trusted_hash = "{digest}"',
            )
            install_manifest.record("toml_table", config_toml, header=codex_host.hook_state_table(key))
        if our_commands:
            actions.append("✓ Trusted the hooks in config.toml (Codex skips untrusted hooks silently)")

    # 4. Self-heal a forced global default from an old install ---------------
    import re as _re
    if _re.search(r'^model_provider\s*=\s*"llm_router"\s*$', text, _re.MULTILINE):
        actions.append(
            "✓ Reverted Codex's default model_provider (was force-set to 'llm_router' by an "
            f"earlier install, which broke Codex CLI — see {config_toml})"
        )
    text = _remove_toml_scalar_if_equals(text, "model", "auto")
    text = _remove_toml_scalar_if_equals(text, "model_provider", "llm_router")

    if text != original:
        try:
            bak = codex_dir / "config.toml.llm_router-bak"
            if config_toml.exists() and not bak.exists():
                _shutil.copy2(config_toml, bak)
                actions.append(f"✓ Backed up Codex config to {bak}")
            config_toml.write_text(text)
        except OSError as e:
            actions.append(f"  Could not update {config_toml}: {e}")

    # 5. AGENTS.md -----------------------------------------------------------
    rules_src = pathlib.Path(__file__).parent.parent / "rules" / "codex-rules.md"
    if rules_src.exists():
        rules_text = _localized_rules_text(rules_src)
        existing = agents_md.read_text() if agents_md.exists() else ""
        updated = codex_host.upsert_marked_block(existing, rules_text)
        if updated != existing:
            agents_md.write_text(updated)
            actions.append(f"✓ Wrote routing rules block to {agents_md}")
        else:
            actions.append(f"  Routing rules already current in {agents_md} (skipped)")
        install_manifest.record("codex_agents_block", agents_md)

    # 6. Legacy files Codex never read ---------------------------------------
    actions.extend(_cleanup_codex_legacy(codex_dir))

    if mode == "gateway":
        actions += _install_codex_gateway_config(codex_dir)
    elif mode not in {"mcp", "companion", "mcp-only", "rules", "auto"}:
        actions.append(f"  Unknown Codex mode {mode!r}; installed MCP, hooks and rules only")

    return actions


def _install_project_files(root, *, use_symlink: bool | None = None) -> list[str]:
    """Write one set of project rules both agents read.

    ``AGENTS.md`` gets a marked llm-router block (Codex reads AGENTS.md).
    ``CLAUDE.md`` becomes a symlink to it (Claude Code reads CLAUDE.md), or a
    copy on Windows. An existing CLAUDE.md that is a real file is kept and gets
    the same marked block appended -- a user's file is never replaced by a
    link. Re-runs replace the block in place.
    """
    import os
    import pathlib

    from llm_router import codex_host, install_manifest
    from llm_router.install_hooks import _localized_rules_text

    root = pathlib.Path(root)
    actions: list[str] = []
    template = pathlib.Path(__file__).parent.parent / "rules" / "project-agents.md"
    if not template.exists():
        return [f"  rules template missing: {template}"]
    body = _localized_rules_text(template)
    if use_symlink is None:
        use_symlink = os.name != "nt"

    agents = root / "AGENTS.md"
    existing = agents.read_text() if agents.exists() else ""
    updated = codex_host.upsert_marked_block(existing, body)
    if updated != existing:
        agents.write_text(updated)
        actions.append(f"✓ {'Updated' if existing else 'Created'} {agents}")
    else:
        actions.append(f"  {agents} already current (skipped)")
    install_manifest.record("codex_agents_block", agents)

    claude = root / "CLAUDE.md"
    if claude.is_symlink():
        try:
            target = (claude.parent / os.readlink(claude)).resolve()
        except OSError:
            target = None
        if target == agents.resolve():
            actions.append(f"  {claude} already links to AGENTS.md (skipped)")
        else:
            actions.append(f"  {claude} is a link elsewhere — left alone")
    elif claude.exists():
        text = claude.read_text()
        new_text = codex_host.upsert_marked_block(text, body)
        if new_text != text:
            claude.write_text(new_text)
            actions.append(f"✓ Added the rules block to existing {claude} (kept as a file)")
        else:
            actions.append(f"  {claude} already current (skipped)")
        install_manifest.record("codex_agents_block", claude)
    elif use_symlink:
        claude.symlink_to("AGENTS.md")
        # Not kind "file": record() resolves an existing path, and resolving
        # the link yields AGENTS.md -- uninstall would then delete the user's
        # rules file instead of our link.
        install_manifest.record("claude_link", agents, link=str(claude.absolute()))
        actions.append(f"✓ Linked {claude} -> AGENTS.md")
    else:
        claude.write_text(updated)
        install_manifest.record("codex_agents_block", claude)
        actions.append(f"✓ Copied the rules into {claude} (symlinks unavailable here)")
    return actions


def _cleanup_codex_legacy(codex_dir) -> list[str]:
    """Remove what earlier installers wrote to files Codex does not read.

    Only content that is recognisably ours is touched: the exact YAML block,
    JSON entries whose command is ours, a rules file that starts with our
    header, and an instructions.md block recorded in the manifest (or a file
    that is nothing but our rules).
    """
    import json as _json
    import pathlib

    from llm_router import install_manifest

    actions: list[str] = []
    codex_dir = pathlib.Path(codex_dir)

    yaml_path = codex_dir / "config.yaml"
    if yaml_path.exists():
        try:
            y = yaml_path.read_text()
            if _CODEX_LEGACY_YAML_BLOCK in y:
                y = y.replace(_CODEX_LEGACY_YAML_BLOCK, "", 1)
                if y.strip():
                    yaml_path.write_text(y)
                else:
                    yaml_path.unlink()
                actions.append(f"✓ Removed legacy MCP block from {yaml_path} (Codex never read it)")
        except OSError:
            pass

    json_path = codex_dir / "config.json"
    if json_path.exists():
        try:
            data = _json.loads(json_path.read_text())
            servers = data.get("mcpServers") if isinstance(data, dict) else None
            if isinstance(servers, dict):
                removed = False
                for name in ("llm_router", "llm-router"):
                    ent = servers.get(name)
                    if isinstance(ent, dict) and (
                        ent.get("command") in ("llm-router", "llm_router")
                        or "claude-code-llm-router" in (ent.get("args") or [])
                    ):
                        servers.pop(name)
                        removed = True
                if removed:
                    if servers or len(data) > 1:
                        json_path.write_text(_json.dumps(data, indent=2))
                    else:
                        json_path.unlink()
                    actions.append(f"✓ Removed legacy MCP entry from {json_path} (Codex never read it)")
        except (OSError, ValueError):
            pass

    rules_md = codex_dir / "rules" / "llm_router.md"
    if rules_md.exists():
        try:
            if rules_md.read_text().lstrip().startswith("<!-- llm_router-rules-version"):
                rules_md.unlink()
                actions.append(f"✓ Removed legacy {rules_md} (Codex reads AGENTS.md, not rules/*.md)")
        except OSError:
            pass

    instructions = codex_dir / "instructions.md"
    if instructions.exists():
        try:
            rec = install_manifest.find("text_block", instructions)
            created = install_manifest.find("created_file", instructions)
            content = instructions.read_text()
            if rec and rec.get("block") and rec["block"] in content:
                remaining = content.replace(rec["block"], "", 1)
                if remaining.strip():
                    instructions.write_text(remaining)
                else:
                    instructions.unlink()
                actions.append(f"✓ Removed legacy routing rules from {instructions}")
            elif created and content.lstrip().startswith("<!-- llm_router-rules-version"):
                instructions.unlink()
                actions.append(f"✓ Removed legacy {instructions}")
        except OSError:
            pass

    return actions


def _merge_json_mcp_block(
    config_path, server_name: str, server_entry: dict, root_key: str = "mcpServers"
) -> list[str]:
    """Merge an MCP server entry into a JSON config file. Returns list of action strings."""
    import json as _json
    import pathlib

    actions: list[str] = []
    config_path = pathlib.Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing = {}
    if config_path.exists():
        try:
            existing = _json.loads(config_path.read_text())
        except Exception:
            existing = {}

    servers = existing.setdefault(root_key, {})
    if server_name not in servers:
        servers[server_name] = server_entry
        config_path.write_text(_json.dumps(existing, indent=2))
        actions.append(f"✓ Added llm_router MCP server to {config_path}")
        # RED2-9-*: record so uninstall reverses it automatically.
        try:
            from llm_router import install_manifest
            install_manifest.record("json_mcp", config_path, root_key=root_key, server=server_name)
        except Exception:
            pass
    else:
        actions.append(f"  llm_router already in {config_path} (skipped)")
    return actions


def _remove_json_mcp_block(
    config_path, server_name: str = "llm_router", root_key: str = "mcpServers"
) -> list[str]:
    """RED2-8-01: inverse of _merge_json_mcp_block — surgically remove the llm_router
    MCP entry from a host's JSON config, leaving every other server intact."""
    import json as _json
    import pathlib

    actions: list[str] = []
    config_path = pathlib.Path(config_path)
    if not config_path.exists():
        return actions
    try:
        existing = _json.loads(config_path.read_text())
    except Exception:
        return actions
    servers = existing.get(root_key)
    if isinstance(servers, dict) and server_name in servers:
        del servers[server_name]
        try:
            config_path.write_text(_json.dumps(existing, indent=2))
            actions.append(f"✓ Removed llm_router MCP server from {config_path}")
        except OSError as e:
            actions.append(f"  Could not update {config_path}: {e}")
    return actions


def _remove_toml_table_block(text: str, header: str) -> str:
    """Remove a `[header]` TOML table and its body (until the next table / EOF).

    RED1-9-02: the body must consume only lines that do NOT begin a new [table].
    The prior regex used a `(?!\\n\\[)` lookahead that, once the newline was
    consumed, no longer guarded the next line — so adjacent tables NOT separated
    by a blank line (valid TOML) were swallowed through EOF, destroying unrelated
    provider config. The corrected body anchors each line at ^ (MULTILINE) and
    stops at the first line starting with '['.
    """
    import re
    pattern = re.compile(
        rf'(?m)^\[{re.escape(header)}\][^\n]*\n(?:(?!\[).*(?:\n|$))*'
    )
    return pattern.sub("", text, count=1)


def uninstall_host_integrations() -> list[str]:
    """RED2-8-01: remove the live MCP registrations that `llm-router install --host
    <codex|cursor|gemini-cli|vscode|copilot-cli|openclaw|trae|pi>` wrote, so a
    `llm_router uninstall` does not leave dangling `llm_router` entries that break those
    tools after `pip uninstall`. Each remover is home-scoped and defensive — a
    no-op when the host was never installed, and never aborts on one host's error.

    NOTE: this is the enumerated LEGACY fallback for the home-directory MCP
    registrations. New installs are covered authoritatively by the install
    manifest (install_manifest.apply_uninstall), which also handles the
    project-scoped writers (.github/copilot-instructions.md, Trae .rules) via
    their recorded created/appended entries. uninstall_ide_configs() removes the
    project MCP/rule files it knows (.vscode/mcp.json, .windsurf/mcp.json,
    .cursor/rules/use-llm_router.mdc) against the cwd.
    """
    import pathlib
    import shutil as _shutil
    import sys

    actions: list[str] = []
    home = pathlib.Path.home()

    # JSON MCP registrations (root_key varies by host).
    json_targets = [
        (home / ".gemini" / "settings.json", "mcpServers"),
        (home / ".cursor" / "mcp.json", "mcpServers"),
        (home / ".config" / "gh" / "copilot" / "mcp.json", "mcpServers"),
        (home / ".openclaw" / "mcp.json", "mcpServers"),
        (home / ".config" / "opencode" / "config.json", "mcpServers"),  # RED2-9-01
        (home / ".pi" / "agent" / "mcp.json", "mcpServers"),  # P212-6 (D-315)
    ]
    if sys.platform == "darwin":
        json_targets.append((home / "Library" / "Application Support" / "Code" / "User" / "mcp.json", "servers"))
        json_targets.append((home / "Library" / "Application Support" / "Trae" / "mcp.json", "mcpServers"))
    elif sys.platform == "win32":
        appdata = pathlib.Path(home / "AppData" / "Roaming")
        json_targets.append((appdata / "Code" / "User" / "mcp.json", "servers"))
        json_targets.append((appdata / "Trae" / "mcp.json", "mcpServers"))
    else:
        json_targets.append((home / ".config" / "Code" / "User" / "mcp.json", "servers"))
        json_targets.append((home / ".config" / "Trae" / "mcp.json", "mcpServers"))

    for path, root_key in json_targets:
        try:
            actions += _remove_json_mcp_block(path, "llm_router", root_key)
        except Exception as e:  # noqa: BLE001 — one host must never abort the rest
            actions.append(f"  host cleanup skipped for {path}: {e}")

    # Gemini CLI extension directory (whole llm_router extension).
    try:
        ext_dir = home / ".gemini" / "extensions" / "llm_router"
        if ext_dir.exists():
            _shutil.rmtree(ext_dir, ignore_errors=True)
            actions.append(f"✓ Removed Gemini CLI extension dir {ext_dir}")
    except Exception as e:  # noqa: BLE001
        actions.append(f"  gemini extension cleanup skipped: {e}")

    # Cursor routing-rules file llm_router authored.
    try:
        cursor_rules = home / ".cursor" / "rules" / "llm_router.md"
        if cursor_rules.exists():
            cursor_rules.unlink()
            actions.append(f"✓ Removed {cursor_rules}")
    except Exception as e:  # noqa: BLE001
        actions.append(f"  cursor rules cleanup skipped: {e}")

    # Codex: remove the gateway TOML table and the appended config.yaml MCP block.
    try:
        codex_dir = home / ".codex"
        config_toml = codex_dir / "config.toml"
        if config_toml.exists():
            original = config_toml.read_text()
            updated = _remove_toml_table_block(original, "model_providers.llm_router")
            if updated != original:
                # RED2-10-05: no persistent .llm_router-bak (uninstall leaves nothing
                # llm_router-authored); the removal regex is ^-anchored + tested.
                config_toml.write_text(updated)
                actions.append(f"✓ Removed [model_providers.llm_router] from {config_toml}")
            # Tool-approval tables outlive a manifest replay when Codex wrote
            # them; with the server table gone they make Codex refuse to start.
            text = config_toml.read_text()
            cleaned, cleanup = _clear_codex_orphan_tables(text, config_toml)
            if cleaned != text:
                config_toml.write_text(cleaned)
            actions.extend(cleanup)
        config_yaml = codex_dir / "config.yaml"
        if config_yaml.exists():
            y = config_yaml.read_text()
            mcp_block = (
                "\nmcp:\n"
                "  servers:\n"
                "    llm_router:\n"
                "      command: llm-router\n"
                "      args: []\n"
            )
            if mcp_block in y:
                config_yaml.write_text(y.replace(mcp_block, ""))
                actions.append(f"✓ Removed llm_router MCP block from {config_yaml}")
        # RED2-9-02: strip the LIVE llm_router PostToolUse entry from ~/.codex/hooks.json
        # (it invokes codex-post-tool.py on every Bash call — a phantom hook after
        # uninstall). Filter out any PostToolUse entry that references llm_router's hook.
        import json as _json
        hooks_json = codex_dir / "hooks.json"
        if hooks_json.exists():
            try:
                cur = _json.loads(hooks_json.read_text())
                ptu = cur.get("hooks", {}).get("PostToolUse", [])
                kept = [
                    entry for entry in ptu
                    if not any(
                        "codex-post-tool.py" in str(h.get("command", "")) or ".llm_router/hooks" in str(h.get("command", ""))
                        for h in entry.get("hooks", [])
                    )
                ]
                if len(kept) != len(ptu):
                    cur.setdefault("hooks", {})["PostToolUse"] = kept
                    hooks_json.write_text(_json.dumps(cur, indent=2))
                    actions.append(f"✓ Removed llm_router PostToolUse hook from {hooks_json}")
            except (OSError, ValueError):
                pass
    except Exception as e:  # noqa: BLE001
        actions.append(f"  codex cleanup skipped: {e}")

    return actions


def _append_routing_rules(dest_path, rules_filename: str) -> list[str]:
    """Append routing rules from src/rules/ to dest_path. Returns action strings.

    RED1-20 (P0): tool names are LOCALIZED against the active surface before the
    file is written. They were not, and this is the function serving the ten
    NON-Claude hosts — Cursor, Windsurf, Copilot, Gemini CLI, opencode, Trae and
    the rest. The Claude Code path had already been fixed (CHZ-SURF-01, via
    `_localized_rules_text`); every other host still got the bundle verbatim, so
    a rules file naming `llm_code` taught those models to make a call that 404s,
    in every session, for the life of the file.

    That asymmetry is the finding, not a detail of it. Vendor-neutrality is the
    North Star's central claim and the vendor-neutral path was the unlocalized one.

    This is now the ONLY copy. `cli.py` held a second implementation that also
    lacked the resolver AND never recorded to the install manifest, so the rules
    files it wrote survived `llm_router uninstall`. Two implementations of one
    installer is the RED8-06 duplicate-source class, and is precisely why one got
    fixed and the other did not.
    """
    import pathlib

    from llm_router.install_hooks import _localized_rules_text

    actions: list[str] = []
    dest_path = pathlib.Path(dest_path)
    rules_src = pathlib.Path(__file__).parent.parent / "rules" / rules_filename
    if not rules_src.exists():
        return actions
    rules_text = _localized_rules_text(rules_src)
    if dest_path.exists():
        existing = dest_path.read_text()
        if "llm_router" not in existing:
            block = f"\n\n{rules_text}"
            with dest_path.open("a") as f:
                f.write(block)
            actions.append(f"✓ Appended routing rules to {dest_path}")
            try:
                from llm_router import install_manifest
                install_manifest.record("text_block", dest_path, block=block)
            except Exception:
                pass
        else:
            actions.append(f"  Routing rules already in {dest_path} (skipped)")
    else:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(rules_text)
        actions.append(f"✓ Created {dest_path} with routing rules")
        try:
            from llm_router import install_manifest
            # RED1-10-02: record the exact text so uninstall strips only llm_router's
            # content and preserves anything the user appended later.
            install_manifest.record("created_file", dest_path, block=rules_text)
        except Exception:
            pass
    return actions


def _copy_hook_script(hook_filename: str, dest_dir) -> tuple[str, list[str]]:
    """Copy a hook script from src/hooks/ to dest_dir. Returns (dest_path_str, actions)."""
    import pathlib
    import shutil as _shutil

    actions: list[str] = []
    dest_dir = pathlib.Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    src = pathlib.Path(__file__).parent.parent / "hooks" / hook_filename
    dest = dest_dir / hook_filename
    if src.exists():
        _shutil.copy2(src, dest)
        dest.chmod(0o755)
        actions.append(f"✓ Installed hook script to {dest}")
        try:
            from llm_router import install_manifest
            install_manifest.record("file", dest)
        except Exception:
            pass
    return str(dest), actions


def _install_opencode_files() -> list[str]:
    """Write OpenCode-specific config files and return a list of actions taken."""
    import pathlib

    actions: list[str] = []
    home = pathlib.Path.home()
    opencode_dir = home / ".config" / "opencode"
    opencode_dir.mkdir(parents=True, exist_ok=True)

    # 1. MCP server entry
    server_entry = {"command": "llm-router", "args": []}
    actions += _merge_json_mcp_block(opencode_dir / "config.json", "llm_router", server_entry)

    # 2. Hook script
    hook_dest, hook_actions = _copy_hook_script(
        "opencode-post-tool.py", paths.state_path("hooks")
    )
    actions += hook_actions

    # 3. Routing rules
    actions += _append_routing_rules(opencode_dir / "instructions.md", "opencode-rules.md")

    return actions


_GEMINI_EVENTS = ("SessionStart", "PostToolUse", "UserPromptSubmit", "SessionEnd")
_GEMINI_USER_DISABLED = "user disabled (legacy enabled:false)"


def _gemini_event_list(value) -> tuple[list, str | None]:
    """The hook list for one Gemini hooks.json event, plus a note when the value was reset.
    Older installs wrote an event as a toggle dict (``"PostToolUse": {"enabled": true}``)
    that carries no command: it becomes ``[]`` (a dict holding a ``command`` becomes one
    entry, ``enabled`` dropped). A list is returned unchanged. Any other shape is reset to
    ``[]`` with a note; the caller backs the original file up first."""
    if isinstance(value, list):
        return value, None
    if isinstance(value, dict):
        if value.get("command"):
            return [{k: v for k, v in value.items() if k != "enabled"}], None
        if set(value) <= {"enabled"}:
            return [], None
    return [], "reset non-list {event} (backed up)"


def _write_text_atomic(path: Path, text: str) -> None:
    """Temp file in the same directory, fsync, os.replace; the file's mode is kept."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _install_gemini_cli_files() -> list[str]:
    """Write Gemini CLI-specific config files and return a list of actions taken."""
    import json as _json
    import pathlib

    actions: list[str] = []
    home = pathlib.Path.home()
    gemini_dir = home / ".gemini"
    gemini_dir.mkdir(parents=True, exist_ok=True)

    # 1. MCP server entry in ~/.gemini/settings.json
    server_entry = {"command": "llm-router", "args": []}
    actions += _merge_json_mcp_block(gemini_dir / "settings.json", "llm_router", server_entry)

    # 2. Extension manifest + hooks directory
    ext_dir = gemini_dir / "extensions" / "llm_router"
    _ext_existed = ext_dir.exists()
    ext_dir.mkdir(parents=True, exist_ok=True)
    if not _ext_existed:
        try:
            from llm_router import install_manifest
            install_manifest.record("dir", ext_dir)
        except Exception:
            pass

    ext_manifest = ext_dir / "gemini-extension.json"
    if not ext_manifest.exists():
        manifest = {
            "name": "llm_router",
            "version": "9.0.1",
            "description": "Route tasks to cheapest capable model — 20+ providers",
            "mcpServers": {"llm_router": server_entry},
        }
        ext_manifest.write_text(_json.dumps(manifest, indent=2))
        actions.append(f"✓ Created Gemini CLI extension manifest at {ext_manifest}")
    else:
        actions.append(f"  Extension manifest already exists at {ext_manifest} (skipped)")

    # 3. Hook scripts
    post_hook_dest, hook_actions = _copy_hook_script(
        "gemini-cli-post-tool.py", paths.state_path("hooks")
    )
    actions += hook_actions

    auto_hook_dest, hook_actions = _copy_hook_script(
        "gemini-cli-auto-route.py", paths.state_path("hooks")
    )
    actions += hook_actions

    status_hook_dest, hook_actions = _copy_hook_script(
        "status-bar.py", paths.state_path("hooks")
    )
    actions += hook_actions

    end_hook_dest, hook_actions = _copy_hook_script(
        "gemini-cli-session-end.py", paths.state_path("hooks")
    )
    actions += hook_actions

    start_hook_dest, hook_actions = _copy_hook_script(
        "session-start.py", paths.state_path("hooks")
    )
    actions += hook_actions

    # 4. Extension hooks.json
    hooks_dir = ext_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hooks_json = hooks_dir / "hooks.json"
    hook_entry = {
        "hooks": {
            "SessionStart": [
                {"matcher": "*", "command": start_hook_dest}
            ],
            "PostToolUse": [
                {"matcher": "*", "command": post_hook_dest}
            ],
            "UserPromptSubmit": [
                {"matcher": "*", "command": auto_hook_dest},
                {"matcher": "*", "command": status_hook_dest}
            ],
            "SessionEnd": [
                {"matcher": "*", "command": end_hook_dest}
            ]
        }
    }
    if not hooks_json.exists():
        hooks_json.write_text(_json.dumps(hook_entry, indent=2))
        actions.append(f"✓ Created Gemini CLI hooks.json at {hooks_json}")
    else:
        # Merge if missing
        try:
            current = _json.loads(hooks_json.read_text())
            original_text = hooks_json.read_text()
            hooks = current.setdefault("hooks", {})
            if any(isinstance(v, dict) and v.get("enabled") is False for v in hooks.values()):
                # The user switched the hooks off by hand; honour it, register nothing.
                actions.insert(0, f"SKIP {_GEMINI_USER_DISABLED}")
                actions.append(f"  {hooks_json}: enabled:false -- hooks left unregistered")
                actions += _append_routing_rules(ext_dir / "INSTRUCTIONS.md", "gemini-cli-rules.md")
                return actions
            for _event in _GEMINI_EVENTS:
                hooks[_event], _note = _gemini_event_list(hooks.get(_event))
                if _note:
                    actions.append("  " + _note.format(event=_event))

            # SessionStart
            start_hooks = hooks.setdefault("SessionStart", [])
            if not any(start_hook_dest in str(h) for h in start_hooks):
                start_hooks.append({"matcher": "*", "command": start_hook_dest})
                actions.append("✓ Added SessionStart hook to Gemini CLI")
            
            # PostToolUse
            post_hooks = hooks.setdefault("PostToolUse", [])
            if not any(post_hook_dest in str(h) for h in post_hooks):
                post_hooks.append({"matcher": "*", "command": post_hook_dest})
                actions.append("✓ Added PostToolUse hook to Gemini CLI")
            
            # UserPromptSubmit
            auto_hooks = hooks.setdefault("UserPromptSubmit", [])
            if not any(auto_hook_dest in str(h) for h in auto_hooks):
                auto_hooks.append({"matcher": "*", "command": auto_hook_dest})
                actions.append("✓ Added UserPromptSubmit hook to Gemini CLI")
            
            if not any(status_hook_dest in str(h) for h in auto_hooks):
                auto_hooks.append({"matcher": "*", "command": status_hook_dest})
                actions.append("✓ Added status-bar hook to Gemini CLI")

            # SessionEnd
            end_hooks = hooks.setdefault("SessionEnd", [])
            if not any(end_hook_dest in str(h) for h in end_hooks):
                end_hooks.append({"matcher": "*", "command": end_hook_dest})
                actions.append("✓ Added SessionEnd hook to Gemini CLI")
            
            new_text = _json.dumps(current, indent=2)
            if _json.loads(new_text) != _json.loads(original_text):
                from llm_router.install_hooks import _backup_before_overwrite
                backup = _backup_before_overwrite(hooks_json)
                if backup is None:
                    actions.append(f"  Could not back up {hooks_json}; left unchanged")
                else:
                    _write_text_atomic(hooks_json, new_text)
                    actions.append(f"✓ Backed up {hooks_json} to {backup}")
        except Exception as e:
            actions.append(f"  Could not update {hooks_json}: {e}")

    # 5. Routing rules
    actions += _append_routing_rules(ext_dir / "INSTRUCTIONS.md", "gemini-cli-rules.md")

    return actions


# ── Pi coding agent (pi.dev): MCP config only (D-315) ─────────────────────────
#
# Pi 0.99.1 reads MCP servers from ~/.pi/agent/mcp.json ({"mcpServers": {...}}, the shape
# Claude Code and Cursor use; Pi's docs/mcp.md) and checks them with `pi mcp list`, which
# connects to every enabled server and exits 1 while any of them is not connected. No hooks,
# no extension: the end-of-turn line on Pi is planned in 16.1.

PI_SERVER = "llm_router"
PI_SMOKE_TIMEOUT_S = 60
PI_REASON_MAX = 300  # receipt reasons/details stay short; Pi can echo a server's whole stderr
_PI_LEGACY_NAMES = ("llm-router",)
_PI_RERUN = "then rerun `llm-router install --host pi`"


def pi_agent_dir() -> Path:
    return Path.home() / ".pi" / "agent"


def pi_mcp_path() -> Path:
    return pi_agent_dir() / "mcp.json"


def _clip(text: object, limit: int = PI_REASON_MAX) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= limit else f"{s[:limit]}… ({len(s) - limit} more chars)"


def _read_pi_mcp(path: Path) -> tuple[dict, str | None]:
    """(config, None), or ({}, why the file is refused). A missing file is an empty config."""
    import json as _json

    if not path.exists():
        return {}, None
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return {}, f"{path} could not be read ({_clip(exc)}); left unchanged"
    except ValueError as exc:
        return {}, f"{path} is not valid JSON ({_clip(exc)}); left unchanged. Fix it, {_PI_RERUN}"
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        return {}, (f"{path} has no mcpServers object (Pi expects {{\"mcpServers\": {{...}}}}); left unchanged. "
                    f"Fix it, {_PI_RERUN}")
    return data, None


def _pi_write_refusal(path: Path) -> str | None:
    """Why ``path`` must not be replaced: a symlink (dotfile managers; replacing it would fork
    the user's managed config) or a file the user made read-only."""
    if path.is_symlink():
        try:
            target = os.readlink(path)
        except OSError:
            target = "?"
        return (f"{path} is a symlink (to {target}); left unchanged. Add "
                '"llm_router": {"command": "llm-router", "args": []} under mcpServers in the file it points at')
    if path.exists() and not os.access(path, os.W_OK):
        return f"{path} is read-only; left unchanged. Make it writable, {_PI_RERUN}"
    return None


def _is_router_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    args = entry.get("args") if isinstance(entry.get("args"), list) else []
    return (entry.get("command") in ("llm-router", "llm_router")
            or any(str(a) in ("claude-code-llm-router", "llm-routing") for a in args))


def _write_pi_mcp(path: Path, text: str) -> None:
    """Atomic: a unique temp file in the same dir, fsync, os.replace. An existing file keeps
    its mode; a new one is 0600 in dirs created 0700 (users later add bearer headers here)."""
    import tempfile

    from llm_router import state_files

    state_files.private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


def _install_pi_files() -> list[str]:
    """Add ``mcpServers.llm_router`` to ~/.pi/agent/mcp.json. Every other key and server is
    kept; an existing ``llm_router`` entry is left as it is; the file's mode is kept. A file
    that is not valid JSON, has no ``mcpServers`` object, is a symlink or is read-only is
    refused, not touched: the first action is then ``SKIP <reason>``."""
    import json as _json

    from llm_router import install_manifest

    path = pi_mcp_path()
    data, refusal = _read_pi_mcp(path)
    refusal = _pi_write_refusal(path) or refusal
    if refusal:
        return [f"SKIP {refusal}"]
    servers = data.setdefault("mcpServers", {})
    actions: list[str] = []
    if PI_SERVER in servers:
        actions.append(f"  llm_router already in {path} (left as it is)")
    else:
        servers[PI_SERVER] = {"command": "llm-router", "args": []}
        _write_pi_mcp(path, _json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        install_manifest.record("json_mcp", path, root_key="mcpServers", server=PI_SERVER)
        actions.append(f"✓ Added llm_router MCP server to {path}")
    for name in _PI_LEGACY_NAMES:
        if _is_router_entry(servers.get(name)):
            actions.append(f"  {path} also has an older llm-router entry '{name}' (Pi lists its tools twice); "
                           f"remove it with `pi mcp remove {name}`")
    return actions


def _pi_binary() -> str | None:
    found = shutil.which("pi")
    return os.path.abspath(found) if found else None  # a relative PATH entry; the smoke runs elsewhere


def pi_smoke(agent_dir: Path | None = None, *, run=None) -> dict:
    """``pi mcp list --json`` against ``agent_dir`` (default ~/.pi/agent).

    ``{"status": "skipped"}`` when no ``pi`` binary is on PATH (never a failure);
    ``passed`` when Pi lists the llm_router server as connected with at least one tool;
    else ``failed``, also for any output this parser does not expect. Only our server is
    judged: Pi exits 1 when any other server in the user's file is down, and the detail says
    so. Note ``pi mcp list`` starts every enabled server in the file."""
    import json as _json
    import subprocess

    pi = _pi_binary()
    if pi is None:
        return {"status": "skipped", "detail": "pi binary not on PATH"}
    agent_dir = agent_dir or pi_agent_dir()
    run = run or subprocess.run
    try:
        proc = run([pi, "mcp", "list", "--json"], capture_output=True, text=True, encoding="utf-8",
                   errors="replace", stdin=subprocess.DEVNULL, timeout=PI_SMOKE_TIMEOUT_S, cwd=str(agent_dir),
                   env={**os.environ, "PI_CODING_AGENT_DIR": str(agent_dir)})
    except subprocess.TimeoutExpired:
        return {"status": "failed", "detail": f"`pi mcp list` did not finish in {PI_SMOKE_TIMEOUT_S}s"}
    except (OSError, ValueError) as exc:
        return {"status": "failed", "detail": _clip(f"`pi mcp list` could not run: {exc}")}
    out, err = proc.stdout or "", proc.stderr or ""
    try:
        data = _json.loads(out)
    except ValueError:
        return {"status": "failed",
                "detail": _clip(f"`pi mcp list` exited {proc.returncode} without JSON: {(err or out).strip()[-200:]}")}
    if not isinstance(data, dict) or not isinstance(data.get("servers", []), list):
        return {"status": "failed", "detail": "`pi mcp list --json` output has no servers list"}
    errors = data.get("errors") if isinstance(data.get("errors"), list) else []
    server = next((s for s in data.get("servers", []) if isinstance(s, dict) and s.get("name") == PI_SERVER), None)
    if server is None:
        why = "; ".join(str(e) for e in errors)
        return {"status": "failed", "detail": _clip(f"llm_router not listed by `pi mcp list`{': ' + why if why else ''}")}
    tools = server.get("tools") if isinstance(server.get("tools"), list) else []
    if server.get("state") != "connected" or not tools:
        why = server.get("error") or f"{len(tools)} tools"
        return {"status": "failed", "detail": _clip(f"llm_router {server.get('state')}: {why}")}
    detail = f"llm_router connected, {len(tools)} tools"
    if proc.returncode != 0:
        detail += f"; `pi mcp list` exited {proc.returncode} because of another server"
    return {"status": "passed", "detail": detail}


def pi_row() -> dict:
    """Receipt row for Pi after ``_install_pi_files`` returned: ``wired`` when mcp.json
    registers llm_router and the smoke did not fail; the smoke result rides along in
    ``smoke`` (left out of the digest, like ``via``). Never raises for an odd smoke."""
    data, refusal = _read_pi_mcp(pi_mcp_path())
    if refusal or PI_SERVER not in data.get("mcpServers", {}):
        return {"status": "error", "reason": refusal or f"llm_router missing from {pi_mcp_path()} after install",
                "via": "files", "hook_version": None}
    try:
        smoke = pi_smoke()
    except Exception as exc:  # noqa: BLE001 -- an odd smoke is a failed smoke, never an aborted install
        from llm_router import failopen
        failopen.record("P212-PI-SMOKE", exc)
        smoke = {"status": "failed", "detail": _clip(f"smoke crashed: {type(exc).__name__}: {exc}")}
    row = {"status": "wired", "reason": None, "via": "files", "hook_version": None, "smoke": smoke}
    if smoke["status"] == "failed":
        row.update(status="error", reason=f"pi mcp list: {smoke['detail']}")
    return row


def _install_copilot_cli_files() -> list[str]:
    """Write GitHub Copilot CLI config files and return a list of actions taken."""
    import pathlib

    actions: list[str] = []
    home = pathlib.Path.home()

    # Copilot CLI MCP config — ~/.config/gh/copilot/mcp.json
    copilot_dir = home / ".config" / "gh" / "copilot"
    copilot_dir.mkdir(parents=True, exist_ok=True)

    server_entry = {"command": "llm-router", "args": []}
    actions += _merge_json_mcp_block(copilot_dir / "mcp.json", "llm_router", server_entry)

    # Routing rules → ~/.config/gh/copilot/instructions.md
    actions += _append_routing_rules(
        copilot_dir / "instructions.md", "copilot-cli-rules.md"
    )

    return actions


def _install_openclaw_files() -> list[str]:
    """Write OpenClaw config files and return a list of actions taken."""
    import pathlib

    actions: list[str] = []
    home = pathlib.Path.home()
    openclaw_dir = home / ".openclaw"
    openclaw_dir.mkdir(parents=True, exist_ok=True)

    server_entry = {"command": "llm-router", "args": []}
    actions += _merge_json_mcp_block(openclaw_dir / "mcp.json", "llm_router", server_entry)
    actions += _append_routing_rules(openclaw_dir / "instructions.md", "openclaw-rules.md")

    return actions


def _install_trae_files() -> list[str]:
    """Write Trae IDE config files and return a list of actions taken."""
    import pathlib
    import sys

    actions: list[str] = []
    home = pathlib.Path.home()

    # Trae config location differs by platform
    if sys.platform == "darwin":
        trae_dir = home / "Library" / "Application Support" / "Trae"
    elif sys.platform == "win32":
        trae_dir = pathlib.Path(home / "AppData" / "Roaming" / "Trae")
    else:
        trae_dir = home / ".config" / "Trae"
    trae_dir.mkdir(parents=True, exist_ok=True)

    server_entry = {"command": "llm-router", "args": []}
    actions += _merge_json_mcp_block(trae_dir / "mcp.json", "llm_router", server_entry)

    # .rules file in current project directory (Trae-specific pattern)
    rules_dest = pathlib.Path(".rules")
    actions += _append_routing_rules(rules_dest, "trae-rules.md")

    return actions


def _install_factory_files() -> list[str]:
    """Factory Droid uses .claude-plugin/ format natively — just confirm it's present."""
    import pathlib

    actions: list[str] = []
    plugin_dir = pathlib.Path(__file__).parent.parent.parent.parent / ".factory-plugin"
    if plugin_dir.exists():
        actions.append("✓ .factory-plugin/ manifest present — Factory Droid will auto-load it")
        actions.append("  Install via: factory plugin install ypollak2/llm_router")
    else:
        actions.append("  .factory-plugin/ not found in repo root — run from the llm_router repo dir")
    actions.append("  Or install .claude-plugin/ directly: factory plugin install ypollak2/llm_router")
    return actions


def _install_vscode_files() -> list[str]:
    """Write VS Code MCP config and routing rules. Returns list of actions taken."""
    import pathlib
    import sys

    actions: list[str] = []
    home = pathlib.Path.home()

    # Platform-specific user mcp.json location
    if sys.platform == "darwin":
        mcp_json = home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
    elif sys.platform == "win32":
        appdata = pathlib.Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        mcp_json = appdata / "Code" / "User" / "mcp.json"
    else:
        mcp_json = home / ".config" / "Code" / "User" / "mcp.json"

    server_entry = {"command": "llm-router", "args": []}
    actions += _merge_json_mcp_block(mcp_json, "llm_router", server_entry, root_key="servers")

    # Append routing guidance to .github/copilot-instructions.md in cwd (if it exists)
    copilot_instructions = pathlib.Path.cwd() / ".github" / "copilot-instructions.md"
    actions += _append_routing_rules(copilot_instructions, "vscode-rules.md")

    return actions


def _install_windsurf_files() -> list[str]:
    """Write Windsurf MCP config (project-scoped .windsurf/mcp.json). RED2-10-03:
    windsurf is documented in README/--help but had no installer, so
    `llm-router install --host windsurf` was rejected as an unknown host."""
    import pathlib

    actions: list[str] = []
    mcp_json = pathlib.Path.cwd() / ".windsurf" / "mcp.json"
    server_entry = {"command": "llm-router", "args": []}
    actions += _merge_json_mcp_block(mcp_json, "llm_router", server_entry, root_key="mcpServers")
    return actions


def _install_cursor_files() -> list[str]:
    """Write Cursor IDE MCP config and routing rules. Returns list of actions taken."""
    import pathlib

    actions: list[str] = []
    home = pathlib.Path.home()

    # Global Cursor MCP config (applies across all projects)
    mcp_json = home / ".cursor" / "mcp.json"
    server_entry = {"command": "llm-router", "args": []}
    actions += _merge_json_mcp_block(mcp_json, "llm_router", server_entry, root_key="mcpServers")

    # Append routing rules to ~/.cursor/rules/llm_router.md
    cursor_rules = home / ".cursor" / "rules" / "llm_router.md"
    actions += _append_routing_rules(cursor_rules, "cursor-rules.md")

    return actions


def _install_host(host: str, mode: str = "auto") -> None:
    """Install config for non-Claude Code hosts (writes files for Codex; prints snippets for others)."""
    bold = "\033[1m" if _color_enabled() else ""
    reset = "\033[0m" if _color_enabled() else ""

    if host in _UNSUPPORTED_HOSTS:
        print(f"unsupported: {_UNSUPPORTED_HOSTS[host]}; planned in 16.1")
        sys.exit(2)

    hosts_to_show = list(_HOST_SNIPPETS.keys()) if host == "all" else [host]
    unknown = [h for h in hosts_to_show if h not in _HOST_SNIPPETS]
    if unknown:
        print(f"Unknown host(s): {', '.join(unknown)}")
        print(f"Valid options: {', '.join(_HOST_SNIPPETS)} or 'all'")
        return

    w = shutil.get_terminal_size((80, 24)).columns
    print(f"\n{bold}llm-router install --host {host}{reset}\n")
    print("─" * min(w, 70))

    # Hosts that write files; all others print snippets
    _FILE_WRITERS = {
        "codex":      (lambda: _install_codex_files(mode="mcp" if mode == "auto" else mode),
                       "Restart Codex and run `llm-router doctor --host codex` to verify."),
        "opencode":   (_install_opencode_files,     f"Restart OpenCode and run {route_tool('llm_savings')} to verify."),
        "gemini-cli": (_install_gemini_cli_files,   f"Restart Gemini CLI and run {route_tool('llm_savings')} to verify."),
        "copilot-cli":(_install_copilot_cli_files,  f"Restart Copilot CLI and run {route_tool('llm_savings')} to verify."),
        "openclaw":   (_install_openclaw_files,     f"Restart OpenClaw and run {route_tool('llm_savings')} to verify."),
        "trae":       (_install_trae_files,         f"Restart Trae IDE and run {route_tool('llm_savings')} to verify."),
        "factory":    (_install_factory_files,      "Run: factory plugin install ypollak2/llm_router"),
        "vscode":     (_install_vscode_files,       "Restart VS Code and enable MCP in Copilot settings."),
        "cursor":     (_install_cursor_files,       f"Restart Cursor and run {route_tool('llm_savings')} to verify."),
        "windsurf":   (_install_windsurf_files,     f"Restart Windsurf and run {route_tool('llm_savings')} to verify."),
        "pi":         (_install_pi_files,           "Run `pi mcp list` to verify (llm_router: connected), then /reload in Pi."),
    }

    pi_failed = False
    for h in hosts_to_show:
        if h == "pi" and host == "all":
            from llm_router import host_detect
            if not host_detect.detect_hosts()["pi"].present:
                print("  pi: not detected (no `pi` binary, no ~/.pi); skipped\n")
                print("─" * min(w, 70))
                continue
        if h in _FILE_WRITERS:
            install_fn, verify_hint = _FILE_WRITERS[h]
            label = _HOST_SNIPPETS[h].format(bold=bold, reset=reset, **_SNIPPET_TOOLS).strip()
            print(f"{label}\n")
            actions = install_fn()
            for action in actions:
                print(f"  {action}")
            print()
            if h == "pi":
                # The same check run_full puts in the receipt; a refusal or a failed smoke exits 1.
                row = None if actions and actions[0].startswith("SKIP ") else pi_row()
                smoke = row.get("smoke") if row else None
                if smoke is not None:
                    print(f"  pi mcp list: {smoke['status']}: {smoke['detail']}")
                pi_failed = row is None or row["status"] != "wired"
                if pi_failed:
                    print(f"  Pi not wired: {row['reason'] if row else 'see the SKIP line above'}")
                else:
                    print(f"  {verify_hint}")
            else:
                print(f"  {verify_hint}")
        else:
            snippet = _HOST_SNIPPETS[h].format(bold=bold, reset=reset, **_SNIPPET_TOOLS)
            print(snippet)
        print("─" * min(w, 70))

    print(
        f"\nFor Claude Code (hooks + full cost-routing): {bold}llm-router install{reset}\n"
        f"See docs/hosts/ for setup guides and trade-off explanations.\n"
    )
    if pi_failed:
        sys.exit(1)
