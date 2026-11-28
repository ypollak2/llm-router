"""``llm-router setup-local [--yes]`` — opt-in local routing on a 48 GB+ Mac.

Installs a supervised, dedicated Ollama on 127.0.0.1:11435, pulls
``qwen3.6:35b-a3b-coding``, pins it resident at the window the proxy asks for, and writes the
three ``.env`` keys the proxy reads. It then tells you to run ``llm-router install --proxy-default``,
which (because this command left a sentinel) gives the main proxy the local-agent arguments.

Order is chosen so a refusal changes nothing: RAM gate, ollama binary, peer GPU-memory check and
consent all come first; the service file is written only after them. The core, ``run_setup_local``,
takes every side effect (launchctl, HTTP, the pull, prompts, the RAM probe) as a required keyword
argument, so a test cannot reach the machine by forgetting one. Only ``cmd_setup_local`` builds the
real ones.
"""

from __future__ import annotations

import os
import platform
import shlex
import sys
from pathlib import Path

from llm_router import local_ollama as lo, paths, proxy_default as pd
from llm_router.commands.proxy_default import _bold, _green, _red, _stale_plist_note, _read_or_none

_HELP = f"""\
llm-router setup-local — opt-in local routing (needs {lo.MIN_RAM_GB} GB+ RAM)

Usage:
  llm-router setup-local          Ask, then install (a terminal is required to be asked)
  llm-router setup-local --yes    Consent to the download and the background service
  llm-router setup-local --preload  Reload the model after a reboot (run by the login job; exits non-zero on failure)
  llm-router setup-local --help   Show this help (no changes made)

Installs a supervised Ollama on 127.0.0.1:{lo.PORT} for {lo.MODEL} (about {lo.DOWNLOAD_GB:.0f} GB
download, {lo.RESIDENT_GB} GB resident). Refuses below {lo.MIN_RAM_GB} GB RAM, or when another
Ollama's resident models plus this one would pass {lo.GPU_MEMORY_FRACTION:.0%} of RAM.
Then run `llm-router install --proxy-default` to point Claude Code at it.
Undo with `llm-router uninstall` (models are kept; `--purge-models` removes {lo.MODEL}).
"""


def run_setup_local(
    *, yes: bool, stdin_tty: bool, system: str, home: Path, environ: dict[str, str],
    probe_fn, which, fetch, post, runner, stream, ask, sleep, peer_urls: list[str], python: str,
    ready_retries: int = 30, ready_interval_s: float = 1.0,
) -> dict:
    """Returns ``{"ok": bool, "actions": [...], "error": str | None}``. Every dependency is injected."""
    actions: list[str] = []

    def fail(msg: str) -> dict:
        return {"ok": False, "actions": actions, "error": msg}

    total_gb = (probe_fn() or {}).get("total_gb")
    refusal = lo.ram_gate(total_gb)
    if refusal:
        return fail(refusal)
    try:
        pd.service_target(system, home, label=lo.LABEL)
    except RuntimeError as exc:
        return fail(str(exc))
    ollama_bin = which("ollama")
    if not ollama_bin:
        return fail("the `ollama` program was not found on PATH. Install it from https://ollama.com/download, "
                    "then run `llm-router setup-local` again. Nothing was changed.")
    own_resident = lo.is_resident(fetch)
    conflict = lo.peer_conflict([u for u in peer_urls if u.rstrip("/") != lo.URL], fetch, total_gb,
                                own_resident=own_resident)
    if conflict:
        return fail(conflict)

    have_model = any((m.get("model") or m.get("name")) == lo.MODEL
                     for m in ((fetch(f"{lo.URL}/api/tags") or {}).get("models") or []))
    download = "already downloaded" if have_model else f"download about {lo.DOWNLOAD_GB:.0f} GB"
    plan = (f"{lo.MODEL} on 127.0.0.1:{lo.PORT}: {download}, {lo.RESIDENT_GB} GB of memory kept resident, "
            f"a background service ({lo.LABEL}) that starts at login")
    if not yes:
        if not stdin_tty:
            return fail(f"not interactive: nothing was changed. This would set up {plan}. "
                        "Re-run with `--yes` to consent.")
        print(f"\n  {_bold('Local routing')}\n  {plan}")
        try:
            answer = ask("  Go ahead? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer not in ("y", "yes"):
            return fail("declined: nothing was changed")

    dest = pd.service_target(system, home, label=lo.LABEL)[0]
    before = _read_or_none(dest)
    dest, activate = lo.install_service(ollama_bin, system=system, home=home)
    actions.append(f"Wrote {dest}")
    note = _stale_plist_note(dest, before, lo.LABEL)
    if note:
        actions.append(note)
    pre = lo.install_preload_service(python, system=system, home=home)
    actions.append(f"Wrote {pre} (reloads the model at login; not started now)")
    # Recorded before the service starts: if anything below fails, `llm-router uninstall` can still stop it.
    lo.write_sentinel(system=system, ollama_bin=ollama_bin, env_written={}, preload_label=lo.PRELOAD_LABEL)
    ok, detail = pd.activate_service(dest, activate, runner=runner)
    if not ok:
        return fail(f"could not start the local Ollama service (`{activate}`): {detail or 'unknown error'}")
    actions.append(f"Started via `{activate}`")
    if not lo.wait_ready(fetch, sleep, retries=ready_retries, interval_s=ready_interval_s):
        return fail(f"the local Ollama service did not answer on {lo.URL} within "
                    f"{ready_retries * ready_interval_s:.0f}s. Check {home}/.llm-router/logs/local-ollama.err.log, "
                    "then re-run `llm-router setup-local`.")
    actions.append(f"Local Ollama answers on {lo.URL}")

    if have_model:
        actions.append(f"{lo.MODEL} already downloaded")
    else:
        from llm_router import model_pull

        res = model_pull.pull_model(lo.MODEL, approved=True, stream=stream, base_url=lo.URL)
        if res.status != "success":
            return fail(f"pulling {lo.MODEL} failed ({res.detail or res.status}). Re-run `llm-router "
                        "setup-local`; a partial download resumes.")
        actions.append(f"Pulled {lo.MODEL}")

    if not lo.preload(post):
        return fail(f"{lo.MODEL} did not load at a {lo.WINDOW}-token window; check "
                    f"{home}/.llm-router/logs/local-ollama.err.log")
    if not lo.is_resident(fetch):
        return fail(f"{lo.MODEL} is not resident after the preload; check "
                    f"{home}/.llm-router/logs/local-ollama.err.log")
    actions.append(f"{lo.MODEL} resident at window {lo.WINDOW}, kept loaded (keep_alive -1)")

    written, kept = lo.write_env(environ)
    lo.write_sentinel(system=system, ollama_bin=ollama_bin, env_written=written, preload_label=lo.PRELOAD_LABEL)
    for key, value in written.items():
        actions.append(f"Set {key}={value} in {paths.state_path('.env')}")
    for key in kept:
        actions.append(f"Kept your existing {key} (an explicit value is never overridden)")
    return {"ok": True, "actions": actions, "error": None}


def _log_preload(line: str) -> None:
    import time

    try:
        p = paths.state_path("logs", "local-ollama-preload.log")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}\n")
    except OSError:
        pass


def _restart_command(system: str, dest: Path) -> str:
    if system == "Linux":
        return f"systemctl --user restart {pd._SYSTEMD_UNITS[pd.LABEL]}"  # noqa: SLF001
    return pd.launchd_activate_command(dest, pd.LABEL)  # kickstart -k when loaded


def run_preload(*, system: str, home: Path, probe_fn, fetch, post, runner, sleep, peer_urls: list[str],
                retries: int = 120, interval_s: float = 1.0) -> int:
    """``setup-local --preload``: after a reboot, wait for :11435, check peer memory, load the model
    pinned at the proxy's window, then restart the proxy so its start-up preflight sees it resident.
    One log line per run; any failure returns non-zero (never 0 on failure)."""
    def done(rc: int, msg: str) -> int:
        _log_preload(f"rc={rc} {msg}")
        if rc:
            print(f"llm-router setup-local --preload: {msg}", file=sys.stderr)
        return rc

    if lo.read_sentinel() is None:
        return done(1, "local routing is not set up (no sentinel); run `llm-router setup-local`")
    if not lo.wait_ready(fetch, sleep, retries=retries, interval_s=interval_s):
        return done(1, f"{lo.URL} did not answer within {retries * interval_s:.0f}s; model not loaded")
    conflict = lo.peer_conflict([u for u in peer_urls if u.rstrip("/") != lo.URL], fetch,
                                (probe_fn() or {}).get("total_gb"), own_resident=lo.is_resident(fetch))
    if conflict:
        return done(1, f"not loading: {conflict}")
    if not lo.preload(post) or not lo.is_resident(fetch):
        return done(1, f"{lo.MODEL} did not load at window {lo.WINDOW}")
    msg = f"{lo.MODEL} resident at window {lo.WINDOW}"
    # The proxy decides local serving ONCE, at start (proxy/server.py:2663 sets local_disabled for the
    # process lifetime; :565 hands it to LocalModeState), so a proxy that started before the model was
    # loaded stays tier-only until restarted. Restart it, and only when proxy-default is installed.
    if pd.read_sentinel() is not None:
        dest = pd.service_target(system, home, label=pd.LABEL)[0]
        ok, detail = pd.activate_service(dest, _restart_command(system, dest), runner=runner)
        if not ok:
            return done(1, f"{msg}; could not restart the proxy: {detail or 'unknown error'}")
        msg += "; proxy restarted"
    return done(0, msg)


def _real_fetch(url: str) -> dict | None:
    import json
    import urllib.request
    from urllib.parse import urlparse

    if (urlparse(url).hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        return None
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 - loopback checked above
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - a probe answers yes or no
        return None


def _real_post(url: str, body: dict) -> dict | None:
    import json
    import urllib.request
    from urllib.parse import urlparse

    if (urlparse(url).hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        return None
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:  # noqa: S310 - loopback checked above
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def cmd_setup_local(args: list[str]) -> int:
    if any(a in ("--help", "-h", "help") for a in args):
        print(_HELP)
        return 0
    unknown = [a for a in args if a not in ("--yes", "--preload")]
    if unknown:
        print(f"llm-router setup-local: unknown argument {unknown[0]!r}; see `llm-router setup-local --help`",
              file=sys.stderr)
        return 2
    import shutil
    import subprocess
    import time

    from llm_router import hardware, model_pull
    from llm_router.toolkit.adapters.ollama import get_ollama_url

    if "--preload" in args:
        return run_preload(system=platform.system(), home=Path.home(), probe_fn=hardware.probe,
                           fetch=_real_fetch, post=_real_post, runner=subprocess.run, sleep=time.sleep,
                           peer_urls=[get_ollama_url()])

    def _progress_stream(url: str, body: dict):
        last = -1
        for obj in model_pull.default_stream(url, body):
            done, total = obj.get("completed"), obj.get("total")
            if isinstance(done, int) and isinstance(total, int) and total:
                pct = done * 100 // total
                if pct != last and pct % 5 == 0:
                    print(f"  downloading {lo.MODEL}: {pct}%", flush=True)
                    last = pct
            yield obj

    print(f"\n{_bold('Local routing: setting up...')}")
    result = run_setup_local(
        yes="--yes" in args, stdin_tty=sys.stdin.isatty() and sys.stdout.isatty(),
        system=platform.system(), home=Path.home(), environ=dict(os.environ),
        probe_fn=hardware.probe, which=shutil.which, fetch=_real_fetch, post=_real_post,
        runner=subprocess.run, stream=_progress_stream, ask=input, sleep=time.sleep,
        peer_urls=[get_ollama_url()], python=sys.executable,
    )
    for a in result["actions"]:
        print(f"  {_green('✓')}  {a}")
    if not result["ok"]:
        print(f"\n  {_red('✗')}  {result['error']}\n")
        return 1
    print(f"\n{_green('✓')} {_bold('The local model is installed and loaded.')}")
    print("  Next: run `llm-router install --proxy-default` so Claude Code uses it, then restart Claude Code.")
    print("  If the proxy was already installed, that command writes the new arguments and tells you the")
    print(f"  one step to apply them: unload then load {shlex.quote(str(pd.service_target(platform.system(), Path.home())[0]))}.")
    print("  `llm-router doctor` shows whether local routing is live.\n")
    return 0


def uninstall_local_ollama(*, home: Path, system: str, runner, purge_models: bool = False) -> list[str]:
    """Stop and remove what ``setup-local`` installed: the service, its file, the sentinel, and the
    ``.env`` lines it wrote (only those exact lines). The downloaded model stays unless
    ``purge_models``. Nothing is done when there is no sentinel. ``runner`` is injected."""
    sentinel = lo.read_sentinel()
    if sentinel is None:
        return []
    actions: list[str] = []
    if purge_models:
        # `ollama rm` is a client call: it needs the server, so it runs before the service is stopped.
        try:
            res = runner([sentinel.get("ollama_bin") or "ollama", "rm", lo.MODEL], capture_output=True, text=True,
                         timeout=60, env={**os.environ, "OLLAMA_HOST": f"127.0.0.1:{lo.PORT}"})
            actions.append(f"Removed model {lo.MODEL}" if res.returncode == 0
                           else f"WARN could not remove model {lo.MODEL}: {(res.stderr or res.stdout or '').strip()}")
        except Exception as exc:  # noqa: BLE001
            actions.append(f"WARN could not remove model {lo.MODEL}: {exc}")
    # The preload job first: nothing should reload the model while the server goes away.
    for label, what in ((lo.PRELOAD_LABEL, "model preload job"), (lo.LABEL, "local Ollama service")):
        try:
            dest, _ = pd.service_target(system, home, label=label)
        except (RuntimeError, KeyError):
            continue
        ok, detail = pd.deactivate_service(system, dest, runner=runner, label=label)
        actions.append(f"Stopped the {what}{'' if ok else f' (best-effort: {detail})'}")
        if dest.exists():
            try:
                dest.unlink()
                actions.append(f"Removed {dest}")
            except OSError as exc:
                actions.append(f"WARN could not remove {dest}: {exc}")
    for line in lo.remove_env(sentinel.get("env_written") or {}):
        actions.append(f"Removed {line} from {paths.state_path('.env')}")
    lo.remove_sentinel()
    actions.append("Removed local-routing state sentinel")
    if not purge_models:
        actions.append(f"Kept the downloaded model {lo.MODEL} (`llm-router uninstall --purge-models` removes it)")
    return actions
