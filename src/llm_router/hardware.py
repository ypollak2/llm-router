"""Hardware probe (PLAN v16 P2.8 task 1): memory, GPU, arch and disk.

``probe()`` never raises. A failed stage leaves its fields ``None`` and adds a
string to ``errors``. Nothing here changes routing; it is a pure reader.

All sizes are GiB (1024**3 bytes), rounded to two decimals.

Free memory by OS (the choice is deliberate, see each parser):

* macOS: ``(free + inactive + speculative) * page_size`` from ``vm_stat``.
  Inactive pages are reclaimable without swapping and speculative pages are
  readily available, so the sum approximates what the OS can hand out. It
  excludes purgeable pages and the compressor, so it errs low.
* Linux: ``MemAvailable`` from ``/proc/meminfo`` (the kernel's own estimate),
  capped by the cgroup memory limit when one is set (containers): total becomes
  the limit and free at most ``limit - usage``.
* Windows: ``ullAvailPhys`` from ``GlobalMemoryStatusEx``.

``usable_gb = free - max(4 GiB, 20% of total)``, floored at 0: memory we may
offer to a local model while leaving the machine responsive.
"""

from __future__ import annotations

import ctypes
import platform
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from llm_router.safe_subprocess import get_safe_env

_GIB = 1024**3
_TIMEOUT_S = 2.0
_NVIDIA_CMD = [
    "nvidia-smi",
    "--query-gpu=memory.total,memory.used",
    "--format=csv,noheader,nounits",
]

Runner = Callable[[list, float], str]


class MEMORYSTATUSEX(ctypes.Structure):
    """Win32 MEMORYSTATUSEX (DWORD = c_uint32, since c_ulong is 8 bytes on Unix).

    Defined on every OS so the layout can be tested anywhere.
    """

    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _default_runner(argv: list, timeout: float) -> str:
    """Run argv with no shell and a hard timeout of at most 2 s; return stdout."""
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=min(timeout, _TIMEOUT_S),
        shell=False,
        check=True,
        env=get_safe_env(),
    )
    return proc.stdout


def _gb(n_bytes: float) -> float:
    return round(n_bytes / _GIB, 2)


def compute_usable_gb(total_gb: float, free_gb: float) -> float:
    """free - max(4 GiB, 20% of total), floored at 0."""
    return round(max(0.0, free_gb - max(4.0, 0.20 * total_gb)), 2)


def parse_vm_stat(text: str) -> int:
    """Return (free + inactive + speculative) * page_size in bytes."""
    m = re.search(r"page size of (\d+) bytes", text)
    if not m:
        raise ValueError("vm_stat: page size not found")
    page = int(m.group(1))
    pages: dict[str, int] = {}
    for line in text.splitlines():
        lm = re.match(r"\s*Pages (free|inactive|speculative):\s+(\d+)\.?\s*$", line)
        if lm:
            pages[lm.group(1)] = int(lm.group(2))
    missing = {"free", "inactive", "speculative"} - pages.keys()
    if missing:
        raise ValueError(f"vm_stat: missing {sorted(missing)}")
    return sum(pages.values()) * page


def parse_meminfo(text: str) -> tuple[int, int]:
    """Return (MemTotal, MemAvailable) in bytes from /proc/meminfo text."""
    vals: dict[str, int] = {}
    for line in text.splitlines():
        lm = re.match(r"(MemTotal|MemAvailable):\s+(\d+)\s*kB", line)
        if lm:
            vals[lm.group(1)] = int(lm.group(2)) * 1024
    if "MemTotal" not in vals or "MemAvailable" not in vals:
        raise ValueError("meminfo: MemTotal/MemAvailable not found")
    return vals["MemTotal"], vals["MemAvailable"]


def parse_nvidia_smi(text: str) -> list[tuple[int, int]]:
    """Parse ``memory.total,memory.used`` csv (nounits, MiB) into per-GPU pairs.

    A row whose fields are not integers (``[N/A]``, ``[Not Supported]`` on
    unified-memory or virtualised GPUs) is skipped, not fatal for the others.
    """
    out: list[tuple[int, int]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            raise ValueError(f"nvidia-smi: unexpected line {line!r}")
        try:
            out.append((int(parts[0]), int(parts[1])))
        except ValueError:
            continue
    if not out:
        raise ValueError("nvidia-smi: no GPU rows with a numeric memory size")
    return out


def parse_cgroup_limit(max_text: str, current_text: str) -> tuple[int, int] | None:
    """``(limit, usage)`` bytes from cgroup ``memory.max``/``memory.current``
    (v2) or ``memory.limit_in_bytes``/``memory.usage_in_bytes`` (v1) text, or
    None when there is no limit (``max``, or v1's near-2**63 sentinel)."""
    raw = max_text.strip()
    if not raw or raw == "max":
        return None
    limit = int(raw)
    if limit >= 1 << 60:
        return None
    return limit, int(current_text.strip() or 0)


def _read_cgroup_limit() -> tuple[int, int] | None:
    for mx, cur in (("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
                    ("/sys/fs/cgroup/memory/memory.limit_in_bytes",
                     "/sys/fs/cgroup/memory/memory.usage_in_bytes")):
        try:
            return parse_cgroup_limit(Path(mx).read_text(), Path(cur).read_text())
        except (OSError, ValueError):
            continue
    return None


def _read_windows_memory() -> tuple[int, int]:
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        raise OSError("GlobalMemoryStatusEx failed")
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def _translated_arm64(runner: Runner) -> bool:
    """Apple Silicon even when this Python is x86_64 under Rosetta 2
    (``platform.machine()`` then says x86_64). Any failure reads as no."""
    try:
        return runner(["sysctl", "-n", "hw.optional.arm64"], _TIMEOUT_S).strip() == "1"
    except Exception:  # noqa: BLE001
        return False


def _nvidia(result: dict[str, Any], runner: Runner) -> None:
    try:
        rows = parse_nvidia_smi(runner(list(_NVIDIA_CMD), _TIMEOUT_S))
        result["gpu"] = "nvidia"
        # Total VRAM across GPUs, not free VRAM.
        result["vram_gb"] = round(sum(t for t, _ in rows) / 1024, 2)
    except FileNotFoundError:
        result["gpu"], result["vram_gb"] = "none", 0.0  # no nvidia-smi is normal
    except subprocess.CalledProcessError as exc:
        result["gpu"], result["vram_gb"] = "none", 0.0
        detail = str(exc.stderr or exc.output or "").strip()
        if "No devices were found" not in detail:  # nvidia-utils without a GPU is normal
            result["errors"].append(
                f"gpu: nvidia-smi exit {exc.returncode}: {detail[:200] or 'no output'}")
    except Exception as exc:  # noqa: BLE001 - probe never raises
        result["gpu"], result["vram_gb"] = "none", 0.0
        result["errors"].append(f"gpu: {type(exc).__name__}: {exc}")


def probe(
    *,
    runner: Runner | None = None,
    system: str | None = None,
    machine: str | None = None,
    disk_path: str | Path | None = None,
    meminfo_text: str | None = None,
    win_mem_reader: Callable[[], tuple[int, int]] | None = None,
    cgroup_reader: Callable[[], tuple[int, int] | None] | None = None,
) -> dict[str, Any]:
    """Probe this host. Injectable arguments exist so tests never touch the host.

    The memory and GPU stages fail independently: a memory failure never skips
    the GPU probe. ``cgroup_reader`` defaults to the host's cgroup files, except
    when ``meminfo_text`` is injected (a test), where it defaults to no limit.
    """
    run = runner or _default_runner
    result: dict[str, Any] = {
        "total_gb": None, "free_gb": None, "usable_gb": None,
        "gpu": None, "vram_gb": None, "arch": None, "disk_free_gb": None,
        "errors": [],
    }
    errors: list[str] = result["errors"]

    try:
        result["arch"] = machine if machine is not None else platform.machine()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"arch: {type(exc).__name__}: {exc}")
    try:
        path = disk_path if disk_path is not None else Path.home()
        result["disk_free_gb"] = _gb(shutil.disk_usage(path).free)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"disk: {type(exc).__name__}: {exc}")

    sysname = None
    total_b = free_b = None
    try:
        sysname = system if system is not None else platform.system()
        if sysname == "Darwin":
            total_b = int(run(["sysctl", "-n", "hw.memsize"], _TIMEOUT_S).strip())
            free_b = parse_vm_stat(run(["vm_stat"], _TIMEOUT_S))
        elif sysname == "Linux":
            injected = meminfo_text is not None
            if meminfo_text is None:
                meminfo_text = Path("/proc/meminfo").read_text()
            total_b, free_b = parse_meminfo(meminfo_text)
            reader = cgroup_reader or (None if injected else _read_cgroup_limit)
            limit = reader() if reader else None
            if limit and limit[0] < total_b:
                total_b, free_b = limit[0], max(0, min(free_b, limit[0] - limit[1]))
        elif sysname == "Windows":
            total_b, free_b = (win_mem_reader or _read_windows_memory)()
        else:
            errors.append("memory: unsupported os")
        if total_b is not None and free_b is not None:
            result["total_gb"], result["free_gb"] = _gb(total_b), _gb(free_b)
            result["usable_gb"] = compute_usable_gb(result["total_gb"], result["free_gb"])
    except Exception as exc:  # noqa: BLE001 - probe never raises
        errors.append(f"memory: {type(exc).__name__}: {exc}")

    try:
        if sysname == "Darwin":
            if result["arch"] == "arm64" or _translated_arm64(run):
                result["gpu"] = "apple_unified"
                result["vram_gb"] = _gb(total_b) if total_b is not None else None
            else:
                result["gpu"], result["vram_gb"] = "none", 0.0
        elif sysname in ("Linux", "Windows"):
            _nvidia(result, run)
    except Exception as exc:  # noqa: BLE001 - probe never raises
        errors.append(f"gpu: {type(exc).__name__}: {exc}")
    return result


_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def probe_cached(
    ttl_s: float = 30.0, *, _clock: Callable[[], float] = time.monotonic, **probe_kwargs: Any
) -> dict[str, Any]:
    """probe() memoised for ttl_s seconds per argument set; always returns a copy.

    A result with errors is not cached, so one failed stage is retried on the
    next call instead of being served for the whole TTL.
    """
    key = repr(sorted(probe_kwargs.items()))
    with _cache_lock:
        now = _clock()
        hit = _cache.get(key)
        if hit is None or now - hit[0] >= ttl_s:
            fresh = probe(**probe_kwargs)
            if not fresh["errors"]:
                _cache[key] = (now, fresh)
            hit = (now, fresh)
        out = dict(hit[1])
        out["errors"] = list(out["errors"])
        return out
