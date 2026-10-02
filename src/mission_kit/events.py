"""业务事件与时间规则。

所有库存变化都是事件，状态由事件重放得出。统一约定：

- 每个事件携带 ``occurred_at``（含时区偏移的 ISO 8601 字符串），
  拒绝不带偏移的时间，避免跨时区记录被隐式假设错位；
- 重放按 UTC 瞬间排序，同一瞬间按到达先后（日志序号），
  晚到的离线补传事件会落到正确位置；
- 幂等由 ``event_id`` 保证：重复扫码、航段重复通知只生效一次，
  不会制造额外库存。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# 事件类型
LEG_ARRIVAL = "leg_arrival"  # 航段到达通知（按 航段+箱件 幂等）
UNPACK = "unpack"  # 拆箱：箱内物品转入临时库房
SPLIT = "split"  # 分装：从母批次划出子批次
ISSUE = "issue"  # 领用：库房 -> 领用人@诊室
CONSUME = "consume"  # 消耗：必须携带脱敏用途凭证 usage_ref
BORROW = "borrow"  # 借用：库房 -> 借用人（待归还）
RETURN = "return"  # 归还：借用人 -> 库房
DAMAGE = "damage"  # 报损：held=留存待处置 / lost=核销
ENV_READING = "env_reading"  # 温湿度记录；超标自动隔离关联批次
QUARANTINE = "quarantine"  # 隔离：只作用于指定物品
RELEASE = "release"  # 解除隔离：回到隔离前状态
DEVICE_ASSIGN = "device_assign"  # 设备调配到诊室
DEVICE_RETURN = "device_return"  # 设备退回库房
DEVICE_ACTIVATION = "device_activation"  # 设备启用核查（校准/软件/资质）
HANDOVER_ANNOUNCED = "handover_announced"  # 留置交接宣告（双语条款）
TRAINING_LOGGED = "training_logged"  # 接收方培训记录
HANDOVER_CONFIRMED = "handover_confirmed"  # 双方责任确认
HANDOVER_COMPLETED = "handover_completed"  # 交接完成（发凭证）
SHIP_BACK = "ship_back"  # 返运（附返运凭证）
DESTROY = "destroy"  # 销毁（附销毁凭证）

ALL_KINDS = frozenset(
    {
        LEG_ARRIVAL, UNPACK, SPLIT, ISSUE, CONSUME, BORROW, RETURN, DAMAGE,
        ENV_READING, QUARANTINE, RELEASE, DEVICE_ASSIGN, DEVICE_RETURN,
        DEVICE_ACTIVATION, HANDOVER_ANNOUNCED, TRAINING_LOGGED,
        HANDOVER_CONFIRMED, HANDOVER_COMPLETED, SHIP_BACK, DESTROY,
    }
)


@dataclass(frozen=True)
class Event:
    event_id: str
    kind: str
    occurred_at: str  # 含时区偏移的 ISO 8601
    payload: dict
    actor: str = "system"
    source: str = "manual"  # manual / scanner-offline / airway-bill / logger ...
    recorded_at: str | None = None


def parse_instant(value: str) -> datetime:
    """解析含时区偏移的时间并归一到 UTC；朴素时间直接拒绝。"""

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"无效的时间格式: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"时间必须携带时区偏移: {value!r}")
    return parsed.astimezone(timezone.utc)


def event_to_dict(event: Event) -> dict:
    return {
        "event_id": event.event_id,
        "kind": event.kind,
        "occurred_at": event.occurred_at,
        "payload": event.payload,
        "actor": event.actor,
        "source": event.source,
        "recorded_at": event.recorded_at,
    }


def event_from_dict(data: dict) -> Event:
    return Event(
        event_id=data["event_id"],
        kind=data["kind"],
        occurred_at=data["occurred_at"],
        payload=dict(data.get("payload") or {}),
        actor=data.get("actor", "system"),
        source=data.get("source", "manual"),
        recorded_at=data.get("recorded_at"),
    )
