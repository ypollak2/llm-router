"""Parser and probe tests for llm_router.hardware (PLAN v16 P2.8 task 1).

Every probe() call injects system, machine, runner and disk_path; nothing here
reads the real host.
"""

from __future__ import annotations

import ctypes
import subprocess

import pytest

from llm_router import hardware
from llm_router.hardware import (
    MEMORYSTATUSEX, compute_usable_gb, parse_meminfo, parse_nvidia_smi,
    parse_vm_stat, probe, probe_cached,
)

GIB = 1024**3

VM_STAT_ARM = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                5000.
Pages active:                            400000.
Pages inactive:                          200000.
Pages speculative:                         3000.
Pages throttled:                              0.
Pages wired down:                        150000.
Pages purgeable:                          10000.
"Translation faults":                 123456789.
Pages stored in compressor:               90000.
"""

VM_STAT_INTEL = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                              100.
Pages inactive:                          200.
Pages speculative:                       300.
"""

MEMINFO = """MemTotal:       32768000 kB
MemFree:         1000000 kB
MemAvailable:   20000000 kB
Buffers:          100000 kB
"""


@pytest.fixture(autouse=True)
def _fresh_cache():
    hardware.clear_cache()
    yield
    hardware.clear_cache()


def _mac_runner(argv, timeout):
    if argv[:2] == ["sysctl", "-n"]:
        return "17179869184\n"
    if argv == ["vm_stat"]:
        return VM_STAT_ARM
    raise AssertionError(argv)


def test_macos_apple_silicon(tmp_path):
    r = probe(system="Darwin", machine="arm64", runner=_mac_runner, disk_path=tmp_path)
    assert r["total_gb"] == 16.0
    assert r["free_gb"] == round((5000 + 200000 + 3000) * 16384 / GIB, 2)
    assert r["gpu"] == "apple_unified" and r["vram_gb"] == 16.0
    assert r["arch"] == "arm64" and r["errors"] == []
    assert r["usable_gb"] == compute_usable_gb(16.0, r["free_gb"])
    assert r["disk_free_gb"] is not None


def test_parse_vm_stat_page_sizes():
    assert parse_vm_stat(VM_STAT_ARM) == 208000 * 16384
    assert parse_vm_stat(VM_STAT_INTEL) == 600 * 4096


def test_parse_vm_stat_rejects_garbage():
    with pytest.raises(ValueError):
        parse_vm_stat("nothing useful")


def test_linux_with_nvidia(tmp_path):
    calls = []

    def runner(argv, timeout):
        calls.append((argv, timeout))
        return "24576, 1024\n"

    r = probe(system="Linux", machine="x86_64", runner=runner,
              meminfo_text=MEMINFO, disk_path=tmp_path)
    assert r["total_gb"] == round(32768000 * 1024 / GIB, 2)
    assert r["free_gb"] == round(20000000 * 1024 / GIB, 2)
    assert r["gpu"] == "nvidia" and r["vram_gb"] == 24.0 and r["errors"] == []
    argv, timeout = calls[0]
    assert argv == ["nvidia-smi", "--query-gpu=memory.total,memory.used",
                    "--format=csv,noheader,nounits"]
    assert timeout <= 2


def test_parse_nvidia_smi_multi_gpu():
    assert parse_nvidia_smi("8192, 100\n8192, 0\n") == [(8192, 100), (8192, 0)]


def test_linux_without_gpu(tmp_path):
    def runner(argv, timeout):
        raise FileNotFoundError("nvidia-smi")

    r = probe(system="Linux", machine="aarch64", runner=runner,
              meminfo_text=MEMINFO, disk_path=tmp_path)
    assert r["gpu"] == "none" and r["vram_gb"] == 0.0 and r["errors"] == []
    assert r["total_gb"] is not None


def test_parse_meminfo():
    assert parse_meminfo(MEMINFO) == (32768000 * 1024, 20000000 * 1024)
    with pytest.raises(ValueError):
        parse_meminfo("MemTotal: 1 kB\n")


def test_windows_fake_reader(tmp_path):
    def runner(argv, timeout):
        raise FileNotFoundError("nvidia-smi")

    r = probe(system="Windows", machine="AMD64", runner=runner, disk_path=tmp_path,
              win_mem_reader=lambda: (16 * GIB, 6 * GIB))
    assert (r["total_gb"], r["free_gb"]) == (16.0, 6.0)
    assert r["usable_gb"] == 2.0 and r["gpu"] == "none" and r["errors"] == []


def test_memorystatusex_layout():
    names = [f[0] for f in MEMORYSTATUSEX._fields_]
    assert names == ["dwLength", "dwMemoryLoad", "ullTotalPhys", "ullAvailPhys",
                     "ullTotalPageFile", "ullAvailPageFile", "ullTotalVirtual",
                     "ullAvailVirtual", "ullAvailExtendedVirtual"]
    assert ctypes.sizeof(MEMORYSTATUSEX) == 64


@pytest.mark.parametrize("total,free,expected", [
    (16, 8, 4.0),       # reserve = max(4, 3.2) = 4
    (20, 10, 6.0),      # 20% == 4: tie
    (64, 30, 17.2),     # 20% dominates: 12.8
    (16, 3.9, 0.0),     # below reserve floors at 0
    (16, 0, 0.0),
])
def test_usable_formula(total, free, expected):
    assert compute_usable_gb(total, free) == expected


def test_runner_failure_is_graceful(tmp_path):
    for exc in (RuntimeError("boom"), subprocess.TimeoutExpired(["sysctl"], 2)):
        def runner(argv, timeout, exc=exc):
            raise exc

        r = probe(system="Darwin", machine="arm64", runner=runner, disk_path=tmp_path)
        assert r["total_gb"] is None and r["usable_gb"] is None
        assert r["errors"] and all(isinstance(e, str) for e in r["errors"])


def test_unsupported_os(tmp_path):
    r = probe(system="Plan9", machine="x", runner=_mac_runner, disk_path=tmp_path)
    assert any("unsupported os" in e for e in r["errors"])
    assert r["total_gb"] is None


def test_default_runner_no_shell_and_capped_timeout(monkeypatch):
    seen = {}

    class P:
        stdout = "ok"

    def fake_run(argv, **kw):
        seen.update(kw, argv=argv)
        return P()

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)
    assert hardware._default_runner(["x"], 30) == "ok"
    assert seen["shell"] is False and seen["timeout"] <= 2
    assert isinstance(seen["argv"], list)


def test_probe_cached_ttl_and_copy(tmp_path):
    n = {"calls": 0}

    def runner(argv, timeout):
        n["calls"] += 1
        return _mac_runner(argv, timeout)

    now = {"t": 100.0}
    kw = dict(system="Darwin", machine="arm64", runner=runner, disk_path=tmp_path,
              _clock=lambda: now["t"])
    a = probe_cached(30, **kw)
    first = n["calls"]
    a["total_gb"] = -1
    a["errors"].append("mutated")
    b = probe_cached(30, **kw)
    assert n["calls"] == first and b["total_gb"] == 16.0 and b["errors"] == []
    now["t"] += 31
    probe_cached(30, **kw)
    assert n["calls"] == 2 * first
