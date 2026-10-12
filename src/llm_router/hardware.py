"""Hardware probe (PLAN v16 P2.8 task 1): memory, GPU, arch and disk.

``probe()`` never raises. A failed stage leaves its fields ``None`` and adds a
string to ``errors``. Nothing here changes routing; it is a pure reader.

All sizes are GiB (1024**3 bytes), rounded to two decimals.

Free memory by OS (the choice is deliberate, see each parser):

* macOS: ``(free + inactive + speculative) * page_size`` from ``vm_stat``.
  Inactive pages are reclaimable without swapping and speculative pages are
  readily available, so the sum approximates what the OS can hand out. It
  excludes purgeable pages and the compressor, so it errs low.
* Linux: ``MemAvailable`` from ``/proc/meminfo`` (the kernel's own estimate).
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
    """Parse ``memory.total,memory.used`` csv (nounits, MiB) into per-GPU pairs."""
    out: list[tuple[int, int]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            raise ValueError(f"nvidia-smi: unexpected line {line!r}")
        out.append((int(parts[0]), int(parts[1])))
    if not out:
        raise ValueError("nvidia-smi: no GPU rows")
    return out


def _read_windows_memory() -> tuple[int, int]:
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        raise OSError("GlobalMemoryStatusEx failed")
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def _nvidia(result: dict[str, Any], runner: Runner) -> None:
    try:
        rows = parse_nvidia_smi(runner(list(_NVIDIA_CMD), _TIMEOUT_S))
        result["gpu"] = "nvidia"
        # Total VRAM across GPUs, not free VRAM.
        result["vram_gb"] = round(sum(t for t, _ in rows) / 1024, 2)
    except FileNotFoundError:
        result["gpu"], result["vram_gb"] = "none", 0.0  # no nvidia-smi is normal
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
) -> dict[str, Any]:
    """Probe this host. Injectable arguments exist so tests never touch the host."""
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

    try:
        sysname = system if system is not None else platform.system()
        total_b = free_b = None
        if sysname == "Darwin":
            total_b = int(run(["sysctl", "-n", "hw.memsize"], _TIMEOUT_S).strip())
            free_b = parse_vm_stat(run(["vm_stat"], _TIMEOUT_S))
            if result["arch"] == "arm64":
                result["gpu"], result["vram_gb"] = "apple_unified", _gb(total_b)
            else:
                result["gpu"], result["vram_gb"] = "none", 0.0
        elif sysname == "Linux":
            if meminfo_text is None:
                meminfo_text = Path("/proc/meminfo").read_text()
            total_b, free_b = parse_meminfo(meminfo_text)
        elif sysname == "Windows":
            total_b, free_b = (win_mem_reader or _read_windows_memory)()
        else:
            errors.append("memory: unsupported os")
        if total_b is not None and free_b is not None:
            result["total_gb"], result["free_gb"] = _gb(total_b), _gb(free_b)
            result["usable_gb"] = compute_usable_gb(result["total_gb"], result["free_gb"])
        if sysname in ("Linux", "Windows"):
            _nvidia(result, run)
    except Exception as exc:  # noqa: BLE001 - probe never raises
        errors.append(f"memory: {type(exc).__name__}: {exc}")
    return result


_cache_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None


def clear_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def probe_cached(
    ttl_s: float = 30.0, *, _clock: Callable[[], float] = time.monotonic, **probe_kwargs: Any
) -> dict[str, Any]:
    """probe() memoised for ttl_s seconds; always returns a copy."""
    global _cache
    with _cache_lock:
        now = _clock()
        if _cache is None or now - _cache[0] >= ttl_s:
            _cache = (now, probe(**probe_kwargs))
        out = dict(_cache[1])
        out["errors"] = list(out["errors"])
        return out
