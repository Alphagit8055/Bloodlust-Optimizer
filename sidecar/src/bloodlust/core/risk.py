"""Requirement gating — the fail-closed layer.

An action is ``available`` only when every requirement is *positively satisfied*.
If detection could not determine a fact the requirement depends on, the action
is ``blocked``, not ``available``. That asymmetry is the whole point: a
mis-detected CPU must cost the user a tweak, never their machine.

``blocked`` carries a human-readable reason so the UI can explain the lock
instead of just greying a row out.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional

from .catalog import ActionSpec


def current_platform() -> str:
    """Indirection point so tests can change the platform without patching the
    global ``sys`` object (which breaks unrelated stdlib code that inspects it).
    """
    return sys.platform


@dataclass
class Gate:
    available: bool
    reasons: list[str]
    unknown: list[str]

    @property
    def reason(self) -> str:
        if self.available:
            return ""
        if self.unknown:
            return "Could not verify: " + "; ".join(self.unknown)
        return "; ".join(self.reasons)

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "reasons": self.reasons,
            "unknown": self.unknown,
            "reason": self.reason,
        }


def evaluate(
    spec: ActionSpec,
    hw,
    emulators: Optional[dict] = None,
) -> Gate:
    """Check ``spec.requires`` against detected hardware. Never raises."""
    reasons: list[str] = []
    unknown: list[str] = []
    req = spec.requires or {}
    emulators = emulators or {}

    # ---- platform --------------------------------------------------------
    want = req.get("platform")
    if want:
        if current_platform() != want:
            reasons.append(f"requires {want}, running on {sys.platform}")

    # ---- elevation -------------------------------------------------------
    if req.get("elevated") and not hw.is_elevated:
        reasons.append("requires administrator rights")

    # ---- OS build --------------------------------------------------------
    min_build = req.get("min_os_build")
    if min_build is not None:
        if hw.os_build is None:
            unknown.append("Windows build number")
        elif hw.os_build < int(min_build):
            reasons.append(f"requires Windows build {min_build}+, detected {hw.os_build}")

    # ---- CPU vendor ------------------------------------------------------
    want_vendor = req.get("cpu_vendor")
    if want_vendor:
        if hw.cpu_vendor is None:
            unknown.append("CPU vendor")
        elif hw.cpu_vendor != want_vendor:
            reasons.append(f"requires a {want_vendor} CPU, detected {hw.cpu_vendor}")

    # ---- CPU generation range -------------------------------------------
    gen_min = req.get("cpu_generation_min")
    gen_max = req.get("cpu_generation_max")
    if gen_min is not None or gen_max is not None:
        if hw.cpu_generation is None:
            # Only meaningful once the vendor matched; otherwise the vendor
            # line already explains the block and this would be noise.
            if not want_vendor or hw.cpu_vendor == want_vendor:
                unknown.append("CPU generation")
        else:
            if gen_min is not None and hw.cpu_generation < int(gen_min):
                reasons.append(
                    f"requires CPU generation >= {gen_min}, detected {hw.cpu_generation}"
                )
            if gen_max is not None and hw.cpu_generation > int(gen_max):
                reasons.append(
                    f"requires CPU generation <= {gen_max}, detected {hw.cpu_generation}"
                )

    # ---- GPU vendor ------------------------------------------------------
    want_gpu = req.get("gpu_vendor")
    if want_gpu:
        if hw.gpu_vendor is None:
            unknown.append("discrete GPU vendor")
        elif hw.gpu_vendor != want_gpu:
            reasons.append(f"requires an {want_gpu} GPU, detected {hw.gpu_vendor}")

    # ---- hypervisor absence ---------------------------------------------
    if req.get("hypervisor_absent"):
        if hw.hypervisor_present is None:
            unknown.append("hypervisor state")
        elif hw.hypervisor_present:
            reasons.append(
                "a hypervisor is active (Hyper-V / VBS / Core Isolation). "
                "This tweak would not take effect, so it is left alone"
            )

    # ---- emulator --------------------------------------------------------
    want_emu = req.get("emulator")
    if want_emu:
        info = emulators.get(want_emu)
        if info is None:
            reasons.append(f"unknown emulator target {want_emu!r}")
        elif not getattr(info, "installed", False):
            reasons.append(f"{getattr(info, 'name', want_emu)} is not installed")

    available = not reasons and not unknown
    return Gate(available=available, reasons=reasons, unknown=unknown)


def risk_ceiling_ok(spec_risk: str, ceiling: str, order: dict) -> bool:
    """True when ``spec_risk`` is at or below the user's chosen ceiling."""
    return order[spec_risk] <= order[ceiling]


__all__ = ["Gate", "evaluate", "risk_ceiling_ok"]
