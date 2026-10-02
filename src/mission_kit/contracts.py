"""读取项目已确认的最小数据合同，不包含业务流程实现。"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path

@dataclass(frozen=True)
class DomainRecord:
    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str

def load_record(path: Path) -> DomainRecord:
    """读取登记头；schema v2 新增的 ``mission`` 等字段由 load_manifest 解析。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(DomainRecord)}
    return DomainRecord(**{k: v for k, v in payload.items() if k in known})
