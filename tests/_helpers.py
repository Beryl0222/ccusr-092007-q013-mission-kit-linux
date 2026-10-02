import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mission_kit import EventStore, Inventory, MissionService

T0 = "2026-09-20T09:00:00+08:00"


def ts(minute: int) -> str:
    # 固定 +08:00 时区下的递增时间戳，供各用例复用。
    h = 9 + minute // 60
    m = minute % 60
    return f"2026-09-20T{h:02d}:{m:02d}:00+08:00"


def build_service(path=None):
    store = EventStore(path)
    inventory = store.replay_into(Inventory())
    return MissionService(store, inventory)


def register_standard_box(svc, minute=0):
    """一箱标准物资：无菌手套(冷链)、普通纱布、一台便携筛查仪。"""
    return svc.register_manifest(
        container_id="BOX-1",
        seal_id="SEAL-A001",
        customs_ref="CUS-7788",
        location="airport-custody",
        contents=[
            {"item_id": "ITM-GLOVE", "sku": "GLOVE-S", "lot": "L2026-09",
             "expiry": "2028-03-01", "qty": 100, "unit": "副",
             "kind": "consumable", "cold_chain": True,
             "name": {"zh": "无菌手套", "en": "Sterile gloves"}},
            {"item_id": "ITM-GAUZE", "sku": "GAUZE-5", "lot": "L2026-11",
             "expiry": "2027-06-01", "qty": 50, "unit": "包",
             "kind": "consumable", "cold_chain": False,
             "name": {"zh": "纱布", "en": "Gauze"}},
        ],
        devices=[
            {"serial": "DEV-SCAN-01", "model": "EyeScan-P",
             "name": {"zh": "便携筛查仪", "en": "Portable scanner"},
             "required_software": "4.2.1", "software_version": "4.2.1",
             "calibration_due": "2027-01-15", "calibration_cert": "CAL-9001",
             "required_certs": ["OCT-CERT"], "accessories": ["ITM-PROBE"]},
        ],
        actor="logistics",
        occurred_at=ts(minute),
    )


def register_second_box(svc, minute=0):
    return svc.register_manifest(
        container_id="BOX-2",
        seal_id="SEAL-B002",
        customs_ref="CUS-7789",
        location="airport-custody",
        contents=[
            {"item_id": "ITM-PROBE", "sku": "PROBE-TIP", "lot": "L2026-10",
             "expiry": "2027-12-01", "qty": 20, "unit": "个",
             "kind": "accessory", "cold_chain": False,
             "name": {"zh": "探头套", "en": "Probe cover"}},
        ],
        devices=[],
        actor="logistics",
        occurred_at=ts(minute),
    )
