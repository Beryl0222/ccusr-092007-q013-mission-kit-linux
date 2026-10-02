"""行动器材清单（mission manifest）的加载与校验。

清单随 ``equipment_handoff.json`` 登记，是台账的静态基线：箱件与封签、
耗材批次（报关单号、批号、效期、储运温湿度）、设备（序列号、校准、
软件版本）、人员资质与放行软件清单。

迁移说明：schema_version 1 的文件只有登记头（record_id 等六个字段），
仍可由 ``contracts.load_record`` 读取；``load_manifest`` 要求
schema_version >= 2 且携带 ``mission`` 块，否则抛出 ``ManifestError``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .model import (
    Batch,
    Calibration,
    Case,
    Device,
    Leg,
    Operator,
    Qualification,
    StorageRange,
)

SCHEMA_VERSION = 2


class ManifestError(ValueError):
    """清单缺失或自相矛盾。"""


@dataclass(frozen=True)
class MissionManifest:
    mission_id: str
    org: str
    destination_port: str
    customs_declaration: str
    handover_languages: tuple[str, ...]
    legs: dict[str, Leg]
    cases: dict[str, Case]
    batches: dict[str, Batch]
    devices: dict[str, Device]
    operators: dict[str, Operator]
    allowed_software: dict[str, tuple[str, ...]]


def _storage(data: dict | None) -> StorageRange:
    data = data or {}
    return StorageRange(
        temp_min_c=data.get("temp_min_c"),
        temp_max_c=data.get("temp_max_c"),
        humidity_max_pct=data.get("humidity_max_pct"),
    )


def load_manifest(path: str | Path) -> MissionManifest:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    version = payload.get("schema_version", 1)
    if version < SCHEMA_VERSION or "mission" not in payload:
        raise ManifestError(
            "器材清单需要 schema_version >= 2 且包含 mission 块；"
            "v1 文件仅含登记头，可由 load_record 读取"
        )
    m = payload["mission"]

    legs = {
        leg["leg_id"]: Leg(
            leg_id=leg["leg_id"], flight=leg["flight"], arrived_port=leg["arrived_port"]
        )
        for leg in m.get("legs", [])
    }
    batches: dict[str, Batch] = {}
    for b in m.get("batches", []):
        if b["quantity"] <= 0:
            raise ManifestError(f"批次数量必须为正: {b['batch_id']}")
        try:
            date.fromisoformat(b["expiry"])
        except ValueError as exc:
            raise ManifestError(f"批次数期无效: {b['batch_id']}") from exc
        batches[b["batch_id"]] = Batch(
            batch_id=b["batch_id"],
            name=b["name"],
            lot=b["lot"],
            expiry=b["expiry"],
            quantity=b["quantity"],
            unit=b["unit"],
            customs_ref=b["customs_ref"],
            storage=_storage(b.get("storage")),
            lineage_root=b["batch_id"],
        )
    devices = {
        d["device_id"]: Device(
            device_id=d["device_id"],
            name=d["name"],
            model=d["model"],
            serial=d["serial"],
            software_version=d["software_version"],
            calibration=tuple(
                Calibration(
                    calibrated_at=c["calibrated_at"],
                    valid_until=c["valid_until"],
                    agency=c["agency"],
                    certificate_id=c["certificate_id"],
                )
                for c in d.get("calibration", [])
            ),
        )
        for d in m.get("devices", [])
    }
    operators = {
        o["operator_id"]: Operator(
            operator_id=o["operator_id"],
            name=o["name"],
            qualifications=tuple(
                Qualification(scope=q["scope"], valid_until=q["valid_until"])
                for q in o.get("qualifications", [])
            ),
        )
        for o in m.get("operators", [])
    }
    cases: dict[str, Case] = {}
    for c in m.get("cases", []):
        case = Case(
            case_id=c["case_id"],
            seal_id=c["seal_id"],
            customs_ref=c["customs_ref"],
            batch_refs=tuple(c.get("batches", [])),
            device_refs=tuple(c.get("devices", [])),
        )
        for ref in case.batch_refs:
            if ref not in batches:
                raise ManifestError(f"箱件 {case.case_id} 引用未知批次 {ref}")
        for ref in case.device_refs:
            if ref not in devices:
                raise ManifestError(f"箱件 {case.case_id} 引用未知设备 {ref}")
        cases[case.case_id] = case

    allowed_software = {
        model: tuple(versions) for model, versions in m.get("allowed_software", {}).items()
    }
    for device in devices.values():
        if device.model not in allowed_software:
            raise ManifestError(f"设备型号 {device.model} 缺少放行软件清单")

    languages = tuple(m.get("handover_languages", []))
    if not languages:
        raise ManifestError("handover_languages 不能为空（留置交接须双语）")

    return MissionManifest(
        mission_id=m["mission_id"],
        org=m["org"],
        destination_port=m["destination_port"],
        customs_declaration=m["customs_declaration"],
        handover_languages=languages,
        legs=legs,
        cases=cases,
        batches=batches,
        devices=devices,
        operators=operators,
        allowed_software=allowed_software,
    )
