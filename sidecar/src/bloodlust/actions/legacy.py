"""Curated ports of the legacy GameLoop Optimizer tweak set.

The legacy client described its tweaks as strings; this build does not. Every
action this module can perform is written down in ``PROFILES`` below, and that
table is compiled into the sidecar. The remote catalog may *select* a profile
and show its own label and description, but it cannot add an operation, change
a registry path, or point one action at another action's profile:

* ``legacy.apply`` only accepts a ``profile`` argument;
* the handler refuses unless ``profile == ctx.action_id``;
* unknown profiles are refused before the first side effect.

Operations are intentionally a small closed vocabulary (registry value, delete,
service start type, command, power setting, ADB shell, INI value, and a handful
of named dynamic routines). This keeps the catalog a data file while still
covering the legacy tweak set.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from ..core import paths
from ..core import registry as reg
from ..core.result import ActionResult, Change, ChangeKind
from .base import PowerCfg, action, adb_shell

# --------------------------------------------------------------- common paths

TENCENT = r"Software\Tencent\MobileGamePC"
TENCENT_APP = r"SOFTWARE\WOW6432Node\Tencent\MobileGamePC\AppMarket"
IFEO = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options"
MM = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
KERNEL = r"SYSTEM\CurrentControlSet\Control\Session Manager\kernel"
SESSION = r"SYSTEM\CurrentControlSet\Control\Session Manager"
PRIORITY = r"SYSTEM\CurrentControlSet\Control\PriorityControl"
SYS_PROFILE = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
GRAPHICS = r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers"
DWM = r"SOFTWARE\Microsoft\Windows\Dwm"
NV = r"SYSTEM\CurrentControlSet\Services\nvlddmkm"
GPU_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
INTERFACES = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
PSCHED = r"SOFTWARE\Policies\Microsoft\Windows\Psched"
GAMELOOP_IFEO_EXES = ("aow_exe.exe", "AndroidEmulator.exe", "AndroidEmulatorEn.exe", "AndroidEmulatorEx.exe")


# --------------------------------------------------------------- op builders


def R(
    path: str,
    name: str,
    value: Any,
    hive: str = "HKLM",
    vtype: str = "dword",
    describe: str = "",
) -> dict:
    """A registry value write."""
    return {
        "op": "reg",
        "hive": hive.upper(),
        "path": path,
        "name": name,
        "vtype": vtype,
        "value": value,
        "describe": describe,
    }


def S(path: str, name: str, value: str, hive: str = "HKLM", describe: str = "") -> dict:
    return R(path, name, value, hive=hive, vtype="sz", describe=describe)


def DEL(path: str, name: str, hive: str = "HKLM", describe: str = "") -> dict:
    return {"op": "del", "hive": hive.upper(), "path": path, "name": name, "describe": describe}


def C(argv: list[str], restore: Optional[list[str]] = None, *, describe: str = "", timeout: int = 90) -> dict:
    return {"op": "cmd", "argv": argv, "restore": restore, "describe": describe, "timeout": timeout}


def SVC(name: str, start: str, *, stop: bool = False, describe: str = "") -> dict:
    return {"op": "svc", "name": name, "start": start, "stop": stop, "describe": describe}


def ADB(argv: list[str], restore: Optional[list[str]] = None, *, describe: str = "") -> dict:
    return {"op": "adb", "argv": argv, "restore": restore, "describe": describe}


def PC(sub: str, setting: str, *, ac: Optional[int] = None, dc: Optional[int] = None, describe: str = "") -> dict:
    return {"op": "powercfg", "sub": sub, "setting": setting, "ac": ac, "dc": dc, "describe": describe}


def INI(section: str, key: str, value: str, *, describe: str = "") -> dict:
    return {"op": "ini", "section": section, "key": key, "value": value, "describe": describe}


def DYN(name: str, **kw: Any) -> dict:
    return {"op": "dyn", "name": name, **kw}


def IFEO_PRIO(exe: str, *, cpu: Optional[int] = None, io: Optional[int] = None) -> list[dict]:
    base = IFEO + "\\" + exe + "\\PerfOptions"
    ops: list[dict] = []
    if cpu is not None:
        ops.append(R(base, "CpuPriorityClass", cpu, describe=f"{exe} CPU priority"))
    if io is not None:
        ops.append(R(base, "IoPriority", io, describe=f"{exe} I/O priority"))
    return ops


def PS(script: str, restore: Optional[str] = None, *, describe: str = "") -> dict:
    """A PowerShell routine whose script is compiled in, never catalog-supplied."""
    argv = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-Command", script,
    ]
    restore_argv = None
    if restore is not None:
        restore_argv = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command", restore,
        ]
    return C(argv, restore_argv, describe=describe, timeout=180)


# ------------------------------------------------------------------ metadata

PROFILES: dict[str, dict] = {}


def P(
    pid: str,
    label: str,
    family: str,
    risk: str,
    description: str,
    ops: list[dict],
    *,
    tags: tuple[str, ...] = (),
    requires: Optional[dict] = None,
    reversible: bool = True,
    reboot: bool = False,
    enabled: Optional[bool] = None,
) -> None:
    if pid in PROFILES:
        raise RuntimeError(f"duplicate legacy profile {pid!r}")
    PROFILES[pid] = {
        "id": pid,
        "label": label,
        "family": family,
        "risk": risk,
        "description": description,
        "ops": ops,
        "tags": list(tags),
        "requires": dict(requires or {}),
        "reversible": reversible,
        "needs_reboot": reboot,
        "default_enabled": (risk in ("safe", "moderate")) if enabled is None else enabled,
    }


# ======================================================================
# GameLoop / Tencent: engine settings, launcher priority, compatibility
# ======================================================================

_TENCENT_MEM = 8192

P(
    "gl_vm_ram_8gb", "VM RAM: 8 GB", "gameloop", "moderate",
    "Set the Tencent engine's fixed VM memory to 8192 MB. Only useful when GameLoop's Auto value launches too low; restart GameLoop to apply.",
    [R(TENCENT, "VMMemorySizeInMB", _TENCENT_MEM, hive="HKCU")],
    tags=("gameloop", "memory"), requires={"emulator": "gameloop"},
)
P(
    "gl_vm_cpu_host", "VM CPU: match host cores", "gameloop", "moderate",
    "Set VMCpuCount to the number of physical cores this machine reports. More vCPUs is not automatically faster; benchmark it.",
    [{"op": "reg_host_cores", "hive": "HKCU", "path": TENCENT, "name": "VMCpuCount",
      "describe": "match detected physical core count"}],
    tags=("gameloop", "cpu"), requires={"emulator": "gameloop"},
)
P(
    "gl_vm_cpu_6", "VM CPU: 6 cores", "gameloop", "moderate",
    "Set VMCpuCount to 6. A safe fixed preset for 6-core desktops; restart GameLoop to apply.",
    [R(TENCENT, "VMCpuCount", 6, hive="HKCU")],
    tags=("gameloop", "cpu"), requires={"emulator": "gameloop"},
)
for _pid, _label, _value in (
    ("gl_fxaa_off", "FXAA: off", 0),
    ("gl_fxaa_balanced", "FXAA: balanced", 1),
    ("gl_fxaa_high", "FXAA: high", 2),
):
    P(
        _pid, _label, "gameloop", "safe",
        "Set the Tencent engine's FXAA quality. Anti-aliasing is not exposed in GameLoop's normal panel; lower settings reduce GPU cost.",
        [R(TENCENT, "FxaaQuality", _value, hive="HKCU")],
        tags=("gameloop", "graphics"), requires={"emulator": "gameloop"},
    )
P(
    "gl_vulkan_on", "Renderer: enable Vulkan", "gameloop", "moderate",
    "Set EnableVulkan=1 in the Tencent engine key. Some GPUs render faster; others regress. Benchmark before keeping it.",
    [R(TENCENT, "EnableVulkan", 1, hive="HKCU")],
    tags=("gameloop", "renderer"), requires={"emulator": "gameloop"},
)
P(
    "gl_vulkan_off", "Renderer: disable Vulkan", "gameloop", "safe",
    "Set EnableVulkan=0 so GameLoop stays on its OpenGL/DirectX path.",
    [R(TENCENT, "EnableVulkan", 0, hive="HKCU")],
    tags=("gameloop", "renderer"), requires={"emulator": "gameloop"},
)
P(
    "gl_opengl_version_3", "OpenGL version: 3.x", "gameloop", "moderate",
    "Set OpenGLVersion=3 in the Tencent engine key. Restart GameLoop to apply.",
    [R(TENCENT, "OpenGLVersion", 3, hive="HKCU")],
    tags=("gameloop", "renderer"), requires={"emulator": "gameloop"},
)
P(
    "gl_renderer_gles", "Renderer engine: GLES", "gameloop", "moderate",
    "Set RendererEngine=GLES in the Tencent engine key for the OpenGL path.",
    [S(TENCENT, "RendererEngine", "GLES", hive="HKCU")],
    tags=("gameloop", "renderer"), requires={"emulator": "gameloop"},
)
P(
    "gl_vsync_off", "VSync: off", "gameloop", "safe",
    "Disable the emulator's V-Sync. Lower input latency; enable it again only if tearing is the real problem.",
    [R(TENCENT, "VSyncEnabled", 0, hive="HKCU")],
    tags=("gameloop", "latency"), requires={"emulator": "gameloop"},
)
P(
    "gl_vsync_on", "VSync: on", "gameloop", "safe",
    "Enable the emulator's V-Sync to stop tearing.",
    [R(TENCENT, "VSyncEnabled", 1, hive="HKCU")],
    tags=("gameloop", "latency"), requires={"emulator": "gameloop"},
)
for _pid, _label, _value in (
    ("gl_shader_cache_on", "Shader cache: on", 1),
    ("gl_shader_cache_off", "Shader cache: off", 0),
    ("gl_local_shader_cache_on", "Local shader cache: on", 1),
    ("gl_local_shader_cache_off", "Local shader cache: off", 0),
    ("gl_render_optimize_on", "Rendering optimization: on", 1),
    ("gl_render_optimize_off", "Rendering optimization: off", 0),
):
    _name = {
        "gl_shader_cache_on": "ShaderCacheEnabled", "gl_shader_cache_off": "ShaderCacheEnabled",
        "gl_local_shader_cache_on": "LocalShaderCacheEnabled",
        "gl_local_shader_cache_off": "LocalShaderCacheEnabled",
        "gl_render_optimize_on": "RenderOptimizeEnabled",
        "gl_render_optimize_off": "RenderOptimizeEnabled",
    }[_pid]
    P(
        _pid, _label, "gameloop", "safe",
        f"Set {_name}={_value} in the Tencent engine key.",
        [R(TENCENT, _name, _value, hive="HKCU")],
        tags=("gameloop", "graphics"), requires={"emulator": "gameloop"},
    )
P(
    "gl_dedicated_gpu_on", "Prioritize dedicated GPU", "gameloop", "moderate",
    "Set GraphicsCardEnabled=1 and SetGraphicsCard=1 so GameLoop binds to the discrete GPU on dual-GPU laptops.",
    [R(TENCENT, "GraphicsCardEnabled", 1, hive="HKCU"), R(TENCENT, "SetGraphicsCard", 1, hive="HKCU")],
    tags=("gameloop", "gpu"), requires={"emulator": "gameloop"},
)
P(
    "gl_dedicated_gpu_off", "Let GameLoop choose the GPU", "gameloop", "safe",
    "Set GraphicsCardEnabled=0 so GameLoop follows Windows' GPU preference.",
    [R(TENCENT, "GraphicsCardEnabled", 0, hive="HKCU")],
    tags=("gameloop", "gpu"), requires={"emulator": "gameloop"},
)
P(
    "gl_internal_messaging_off", "Disable GameLoop internal messaging", "gameloop", "moderate",
    "Set InterMessageLocalSwitch=0 in the Tencent engine key to stop the launcher's internal message channel.",
    [R(TENCENT, "InterMessageLocalSwitch", 0, hive="HKCU")],
    tags=("gameloop", "background"), requires={"emulator": "gameloop"},
)
P(
    "gl_predownload_off", "Disable game pre-download", "gameloop", "moderate",
    "Set PreDownloadCloudSwitch=0 so GameLoop does not pre-download updates in the background.",
    [R(TENCENT, "PreDownloadCloudSwitch", 0, hive="HKCU")],
    tags=("gameloop", "background"), requires={"emulator": "gameloop"},
)
P(
    "gl_autostart_off", "Disable GameLoop autostart", "gameloop", "moderate",
    "Set AutoStart=0 in the Tencent engine key so the launcher stops starting with Windows.",
    [R(TENCENT, "AutoStart", 0, hive="HKCU")],
    tags=("gameloop", "background"), requires={"emulator": "gameloop"},
)
P(
    "gl_appmarket_autostart_off", "Disable AppMarket autostart", "gameloop", "moderate",
    "Set AutoStart=0 in the AppMarket key so the store front stops launching in the background.",
    [R(TENCENT_APP, "AutoStart", 0, hive="HKCU")],
    tags=("gameloop", "background"), requires={"emulator": "gameloop"},
)
P(
    "gl_background_activity_off", "Disable all GameLoop background activity", "gameloop", "aggressive",
    "Bundle: stop autostart, pre-download, internal messaging and AppMarket autostart in one pass.",
    [
        R(TENCENT, "AutoStart", 0, hive="HKCU"),
        R(TENCENT, "PreDownloadCloudSwitch", 0, hive="HKCU"),
        R(TENCENT, "InterMessageLocalSwitch", 0, hive="HKCU"),
        R(TENCENT_APP, "AutoStart", 0, hive="HKCU"),
    ],
    tags=("gameloop", "background"), requires={"emulator": "gameloop"},
)
P(
    "gl_hw_decoding_on", "Hardware video decoding: on", "gameloop", "moderate",
    "Set EnableHardwareDecoding=1 in Config.ini under [engine]. The file is backed up before editing and only this line is restored on undo.",
    [INI("engine", "EnableHardwareDecoding", "1", describe="enable hardware video decoding")],
    tags=("gameloop", "video"), requires={"emulator": "gameloop"},
)
P(
    "gl_hw_decoding_off", "Hardware video decoding: off", "gameloop", "safe",
    "Set EnableHardwareDecoding=0 in Config.ini under [engine].",
    [INI("engine", "EnableHardwareDecoding", "0", describe="disable hardware video decoding")],
    tags=("gameloop", "video"), requires={"emulator": "gameloop"},
)
P(
    "gl_renderer_directx_ini", "Config.ini renderer: DirectX", "gameloop", "moderate",
    "Set Engine_100038.Renderer=0 (DirectX/Smart) in Config.ini. Registry stays the runtime source; the INI keeps it sticky.",
    [INI("Engine_100038", "Renderer", "0")], tags=("gameloop", "renderer"),
    requires={"emulator": "gameloop"},
)
P(
    "gl_renderer_opengl_ini", "Config.ini renderer: OpenGL+", "gameloop", "moderate",
    "Set Engine_100038.Renderer=1 (OpenGL+) in Config.ini.",
    [INI("Engine_100038", "Renderer", "1")], tags=("gameloop", "renderer"),
    requires={"emulator": "gameloop"},
)
P(
    "gl_renderer_vulkan_ini", "Config.ini renderer: Vulkan", "gameloop", "moderate",
    "Set Engine_100038.Renderer=2 (Vulkan) in Config.ini.",
    [INI("Engine_100038", "Renderer", "2")], tags=("gameloop", "renderer"),
    requires={"emulator": "gameloop"},
)
P(
    "gl_vm_dpi_320", "VM DPI: 320", "gameloop", "moderate",
    "Set VMDPI=320, the legacy control profile for a 1440x1080 guest. Restart the emulator to apply.",
    [R(TENCENT, "VMDPI", 320, hive="HKCU")], tags=("gameloop", "display"),
    requires={"emulator": "gameloop"},
)
P(
    "gl_vm_resolution_1440x1080", "VM resolution: 1440x1080", "gameloop", "moderate",
    "Set VMResWidth=1440 and VMResHeight=1080 in the Tencent engine key.",
    [R(TENCENT, "VMResWidth", 1440, hive="HKCU"), R(TENCENT, "VMResHeight", 1080, hive="HKCU")],
    tags=("gameloop", "display"), requires={"emulator": "gameloop"},
)
P(
    "gl_force_directx", "Force DirectX rendering", "gameloop", "moderate",
    "Set ForceDirectX=1 in the Tencent engine key. Use only when the OpenGL path is broken on this driver.",
    [R(TENCENT, "ForceDirectX", 1, hive="HKCU")], tags=("gameloop", "renderer"),
    requires={"emulator": "gameloop"},
)
P(
    "gl_engine_cpu_high", "Give the engine high CPU priority", "gameloop", "moderate",
    "Set CpuPriorityClass=3 (High) for aow_exe.exe through IFEO. Applied at every process start.",
    IFEO_PRIO("aow_exe.exe", cpu=3), tags=("gameloop", "priority", "ifeo"),
)
P(
    "gl_engine_io_critical", "Give the engine top disk priority", "gameloop", "moderate",
    "Set IoPriority=3 (Critical) for aow_exe.exe through IFEO so guest disk I/O is not queued behind background work.",
    IFEO_PRIO("aow_exe.exe", io=3), tags=("gameloop", "priority", "ifeo"),
)
P(
    "gl_launcher_above_normal", "Launcher: above-normal CPU", "gameloop", "moderate",
    "Set CpuPriorityClass=6 (Above Normal) for AppMarket.exe.",
    IFEO_PRIO("AppMarket.exe", cpu=6), tags=("gameloop", "priority", "ifeo"),
)
P(
    "gl_launcher_high", "Launcher: high CPU", "gameloop", "aggressive",
    "Set CpuPriorityClass=3 (High) for AppMarket.exe. Benchmark it; a busy store UI can steal time from the game.",
    IFEO_PRIO("AppMarket.exe", cpu=3), tags=("gameloop", "priority", "ifeo"),
)
P(
    "gl_cef_low_priority", "Lower the store/ads UI priority", "gameloop", "moderate",
    "Set cef_frame_render.exe to Below Normal CPU and Low I/O priority so the launcher's web UI yields to the engine.",
    IFEO_PRIO("cef_frame_render.exe", cpu=5, io=1), tags=("gameloop", "priority", "ifeo"),
)
P(
    "gl_high_dpi_aware", "Stop Windows scaling GameLoop", "gameloop", "moderate",
    "Add HIGHDPIAWARE to GameLoop's emulator executables in AppCompatFlags. Stops DPI virtualization blur and scaling cost.",
    [DYN("game_loop_layers", flags="HIGHDPIAWARE")], tags=("gameloop", "compat"),
    requires={"emulator": "gameloop"},
)
P(
    "gl_dscp_ef", "DSCP EF marking for GameLoop", "gameloop", "moderate",
    "Tag GameLoop traffic with DSCP 46 (Expedited Forwarding) so routers with QoS treat it as latency-sensitive.",
    [DYN("qos_dscp")], tags=("gameloop", "network"), requires={"emulator": "gameloop"},
)
P(
    "gl_qos_bandwidth", "Remove QoS bandwidth reservation", "gameloop", "moderate",
    "Set NonBestEffortLimit=0 so Windows stops reserving 20% of the link for QoS-aware apps. Reboot to apply.",
    [R(PSCHED, "NonBestEffortLimit", 0)], tags=("gameloop", "network"), reboot=True,
)
P(
    "gl_gpo_qos", "Persistent DSCP policy for GameLoop", "gameloop", "aggressive",
    "Create per-app GPO QoS policies with DSCP 46 and unlimited throttle for GameLoop executables.",
    [DYN("gpo_qos")], tags=("gameloop", "network"), requires={"emulator": "gameloop"},
)
P(
    "gl_defender_exclusions", "Exclude GameLoop from Defender scans", "gameloop", "aggressive",
    "Add the GameLoop install folder to Defender path exclusions. Emulator disk I/O is heavy; the trade-off is that this folder is no longer scanned in real time.",
    [DYN("defender_exclusions")], tags=("gameloop", "defender"),
    requires={"emulator": "gameloop"},
)


# ======================================================================
# Android guest (ADB)
# ======================================================================

_ADB = {"emulator": "gameloop"}

P(
    "adb_guest_animations_off", "Guest: disable animations", "adb", "safe",
    "Set window, transition and animator scales to 0 inside the emulator. Makes the UI feel faster; a factory reset reverts it.",
    [
        ADB(["settings", "put", "global", "window_animation_scale", "0"]),
        ADB(["settings", "put", "global", "transition_animation_scale", "0"]),
        ADB(["settings", "put", "global", "animator_duration_scale", "0"]),
    ],
    tags=("adb", "ui"), requires=_ADB,
)
P(
    "adb_guest_hw_overlays_off", "Guest: force GPU composition", "adb", "aggressive",
    "Tell SurfaceFlinger to GPU-composite every layer (service call SurfaceFlinger 1008 i32 0). Some scenes lose FPS; opt in only if it helps.",
    [ADB(["service", "call", "SurfaceFlinger", "1008", "i32", "0"], ["service", "call", "SurfaceFlinger", "1008", "i32", "1"],
         describe="disable hardware overlays")],
    tags=("adb", "graphics"), requires=_ADB, reboot=True,
)
P(
    "adb_guest_bg_process_limit_zero", "Guest: background process limit 0", "adb", "aggressive",
    "Set background_process_limit=0 so Android keeps no cached background processes. The foreground game is untouched.",
    [ADB(["settings", "put", "global", "background_process_limit", "0"],
         ["settings", "put", "global", "background_process_limit", "-1"])],
    tags=("adb", "memory"), requires=_ADB,
)
P(
    "adb_guest_lmk_aggressive", "Guest: reap cached helpers sooner", "adb", "aggressive",
    "Raise only the low-memory-killer tail bands so cached helpers (gameloopplus, AppMarket, qqdownloader) are reaped earlier. Foreground and visible bands stay stock. Session-only.",
    [ADB(["echo '12288,15360,18432,32256,55296,80640' > /sys/module/lowmemorykiller/parameters/minfree"],
         ["echo '12288,15360,18432,21504,24576,30720' > /sys/module/lowmemorykiller/parameters/minfree"])],
    tags=("adb", "memory"), requires=_ADB,
)
P(
    "adb_guest_camera_off", "Guest: stop camera service", "adb", "moderate",
    "Ask the guest to stop its camera service. A VM has no camera; some GameLoop images restart it immediately and the result says so.",
    [ADB(["ctl.stop", "cameraserver"], ["ctl.start", "cameraserver"])],
    tags=("adb", "services"), requires=_ADB,
)
P(
    "adb_guest_prelaunch_cleanup", "Guest: pre-launch cleanup", "adb", "safe",
    "One-shot: kill cached background processes and trim caches before launching the game. Does not touch foreground or persistent apps.",
    [
        ADB(["am", "kill-all"]),
        ADB(["pm", "trim-caches", "999999999"]),
    ],
    tags=("adb", "cleanup"), requires=_ADB, reversible=False,
)
P(
    "adb_guest_play_services_off", "Guest: stop Google Play services", "adb", "danger",
    "Disable com.google.android.gms for the session. Frees RAM but breaks anything that depends on Play services; a factory reset or re-enable restores it.",
    [ADB(["pm", "disable-user", "--user", "0", "com.google.android.gms"],
         ["pm", "enable", "com.google.android.gms"])],
    tags=("adb", "services"), requires=_ADB,
)
P(
    "adb_guest_aot_compile", "Guest: AOT-compile all packages", "adb", "moderate",
    "Run cmd package compile -m speed -f --full -a to precompile packages and reduce JIT stutter. Takes a few minutes.",
    [ADB(["cmd", "package", "compile", "-m", "speed", "-f", "--full", "-a"])],
    tags=("adb", "performance"), requires=_ADB, reversible=False,
)
P(
    "adb_guest_hwui_opengl", "Guest: HWUI renderer OpenGL", "adb", "aggressive",
    "Set debug.hwui.renderer=opengl and restart the Android runtime. Some GameLoop images ignore it; reboot to revert.",
    [ADB(["setprop", "debug.hwui.renderer", "opengl"], ["setprop", "debug.hwui.renderer", "skiagl"])],
    tags=("adb", "graphics"), requires=_ADB, reboot=True,
)
P(
    "adb_guest_kernel_tuning", "Guest: kernel tuning bundle", "adb", "aggressive",
    "Bundle vm.swappiness=10 with the noop I/O scheduler on every block device. Requires root; reports honestly when the guest refuses. The scheduler change lasts until the guest reboots and is not journaled.",
    [DYN("adb_kernel_tuning")], tags=("adb", "kernel"), requires=_ADB, reboot=True,
    reversible=False,
)
P(
    "adb_guest_build_prop_perf", "Guest: add performance props", "adb", "danger",
    "Append a documented performance block to /system/build.prop. Requires root and a writable system partition; the original file is copied to /sdcard/build.prop.backup first. Undo is manual: restore that backup.",
    [DYN("adb_build_prop")], tags=("adb", "kernel"), requires=_ADB, reboot=True,
    reversible=False,
)
P(
    "adb_guest_swappiness", "Guest: lower swappiness", "adb", "moderate",
    "Set vm.swappiness=10 so the guest prefers reclaiming cache over swapping game pages. Requires root.",
    [ADB(["sysctl", "-w", "vm.swappiness=10"], ["sysctl", "-w", "vm.swappiness=60"])],
    tags=("adb", "kernel"), requires=_ADB,
)
P(
    "adb_guest_io_scheduler", "Guest: noop I/O scheduler", "adb", "aggressive",
    "Set the noop/none scheduler on every block device. Best on SSDs; do not use on spinning media. Lasts until the guest reboots.",
    [DYN("adb_io_scheduler")], tags=("adb", "kernel"), requires=_ADB, reboot=True,
    reversible=False,
)
P(
    "adb_guest_trim_caches", "Guest: trim caches", "adb", "safe",
    "Ask Android to trim cached files. One-shot and harmless.",
    [ADB(["pm", "trim-caches", "999999999"])],
    tags=("adb", "cleanup"), requires=_ADB, reversible=False,
)
P(
    "adb_guest_force_stop_store", "Guest: stop Play Store", "adb", "moderate",
    "Force-stop com.android.vending for this session. It restarts on demand.",
    [ADB(["am", "force-stop", "com.android.vending"])],
    tags=("adb", "background"), requires=_ADB, reversible=False,
)
P(
    "adb_guest_disable_facebook", "Guest: disable Facebook app", "adb", "moderate",
    "Disable com.facebook.katana and com.facebook.orca inside the emulator. Facebook's background services are a well-known RAM/CPU drain in GameLoop.",
    [DYN("adb_facebook")], tags=("adb", "background"), requires=_ADB,
)


# ======================================================================
# Windows scheduler, kernel, services and shell
# ======================================================================

_EL = {"elevated": True}

P(
    "win_win32_priority_separation", "Short quanta + high foreground boost", "windows", "moderate",
    "Set Win32PrioritySeparation=0x26. Short variable quanta with a strong foreground boost gives the game more CPU time and lowers input latency.",
    [R(PRIORITY, "Win32PrioritySeparation", 38)], tags=("scheduler", "latency"), requires=_EL,
)
P(
    "win_global_timer_resolution", "System-wide 0.5 ms timer", "windows", "moderate",
    "Set GlobalTimerResolutionRequests=1 so any process requesting a 0.5 ms timer applies it system-wide. Reboot to apply.",
    [R(KERNEL, "GlobalTimerResolutionRequests", 1)], tags=("scheduler", "latency"),
    requires=_EL, reboot=True,
)
P(
    "win_keep_kernel_in_ram", "Keep the kernel in RAM", "windows", "aggressive",
    "Set DisablePagingExecutive=1. Keeps the kernel and drivers in physical memory and removes kernel page-fault stalls. Costs roughly 200-400 MB of RAM.",
    [R(MM, "DisablePagingExecutive", 1)], tags=("kernel", "memory"), requires=_EL, reboot=True,
)
P(
    "win_network_throttling_off", "Remove the network throttling cap", "windows", "aggressive",
    "Set NetworkThrottlingIndex=0xFFFFFFFF. Removes the default 10-packets/ms cap. On some systems this raises NDIS DPC latency instead; benchmark it.",
    [R(SYS_PROFILE, "NetworkThrottlingIndex", 0xFFFFFFFF)], tags=("network", "latency"),
    requires=_EL,
)
P(
    "win_system_responsiveness", "Reserve less CPU for background apps", "windows", "moderate",
    "Set MMCSS SystemResponsiveness=10, the lowest value the kernel honours. The game gets the other 90%.",
    [R(SYS_PROFILE, "SystemResponsiveness", 10)], tags=("scheduler", "mmcss"), requires=_EL,
)
P(
    "win_nagle_off", "Disable Nagle's algorithm", "windows", "moderate",
    "Set TcpAckFrequency=1, TcpNoDelay=1 and TcpDelAckTicks=0 on every TCP/IP interface. Small packets are sent immediately.",
    [DYN("tcp_ack")], tags=("network", "latency"),
)
P(
    "win_presence_qos_off", "Stop idle-mouse throttling", "windows", "moderate",
    "Set DisableUserPresenceQos=1 so Windows does not treat an idle mouse as 'away' and quietly throttle the game.",
    [R(r"SYSTEM\CurrentControlSet\Control\Power\PowerThrottling", "DisableUserPresenceQos", 1)],
    tags=("scheduler", "latency"), requires=_EL,
)
P(
    "win_driver_search_off", "Stop Windows replacing the GPU driver", "windows", "safe",
    "Set SearchOrderConfig=0 so Windows Update does not silently replace a working GPU driver mid-session.",
    [R(r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", "SearchOrderConfig", 0)],
    tags=("gpu", "stability"),
)
P(
    "win_mpo_off", "Disable MPO (stutter fix)", "windows", "moderate",
    "Set DWM OverlayTestMode=5 to disable Multi-Plane Overlay. Fixes some flicker and stutter on NVIDIA and AMD; reboot to apply.",
    [R(DWM, "OverlayTestMode", 5)], tags=("graphics", "stutter"), reboot=True,
)
P(
    "win_pool_usage_max", "Cap paged-pool growth", "windows", "moderate",
    "Set PoolUsageMaximum=60 to avoid the slow-after-hours paged-pool growth pattern. Reboot to apply.",
    [R(MM, "PoolUsageMaximum", 60)], tags=("memory", "kernel"), requires=_EL, reboot=True,
)
P(
    "win_clear_pagefile_off", "Faster shutdown: don't clear pagefile", "windows", "safe",
    "Set ClearPageFileAtShutdown=0. Windows stops wiping the pagefile on every shutdown, which shortens shutdown and avoids needless disk writes.",
    [R(MM, "ClearPageFileAtShutdown", 0)], tags=("shutdown", "storage"), requires=_EL,
)
P(
    "win_page_combining_off", "Disable page combining", "windows", "danger",
    "Set DisablePageCombining=1. Can free memory-management overhead on some builds, but it removes a memory-saving feature. Reboot to apply.",
    [R(MM, "DisablePageCombining", 1)], tags=("memory", "danger"), requires=_EL, reboot=True,
)
P(
    "win_svchost_20gb", "Group services into fewer processes", "windows", "danger",
    "Set SvcHostSplitThresholdInKB to 20 GB so Windows groups service hosts into fewer processes. Trims per-process overhead but reduces service isolation. Reboot.",
    [R(r"SYSTEM\CurrentControlSet\Control", "SvcHostSplitThresholdInKB", 0x1400000)],
    tags=("services", "danger"), requires=_EL, reboot=True,
)
P(
    "win_port_thread_priority", "Printer stack: high port thread", "windows", "danger",
    "Set PortThreadPriority=1 under LanmanServer. A legacy print-spooler tweak; keep it only if you understand the trade-off.",
    [R(r"SYSTEM\CurrentControlSet\Services\LanmanServer\Parameters", "PortThreadPriority", 1)],
    tags=("danger",), requires=_EL, reboot=True,
)
P(
    "win_vbs_off", "Disable VBS / HVCI", "windows", "danger",
    "Turn off Virtualization-Based Security and Hypervisor-Enforced Code Integrity. This can recover 5-10% FPS in the emulator but lowers malware resistance. Reboot.",
    [
        C(["bcdedit", "/set", "hypervisorlaunchtype", "off"],
          ["bcdedit", "/set", "hypervisorlaunchtype", "auto"], describe="disable hypervisor launch"),
        R(r"SYSTEM\CurrentControlSet\Control\DeviceGuard", "EnableVirtualizationBasedSecurity", 0),
        R(r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity", "Enabled", 0),
    ],
    tags=("danger", "security", "fps"), requires=_EL, reboot=True,
)
P(
    "win_mitigations_off", "Disable CPU mitigations", "windows", "danger",
    "Set FeatureSettingsOverride and FeatureSettingsOverrideMask to 3. Removes Spectre/Meltdown mitigations for speed at a real security cost. Reboot.",
    [
        R(MM, "FeatureSettingsOverride", 3),
        R(MM, "FeatureSettingsOverrideMask", 3),
    ],
    tags=("danger", "security"), requires=_EL, reboot=True,
)
P(
    "win_bcdedit_timers", "BCEdit timer tweaks", "windows", "danger",
    "Disable dynamic ticks, force the platform tick and request the enhanced TSC sync policy. Can help or hurt depending on firmware. Reboot.",
    [
        C(["bcdedit", "/set", "disabledynamictick", "yes"],
          ["bcdedit", "/set", "disabledynamictick", "no"], describe="disable dynamic ticks"),
        C(["bcdedit", "/set", "useplatformtick", "yes"],
          ["bcdedit", "/deletevalue", "useplatformtick"], describe="force platform tick"),
        C(["bcdedit", "/set", "tscsyncpolicy", "enhanced"],
          ["bcdedit", "/set", "tscsyncpolicy", "default"], describe="enhanced TSC sync"),
    ],
    tags=("danger", "timers"), requires=_EL, reboot=True,
)
P(
    "win_hpet_off", "Remove the HPET override", "windows", "danger",
    "Delete the useplatformclock BCD value so Windows uses the invariant TSC instead of forcing HPET. Reboot.",
    [C(["bcdedit", "/deletevalue", "useplatformclock"],
       ["bcdedit", "/set", "useplatformclock", "true"], describe="remove HPET override")],
    tags=("danger", "timers"), requires=_EL, reboot=True,
)
P(
    "win_ntfs_8dot3_off", "Disable NTFS 8.3 names", "windows", "moderate",
    "Disable short-filename creation on NTFS. Speeds up directory churn in emulator disk images.",
    [C(["fsutil", "behavior", "set", "disable8dot3", "1"],
       ["fsutil", "behavior", "set", "disable8dot3", "0"], describe="disable 8.3 names")],
    tags=("storage",), requires=_EL, reboot=True,
)
P(
    "win_trim_enable", "Verify and enable TRIM", "windows", "safe",
    "Set DisableDeleteNotify=0 so SSDs receive TRIM. One-shot; the previous state is not known, so there is nothing to revert.",
    [C(["fsutil", "behavior", "set", "DisableDeleteNotify", "0"], describe="enable TRIM")],
    tags=("storage",), requires=_EL, reversible=False,
)
P(
    "win_modern_standby_off", "Disable Modern Standby", "windows", "danger",
    "Set PlatformAoAcOverride=0 to force classic S3 sleep on laptops that expose it. Reboot; some machines do not support S3 at all.",
    [R(r"SYSTEM\CurrentControlSet\Control\Power", "PlatformAoAcOverride", 0)],
    tags=("power", "danger"), requires=_EL, reboot=True,
)
P(
    "win_audio_latency", "Lower audio latency", "windows", "moderate",
    "Raise the Audio MMCSS task to GPU Priority 8, Priority 6, Scheduling Category High and SFIO Priority High.",
    [
        R(SYS_PROFILE + r"\Tasks\Audio", "GPU Priority", 8),
        R(SYS_PROFILE + r"\Tasks\Audio", "Priority", 6),
        S(SYS_PROFILE + r"\Tasks\Audio", "Scheduling Category", "High"),
        S(SYS_PROFILE + r"\Tasks\Audio", "SFIO Priority", "High"),
    ],
    tags=("audio", "latency"), requires=_EL,
)
P(
    "win_sticky_keys_off", "Disable Sticky Keys popup", "windows", "safe",
    "Set StickyKeys Flags=506 so five taps of Shift no longer opens the accessibility prompt mid-game.",
    [S(r"Control Panel\Accessibility\StickyKeys", "Flags", "506", hive="HKCU")],
    tags=("input",),
)
P(
    "win_filter_keys_off", "Disable Filter Keys popup", "windows", "safe",
    "Set Keyboard Response Flags=122 so a held key no longer opens Filter Keys mid-game.",
    [S(r"Control Panel\Accessibility\Keyboard Response", "Flags", "122", hive="HKCU")],
    tags=("input",),
)
P(
    "win_widgets_off", "Disable Windows 11 Widgets", "windows", "safe",
    "Hide the Widgets button and disable the news/interests feed.",
    [
        R(r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "TaskbarDa", 0, hive="HKCU"),
        R(r"SOFTWARE\Policies\Microsoft\Dsh", "AllowNewsAndInterests", 0),
    ],
    tags=("shell", "background"),
)
P(
    "win_push_notifications_off", "Disable push notifications", "windows", "safe",
    "Set ToastEnabled=0 so notifications stop interrupting fullscreen play.",
    [R(r"Software\Microsoft\Windows\CurrentVersion\PushNotifications", "ToastEnabled", 0, hive="HKCU")],
    tags=("shell", "focus"),
)
P(
    "win_update_no_restart", "No automatic update restart", "windows", "moderate",
    "Set NoAutoRebootWithLoggedOnUsers=1 so Windows Update cannot reboot the machine while you are playing.",
    [R(r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU", "NoAutoRebootWithLoggedOnUsers", 1)],
    tags=("stability",), requires=_EL,
)
P(
    "win_mouse_buffer", "Reduce mouse input buffer", "windows", "moderate",
    "Set MouseDataQueueSize=20 under mouclass. A shallower queue lowers mouse latency on some controllers.",
    [R(r"SYSTEM\CurrentControlSet\Services\mouclass\Parameters", "MouseDataQueueSize", 20)],
    tags=("input", "latency"), requires=_EL, reboot=True,
)
P(
    "win_keyboard_buffer", "Reduce keyboard input buffer", "windows", "moderate",
    "Set KeyboardDataQueueSize=20 under kbdclass.",
    [R(r"SYSTEM\CurrentControlSet\Services\kbdclass\Parameters", "KeyboardDataQueueSize", 20)],
    tags=("input", "latency"), requires=_EL, reboot=True,
)
P(
    "win_ntfs_last_access_off", "Disable NTFS last-access writes", "windows", "safe",
    "Set NtfsDisableLastAccessUpdate=1. Stops a metadata write on every file read, which matters for emulator disk images.",
    [R(r"SYSTEM\CurrentControlSet\Control\FileSystem", "NtfsDisableLastAccessUpdate", 1)],
    tags=("storage",), requires=_EL,
)
P(
    "win_prefetch_off", "Disable Prefetch", "windows", "aggressive",
    "Set EnablePrefetcher=0. Reduces background disk work on fast SSDs, but slows cold application launches.",
    [R(MM + r"\PrefetchParameters", "EnablePrefetcher", 0)],
    tags=("storage", "aggressive"), requires=_EL,
)
P(
    "win_memory_compression_off", "Disable memory compression", "windows", "aggressive",
    "Disable the Memory Compression store. Frees the compression worker's CPU at the cost of more pagefile use.",
    [PS("Disable-MMAgent -mc", "Enable-MMAgent -mc", describe="disable memory compression")],
    tags=("memory", "aggressive"), requires=_EL, reboot=True,
)
P(
    "win_search_off", "Disable Windows Search indexer", "windows", "moderate",
    "Stop and disable the WSearch service. Removes constant background disk and CPU work.",
    [SVC("WSearch", "disabled", stop=True)], tags=("services", "background"), requires=_EL,
)
P(
    "win_sysmain_off", "Disable SysMain (Superfetch)", "windows", "moderate",
    "Stop and disable SysMain. On an SSD its prefetching is mostly unnecessary background I/O.",
    [SVC("SysMain", "disabled", stop=True)], tags=("services", "storage"), requires=_EL,
)
P(
    "win_telemetry_off", "Disable telemetry services", "windows", "moderate",
    "Disable DiagTrack and dmwappushservice and set AllowTelemetry=0.",
    [
        SVC("DiagTrack", "disabled", stop=True),
        SVC("dmwappushservice", "disabled", stop=True),
        R(r"SOFTWARE\Policies\Microsoft\Windows\DataCollection", "AllowTelemetry", 0),
    ],
    tags=("services", "privacy"), requires=_EL,
)
P(
    "win_error_reporting_off", "Disable Windows Error Reporting", "windows", "moderate",
    "Stop and disable WerSvc and suppress the crash UI. Cuts background crash uploads.",
    [
        SVC("WerSvc", "disabled", stop=True),
        R(r"SOFTWARE\Microsoft\Windows\Windows Error Reporting", "DontShowUI", 1),
    ],
    tags=("services", "background"), requires=_EL,
)
P(
    "win_delivery_optimization_off", "Disable Delivery Optimization", "windows", "moderate",
    "Disable the DoSvc peer-to-peer update cache and set DODownloadMode=0.",
    [
        SVC("DoSvc", "disabled", stop=True),
        R(r"SOFTWARE\Microsoft\Windows\CurrentVersion\DeliveryOptimization\Config", "DODownloadMode", 0),
    ],
    tags=("services", "network"), requires=_EL,
)
P(
    "win_office_telemetry_off", "Disable Office telemetry", "windows", "moderate",
    "Set the Office 16.0 client telemetry policy to disabled. Office keeps working; it stops phoning home.",
    [R(r"SOFTWARE\Policies\Microsoft\Office\16.0\Common\ClientTelemetry", "DisableTelemetry", 1)],
    tags=("privacy", "office"), requires=_EL,
)
P(
    "win_cortana_off", "Disable Cortana / Bing search", "windows", "moderate",
    "Set BingSearchEnabled=0, CortanaConsent=0 and AllowCortana=0. Stops web-backed search and its background work.",
    [
        R(r"Software\Microsoft\Windows\CurrentVersion\Search", "BingSearchEnabled", 0, hive="HKCU"),
        R(r"Software\Microsoft\Windows\CurrentVersion\Search", "CortanaConsent", 0, hive="HKCU"),
        R(r"SOFTWARE\Policies\Microsoft\Windows\Windows Search", "AllowCortana", 0),
    ],
    tags=("privacy", "background"), requires=_EL,
)
P(
    "win_background_uwp_off", "Disable background UWP apps", "windows", "moderate",
    "Set GlobalUserDisabled=1 so Store apps stop running in the background.",
    [R(r"Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications", "GlobalUserDisabled", 1, hive="HKCU")],
    tags=("background", "uwp"),
)
P(
    "win_etw_autologgers_off", "Disable ETW autologgers", "windows", "aggressive",
    "Set Start=0 for the DiagLog and SQM autologgers. Reduces tracing overhead; diagnostics tools lose data.",
    [
        R(r"SYSTEM\CurrentControlSet\Control\WMI\Autologger\DiagLog", "Start", 0),
        R(r"SYSTEM\CurrentControlSet\Control\WMI\Autologger\SQMLogger", "Start", 0),
    ],
    tags=("tracing", "aggressive"), requires=_EL, reboot=True,
)
P(
    "win_shutdown_tweaks", "Faster shutdown and responsiveness", "windows", "moderate",
    "Set WaitToKillServiceTimeout=2000 and ClearPageFileAtShutdown=0.",
    [
        S(r"SYSTEM\CurrentControlSet\Control", "WaitToKillServiceTimeout", "2000"),
        R(MM, "ClearPageFileAtShutdown", 0),
    ],
    tags=("shutdown",), requires=_EL,
)
P(
    "win_storage_sense_off", "Disable Storage Sense", "windows", "safe",
    "Turn off Storage Sense's automatic cleanup schedule.",
    [R(r"Software\Microsoft\Windows\CurrentVersion\StorageSense\Parameters\StoragePolicy", "01", 0, hive="HKCU")],
    tags=("storage", "background"),
)
P(
    "win_content_delivery_off", "Disable Start ads and suggestions", "windows", "safe",
    "Stop Start-menu suggestions, silent app installs and preinstalled app pushes.",
    [
        R(r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-338388Enabled", 0, hive="HKCU"),
        R(r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-338389Enabled", 0, hive="HKCU"),
        R(r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-310093Enabled", 0, hive="HKCU"),
        R(r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SilentInstalledAppsEnabled", 0, hive="HKCU"),
        R(r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "PreInstalledAppsEnabled", 0, hive="HKCU"),
        R(r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "OemPreInstalledAppsEnabled", 0, hive="HKCU"),
        R(r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SystemPaneSuggestionsEnabled", 0, hive="HKCU"),
    ],
    tags=("shell", "privacy"),
)
P(
    "win_edge_background_off", "Stop Edge running in the background", "windows", "safe",
    "Set Edge BackgroundModeEnabled=0 so the browser does not keep background processes alive.",
    [R(r"SOFTWARE\Policies\Microsoft\Edge", "BackgroundModeEnabled", 0)],
    tags=("shell", "background"), requires=_EL,
)
P(
    "win_ceip_tasks_off", "Disable CEIP scheduled tasks", "windows", "moderate",
    "Disable the Customer Experience Improvement Program and Application Experience upload tasks.",
    [
        C(["schtasks", "/Change", "/TN", r"\Microsoft\Windows\Customer Experience Improvement Program\Consolidator", "/Disable"],
          ["schtasks", "/Change", "/TN", r"\Microsoft\Windows\Customer Experience Improvement Program\Consolidator", "/Enable"]),
        C(["schtasks", "/Change", "/TN", r"\Microsoft\Windows\Application Experience\ProgramDataUpdater", "/Disable"],
          ["schtasks", "/Change", "/TN", r"\Microsoft\Windows\Application Experience\ProgramDataUpdater", "/Enable"]),
        C(["schtasks", "/Change", "/TN", r"\Microsoft\Windows\Application Experience\Microsoft Compatibility Appraiser", "/Disable"],
          ["schtasks", "/Change", "/TN", r"\Microsoft\Windows\Application Experience\Microsoft Compatibility Appraiser", "/Enable"]),
    ],
    tags=("privacy", "tasks"), requires=_EL,
)
P(
    "win_services_extended", "Disable extra background services", "windows", "aggressive",
    "Disable a curated set of non-essential services: RetailDemo, MapsBroker, WMPNetworkSvc, PhoneSvc, Fax, RemoteRegistry and Geolocation.",
    [
        SVC("RetailDemo", "disabled", stop=True),
        SVC("MapsBroker", "disabled", stop=True),
        SVC("WMPNetworkSvc", "disabled", stop=True),
        SVC("PhoneSvc", "disabled", stop=True),
        SVC("Fax", "disabled", stop=True),
        SVC("RemoteRegistry", "disabled", stop=True),
        SVC("lfsvc", "disabled", stop=True),
    ],
    tags=("services", "aggressive"), requires=_EL,
)
P(
    "win_empty_working_sets", "Trim working sets now", "windows", "safe",
    "Ask every user process to release its unused working set. One-shot; Windows refills the pages on demand.",
    [PS("$sig='[DllImport(\"psapi.dll\")] public static extern bool EmptyWorkingSet(IntPtr h);';"
        "$t=Add-Type -MemberDefinition $sig -Name P -PassThru;"
        "Get-Process | ForEach-Object { try { [void]$t::EmptyWorkingSet($_.Handle) } catch {} };"
        "'Working sets trimmed.'", None, describe="trim working sets")],
    tags=("memory", "one-shot"), reversible=False,
)
P(
    "win_windowed_game_opt", "Optimize windowed games", "windows", "safe",
    "Set DirectXUserGlobalSettings SwapEffectUpgradeEnable=1 so windowed/borderless games use the modern swap path.",
    [S(r"Software\Microsoft\DirectX\UserGpuPreferences", "DirectXUserGlobalSettings",
       "SwapEffectUpgradeEnable=1;", hive="HKCU")],
    tags=("graphics", "fullscreen"),
)
P(
    "win_game_mode_flags", "Enable Windows Game Mode flags", "windows", "safe",
    "Set AllowAutoGameMode=1 and AutoGameModeEnabled=1 under GameBar.",
    [
        R(r"Software\Microsoft\GameBar", "AllowAutoGameMode", 1, hive="HKCU"),
        R(r"Software\Microsoft\GameBar", "AutoGameModeEnabled", 1, hive="HKCU"),
    ],
    tags=("gaming", "scheduler"),
)
P(
    "win_fso_flags", "Fullscreen optimizations for GameLoop", "windows", "moderate",
    "Add the fullscreen-optimization compatibility flags to GameLoop's executables, preserving any existing layer flags.",
    [DYN("game_loop_layers", flags="FSO")], tags=("gameloop", "fullscreen"),
    requires={"emulator": "gameloop"},
)
P(
    "win_msmq_tcp_nodelay", "MSMQ: disable Nagle", "windows", "moderate",
    "Set TCPNoDelay=1 under MSMQ Parameters.",
    [R(r"SOFTWARE\Microsoft\MSMQ\Parameters", "TCPNoDelay", 1)],
    tags=("network", "latency"), requires=_EL,
)
P(
    "win_io_page_lock_limit", "Raise IoPageLockLimit", "windows", "danger",
    "Set IoPageLockLimit=131072 (128 MB) so more I/O can be locked in memory. Helps heavy disk streams; can starve low-RAM systems.",
    [R(MM, "IoPageLockLimit", 131072)], tags=("memory", "danger"), requires=_EL, reboot=True,
)
P(
    "win_system_cooling_active", "System cooling policy: active", "windows", "safe",
    "Set the processor cooling policy to Active so the fan ramps before the CPU downclocks.",
    [PC("54533251-82be-4824-96c1-47b60b740d00", "94d3a615-a899-4ac5-ae2b-e4d8f634367f",
        ac=1, dc=1, describe="system cooling policy active")],
    tags=("power", "thermal"), requires=_EL,
)
P(
    "win_fth_disable", "Disable Fault Tolerant Heap", "windows", "moderate",
    "Set FTH Enabled=0. FTH silently shims crashing apps and adds overhead; disabling it makes real crashes visible.",
    [R(r"SOFTWARE\Microsoft\FTH", "Enabled", 0)], tags=("stability",), requires=_EL, reboot=True,
)


# ======================================================================
# Power / processor
# ======================================================================

_SUB_PROC = "54533251-82be-4824-96c1-47b60b740d00"
_EPP = "36687f9e-e3a5-4dbf-b1dc-15eb381c6863"
_BOOST_POL = "45bcc044-d885-43e2-8605-ee0ec6e96b59"
_IDLE_DISABLE = "5d76a2ca-e8c0-402f-a133-2158492d58ad"
_SUB_PCIE = "501a4d13-42af-4429-9fd1-a8218c268e20"
_ASPM = "ee12f906-d277-404b-b6da-e5fa1a576df5"
_SUB_DISK = "0012ee47-9041-4b5d-9b77-535fba8b1442"
_DISK_IDLE = "6738e2c4-e8a5-4a42-b16a-e040e769756e"

P(
    "power_epp_zero", "Intel Speed Shift EPP = 0", "power", "aggressive",
    "Set the energy-performance preference to 0 on AC and battery. Lets the CPU pick maximum frequency sooner; costs power and heat.",
    [PC(_SUB_PROC, _EPP, ac=0, dc=0, describe="Speed Shift EPP 0")],
    tags=("intel", "cpu", "aggressive"), requires={"elevated": True, "cpu_vendor": "intel"},
)
P(
    "power_cstate_limit", "Disable processor idle states", "power", "aggressive",
    "Set IDLEDISABLE=1 for AC and battery so the CPU does not enter deep C-states. Raises idle power and heat.",
    [PC(_SUB_PROC, _IDLE_DISABLE, ac=1, dc=1, describe="disable idle states")],
    tags=("cpu", "aggressive"), requires={"elevated": True},
)
P(
    "power_boost_policy_max", "Boost policy: maximum", "power", "aggressive",
    "Set PERFBOOSTPOL=100 so the processor boosts aggressively when a game thread needs it.",
    [PC(_SUB_PROC, _BOOST_POL, ac=100, dc=100, describe="boost policy maximum")],
    tags=("cpu", "aggressive"), requires={"elevated": True},
)
P(
    "power_pcie_aspm_off", "Disable PCIe ASPM", "power", "aggressive",
    "Set the PCI Express link state power management to Off. Removes a common source of micro-stutter; adds a little idle power.",
    [PC(_SUB_PCIE, _ASPM, ac=0, dc=0, describe="PCIe ASPM off")],
    tags=("pcie", "latency", "aggressive"), requires={"elevated": True},
)
P(
    "power_disk_sleep_never", "Never sleep the disk", "power", "moderate",
    "Set the disk idle timeout to 0 so the emulator's disk image is never spun down mid-session.",
    [PC(_SUB_DISK, _DISK_IDLE, ac=0, dc=0, describe="disk never sleeps")],
    tags=("storage",), requires={"elevated": True},
)


# ======================================================================
# GPU: NVIDIA and AMD
# ======================================================================

_GPU = {"elevated": True}

P(
    "gpu_nvidia_scheduler_tweaks", "NVIDIA: Direct Flip + GPU timer", "gpu", "moderate",
    "Set EnableDirectFlip=1, EnableIndependentFlip=1, UseGpuTimer=1 and per-core DPC scheduling on nvlddmkm.",
    [
        R(NV, "EnableDirectFlip", 1), R(NV, "EnableIndependentFlip", 1),
        R(NV, "UseGpuTimer", 1), R(NV, "RmGpsPsEnablePerCpuCoreDpc", 1),
    ],
    tags=("nvidia", "scheduler"), requires={**_GPU, "gpu_vendor": "nvidia"}, reboot=True,
)
P(
    "gpu_nvidia_hdcp_off", "NVIDIA: disable HDCP", "gpu", "moderate",
    "Disable HDCP 2.2 initialization on the NVIDIA GPU class key. Removes display-path overhead when no protected content is played.",
    [DYN("gpu_class_values", vendor="nvidia", values={"RmDisableHdcp22": 1, "RMSkipHdcp22Init": 1})],
    tags=("nvidia", "display"), requires={**_GPU, "gpu_vendor": "nvidia"}, reboot=True,
)
P(
    "gpu_nvidia_preemption_off", "NVIDIA: disable driver preemption", "gpu", "aggressive",
    "Disable mid-buffer and async preemption on nvlddmkm. Can reduce micro-stutter; a hung GPU command can stall longer.",
    [
        R(NV, "DisablePreemption", 1),
        R(NV, "EnableMidBufferPreemption", 0),
        R(NV, "EnableAsyncMidBufferPreemption", 0),
    ],
    tags=("nvidia", "aggressive"), requires={**_GPU, "gpu_vendor": "nvidia"}, reboot=True,
)
P(
    "gpu_nvidia_telemetry_off", "NVIDIA: disable telemetry", "gpu", "moderate",
    "Opt out of NVIDIA telemetry, stop and disable NvTelemetryContainer, and disable its scheduled tasks.",
    [
        R(r"SOFTWARE\NVIDIA Corporation\NvControlPanel2\Client", "OptInOrOutPreference", 0),
        R(r"SOFTWARE\NVIDIA Corporation\Global\FTS", "EnableRID44231", 0),
        R(r"SOFTWARE\NVIDIA Corporation\Global\FTS", "EnableRID64640", 0),
        R(r"SOFTWARE\NVIDIA Corporation\Global\FTS", "EnableRID66610", 0),
        SVC("NvTelemetryContainer", "disabled", stop=True),
    ],
    tags=("nvidia", "privacy"), requires=_GPU,
)
P(
    "gpu_nvidia_profiling_access", "NVIDIA: allow GPU counter access", "gpu", "moderate",
    "Set RmProfilingAdminOnly=0 so GPU performance counters can be read without elevation.",
    [R(NV + r"\Global\NVTweak", "RmProfilingAdminOnly", 0)],
    tags=("nvidia", "diagnostic"), requires={**_GPU, "gpu_vendor": "nvidia"},
)
P(
    "gpu_nvidia_gsp_firmware", "NVIDIA: enable GSP firmware", "gpu", "moderate",
    "Enable the GPU System Processor firmware on the NVIDIA class key. Usually lowers latency on RTX 20-series and newer.",
    [DYN("gpu_class_values", vendor="nvidia", values={"EnableGpuFirmware": 1})],
    tags=("nvidia", "firmware"), requires={**_GPU, "gpu_vendor": "nvidia"}, reboot=True,
)
P(
    "gpu_nvidia_msi_mode", "NVIDIA: MSI interrupt mode", "gpu", "moderate",
    "Set MSISupported=1 and MessageNumberLimit=2 on the NVIDIA PCI device. Lowers DPC latency on many boards. Reboot.",
    [DYN("gpu_msi")], tags=("nvidia", "latency"), requires=_GPU, reboot=True,
)
P(
    "gpu_nvidia_watchdog_timeout", "NVIDIA: extend RM watchdog", "gpu", "aggressive",
    "Raise the NVIDIA RM watchdog timeout to 10 seconds so a long shader compile is not mistaken for a driver hang.",
    [R(NV, "RmWatchdogTimeout", 10)], tags=("nvidia", "stability"),
    requires={**_GPU, "gpu_vendor": "nvidia"}, reboot=True,
)
P(
    "gpu_amd_ulps_off", "AMD: disable ULPS", "gpu", "moderate",
    "Disable Ultra Low Power State on the AMD GPU class key. Stops clock ramp delays when a frame burst starts.",
    [DYN("gpu_class_values", vendor="amd", values={"EnableUlps": 0})],
    tags=("amd", "power"), requires={**_GPU, "gpu_vendor": "amd"}, reboot=True,
)
P(
    "gpu_amd_crash_defender_off", "AMD: disable Crash Defender", "gpu", "moderate",
    "Stop and disable the AMD Crash Defender service. It can mask real driver resets and adds a background process.",
    [SVC("AMD Crash Defender Service", "disabled", stop=True)],
    tags=("amd", "services"), requires={**_GPU, "gpu_vendor": "amd"},
)
P(
    "gpu_amd_flip_queue", "AMD: flip queue size 1", "gpu", "aggressive",
    "Set FlipQueueSize=1 on the AMD GPU class key to reduce render-ahead and input lag. May lower average FPS.",
    [DYN("gpu_class_values", vendor="amd", values={"FlipQueueSize": 1})],
    tags=("amd", "latency", "aggressive"), requires={**_GPU, "gpu_vendor": "amd"}, reboot=True,
)
P(
    "gpu_amd_surface_format", "AMD: surface format optimization", "gpu", "moderate",
    "Enable AMD surface-format replacements and Catalyst AI texture optimization on the GPU class key.",
    [DYN("gpu_class_values", vendor="amd",
         values={"SurfaceFormatReplacementsEnable": 1, "CatalystAI": 1})],
    tags=("amd", "graphics"), requires={**_GPU, "gpu_vendor": "amd"}, reboot=True,
)
P(
    "gpu_amd_stutter_mode_off", "AMD: disable StutterMode", "gpu", "aggressive",
    "Set StutterMode=0 on the AMD GPU class key. Only relevant on Polaris through RDNA2; newer cards ignore it.",
    [DYN("gpu_class_values", vendor="amd", values={"StutterMode": 0})],
    tags=("amd", "stutter", "aggressive"), requires={**_GPU, "gpu_vendor": "amd"}, reboot=True,
)


# ======================================================================
# Network extras
# ======================================================================

P(
    "net_interrupt_moderation_off", "Disable adapter interrupt moderation", "network", "aggressive",
    "Disable interrupt moderation on active physical adapters. Lowers latency but raises CPU interrupts; benchmark it.",
    [DYN("nic_interrupt_moderation", enable=False)],
    tags=("network", "latency", "aggressive"), requires=_EL, reboot=True,
)
P(
    "net_nic_power_saving_off", "Disable NIC power saving", "network", "moderate",
    "Disable 'allow the computer to turn off this device' on active physical adapters.",
    [DYN("nic_power_saving", enable=False)],
    tags=("network", "power"), requires=_EL,
)
P(
    "net_dns_cache_tuning", "Hot DNS resolver cache", "network", "moderate",
    "Raise the DNS positive cache TTL to 24 hours so repeated lookups stay local.",
    [R(r"SYSTEM\CurrentControlSet\Services\Dnscache\Parameters", "MaxCacheTtl", 86400)],
    tags=("network", "dns"), requires=_EL,
)


# ======================================================================
# Cleanup
# ======================================================================

P(
    "clean_gameloop_shader_cache", "Clean GameLoop shader cache", "cleanup", "moderate",
    "Delete GameLoop's ShaderCache and GPU cache folders. They regenerate; stale shaders are a common cause of post-update stutter.",
    [DYN("clean_paths", under="gameloop", names=("ShaderCache", "GPUCache", "Cache"))],
    tags=("cleanup", "gameloop"), requires={"emulator": "gameloop"}, reversible=False,
)
P(
    "clean_gameloop_web_cache", "Clean AppMarket web cache", "cleanup", "moderate",
    "Delete the AppMarket/CEF Cache, Code Cache, GPUCache and WebKit folders.",
    [DYN("clean_paths", under="gameloop", names=("Cache", "Code Cache", "GPUCache", "WebKit"))],
    tags=("cleanup", "gameloop"), requires={"emulator": "gameloop"}, reversible=False,
)
P(
    "clean_gameloop_logs", "Clean GameLoop logs", "cleanup", "safe",
    "Delete loose GameLoop log files so the next benchmark run is easy to read.",
    [DYN("clean_files", under="gameloop", patterns=("*.log", "*.dmp"))],
    tags=("cleanup", "gameloop"), requires={"emulator": "gameloop"}, reversible=False,
)
P(
    "clean_mobile_shared_logs", "Clean MobileGamePCShared logs", "cleanup", "safe",
    "Delete logcat, dump and log artifacts under MobileGamePCShared.",
    [DYN("clean_files", under="gameloop", patterns=("logcat*.txt", "*.dmp", "*.log"))],
    tags=("cleanup", "gameloop"), requires={"emulator": "gameloop"}, reversible=False,
)
P(
    "clean_nvidia_shader_caches", "Clean NVIDIA shader caches", "cleanup", "safe",
    "Delete NVIDIA DXCache and GLCache. They rebuild on demand.",
    [DYN("clean_known", paths=(r"%LOCALAPPDATA%\NVIDIA\DXCache", r"%LOCALAPPDATA%\NVIDIA\GLCache"))],
    tags=("cleanup", "nvidia"), reversible=False,
)
P(
    "clean_amd_shader_caches", "Clean AMD shader caches", "cleanup", "safe",
    "Delete AMD DxCache and VkCache. They rebuild on demand.",
    [DYN("clean_known", paths=(r"%LOCALAPPDATA%\AMD\DxCache", r"%LOCALAPPDATA%\AMD\VkCache"))],
    tags=("cleanup", "amd"), reversible=False,
)
P(
    "clean_directx_shader_cache", "Clean DirectX shader cache", "cleanup", "safe",
    "Delete the per-user D3DSCache folder.",
    [DYN("clean_known", paths=(r"%LOCALAPPDATA%\D3DSCache",))],
    tags=("cleanup", "graphics"), reversible=False,
)
P(
    "clean_softwaredistribution", "Purge Windows Update cache", "cleanup", "aggressive",
    "Stop wuauserv and delete the SoftwareDistribution download cache. Windows re-downloads what it still needs.",
    [DYN("clean_update_cache")], tags=("cleanup", "windows"), requires=_EL, reversible=False,
)
P(
    "clean_delivery_optimization_cache", "Purge Delivery Optimization cache", "cleanup", "moderate",
    "Delete the Delivery Optimization download cache.",
    [DYN("clean_known", paths=(r"C:\Windows\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache",))],
    tags=("cleanup", "windows"), requires=_EL, reversible=False,
)
P(
    "clean_crash_dumps_wer", "Clear crash dumps and WER", "cleanup", "safe",
    "Delete per-user crash dumps, minidumps and queued Windows Error Reporting files.",
    [DYN("clean_known",
         paths=(r"%LOCALAPPDATA%\CrashDumps", r"C:\Windows\Minidump",
                r"C:\ProgramData\Microsoft\Windows\WER\ReportQueue",
                r"C:\ProgramData\Microsoft\Windows\WER\ReportArchive"))],
    tags=("cleanup", "windows"), reversible=False,
)
P(
    "clean_windows_old", "Remove Windows.old", "cleanup", "danger",
    "Delete C:\\Windows.old. This removes the rollback copy of the previous Windows install; it cannot be undone.",
    [DYN("clean_known", paths=(r"C:\Windows.old",))],
    tags=("cleanup", "danger"), requires=_EL, reversible=False,
)
P(
    "clean_dism_component", "Deep component-store cleanup", "cleanup", "danger",
    "Run DISM /StartComponentCleanup /ResetBase. Frees component-store space but makes installed updates non-removable.",
    [C(["dism.exe", "/Online", "/Cleanup-Image", "/StartComponentCleanup", "/ResetBase"],
       describe="DISM component cleanup")],
    tags=("cleanup", "danger"), requires=_EL, reversible=False,
)
P(
    "clean_aow_drv_log", "Clean C:\\aow_drv.log", "cleanup", "safe",
    "Delete the legacy GameLoop AOW driver log. It can grow without bound.",
    [DYN("clean_known", paths=(r"C:\aow_drv.log",))],
    tags=("cleanup", "gameloop"), reversible=False,
)


# ======================================================================
# Operation runner
# ======================================================================

# Options, not baseline. "Apply all" walks every default-enabled row in a
# family, so two contradictory presets must never both be enabled: that would
# write the same value twice and leave whichever ran last. Renderer, VSync and
# quality presets are therefore opt-in; only one member of a pair (or neither,
# when the right answer depends on the hardware) carries the default.
_NOT_BASELINE = {
    # GameLoop: pick one renderer, one quality level, one priority profile.
    "gl_vm_cpu_6",
    "gl_fxaa_balanced",
    "gl_fxaa_high",
    "gl_vulkan_on",
    "gl_vulkan_off",
    "gl_opengl_version_3",
    "gl_renderer_gles",
    "gl_vsync_on",
    "gl_shader_cache_off",
    "gl_local_shader_cache_off",
    "gl_render_optimize_off",
    "gl_dedicated_gpu_on",
    "gl_dedicated_gpu_off",
    "gl_hw_decoding_off",
    "gl_renderer_directx_ini",
    "gl_renderer_opengl_ini",
    "gl_renderer_vulkan_ini",
    "gl_vm_dpi_320",
    "gl_vm_resolution_1440x1080",
    "gl_force_directx",
    # One-shot jobs that take minutes or restart the guest.
    "adb_guest_aot_compile",
    "adb_guest_play_services_off",
    "adb_guest_hw_overlays_off",
    "adb_guest_bg_process_limit_zero",
    "adb_guest_lmk_aggressive",
    "adb_guest_hwui_opengl",
    "adb_guest_kernel_tuning",
    "adb_guest_build_prop_perf",
    "adb_guest_io_scheduler",
    # Mutually exclusive GPU vendor families are gated anyway; keep the
    # slower ones out of a batch run.
    "gpu_amd_flip_queue",
    "gpu_amd_stutter_mode_off",
}

for _pid in _NOT_BASELINE:
    PROFILES[_pid]["default_enabled"] = False


_VTYPES = {
    "dword": reg.REG_DWORD,
    "qword": reg.REG_QWORD,
    "sz": reg.REG_SZ,
    "expand_sz": reg.REG_EXPAND_SZ,
    "binary": reg.REG_BINARY,
}

_SERVICE_START = {"auto": 2, "manual": 3, "demand": 3, "disabled": 4, "delayed": 2}

_PS_HEAD = [
    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
    "-Command",
]


def _ps_argv(script: str) -> list[str]:
    return _PS_HEAD + [script]


def _gameloop_root(ctx) -> Optional[Path]:
    info = ctx.emulators.get("gameloop")
    if not info or not getattr(info, "installed", False) or not getattr(info, "root", None):
        return None
    root = Path(info.root)
    return root if root.is_dir() else None


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _size_of(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


def _delete_path(path: Path, *, dry: bool) -> int:
    """Delete a file or tree, returning bytes that were (or would be) freed."""
    size = _size_of(path)
    if dry or not path.exists():
        return size
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass
    return size


def _reg(ctx, op: dict, result: ActionResult) -> None:
    vtype = _VTYPES.get(op.get("vtype", "dword"), reg.REG_DWORD)
    value = op.get("value")
    change = ctx.reg.set_value(
        op["hive"], op["path"], op["name"], vtype, value,
        # Name the value in the result: the UI shows this line, not the path.
        describe=op.get("describe") or op["name"],
    )
    if change:
        result.add(change)


def _del(ctx, op: dict, result: ActionResult) -> None:
    change = ctx.reg.delete_value(
        op["hive"], op["path"], op["name"],
        describe=op.get("describe") or f"removed {op['name']}",
    )
    if change:
        result.add(change)


def _cmd(ctx, op: dict, result: ActionResult) -> None:
    out = ctx.shell.run(
        op["argv"], timeout=op.get("timeout", 90),
        describe=op.get("describe", ""), reversible=bool(op.get("restore")),
    )
    if op.get("restore"):
        ctx.shell.record_restore(op["restore"], describe=op.get("describe", ""))
    if not out.skipped and not out.ok:
        result.warn(f"{op.get('describe') or op['argv'][0]}: {out.stderr or out.stdout or 'exit ' + str(out.returncode)}")
    result.add(
        Change(
            kind=ChangeKind.COMMAND,
            target=" ".join(op["argv"][:2]),
            detail=op.get("describe") or " ".join(op["argv"][:3]),
            reversible=bool(op.get("restore")),
        )
    )


def _svc(ctx, op: dict, result: ActionResult) -> None:
    name = op["name"]
    start = _SERVICE_START.get(str(op.get("start", "manual")).lower(), 3)
    base = rf"SYSTEM\CurrentControlSet\Services\{name}"
    change = ctx.reg.set_value(
        "HKLM", base, "Start", reg.REG_DWORD, start,
        describe=op.get("describe") or f"{name} start type",
    )
    if change:
        result.add(change)
    if op.get("stop"):
        query = ctx.shell.run(["sc.exe", "query", name], timeout=20, record=False,
                              describe=f"query {name}")
        was_running = "RUNNING" in (query.stdout or "").upper()
        out = ctx.shell.run(["sc.exe", "stop", name], timeout=45,
                            describe=f"stop {name}", reversible=was_running)
        if out.skipped:
            return
        if not out.ok and "not installed" not in (out.stderr or "").lower():
            result.warn(f"could not stop {name}: {out.stderr or out.stdout}")
        elif was_running:
            ctx.shell.record_restore(["sc.exe", "start", name],
                                     describe=f"restart {name}")
        result.add(
            Change(
                kind=ChangeKind.SERVICE,
                target=name,
                detail=f"start type {op.get('start')}" + ("; stopped" if was_running else ""),
                reversible=True,
            )
        )


def _adb(ctx, op: dict, result: ActionResult) -> None:
    out = adb_shell(ctx, [], op["argv"], describe=op.get("describe", ""))
    if op.get("restore"):
        adb = _adb_bin(ctx)
        if adb:
            ctx.shell.record_restore([adb, "shell"] + op["restore"], describe=op.get("describe", ""))
    if not out.skipped and not out.ok:
        result.warn(f"adb {' '.join(op['argv'][:2])}: {out.stderr or out.stdout or 'exit ' + str(out.returncode)}")
    result.add(
        Change(
            kind=ChangeKind.ADB,
            target="adb shell " + " ".join(op["argv"][:2]),
            detail=op.get("describe") or " ".join(op["argv"][:3]),
            reversible=bool(op.get("restore")),
        )
    )


def _adb_bin(ctx) -> Optional[str]:
    for key in ("gameloop", "mumu", "ldplayer"):
        info = ctx.emulators.get(key)
        if info and getattr(info, "adb", None):
            return info.adb
    return None


def _powercfg(ctx, op: dict, result: ActionResult) -> None:
    pc = PowerCfg(ctx.shell, ctx.journal)
    for change in pc.set_index(
        op["sub"], op["setting"], ac=op.get("ac"), dc=op.get("dc"),
        describe=op.get("describe", ""),
    ):
        result.add(change)


# ------------------------------------------------------------------ INI file


def _read_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    # GameLoop writes UTF-16LE on some installs without a BOM. ASCII text in
    # UTF-16LE has a NUL in every other byte, which UTF-8 never produces.
    if b"\x00" in raw[:400]:
        try:
            return raw.decode("utf-16-le"), "utf-16-le"
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1"


def _write_text(path: Path, text: str, encoding: str) -> None:
    if encoding == "utf-16":
        path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    else:
        path.write_text(text, encoding=encoding, newline="")


def _set_ini_value(text: str, section: str, key: str, value: str) -> str:
    """Set ``[section] key=value`` without rewriting the rest of the file."""
    lines = text.splitlines(keepends=True)
    header = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped[1:-1].strip().lower() == section.lower():
                header = i
                break
    if header is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + f"[{section}]\n{key}={value}\n"

    end = len(lines)
    for j in range(header + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = j
            break

    for j in range(header + 1, end):
        stripped = lines[j].strip()
        if "=" in stripped and stripped.split("=", 1)[0].strip().lower() == key.lower():
            newline = "\r\n" if lines[j].endswith("\r\n") else "\n"
            lines[j] = f"{key}={value}{newline}"
            return "".join(lines)

    newline = "\r\n" if "\r\n" in text else "\n"
    insert = f"{key}={value}{newline}"
    insert_at = end if end > header + 1 else header + 1
    lines.insert(insert_at, insert)
    return "".join(lines)


def _ini(ctx, op: dict, result: ActionResult) -> None:
    root = _gameloop_root(ctx)
    if root is None:
        result.warn("GameLoop is not installed; Config.ini was not touched")
        return

    candidates = []
    try:
        candidates = list(root.rglob("Config.ini"))
    except OSError:
        candidates = []
    if not candidates:
        result.warn("Config.ini not found under the GameLoop install folder")
        return
    target = candidates[0]
    if not _inside(root, target):
        result.warn("Config.ini resolved outside the GameLoop folder; refusing")
        return

    try:
        text, encoding = _read_text(target)
    except OSError as exc:
        result.warn(f"Config.ini is not readable: {exc}")
        return

    updated = _set_ini_value(text, op["section"], op["key"], op["value"])
    if updated == text:
        return

    if not ctx.preview:
        backup_dir = paths.data_dir() / "file-backups" / ctx.action_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{target.name}.{len(list(backup_dir.glob(target.name + '.*')))}"
        try:
            shutil.copy2(target, backup)
            ctx.journal.record_file(str(target), str(backup))
            _write_text(target, updated, encoding)
        except OSError as exc:
            result.warn(f"could not write Config.ini: {exc}")
            return

    result.add(
        Change(
            kind=ChangeKind.FILE,
            target=str(target),
            detail=f"[{op['section']}] {op['key']}={op['value']}",
            reversible=True,
        )
    )


# ------------------------------------------------------------ dynamic routines


def _dyn(ctx, op: dict, result: ActionResult) -> None:
    name = op["name"]
    fn = _DYNAMIC.get(name)
    if fn is None:
        raise RuntimeError(f"unknown dynamic routine {name!r}")
    fn(ctx, op, result)


def _dyn_host_cores(ctx, op, result):
    cores = getattr(ctx.hw, "cpu_cores", None) or 8
    if not getattr(ctx.hw, "cpu_cores", None):
        result.warn("physical core count could not be detected; using 8")
    _reg(ctx, {**op, "op": "reg", "vtype": "dword", "value": int(cores)}, result)


def _dyn_tcp_ack(ctx, op, result):
    touched = 0
    for sub in ctx.backend.subkeys("HKLM", INTERFACES):
        base = INTERFACES + "\\" + sub
        for name, value in (("TcpAckFrequency", 1), ("TcpNoDelay", 1), ("TcpDelAckTicks", 0)):
            change = ctx.reg.set_value("HKLM", base, name, reg.REG_DWORD, value,
                                       describe=name)
            if change:
                result.add(change)
                touched += 1
    if touched == 0:
        result.warn("no TCP/IP interface keys were found to tune")


def _dyn_game_loop_layers(ctx, op, result):
    root = _gameloop_root(ctx)
    if root is None:
        result.warn("GameLoop is not installed; compatibility flags were not written")
        return
    key = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
    token = "HIGHDPIAWARE" if op.get("flags") == "HIGHDPIAWARE" else "DISABLEDXMAXIMIZEDWINDOWEDMODE"
    seen = set()
    for exe in GAMELOOP_IFEO_EXES:
        for path in root.rglob(exe):
            if not _inside(root, path):
                continue
            full = str(path)
            if full in seen:
                continue
            seen.add(full)
            prev = ctx.reg.get_value("HKCU", key, full)
            current = str(prev[1]) if prev else ""
            if token in current:
                continue
            merged = (current + " " + token).strip() if current else f"~ {token}"
            change = ctx.reg.set_value("HKCU", key, full, reg.REG_SZ, merged,
                                       describe=f"compatibility flags for {exe}")
            if change:
                result.add(change)
    if not seen:
        result.warn("no GameLoop executables were found to flag")


def _dyn_defender_exclusions(ctx, op, result):
    root = _gameloop_root(ctx)
    if root is None:
        result.warn("GameLoop is not installed; no exclusion was added")
        return
    script = (
        f"Add-MpPreference -ExclusionPath '{root}' -Force -ErrorAction Stop; "
        f"Write-Output 'excluded {root}'"
    )
    _cmd(ctx, C(_ps_argv(script),
                _ps_argv(f"Remove-MpPreference -ExclusionPath '{root}' -Force -ErrorAction SilentlyContinue"),
                describe=f"exclude {root} from Defender"), result)


def _dyn_qos_dscp(ctx, op, result):
    exes = ", ".join(f"'{e}'" for e in GAMELOOP_IFEO_EXES)
    apply = (
        f"$exes=@({exes}); foreach($exe in $exes){{"
        f"$n='GameLoop_DSCP_'+($exe -replace '\\.exe','');"
        f"Get-NetQosPolicy -Name $n -ErrorAction SilentlyContinue | Remove-NetQosPolicy -Confirm:$false -ErrorAction SilentlyContinue;"
        f"New-NetQosPolicy -Name $n -AppPathNameMatchCondition $exe -DSCPAction 46 -PolicyStore ActiveStore -NetworkProfile All -ErrorAction SilentlyContinue | Out-Null}};"
        f"'DSCP policies refreshed'"
    )
    restore = (
        f"$exes=@({exes}); foreach($exe in $exes){{"
        f"$n='GameLoop_DSCP_'+($exe -replace '\\.exe','');"
        f"Get-NetQosPolicy -Name $n -ErrorAction SilentlyContinue | Remove-NetQosPolicy -Confirm:$false -ErrorAction SilentlyContinue}}"
    )
    _cmd(ctx, C(_ps_argv(apply), _ps_argv(restore), describe="DSCP EF for GameLoop"), result)


def _dyn_gpo_qos(ctx, op, result):
    base = r"SOFTWARE\Policies\Microsoft\Windows\QoS"
    values = (
        ("Version", reg.REG_SZ, "1.0"),
        ("Protocol", reg.REG_SZ, "*"),
        ("Local Port", reg.REG_SZ, "*"),
        ("Local IP", reg.REG_SZ, "*"),
        ("Local IP Prefix Length", reg.REG_SZ, "*"),
        ("Remote Port", reg.REG_SZ, "*"),
        ("Remote IP", reg.REG_SZ, "*"),
        ("Remote IP Prefix Length", reg.REG_SZ, "*"),
        ("DSCP Value", reg.REG_DWORD, 46),
        ("Throttle Rate", reg.REG_DWORD, -1),
    )
    for exe in GAMELOOP_IFEO_EXES:
        key = base + r"\GameLoop_" + exe[:-4].replace(".", "_")
        created = ctx.reg.create_key("HKLM", key, describe=f"QoS policy for {exe}")
        if created:
            result.add(created)
        change = ctx.reg.set_value("HKLM", key, "Application Name", reg.REG_SZ, exe,
                                   describe="QoS application")
        if change:
            result.add(change)
        for name, vtype, value in values:
            change = ctx.reg.set_value("HKLM", key, name, vtype, value,
                                       describe=f"QoS {name}")
            if change:
                result.add(change)


def _dyn_gpu_class_values(ctx, op, result):
    vendor = op.get("vendor", "")
    values = op.get("values", {})
    found = 0
    for sub in ctx.backend.subkeys("HKLM", GPU_CLASS):
        base = GPU_CLASS + "\\" + sub
        desc = ctx.reg.get_value("HKLM", base, "DriverDesc")
        if not desc:
            continue
        if vendor and vendor.lower() not in str(desc[1]).lower():
            continue
        found += 1
        for name, value in values.items():
            change = ctx.reg.set_value("HKLM", base, name, reg.REG_DWORD, int(value),
                                       describe=f"{name} on {desc[1]}")
            if change:
                result.add(change)
    if not found:
        result.warn(f"no {vendor or 'matching'} GPU class key was found; nothing changed")


def _dyn_gpu_msi(ctx, op, result):
    script = (
        "Get-ChildItem 'HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\PCI' -Recurse -ErrorAction SilentlyContinue | "
        "Where-Object { $_.PSChildName -eq 'Device Parameters' } | ForEach-Object { "
        "$parent = Split-Path $_.PSPath -Parent; "
        "$d = (Get-ItemProperty $parent -Name DeviceDesc -ErrorAction SilentlyContinue).DeviceDesc; "
        "if ($d -like '*NVIDIA*') { "
        "Set-ItemProperty $_.PSPath -Name MSISupported -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue; "
        "Set-ItemProperty $_.PSPath -Name MessageNumberLimit -Value 2 -Type DWord -Force -ErrorAction SilentlyContinue; "
        "Write-Output \"MSI on: $parent\" } }"
    )
    restore = (
        "Get-ChildItem 'HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\PCI' -Recurse -ErrorAction SilentlyContinue | "
        "Where-Object { $_.PSChildName -eq 'Device Parameters' } | ForEach-Object { "
        "$parent = Split-Path $_.PSPath -Parent; "
        "$d = (Get-ItemProperty $parent -Name DeviceDesc -ErrorAction SilentlyContinue).DeviceDesc; "
        "if ($d -like '*NVIDIA*') { "
        "Remove-ItemProperty $_.PSPath -Name MSISupported -ErrorAction SilentlyContinue; "
        "Remove-ItemProperty $_.PSPath -Name MessageNumberLimit -ErrorAction SilentlyContinue } }"
    )
    _cmd(ctx, C(_ps_argv(script), _ps_argv(restore), describe="NVIDIA MSI mode"), result)


def _dyn_nic_interrupt_moderation(ctx, op, result):
    enable = bool(op.get("enable"))
    value = "Enabled" if enable else "Disabled"
    other = "Disabled" if enable else "Enabled"
    script = (
        f"Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object {{ $_.Status -eq 'Up' }} | "
        f"ForEach-Object {{ Set-NetAdapterAdvancedProperty -Name $_.Name -DisplayName 'Interrupt Moderation' "
        f"-DisplayValue '{value}' -ErrorAction SilentlyContinue }}; 'interrupt moderation {value}'"
    )
    restore = (
        f"Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object {{ $_.Status -eq 'Up' }} | "
        f"ForEach-Object {{ Set-NetAdapterAdvancedProperty -Name $_.Name -DisplayName 'Interrupt Moderation' "
        f"-DisplayValue '{other}' -ErrorAction SilentlyContinue }}"
    )
    _cmd(ctx, C(_ps_argv(script), _ps_argv(restore), describe="adapter interrupt moderation"), result)


def _dyn_nic_power_saving(ctx, op, result):
    enable = bool(op.get("enable"))
    if enable:
        script = ("Get-NetAdapter -Physical -ErrorAction SilentlyContinue | ForEach-Object { "
                  "Enable-NetAdapterPowerManagement -Name $_.Name -ErrorAction SilentlyContinue }; 'power management enabled'")
        restore = ("Get-NetAdapter -Physical -ErrorAction SilentlyContinue | ForEach-Object { "
                   "Disable-NetAdapterPowerManagement -Name $_.Name -ErrorAction SilentlyContinue }")
    else:
        script = ("Get-NetAdapter -Physical -ErrorAction SilentlyContinue | ForEach-Object { "
                  "Disable-NetAdapterPowerManagement -Name $_.Name -ErrorAction SilentlyContinue }; 'power management disabled'")
        restore = ("Get-NetAdapter -Physical -ErrorAction SilentlyContinue | ForEach-Object { "
                   "Enable-NetAdapterPowerManagement -Name $_.Name -ErrorAction SilentlyContinue }")
    _cmd(ctx, C(_ps_argv(script), _ps_argv(restore), describe="NIC power saving"), result)


def _dyn_adb_kernel_tuning(ctx, op, result):
    apply = (
        "sysctl -w vm.swappiness=10 2>/dev/null; "
        "for d in /sys/block/*/queue/scheduler; do [ -f \"$d\" ] && "
        "(echo noop > \"$d\" 2>/dev/null || echo none > \"$d\" 2>/dev/null); done; "
        "echo tuned"
    )
    restore = "sysctl -w vm.swappiness=60 2>/dev/null; echo restored"
    _adb(ctx, ADB(["sh", "-c", apply], ["sh", "-c", restore], describe="guest kernel tuning"), result)


def _dyn_adb_io_scheduler(ctx, op, result):
    script = (
        "for d in /sys/block/*/queue/scheduler; do [ -f \"$d\" ] && "
        "(echo noop > \"$d\" 2>/dev/null || echo none > \"$d\" 2>/dev/null); done; echo io-tuned"
    )
    _adb(ctx, ADB(["sh", "-c", script], describe="guest I/O scheduler"), result)


def _dyn_adb_build_prop(ctx, op, result):
    adb = _adb_bin(ctx)
    if adb is None:
        result.warn("no adb executable found")
        return
    for argv, note in (
        ([adb, "root"], "adb root"),
        ([adb, "remount"], "adb remount"),
        ([adb, "shell", "sh", "-c", "cat /system/build.prop > /sdcard/build.prop.backup"],
         "backup build.prop"),
    ):
        out = ctx.shell.run(argv, timeout=60, describe=note)
        if not out.skipped and not out.ok:
            result.warn(f"{note} failed: {out.stderr or out.stdout}")
            return
    block = (
        "# === GameLoop Optimizer Performance Props ===\\n"
        "dalvik.vm.heapsize=512m\\n"
        "dalvik.vm.heapgrowthlimit=256m\\n"
        "dalvik.vm.heaptargetutilization=0.75\\n"
        "dalvik.vm.heapminfree=2m\\n"
        "dalvik.vm.heapmaxfree=8m\\n"
    )
    script = (
        "grep -q 'GameLoop Optimizer Performance Props' /system/build.prop 2>/dev/null || "
        f"printf '{block}' >> /system/build.prop; echo build.prop-updated"
    )
    adb_shell(ctx, [], ["sh", "-c", script], describe="append build.prop props")
    result.add(Change(kind=ChangeKind.ADB, target="/system/build.prop",
                      detail="performance props appended; original on /sdcard/build.prop.backup",
                      reversible=False))
    result.warn("reboot the emulator to apply build.prop changes")


def _dyn_adb_facebook(ctx, op, result):
    for pkg in ("com.facebook.katana", "com.facebook.orca"):
        _adb(ctx, ADB(["pm", "disable-user", "--user", "0", pkg],
                      ["pm", "enable", pkg], describe=f"disable {pkg}"), result)


def _dyn_clean_paths(ctx, op, result):
    root = _gameloop_root(ctx)
    if root is None:
        result.warn("GameLoop is not installed; nothing to clean")
        return
    names = set(op.get("names", ()))
    freed = 0
    for candidate in root.rglob("*"):
        if candidate.is_dir() and candidate.name in names and _inside(root, candidate):
            size = _size_of(candidate)
            _delete_path(candidate, dry=ctx.preview)
            freed += size
            result.add(Change(kind=ChangeKind.FILE, target=str(candidate),
                              detail=f"{'would free' if ctx.preview else 'freed'} "
                                     f"{size / (1024 ** 2):.1f} MB",
                              reversible=False))
    result.message = (f"{'would free' if ctx.preview else 'freed'} {freed / (1024 ** 2):.1f} MB")
    if freed == 0:
        result.message = "no matching cache folders were found"


def _dyn_clean_files(ctx, op, result):
    root = _gameloop_root(ctx)
    if root is None:
        result.warn("GameLoop is not installed; nothing to clean")
        return
    freed = 0
    count = 0
    for pattern in op.get("patterns", ()):
        for candidate in root.rglob(pattern):
            if candidate.is_file() and _inside(root, candidate):
                freed += _delete_path(candidate, dry=ctx.preview)
                count += 1
    result.add(Change(kind=ChangeKind.FILE, target=str(root),
                      detail=f"{'would remove' if ctx.preview else 'removed'} {count} file(s), "
                             f"{freed / (1024 ** 2):.1f} MB", reversible=False))
    result.message = f"{'would free' if ctx.preview else 'freed'} {freed / (1024 ** 2):.1f} MB"


def _dyn_clean_known(ctx, op, result):
    freed = 0
    for raw in op.get("paths", ()):
        path = Path(os.path.expandvars(raw))
        if not path.exists():
            continue
        size = _size_of(path)
        _delete_path(path, dry=ctx.preview)
        freed += size
        result.add(Change(kind=ChangeKind.FILE, target=str(path),
                          detail=f"{'would free' if ctx.preview else 'freed'} "
                                 f"{size / (1024 ** 2):.1f} MB", reversible=False))
    result.message = f"{'would free' if ctx.preview else 'freed'} {freed / (1024 ** 2):.1f} MB"


def _dyn_clean_update_cache(ctx, op, result):
    stop = ctx.shell.run(["net.exe", "stop", "wuauserv"], timeout=60, describe="stop Windows Update")
    if not stop.skipped and not stop.ok:
        result.warn(f"could not stop wuauserv: {stop.stderr or stop.stdout}")
    freed = 0
    target = Path(r"C:\Windows\SoftwareDistribution\Download")
    if target.exists():
        freed += _delete_path(target, dry=ctx.preview)
    if not ctx.preview:
        ctx.shell.run(["net.exe", "start", "wuauserv"], timeout=60, describe="start Windows Update")
    result.add(Change(kind=ChangeKind.FILE, target=str(target),
                      detail=f"{'would free' if ctx.preview else 'freed'} {freed / (1024 ** 2):.1f} MB",
                      reversible=False))
    result.message = f"{'would free' if ctx.preview else 'freed'} {freed / (1024 ** 2):.1f} MB"


_DYNAMIC: dict[str, Callable] = {
    "reg_host_cores": _dyn_host_cores,
    "tcp_ack": _dyn_tcp_ack,
    "game_loop_layers": _dyn_game_loop_layers,
    "defender_exclusions": _dyn_defender_exclusions,
    "qos_dscp": _dyn_qos_dscp,
    "gpo_qos": _dyn_gpo_qos,
    "gpu_class_values": _dyn_gpu_class_values,
    "gpu_msi": _dyn_gpu_msi,
    "nic_interrupt_moderation": _dyn_nic_interrupt_moderation,
    "nic_power_saving": _dyn_nic_power_saving,
    "adb_kernel_tuning": _dyn_adb_kernel_tuning,
    "adb_io_scheduler": _dyn_adb_io_scheduler,
    "adb_build_prop": _dyn_adb_build_prop,
    "adb_facebook": _dyn_adb_facebook,
    "clean_paths": _dyn_clean_paths,
    "clean_files": _dyn_clean_files,
    "clean_known": _dyn_clean_known,
    "clean_update_cache": _dyn_clean_update_cache,
}

_RUNNERS: dict[str, Callable] = {
    "reg": _reg,
    "del": _del,
    "cmd": _cmd,
    "svc": _svc,
    "adb": _adb,
    "powercfg": _powercfg,
    "ini": _ini,
    "dyn": _dyn,
}


# ------------------------------------------------------------------ handler


@action(
    args={"profile": {"type": str}},
    doc="Apply one verified legacy tweak profile. The profile table is compiled into this build.",
)
def apply_profile(ctx, args) -> ActionResult:
    profile_id = str(args.get("profile", ""))
    result = ActionResult(action_id=ctx.action_id)

    # The catalog can rename a row, but it cannot point it at another row's
    # compiled operations. This is the allowlist for legacy tweaks.
    if profile_id != ctx.action_id:
        raise RuntimeError(
            f"profile {profile_id!r} does not match action {ctx.action_id!r}"
        )
    profile = PROFILES.get(profile_id)
    if profile is None:
        raise RuntimeError(f"no compiled profile for action {ctx.action_id!r}")

    for op in profile["ops"]:
        runner = _RUNNERS.get(op.get("op", ""))
        if runner is None:
            result.warn(f"unknown operation {op.get('op')!r} was skipped")
            continue
        try:
            runner(ctx, op, result)
        except Exception as exc:  # noqa: BLE001 - one failed value must not hide the rest
            ctx.log.warning("legacy %s op %s failed: %s", profile_id, op.get("op"), exc)
            result.warn(f"{op.get('describe') or op.get('name') or op.get('op')}: {exc}")

    result.needs_reboot = bool(profile.get("needs_reboot"))
    result.message = f"{profile['label']}: {'previewed' if ctx.preview else 'applied'}"
    return result


HANDLERS = {
    "legacy.apply": apply_profile,
}



