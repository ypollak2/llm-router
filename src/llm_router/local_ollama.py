# SPDX-License-Identifier: MIT
"""The dedicated local-model Ollama that ``llm-router setup-local`` installs.

One model (``qwen3.6:35b-a3b-coding``), one server (127.0.0.1:11435), one resident
runner that never expires. This is the shape the owner's live setup runs by hand
(no supervisor, so a reboot silently turned local routing off). This module holds
everything that is a fact about that shape and nothing that touches the machine:
every probe takes an injected ``fetch`` / ``post`` so a test never opens a socket.

What window the proxy asks Ollama for (the question this module must answer, not guess):

* ``proxy/server.py`` ``ProxyConfig.__post_init__`` leaves ``num_ctx`` None when no
  ``--num-ctx`` is given and then calls ``local_models.num_ctx(model)``.
* ``local_models.num_ctx`` returns ``raised_window()`` for the shared model when
  ``LLM_ROUTER_WINDOW_STEP`` >= 32768 and ``LLM_ROUTER_LOCAL_MODEL`` is set;
  otherwise the table value (32768 for this model).
* The live ``~/.llm-router/.env`` sets both, so the live runner sits at 131072.

So the window is not a property of the model, it is a property of two env keys.
``setup-local`` writes those keys (``ENV_KEYS``), and the preload sends the same window
(``WINDOW``); ``tests/test_local_ollama.py`` pins that the two agree through
``local_models.num_ctx`` itself. A preload at another window would load a second runner.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from llm_router import paths, proxy_default as pd

MODEL = "qwen3.6:35b-a3b-coding"
PORT = 11435
URL = f"http://127.0.0.1:{PORT}"
#: Below this the 22.4 GB resident runner plus macOS and a browser do not fit (owner decision 2026-11-28).
MIN_RAM_GB = 48
#: Window the live proxy requests (see the module docstring).
WINDOW = 131072
#: /api/ps size of the runner at WINDOW on the owner's 48 GB Mac (2026-11-27).
RESIDENT_GB = 22.4
#: Metal's usual working-set cap, as in scripts/release/quality_gate.py GPU_MEMORY_FRACTION (#992).
GPU_MEMORY_FRACTION = 0.75
#: Approximate download, shown at consent time ("about 22 GB"). Source: `ollama list` on 2026-11-28 shows
#: qwen3.6:35b-a3b-coding as 22 GB; /api/ps resident size on 2026-11-27 was 22,384,570,530 B (= RESIDENT_GB).
DOWNLOAD_GB = 22.0
LABEL = pd.LOCAL_OLLAMA_LABEL
SYSTEMD_UNIT = "llm_router-local-ollama"
PRELOAD_LABEL = pd.LOCAL_PRELOAD_LABEL
_SENTINEL_NAME = "local_ollama.json"

#: What ``setup-local`` persists to the state ``.env`` (explicit-set-wins, recorded, removed on uninstall).
ENV_KEYS = {
    "LLM_ROUTER_LOCAL_MODEL_URL": URL,
    "LLM_ROUTER_LOCAL_MODEL": MODEL,
    "LLM_ROUTER_WINDOW_STEP": str(WINDOW),
}

Fetch = Callable[[str], "dict | None"]
Post = Callable[[str, dict], "dict | None"]


# ── sentinel ────────────────────────────────────────────────────────────────

def sentinel_path() -> Path:
    return paths.state_path(_SENTINEL_NAME)


def read_sentinel() -> dict | None:
    try:
        data = json.loads(sentinel_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_sentinel(*, system: str, ollama_bin: str, env_written: dict[str, str],
                   preload_label: str | None = None) -> None:
    """``env_written`` is MERGED over the previous sentinel's: a re-run that finds its keys already set
    writes nothing, and must not make uninstall forget the lines an earlier run added."""
    prior = (read_sentinel() or {}).get("env_written")
    env_written = {**(prior if isinstance(prior, dict) else {}), **env_written}
    p = sentinel_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "enabled": True, "model": MODEL, "port": PORT, "window": WINDOW, "label": LABEL,
        "system": system, "ollama_bin": ollama_bin, "preload_label": preload_label,
        "env_written": env_written,  # exactly the lines this command added, nothing else
        "installed_at": time.time(),
    }, indent=2) + "\n")


def remove_sentinel() -> None:
    try:
        sentinel_path().unlink(missing_ok=True)
    except OSError:
        pass


# ── gates ───────────────────────────────────────────────────────────────────

def ram_gate(total_gb: float | None) -> str | None:
    """None when this machine may run the model, else the refusal. Unknown RAM is a refusal:
    the favourable answer must not be the default for a probe that failed."""
    if total_gb is None:
        return (f"could not read this machine's RAM; local routing needs {MIN_RAM_GB} GB or more, "
                "so nothing was changed")
    if total_gb < MIN_RAM_GB:
        return (f"this machine has {total_gb:.0f} GB of RAM; local routing needs {MIN_RAM_GB} GB or more "
                f"(the model holds {RESIDENT_GB} GB resident at a {WINDOW}-token window). Nothing was changed. "
                "Claude routing keeps working: `llm-router install --proxy-default`")
    return None


def peer_conflict(peer_urls: list[str], fetch: Fetch, total_gb: float | None,
                  *, own_resident: bool = False) -> str | None:
    """Would our runner next to another Ollama's resident models overrun GPU memory? Reads
    ``/api/ps`` of each peer only. Same rule as ``scripts/release/quality_gate.py``
    ``_peer_memory_conflict`` (#992): resident bytes plus ours over 75% of RAM -> refuse.
    ``own_resident``: our model already sits on our server, so loading adds nothing."""
    if own_resident or not total_gb:
        return None
    held: list[str] = []
    peer_gb = 0.0
    for url in peer_urls:
        ps = fetch(f"{url.rstrip('/')}/api/ps") or {}
        for m in ps.get("models", []) or []:
            size = float(m.get("size") or 0) / 1e9
            peer_gb += size
            held.append(f"{m.get('model') or m.get('name')} on {url} ({size:.1f} GB)")
    if not peer_gb:
        return None
    need = peer_gb + RESIDENT_GB
    if need <= GPU_MEMORY_FRACTION * total_gb:
        return None
    return (f"loading {MODEL} ({RESIDENT_GB} GB resident) next to {', '.join(held)} needs {need:.1f} GB, "
            f"over {GPU_MEMORY_FRACTION:.0%} of {total_gb:.1f} GB RAM; that overruns GPU memory "
            "(Metal OutOfMemory) and can wedge the other server. Free it first: "
            "`OLLAMA_HOST=<peer> ollama stop <model>`. Nothing was changed.")


def peer_urls() -> list[str]:
    """Other Ollama servers whose resident models share the GPU: the default server plus the
    comma-separated ``QUALITY_GATE_PEER_OLLAMA_URLS`` (same list as scripts/release/quality_gate.py)."""
    import os

    from llm_router.toolkit.adapters.ollama import get_ollama_url

    out: list[str] = []
    for u in [get_ollama_url(), *os.environ.get("QUALITY_GATE_PEER_OLLAMA_URLS", "").split(",")]:
        u = u.strip().rstrip("/")
        if u and u != URL and u not in out:
            out.append(u)
    return out


# ── probes (GET only) and preload ────────────────────────────────────────────

def is_ready(fetch: Fetch, url: str = URL) -> bool:
    return fetch(f"{url}/api/version") is not None


def is_resident(fetch: Fetch, url: str = URL, model: str = MODEL) -> bool:
    ps = fetch(f"{url}/api/ps") or {}
    return any((m.get("model") or m.get("name")) == model for m in ps.get("models", []) or [])


def preload(post: Post, url: str = URL) -> bool:
    """Load the model at ``WINDOW`` and pin it (``keep_alive`` -1). True when Ollama answered."""
    body = {"model": MODEL, "prompt": "", "stream": False, "keep_alive": -1,
            "options": {"num_ctx": WINDOW}}
    return post(f"{url}/api/generate", body) is not None


def wait_ready(fetch: Fetch, sleep: Callable[[float], None], *, retries: int = 30,
               interval_s: float = 1.0) -> bool:
    for _ in range(max(1, retries)):
        if is_ready(fetch):
            return True
        sleep(interval_s)
    return False


# ── service files ────────────────────────────────────────────────────────────

def render_ollama_plist(ollama_bin: str, home: Path) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{ollama_bin}</string>
        <string>serve</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OLLAMA_HOST</key><string>127.0.0.1:{PORT}</string>
        <key>OLLAMA_KEEP_ALIVE</key><string>-1</string>
        <key>OLLAMA_MAX_LOADED_MODELS</key><string>1</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{home}/.llm-router/logs/local-ollama.out.log</string>
    <key>StandardErrorPath</key><string>{home}/.llm-router/logs/local-ollama.err.log</string>
</dict>
</plist>
"""


def render_systemd_unit(ollama_bin: str) -> str:
    return f"""[Unit]
Description=LLM Router dedicated Ollama for the local model
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=OLLAMA_HOST=127.0.0.1:{PORT}
Environment=OLLAMA_KEEP_ALIVE=-1
Environment=OLLAMA_MAX_LOADED_MODELS=1
ExecStart={ollama_bin} serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def install_service(ollama_bin: str, *, system: str, home: Path) -> tuple[Path, str]:
    """Write the service file; never starts it (the caller activates through an injected runner)."""
    dest, activate = pd.service_target(system, home, label=LABEL)
    content = render_ollama_plist(ollama_bin, home) if system == "Darwin" else render_systemd_unit(ollama_bin)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    return dest, activate


def render_preload_plist(python: str, home: Path) -> str:
    """Runs once at login (RunAtLoad, no KeepAlive, no StartInterval): reload the model after a reboot."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{PRELOAD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>llm_router.cli</string>
        <string>setup-local</string>
        <string>--preload</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><false/>
    <key>StandardOutPath</key><string>{home}/.llm-router/logs/local-ollama-preload.out.log</string>
    <key>StandardErrorPath</key><string>{home}/.llm-router/logs/local-ollama-preload.err.log</string>
</dict>
</plist>
"""


def render_preload_systemd_unit(python: str) -> str:
    return f"""[Unit]
Description=LLM Router: reload the local model after login
After=llm_router-local-ollama.service
Wants=llm_router-local-ollama.service

[Service]
Type=oneshot
ExecStart={python} -m llm_router.cli setup-local --preload

[Install]
WantedBy=default.target
"""


def install_preload_service(python: str, *, system: str, home: Path) -> Path:
    """Write the preload job's file. It is NOT started here: it runs at the next login (and
    ``setup-local`` itself has just loaded the model)."""
    dest, _ = pd.service_target(system, home, label=PRELOAD_LABEL)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_preload_plist(python, home) if system == "Darwin" else render_preload_systemd_unit(python))
    return dest


# ── what the proxy plist gets once this is set up ────────────────────────────

def proxy_args(home: Path) -> list[str]:
    """The local-agent arguments of ``com.llm_router.proxy`` (owner's proven shape), or ``[]`` when
    ``setup-local`` has not run (the sentinel is the only switch). ``--tiers conversation`` stays the
    proxy's own ``--tiers``; ``--local-with-tiers`` needs it."""
    if read_sentinel() is None:
        return []
    policy = home / ".llm-router" / "claude_tiers.yaml"
    # The owner's live file is a hand-edited override; a fresh machine has none and the proxy then
    # uses its bundled policy, so name the file only when it exists (a missing named file is a start failure).
    pol = ["--tier-policy", str(policy)] if policy.is_file() else []
    return [
        "--serve", "local-agent", "--local-with-tiers", *pol,
        "--model", f"ollama/{MODEL}", "--ollama-url", URL,
        "--trim", "none", "--keep-alive", "-1",
    ]


# ── state .env (explicit-set-wins; records what it wrote) ───────────────────

def _env_keys_present(env_path: Path) -> set[str]:
    present: set[str] = set()
    try:
        text = env_path.read_text()
    except OSError:
        return present
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export"):].lstrip()
        if line and not line.startswith("#") and "=" in line:
            present.add(line.partition("=")[0].strip())
    return present


def write_env(environ: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Append each of ``ENV_KEYS`` to the state ``.env`` unless it is already set in ``environ`` or the
    file (an explicit value, whatever it is, is never overridden). Returns ``(written, kept)``."""
    env_path = paths.state_path(".env")
    present = _env_keys_present(env_path)
    written: dict[str, str] = {}
    kept: list[str] = []
    for key, value in ENV_KEYS.items():
        if key in environ or key in present:
            kept.append(key)
        else:
            written[key] = value
    if written:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        prior = env_path.read_text() if env_path.exists() else ""
        lead = "" if not prior or prior.endswith("\n") else "\n"
        with open(env_path, "a") as fh:
            fh.write(lead + "".join(f"{k}={v}\n" for k, v in written.items()))
    return written, kept


def remove_env(written: dict[str, str]) -> list[str]:
    """Drop exactly the ``KEY=value`` lines ``write_env`` added (a line the user since changed stays)."""
    env_path = paths.state_path(".env")
    try:
        lines = env_path.read_text().splitlines(keepends=True)
    except OSError:
        return []
    drop = {f"{k}={v}" for k, v in written.items()}
    keep = [ln for ln in lines if ln.strip() not in drop]
    removed = [ln.strip() for ln in lines if ln.strip() in drop]
    if removed:
        env_path.write_text("".join(keep))
    return removed


# ── doctor rows ──────────────────────────────────────────────────────────────

def routing_rows(*, total_gb: float | None, fetch: Fetch, port_up: Callable[[int], bool],
                 settings_base_url: str | None, environ: dict[str, str],
                 ledger_rows: list[dict], now: float, shim_port: int = pd.DEFAULT_PORT
                 ) -> list[tuple[str, bool, str]]:
    """``(name, ok, detail)`` rows for ``llm-router doctor``. Pure: every input is passed in."""
    from llm_router.commands.proxy_default import routes_to_local_port
    from llm_router.proxy import cost_accounting

    recent = [r for r in ledger_rows
              if isinstance(r.get("ts"), (int, float)) and now - r["ts"] <= 86400
              and cost_accounting.served_by(r) == cost_accounting.SERVED_BY_LOCAL]
    resident = is_resident(fetch)
    return [
        (f"RAM >= {MIN_RAM_GB} GB", ram_gate(total_gb) is None,
         f"{total_gb:.0f} GB" if total_gb is not None else "unknown"),
        (f"local Ollama answers on :{PORT}", is_ready(fetch), URL),
        (f"{MODEL} resident", resident, "loaded" if resident else "not loaded"),
        (f"fail-open shim answers on :{shim_port}", bool(port_up(shim_port)), f"127.0.0.1:{shim_port}"),
        ("settings.json ANTHROPIC_BASE_URL is loopback",
         routes_to_local_port(settings_base_url, [shim_port]), settings_base_url or "not set"),
        ("LLM_ROUTER_LOCAL_MODEL_URL set", bool(environ.get("LLM_ROUTER_LOCAL_MODEL_URL", "").strip()),
         environ.get("LLM_ROUTER_LOCAL_MODEL_URL", "").strip() or "not set"),
        ("a call was served locally in the last 24 h", bool(recent), f"{len(recent)} row(s)"),
    ]
