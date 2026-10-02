"""设备启用核查：校准、软件版本、操作者资质。

核查是纯函数，只回答“能不能用、为什么不能”；是否隔离设备由台账
根据失败原因决定——只有涉及设备自身完整性（校准失效、软件未放行）
才隔离该设备，操作者资质不足仅拒绝本次启用，不影响设备与他人。
"""

from __future__ import annotations

from datetime import datetime

from .events import parse_instant
from .model import DEVICE_TERMINAL_STATES, Device, Operator

# 涉及设备自身完整性的失败原因；出现即隔离该设备（且只隔离该设备）。
INTEGRITY_REASONS = frozenset({"calibration_expired", "software_not_allowed"})


def calibration_valid(device: Device, at: datetime) -> bool:
    return any(parse_instant(c.valid_until) >= at for c in device.calibration)


def software_allowed(allowed: dict[str, tuple[str, ...]], device: Device) -> bool:
    return device.software_version in allowed.get(device.model, ())


def operator_qualified(operator: Operator | None, device: Device, at: datetime) -> bool:
    if operator is None:
        return False
    return any(
        q.scope == device.model and parse_instant(q.valid_until) >= at
        for q in operator.qualifications
    )


def activation_blockers(
    *,
    device_state: str,
    device: Device,
    operator: Operator | None,
    allowed_software: dict[str, tuple[str, ...]],
    at: datetime,
) -> list[str]:
    """返回阻止启用的原因代码；空列表表示允许启用。"""

    reasons: list[str] = []
    if device_state == "quarantined":
        reasons.append("device_quarantined")
    elif device_state in DEVICE_TERMINAL_STATES:
        reasons.append("device_unavailable")
    if not calibration_valid(device, at):
        reasons.append("calibration_expired")
    if not software_allowed(allowed_software, device):
        reasons.append("software_not_allowed")
    if not operator_qualified(operator, device, at):
        reasons.append("operator_not_qualified")
    return reasons
