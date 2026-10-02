"""读取项目已确认的最小数据合同，不包含业务流程实现。

v1（``fixtures/equipment_handoff.json``）只携带行动记录的元数据信封；v2 在
信封外增加独立的 append-only 事件流（见 :mod:`mission_kit.ledger`）。v1 记录
不含任何库存状态，迁移时元数据逐字段保留，``events`` 初始为空列表。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CURRENT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class DomainRecord:
    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str


def load_record(path: Path) -> DomainRecord:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return DomainRecord(**{k: payload[k] for k in DomainRecord.__dataclass_fields__})


def migrate_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """把 v1 元数据信封迁移为 v2：元数据原样保留，追加空事件流。"""
    version = payload.get("schema_version", 1)
    if version > CURRENT_SCHEMA_VERSION:
        raise ValueError(f"未知的合同版本 {version}，最高支持 {CURRENT_SCHEMA_VERSION}")
    if version == CURRENT_SCHEMA_VERSION:
        return payload
    if version != 1:
        raise ValueError(f"不支持从版本 {version} 迁移")
    migrated = {k: payload[k] for k in DomainRecord.__dataclass_fields__}
    migrated["schema_version"] = CURRENT_SCHEMA_VERSION
    migrated["events"] = []
    return migrated
