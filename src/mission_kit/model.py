"""跨境眼科行动器材交接的领域模型。

箱件（Case）与封签（seal_id）是全程记录的起点；耗材批次（Batch）按
报关单号登记批号、效期与储运温湿度范围；设备（Device）按序列号登记
校准记录与软件版本。所有标识一经登记不再变更；分装产生的新批次通过
``lineage_root`` 关联回原报关批次，使数量守恒可以按谱系复算。
"""

from __future__ import annotations

from dataclasses import dataclass

# 批次在账状态：数量仍计入结存，可在状态之间流转。
BATCH_LIVE_STATES = ("sealed", "stock", "issued", "borrowed", "quarantined", "damaged")
# 批次终态：数量离账但保留去向凭证，结存复算时计入守恒。
BATCH_TERMINAL_STATES = ("consumed", "destroyed", "handed_over", "shipped_back", "written_off")

# 设备在账状态。
DEVICE_LIVE_STATES = ("in_case", "warehouse", "room", "quarantined")
# 设备终态。
DEVICE_TERMINAL_STATES = ("handed_over", "shipped_back", "destroyed")


@dataclass(frozen=True)
class StorageRange:
    """耗材储运环境范围；缺省的一侧不校验。"""

    temp_min_c: float | None = None
    temp_max_c: float | None = None
    humidity_max_pct: float | None = None

    def allows(self, temp_c: float | None, humidity_pct: float | None) -> bool:
        if temp_c is not None:
            if self.temp_min_c is not None and temp_c < self.temp_min_c:
                return False
            if self.temp_max_c is not None and temp_c > self.temp_max_c:
                return False
        if humidity_pct is not None and self.humidity_max_pct is not None:
            if humidity_pct > self.humidity_max_pct:
                return False
        return True


@dataclass(frozen=True)
class Batch:
    """一批耗材：报关身份 + 批号效期 + 储运条件。"""

    batch_id: str
    name: str
    lot: str
    expiry: str  # YYYY-MM-DD，当日结束（UTC）前有效
    quantity: int  # 初始数量；根批次即报关数量
    unit: str
    customs_ref: str  # 报关单号
    storage: StorageRange
    lineage_root: str  # 分装谱系根批次
    parent_batch: str | None = None


@dataclass(frozen=True)
class Calibration:
    calibrated_at: str
    valid_until: str
    agency: str
    certificate_id: str


@dataclass(frozen=True)
class Device:
    """一台设备：序列号唯一，校准与软件版本决定能否启用。"""

    device_id: str
    name: str
    model: str
    serial: str
    software_version: str
    calibration: tuple[Calibration, ...]


@dataclass(frozen=True)
class Qualification:
    scope: str  # 设备型号
    valid_until: str


@dataclass(frozen=True)
class Operator:
    operator_id: str
    name: str
    qualifications: tuple[Qualification, ...]


@dataclass(frozen=True)
class Case:
    """一个箱件及其封签；内容物在拆箱前保持 sealed。"""

    case_id: str
    seal_id: str
    customs_ref: str
    batch_refs: tuple[str, ...]
    device_refs: tuple[str, ...]


@dataclass(frozen=True)
class Leg:
    """一个航段；重复通知按 (leg_id, case_id) 幂等去重。"""

    leg_id: str
    flight: str
    arrived_port: str


@dataclass(frozen=True)
class Certificate:
    """交接 / 返运 / 销毁凭证；digest 为内容防篡改指纹。"""

    certificate_id: str
    kind: str  # handover / return_shipment / destruction
    item_ids: tuple[str, ...]
    issuer: str
    issued_at: str
    detail: str
    digest: str
