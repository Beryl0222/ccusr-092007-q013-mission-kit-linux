"""跨境义诊器材交接后端。

- contracts：登记头最小合同（保持既有标识与时间含义）；
- manifest：箱件、封签、批号效期、设备校准与人员资质的静态基线；
- ledger：事件溯源台账，数量守恒、幂等、离线补传、按物品隔离；
- devices：设备启用核查（校准 / 软件版本 / 操作者资质）；
- handover：留置资产的双语交接、培训与责任确认、凭证指纹；
- reports：位置 / 可用性 / 接管人查询与结存复算（不含患者诊疗详情）。
"""

from .contracts import DomainRecord, load_record
from .events import Event
from .handover import make_certificate
from .ledger import Ledger, RecordResult, Reject
from .manifest import ManifestError, MissionManifest, load_manifest
from .model import Certificate
from .store import JsonlEventStore

__all__ = [
    "Certificate",
    "DomainRecord",
    "Event",
    "JsonlEventStore",
    "Ledger",
    "ManifestError",
    "MissionManifest",
    "RecordResult",
    "Reject",
    "load_manifest",
    "load_record",
    "make_certificate",
]
