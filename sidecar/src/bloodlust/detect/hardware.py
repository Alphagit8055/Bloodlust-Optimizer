"""Hardware and OS detection.

Feeds the requirement gates in ``core.catalog``: an action that only makes sense
on, say, a 13th-gen Intel part is blocked when detection cannot *prove* the part
is something else. Detection therefore distinguishes three states throughout —
a real value, ``None`` (could not determine), and ``"unknown"`` for strings —
and the gate treats "could not determine" as "refuse", never as "allow".

Sources, in preference order: registry (fast, no process spawn), then ctypes
Win32 calls, then ``powercfg``/``nvidia-smi``. Nothing here shells out to WMI,
which is the slowest option and the one most likely to hang.
"""

from __future__ import annotations

import platform
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from ..core import registry as reg
from ..core.logging import get

_CPU_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
_GPU_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


@dataclass
class Gpu:
    name: str = "unknown"
    vendor: Optional[str] = None      # nvidia | amd | intel | None
    vram_mb: Optional[int] = None
    is_discrete: Optional[bool] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HardwareInfo:
    os_name: str = "unknown"
    os_version: str = "unknown"
    os_build: Optional[int] = None
    arch: str = "unknown"

    cpu_name: str = "unknown"
    cpu_vendor: Optional[str] = None          # intel | amd | other | None
    cpu_generation: Optional[int] = None
    cpu_cores: Optional[int] = None
    cpu_threads: Optional[int] = None
    cpu_max_mhz: Optional[int] = None

    gpus: list[Gpu] = field(default_factory=list)
    primary_gpu: Optional[str] = None

    ram_mb: Optional[int] = None

    hypervisor_present: Optional[bool] = None
    vbs_status: Optional[int] = None
    is_elevated: bool = False
    registry_backend: str = "unknown"

    warnings: list[str] = field(default_factory=list)

    # --------------------------------------------------------------- helpers

    @property
    def discrete_gpu(self) -> Optional[Gpu]:
        for g in self.gpus:
            if g.is_discrete:
                return g
        return None

    @property
    def gpu_vendor(self) -> Optional[str]:
        d = self.discrete_gpu
        return d.vendor if d else None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["gpu_vendor"] = self.gpu_vendor
        d["discrete_gpu"] = self.discrete_gpu.to_dict() if self.discrete_gpu else None
        return d


# ----------------------------------------------------------------- primitives


def _cpu_name(backend: reg.RegistryBackend) -> str:
    hit = backend.read("HKLM", _CPU_KEY, "ProcessorNameString")
    if hit:
        return str(hit[1]).strip()
    return platform.processor() or "unknown"


def _cpu_vendor(name: str) -> Optional[str]:
    low = name.lower()
    if "intel" in low or "core" in low or "xeon" in low:
        return "intel"
    if "amd" in low or "ryzen" in low or "athlon" in low:
        return "amd"
    if low == "unknown":
        return None
    return "other"


# Intel: "Core i7-13700H" -> 13; "Core Ultra 7 255H" -> 2 (series 2);
# AMD: "Ryzen 7 7435HS" -> 7000-series -> 7
_INTEL_LEGACY = re.compile(r"\bi[3579]-(\d{1,2})\d{3}")
_INTEL_ULTRA = re.compile(r"Core Ultra (?:[3579])\s+(\d{3})")
_AMD_RYZEN = re.compile(r"Ryzen\s+[3579]\s+(\d)\d{3}")


def _cpu_generation(name: str, vendor: Optional[str]) -> Optional[int]:
    if vendor == "intel":
        m = _INTEL_LEGACY.search(name)
        if m:
            return int(m.group(1))
        m = _INTEL_ULTRA.search(name)
        if m:
            # Core Ultra 7 255H -> model 255 -> generation 2
            return int(m.group(1)[0])
        return None
    if vendor == "amd":
        m = _AMD_RYZEN.search(name)
        if m:
            return int(m.group(1))
        return None
    return None


def _cpu_max_mhz(backend: reg.RegistryBackend) -> Optional[int]:
    hit = backend.read("HKLM", _CPU_KEY, "~MHz")
    if hit:
        try:
            return int(hit[1])
        except (TypeError, ValueError):
            return None
    return None


def _core_thread_counts() -> tuple[Optional[int], Optional[int]]:
    """Logical count is reliable from the environment; physical needs Win32."""
    import os

    logical = os.cpu_count()
    physical: Optional[int] = None

    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

            class SYSTEM_INFO(ctypes.Structure):
                _fields_ = [
                    ("wProcessorArchitecture", ctypes.c_ushort),
                    ("wReserved", ctypes.c_ushort),
                    ("dwPageSize", ctypes.c_ulong),
                    ("lpMinimumApplicationAddress", ctypes.c_void_p),
                    ("lpMaximumApplicationAddress", ctypes.c_void_p),
                    ("dwActiveProcessorMask", ctypes.POINTER(ctypes.c_ulong)),
                    ("dwNumberOfProcessors", ctypes.c_ulong),
                    ("dwProcessorType", ctypes.c_ulong),
                    ("dwAllocationGranularity", ctypes.c_ulong),
                    ("wProcessorLevel", ctypes.c_ushort),
                    ("wProcessorRevision", ctypes.c_ushort),
                ]

            si = SYSTEM_INFO()
            kernel32.GetSystemInfo(ctypes.byref(si))
            logical = int(si.dwNumberOfProcessors)

            # GetActiveProcessorCount(ALL_PROCESSOR_GROUPS) == 0xffff
            try:
                all_groups = kernel32.GetActiveProcessorCount(ctypes.c_ushort(0xFFFF))
                if all_groups:
                    logical = int(all_groups)
            except (AttributeError, OSError):
                pass

            # GetLogicalProcessorInformationEx is how you get real core counts,
            # but it needs a variable-length buffer walk; the registry exposes
            # the same fact far more cheaply.
        except (AttributeError, OSError) as exc:
            get().debug("GetSystemInfo failed: %s", exc)

    return physical, logical


def _physical_core_count(backend: reg.RegistryBackend) -> Optional[int]:
    """Count ``CentralProcessor`` subkeys by probing — avoids a WMI call.

    Windows numbers them contiguously from 0, so a probe stops at the first gap.
    Bounded at 256 so a corrupt key cannot loop.
    """
    base = r"HARDWARE\DESCRIPTION\System\CentralProcessor"
    for n in range(256):
        if not backend.key_exists("HKLM", f"{base}\\{n}"):
            return n if n > 0 else None
    return 256


def _gpus(backend: reg.RegistryBackend) -> list[Gpu]:
    out: list[Gpu] = []
    for idx in range(8):  # bounded: 8 adapters is already pathological
        key = f"{_GPU_CLASS}\\{idx:04d}"
        hit = backend.read("HKLM", key, "DriverDesc")
        if not hit:
            continue
        name = str(hit[1]).strip()
        low = name.lower()
        if "basic display" in low or "microsoft" in low:
            continue  # the fallback adapter, never the one we tune

        vendor = None
        if "nvidia" in low or "geforce" in low or "rtx" in low or "gtx" in low:
            vendor = "nvidia"
        elif "radeon" in low or "amd" in low or "rx " in low:
            vendor = "amd"
        elif "intel" in low or "arc" in low or "iris" in low or "uhd" in low:
            vendor = "intel"

        vram = _vram_mb(backend, key)
        # <=512 MB and an Intel/Arc name is almost always the iGPU; a real
        # dGPU advertises gigabytes. This is a heuristic, hence Optional.
        discrete = None
        if vram is not None:
            discrete = vram >= 1024
        elif vendor in ("nvidia", "amd"):
            discrete = True
        elif vendor == "intel":
            discrete = "arc" in low

        out.append(Gpu(name=name, vendor=vendor, vram_mb=vram, is_discrete=discrete))
    return out


def _vram_mb(backend: reg.RegistryBackend, key: str) -> Optional[int]:
    hit = backend.read("HKLM", key, "HardwareInformation.qwMemorySize")
    if hit:
        try:
            return int(int(hit[1]) / (1024 * 1024))
        except (TypeError, ValueError):
            pass
    hit = backend.read("HKLM", key, "HardwareInformation.MemorySize")
    if hit:
        try:
            value = int(hit[1])
            # Drivers store this either as bytes or already as MB.
            return value // (1024 * 1024) if value > 1024 * 1024 else value
        except (TypeError, ValueError):
            return None
    return None


def _ram_mb() -> Optional[int]:
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        ms = MEMORYSTATUSEX()
        ms.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):  # type: ignore[attr-defined]
            return None
        return int(ms.ullTotalPhys / (1024 * 1024))
    except (AttributeError, OSError) as exc:
        get().debug("GlobalMemoryStatusEx failed: %s", exc)
        return None


def _hypervisor(backend: reg.RegistryBackend) -> tuple[Optional[bool], Optional[int]]:
    """Best-effort. Returns (present, vbs_status); either may be None."""
    vbs_hit = backend.read(
        "HKLM", r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity", "Enabled"
    )
    vbs = int(vbs_hit[1]) if vbs_hit else None

    # A running hypervisor shows up as this key existing with a nonzero state.
    hv = backend.read(
        "HKLM", r"SYSTEM\CurrentControlSet\Control\DeviceGuard", "VirtualizationBasedSecurityStatus"
    )
    present: Optional[bool] = None
    if hv is not None:
        try:
            present = int(hv[1]) != 0
        except (TypeError, ValueError):
            present = None
    return present, vbs


def is_elevated() -> bool:
    if sys.platform != "win32":
        import os

        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def _windows_build() -> Optional[int]:
    if sys.platform != "win32":
        return None
    try:
        v = sys.getwindowsversion()
        return int(v.build)
    except (AttributeError, ValueError):
        return None


# --------------------------------------------------------------------- facade


def detect(backend: Optional[reg.RegistryBackend] = None) -> HardwareInfo:
    """Build a :class:`HardwareInfo`. Never raises; degrades to unknowns."""
    backend = backend or reg.default_backend()
    info = HardwareInfo()
    info.registry_backend = "winreg" if isinstance(backend, reg.WinregBackend) else "memory"

    if sys.platform == "win32":
        info.os_name = "Windows"
        info.os_build = _windows_build()
        try:
            v = sys.getwindowsversion()
            info.os_version = f"{v.major}.{v.minor}.{v.build}"
        except AttributeError:
            info.os_version = platform.version()
    elif sys.platform == "darwin":
        info.os_name = "macOS"
        info.os_version = platform.mac_ver()[0] or "unknown"
    else:
        info.os_name = "Linux"
        info.os_version = platform.release()
        info.warnings.append("unsupported platform: engine runs in report-only mode")

    info.arch = platform.machine() or "unknown"

    info.cpu_name = _cpu_name(backend)
    info.cpu_vendor = _cpu_vendor(info.cpu_name)
    info.cpu_generation = _cpu_generation(info.cpu_name, info.cpu_vendor)
    info.cpu_threads = None
    info.cpu_cores = _physical_core_count(backend)
    physical, logical = _core_thread_counts()
    info.cpu_threads = logical or physical
    if info.cpu_cores is None:
        info.cpu_cores = physical
    info.cpu_max_mhz = _cpu_max_mhz(backend)

    info.gpus = _gpus(backend)
    if info.gpus:
        d = info.discrete_gpu
        info.primary_gpu = (d or info.gpus[0]).name
    else:
        info.warnings.append("no display adapter found in the registry")

    info.ram_mb = _ram_mb()
    info.hypervisor_present, info.vbs_status = _hypervisor(backend)
    info.is_elevated = is_elevated()

    if info.cpu_vendor is None:
        info.warnings.append("CPU vendor undetermined: vendor-specific tweaks stay locked")
    if not info.is_elevated:
        info.warnings.append("not running elevated: HKLM writes will fail")

    return info


__all__ = ["HardwareInfo", "Gpu", "detect", "is_elevated"]
