"""跨境器材交接后端的领域异常。"""

from __future__ import annotations


class LedgerError(Exception):
    """台账领域错误的基类。"""


class LedgerStateError(LedgerError):
    """暂存事件序号过期等内部一致性错误。"""


class DuplicateSubmission(LedgerError):
    """离线补传或航段重复通知被幂等丢弃时抛出，携带首次事件。"""

    def __init__(self, reason: str, original_event_id: str):
        super().__init__(f"重复事件已忽略（{reason}），首次事件 {original_event_id}")
        self.reason = reason
        self.original_event_id = original_event_id


class ConservationViolation(LedgerError):
    """拆箱、分装、领用等操作破坏数量守恒时抛出。"""


class UnknownReference(LedgerError):
    """引用了台账中不存在的箱件或物品。"""


class SealedContainerError(LedgerError):
    """封签状态与操作不符（重复拆封、未登记封签等）。"""


class ComplianceHold(LedgerError):
    """设备未通过校准、软件或资质核对，事件被拒绝。"""

    def __init__(self, failures: list[str]):
        super().__init__("设备启用核对未通过：" + ", ".join(failures))
        self.failures = failures


class HandoffIncomplete(LedgerError):
    """留置资产的双语交接、培训或责任确认未齐备。"""

    def __init__(self, missing: list[str]):
        super().__init__("留置交接材料不完整，缺少：" + ", ".join(missing))
        self.missing = missing


class PrivacyViolation(LedgerError):
    """事件载荷中出现患者诊疗标识，台账拒绝接收。"""
