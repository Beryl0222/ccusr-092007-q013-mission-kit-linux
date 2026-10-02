"""库存投影：从不可变事件重放出"每件物品在哪里、能否使用、由谁持有"。

数量模型
========
每条耗材库存行（:class:`StockItem`）的数量在任意时刻满足::

    registered = available + issued + quarantined
               + consumed + written_off + retained + shipped_back + destroyed
               + subdivided

其中 ``subdivided`` 是经"分装"转入直接子库存行的数量。拆箱分装只在库存行
之间平移数量（父行减可用、子行增登记，两者互为 subdivided）；领用/借用是
状态平移（总数不减）；只有消耗、报损、留置、返运、销毁五种"处置"会减少
在途数量，且都带凭证或去向。投影在重放时逐条校验，任何破坏守恒的事件立即
抛错。

患者隐私
========
耗材使用只接受不透明就诊代号 ``ENC-XXXX`` 或非临床用途代码；结存报告只
输出聚合计数，不含任何就诊代号。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .errors import (
    ConservationViolation,
    SealedContainerError,
    UnknownReference,
)

# 非患者用途（培训、设备自检等），患者用途一律走 ENC-XXXX 脱敏代号。
NON_CLINICAL_PURPOSES = frozenset({"training", "device_self_test", "quality_check"})


def _as_date(value: str | date) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_utc_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class Box:
    container_id: str
    seal_id: str
    customs_ref: str
    status: str = "sealed"                 # sealed | open
    location: str = "inbound_custody"
    contents: list[str] = field(default_factory=list)  # item_id
    device_serials: list[str] = field(default_factory=list)
    seal_intact: bool = True
    seal_verified: bool = False            # 必须经扫码核验才允许拆箱


@dataclass
class StockItem:
    item_id: str
    sku: str
    lot: str
    expiry: date | None
    unit: str
    kind: str                              # consumable | accessory
    cold_chain: bool
    name: dict[str, str]
    # 数量桶，合计恒等于 registered。
    registered: int = 0
    available: int = 0
    issued: int = 0
    quarantined: int = 0
    consumed: int = 0
    written_off: int = 0
    retained: int = 0
    shipped_back: int = 0
    destroyed: int = 0
    # 位置与责任。
    location: str = ""
    holder: str | None = None
    holder_role: str | None = None
    parent_id: str | None = None
    quarantine_reasons: list[str] = field(default_factory=list)

    @property
    def on_hand(self) -> int:
        return self.available + self.issued + self.quarantined


@dataclass
class Device:
    serial: str
    model: str
    name: dict[str, str]
    customs_ref: str
    required_software: str
    software_version: str | None = None
    calibration_due: date | None = None
    calibration_cert: str | None = None
    required_certs: tuple[str, ...] = ()
    state: str = "available"               # available | in_use | quarantined | retained | shipped_back | destroyed | written_off
    location: str = ""
    holder: str | None = None
    accessories: tuple[str, ...] = ()
    quarantine_reasons: list[str] = field(default_factory=list)


@dataclass
class Operator:
    operator_id: str
    name: str
    role: str
    certifications: dict[str, date] = field(default_factory=dict)  # cert_id -> 到期日


@dataclass
class IssueRecord:
    item_id: str
    qty: int
    recipient: str
    role: str
    room: str


class Inventory:
    """纯函数式重放投影：状态只由 :meth:`apply` 改变。"""

    def __init__(self) -> None:
        self.boxes: dict[str, Box] = {}
        self.items: dict[str, StockItem] = {}
        self.devices: dict[str, Device] = {}
        self.operators: dict[str, Operator] = {}
        # 留置前置材料：serial -> {"docs": 语言->文档号, "training": dict|None,
        # "responsibility": dict|None}
        self.handover_packets: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        """清空投影，以便从事件完整重放。"""
        self.boxes.clear()
        self.items.clear()
        self.devices.clear()
        self.operators.clear()
        self.handover_packets.clear()

    # ------------------------------------------------------------------ #
    def apply(self, event: Any) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is not None:
            handler(event.payload, _as_utc_datetime(event.occurred_at), event.actor)

    # ---------------- 入库与封签 -------------------------------------- #
    def _on_manifest_registered(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        box = Box(
            container_id=p["container_id"],
            seal_id=p["seal_id"],
            customs_ref=p["customs_ref"],
            location=p.get("location", "inbound_custody"),
        )
        if box.container_id in self.boxes:
            raise ConservationViolation(f"箱件 {box.container_id} 重复登记报关身份")
        for line in p["contents"]:
            item_id = line["item_id"]
            if item_id in self.items:
                raise ConservationViolation(f"库存行 {item_id} 重复登记")
            item = StockItem(
                item_id=item_id,
                sku=line["sku"],
                lot=line["lot"],
                expiry=_as_date(line["expiry"]) if line.get("expiry") else None,
                unit=line.get("unit", "个"),
                kind=line.get("kind", "consumable"),
                cold_chain=line.get("cold_chain", False),
                name=line.get("name", {}),
                registered=line["qty"],
                available=line["qty"],
                location=box.location,
            )
            self.items[item_id] = item
            box.contents.append(item_id)
        for dev in p.get("devices", []):
            if dev["serial"] in self.devices:
                raise ConservationViolation(f"设备 {dev['serial']} 重复登记")
            device = Device(
                serial=dev["serial"],
                model=dev["model"],
                name=dev.get("name", {}),
                customs_ref=dev.get("customs_ref", p["customs_ref"]),
                required_software=dev["required_software"],
                software_version=dev.get("software_version"),
                calibration_due=_as_date(dev["calibration_due"]) if dev.get("calibration_due") else None,
                calibration_cert=dev.get("calibration_cert"),
                required_certs=tuple(dev.get("required_certs", [])),
                location=box.location,
                accessories=tuple(dev.get("accessories", [])),
            )
            self.devices[device.serial] = device
            box.device_serials.append(device.serial)
        self.boxes[box.container_id] = box

    def _on_seal_verified(self, p: dict[str, Any], ts: datetime, _actor: str) -> None:
        box = self._box(p["container_id"])
        if p.get("seal_id") and p["seal_id"] != box.seal_id:
            raise SealedContainerError(
                f"箱件 {box.container_id} 封签号不符：登记 {box.seal_id}，扫码 {p['seal_id']}"
            )
        box.seal_intact = bool(p["intact"])
        box.seal_verified = True
        if not p["intact"]:
            # 只隔离这一个箱件内的物品，其他诊室与库房不受影响。
            self._quarantine_contents(
                box, f"封签异常({p.get('note', '未核验')}) @ {ts.isoformat()}"
            )

    def _on_box_opened(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        box = self._box(p["container_id"])
        if not box.seal_verified:
            raise SealedContainerError(
                f"箱件 {box.container_id} 封签未经扫码核验，禁止拆箱"
            )
        if box.status == "open":
            raise SealedContainerError(f"箱件 {box.container_id} 已处于拆箱状态")
        if not box.seal_intact:
            raise SealedContainerError(
                f"箱件 {box.container_id} 封签未通过核验，禁止常规拆箱"
            )
        if p.get("seal_id") and p["seal_id"] != box.seal_id:
            raise SealedContainerError(f"箱件 {box.container_id} 拆封锁签号不符")
        box.status = "open"
        box.location = p.get("room", box.location)
        for item_id in box.contents:
            self.items[item_id].location = box.location
        for serial in box.device_serials:
            self.devices[serial].location = box.location

    # ---------------- 数量平移：分装/领用/归还/借用 ------------------- #
    def _on_subpack_created(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        parent = self._item(p["parent_item_id"])
        qty = p["qty"]
        self._require_available(parent, qty, "分装")
        child = StockItem(
            item_id=p["item_id"],
            sku=parent.sku,
            lot=parent.lot,
            expiry=parent.expiry,
            unit=parent.unit,
            kind=parent.kind,
            cold_chain=parent.cold_chain,
            name=parent.name,
            registered=qty,
            available=qty,
            location=p.get("location", parent.location),
            parent_id=parent.item_id,
        )
        if child.item_id in self.items:
            raise ConservationViolation(f"分装 {child.item_id} 重复建账")
        parent.available -= qty
        self.items[child.item_id] = child

    def _on_items_issued(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        room = p["room"]
        for line in p["items"]:
            item = self._item(line["item_id"])
            qty = line["qty"]
            self._require_available(item, qty, "领用")
            item.available -= qty
            item.issued += qty
            item.location = room
            item.holder = p["recipient"]
            item.holder_role = p.get("role")

    def _on_consumable_used(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        item = self._item(p["item_id"])
        qty = p["qty"]
        purpose = p.get("purpose_code")
        encounter = p.get("encounter_ref")
        if not encounter and purpose not in NON_CLINICAL_PURPOSES:
            raise ConservationViolation(
                "耗材消耗必须登记脱敏就诊代号 ENC-XXXX 或认可的非临床用途代码"
            )
        if item.issued < qty:
            raise ConservationViolation(
                f"{item.item_id} 已发出 {item.issued}{item.unit}，"
                f"无法登记消耗 {qty}{item.unit}（未领用不得消耗）"
            )
        item.issued -= qty
        item.consumed += qty

    def _on_items_returned(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        item = self._item(p["item_id"])
        qty = p["qty"]
        if item.issued < qty:
            raise ConservationViolation(
                f"{item.item_id} 发出仅 {item.issued}{item.unit}，不能归还 {qty}{item.unit}"
            )
        item.issued -= qty
        item.available += qty
        item.location = p.get("location", item.location)
        if item.issued == 0:
            item.holder = None
            item.holder_role = None

    def _on_equipment_checked_out(self, p: dict[str, Any], ts: datetime, _actor: str) -> None:
        device = self._device(p["serial"])
        if device.state != "available":
            raise ConservationViolation(
                f"设备 {device.serial} 当前 {device.state}，不能借出"
            )
        device.state = "in_use"
        device.holder = p["recipient"]
        device.location = p["room"]

    def _on_equipment_returned(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        device = self._device(p["serial"])
        if device.state != "in_use":
            raise ConservationViolation(f"设备 {device.serial} 未处于借用状态")
        device.state = "available"
        device.holder = None
        device.location = p.get("location", device.location)

    # ---------------- 隔离与环境 -------------------------------------- #
    def _on_env_reading(self, p: dict[str, Any], ts: datetime, _actor: str) -> None:
        location = p["location"]
        temp = p.get("temperature_c")
        humidity = p.get("humidity_pct")
        limits = p.get("limits", {})
        breaches: list[str] = []
        if temp is not None and "temp_min" in limits and temp < limits["temp_min"]:
            breaches.append(f"温度 {temp}°C < {limits['temp_min']}")
        if temp is not None and "temp_max" in limits and temp > limits["temp_max"]:
            breaches.append(f"温度 {temp}°C > {limits['temp_max']}")
        if humidity is not None and "hum_max" in limits and humidity > limits["hum_max"]:
            breaches.append(f"湿度 {humidity}% > {limits['hum_max']}")
        if humidity is not None and "hum_min" in limits and humidity < limits["hum_min"]:
            breaches.append(f"湿度 {humidity}% < {limits['hum_min']}")
        if not breaches:
            return
        reason = f"温湿度越限（{'; '.join(breaches)}）@ {ts.isoformat()}"
        # 仅隔离当前存放在该地点、且有冷链/环境要求的物品与在场设备。
        for item in self.items.values():
            if item.location == location and item.cold_chain and item.on_hand:
                self._quarantine_item(item, item.on_hand, reason)
        for device in self.devices.values():
            if device.location == location and device.state in ("available", "in_use"):
                self._quarantine_device(device, reason)

    def _on_item_quarantined(self, p: dict[str, Any], ts: datetime, _actor: str) -> None:
        reason = f"{p['reason']} @ {ts.isoformat()}"
        if "item_id" in p:
            item = self._item(p["item_id"])
            qty = p.get("qty", item.on_hand)
            self._quarantine_item(item, qty, reason)
        for serial in p.get("device_serials", []):
            device = self._device(serial)
            self._quarantine_device(device, reason)
            # 精准隔离：设备异常只连坐其登记附件，不碰同诊室其他器材。
            for accessory_id in device.accessories:
                accessory = self.items.get(accessory_id)
                # 只移动尚在可用/发出状态的部分；已全隔离的附件在重复核对
                # 失败时幂等跳过，不制造虚假数量变动。
                if accessory is not None and accessory.available + accessory.issued:
                    self._quarantine_item(
                        accessory, accessory.available + accessory.issued,
                        f"关联设备 {serial} {reason}",
                    )

    def _on_item_released(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        if "item_id" in p:
            item = self._item(p["item_id"])
            qty = p.get("qty", item.quarantined)
            if qty > item.quarantined:
                raise ConservationViolation(
                    f"{item.item_id} 隔离仅 {item.quarantined}{item.unit}"
                )
            item.quarantined -= qty
            item.available += qty
            if qty >= item.quarantined:
                item.quarantine_reasons.clear()
        for serial in p.get("device_serials", []):
            device = self._device(serial)
            if device.state != "quarantined":
                raise ConservationViolation(f"设备 {serial} 不在隔离状态")
            device.state = "available"
            device.quarantine_reasons.clear()

    # ---------------- 终态处置（均需凭证或去向） ---------------------- #
    def _on_item_written_off(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        item = self._item(p["item_id"])
        qty = p["qty"]
        if not p.get("reason") or not p.get("witness"):
            raise ConservationViolation("报损必须记录原因与见证人")
        self._dispose_item(item, qty, "written_off")

    def _on_items_destroyed(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        if not p.get("certificate_ref") or not p.get("witness"):
            raise ConservationViolation("销毁必须记录销毁凭证编号与见证人")
        for line in p["items"]:
            self._dispose_item(self._item(line["item_id"]), line["qty"], "destroyed")
        for serial in p.get("device_serials", []):
            device = self._device(serial)
            device.state = "destroyed"

    def _on_return_shipment(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        if not p.get("waybill") or not p.get("customs_declaration"):
            raise ConservationViolation("返运必须记录运单号与报关单号")
        for line in p.get("items", []):
            self._dispose_item(self._item(line["item_id"]), line["qty"], "shipped_back")
        for serial in p.get("device_serials", []):
            self._device(serial).state = "shipped_back"

    # ---------------- 设备档案与操作者 -------------------------------- #
    def _on_device_registered(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        if p["serial"] in self.devices:
            raise ConservationViolation(f"设备 {p['serial']} 重复登记")
        self.devices[p["serial"]] = Device(
            serial=p["serial"],
            model=p["model"],
            name=p.get("name", {}),
            customs_ref=p.get("customs_ref", ""),
            required_software=p["required_software"],
            software_version=p.get("software_version"),
            calibration_due=_as_date(p["calibration_due"]) if p.get("calibration_due") else None,
            calibration_cert=p.get("calibration_cert"),
            required_certs=tuple(p.get("required_certs", [])),
            location=p.get("location", "inbound_custody"),
            accessories=tuple(p.get("accessories", [])),
        )

    def _on_calibration_recorded(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        device = self._device(p["serial"])
        device.calibration_due = _as_date(p["calibration_due"])
        device.calibration_cert = p.get("certificate_ref")

    def _on_software_verified(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        self._device(p["serial"]).software_version = p["version"]

    def _on_operator_registered(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        self.operators[p["operator_id"]] = Operator(
            operator_id=p["operator_id"],
            name=p["name"],
            role=p.get("role", ""),
            certifications={
                c["cert_id"]: _as_date(c["expires"]) for c in p.get("certifications", [])
            },
        )

    def _on_device_session_started(self, p: dict[str, Any], ts: datetime, _actor: str) -> None:
        device = self._device(p["serial"])
        device.state = "in_use"
        device.holder = p["operator_id"]
        device.location = p.get("room", device.location)
        if p.get("software_seen"):
            device.software_version = p["software_seen"]

    def _on_device_session_ended(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        device = self._device(p["serial"])
        if device.state == "in_use":
            device.state = "available"
            device.holder = None

    # ---------------- 留置交接 ---------------------------------------- #
    def _on_handover_docs_recorded(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        packet = self._handover_packet(p["serial"])
        for language, ref in p["documents"].items():
            if not ref:
                raise ConservationViolation("交接文档引用不能为空")
            packet["docs"][language] = ref

    def _on_training_completed(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        if not p.get("trainees"):
            raise ConservationViolation("培训验收必须记录受训人")
        packet = self._handover_packet(p["serial"])
        packet["training"] = {
            "trainer": p["trainer"],
            "trainees": p["trainees"],
            "session_ref": p.get("session_ref"),
        }

    def _on_responsibility_accepted(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        if not p.get("owner_name") or not p.get("signoff_ref"):
            raise ConservationViolation("责任确认必须记录接管责任人与签署凭证")
        packet = self._handover_packet(p["serial"])
        packet["responsibility"] = {
            "owner_name": p["owner_name"],
            "owner_org": p.get("owner_org", ""),
            "signoff_ref": p["signoff_ref"],
        }

    def _on_asset_retained(self, p: dict[str, Any], ts: datetime, _actor: str) -> None:
        device = self._device(p["serial"])
        packet = self._handover_packet(p["serial"])
        missing: list[str] = []
        if not {"zh", "en"}.issubset(packet["docs"]):
            missing.append("中英双语交接文档")
        if not packet["training"]:
            missing.append("培训验收记录")
        if not packet["responsibility"]:
            missing.append("接管责任确认")
        if missing:
            # 不改变设备状态——材料补齐前资产仍归行动方负责。
            from .errors import HandoffIncomplete

            raise HandoffIncomplete(missing)
        device.state = "retained"
        device.holder = packet["responsibility"]["owner_name"]
        device.location = p.get("location", device.location)
        packet["retained_at"] = ts.isoformat()

    # ---------------- 运输通知（位置平移，绝不新增库存） -------------- #
    def _on_transport_notice(self, p: dict[str, Any], _ts: datetime, _actor: str) -> None:
        location = p.get("location")
        for container_id in p.get("container_ids", []):
            box = self._box(container_id)
            if location:
                box.location = location
                for item_id in box.contents:
                    if self.items[item_id].on_hand and self.items[item_id].holder is None:
                        self.items[item_id].location = location
                for serial in box.device_serials:
                    device = self.devices[serial]
                    if device.state in ("available", "quarantined"):
                        device.location = location

    # ------------------------------------------------------------------ #
    # 查询接口
    # ------------------------------------------------------------------ #
    def where_is(self, ref: str) -> dict[str, Any]:
        """回答"某件物品在哪里、由谁持有"，支持箱件、设备与库存行。"""
        if ref in self.boxes:
            b = self.boxes[ref]
            return {"ref": ref, "kind": "container", "state": b.status,
                    "location": b.location, "holder": None}
        if ref in self.devices:
            d = self.devices[ref]
            return {"ref": ref, "kind": "device", "state": d.state,
                    "location": d.location, "holder": d.holder}
        item = self.items.get(ref)
        if item is None:
            raise UnknownReference(f"找不到 {ref}")
        return {"ref": ref, "kind": item.kind, "state": self._item_primary_state(item),
                "location": item.location, "holder": item.holder,
                "available": item.available, "quarantined": item.quarantined,
                "issued": item.issued}

    def usable(self, ref: str, as_of: date | str | None = None) -> dict[str, Any]:
        """回答"是否还能使用"，不可用时给出原因。"""
        today = _as_date(as_of) if as_of else date.today()
        if ref in self.devices:
            return self._device_usable(self.devices[ref], today)
        item = self.items.get(ref)
        if item is None:
            raise UnknownReference(f"找不到 {ref}")
        reasons: list[str] = []
        if item.quarantined:
            reasons.append("隔离中：" + "; ".join(item.quarantine_reasons))
        if item.available <= 0:
            reasons.append("无可用库存")
        if item.expiry and item.expiry < today:
            reasons.append(f"已过期（效期 {item.expiry.isoformat()}）")
        return {"ref": ref, "usable": not reasons, "reasons": reasons}

    def custodian(self, ref: str) -> dict[str, Any]:
        """回答"由谁接管"。"""
        if ref in self.devices:
            d = self.devices[ref]
            packet = self.handover_packets.get(ref)
            return {
                "ref": ref,
                "state": d.state,
                "holder": d.holder,
                "responsibility": packet["responsibility"] if packet else None,
            }
        item = self.items.get(ref)
        if item is None:
            raise UnknownReference(f"找不到 {ref}")
        return {"ref": ref, "holder": item.holder, "holder_role": item.holder_role}

    def reconcile(self) -> dict[str, Any]:
        """行动结束复算结存；只输出聚合数字，不含任何就诊/患者信息。

        每行守恒恒等式（分装把数量平移到子行，不计为处置）::

            registered = available + issued + quarantined
                       + consumed + written_off + retained
                       + shipped_back + destroyed + subdivided

        其中 ``subdivided`` 是经分装转入直接子行的数量合计。全行动在册总量
        取所有行 registered 之和减去 subdivided 之和（子行登记不重复计为新增）。
        """
        children_of: dict[str, list[str]] = {}
        for item in self.items.values():
            if item.parent_id is not None:
                children_of.setdefault(item.parent_id, []).append(item.item_id)

        lines = []
        balanced = True
        for item in sorted(self.items.values(), key=lambda x: x.item_id):
            dispositioned = (
                item.consumed + item.written_off + item.retained
                + item.shipped_back + item.destroyed
            )
            subdivided = sum(
                self.items[c].registered for c in children_of.get(item.item_id, [])
            )
            accounted = item.on_hand + dispositioned + subdivided
            ok = accounted == item.registered
            balanced = balanced and ok
            lines.append({
                "item_id": item.item_id,
                "parent_item_id": item.parent_id,
                "sku": item.sku,
                "lot": item.lot,
                "registered": item.registered,
                "available": item.available,
                "issued": item.issued,
                "quarantined": item.quarantined,
                "consumed": item.consumed,
                "written_off": item.written_off,
                "retained": item.retained,
                "shipped_back": item.shipped_back,
                "destroyed": item.destroyed,
                "subdivided": subdivided,
                "balanced": ok,
            })
        registered_total = sum(l["registered"] for l in lines) - sum(
            l["subdivided"] for l in lines
        )
        devices = [
            {"serial": d.serial, "model": d.model, "state": d.state,
             "holder": d.holder, "location": d.location,
             "calibration_due": d.calibration_due.isoformat() if d.calibration_due else None}
            for d in sorted(self.devices.values(), key=lambda x: x.serial)
        ]
        return {"balanced": balanced, "registered_total": registered_total,
                "items": lines, "devices": devices}

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #
    def _box(self, container_id: str) -> Box:
        try:
            return self.boxes[container_id]
        except KeyError:
            raise UnknownReference(f"箱件 {container_id} 未登记") from None

    def _item(self, item_id: str) -> StockItem:
        try:
            return self.items[item_id]
        except KeyError:
            raise UnknownReference(f"库存行 {item_id} 未登记") from None

    def _device(self, serial: str) -> Device:
        try:
            return self.devices[serial]
        except KeyError:
            raise UnknownReference(f"设备 {serial} 未登记") from None

    def _handover_packet(self, serial: str) -> dict[str, Any]:
        self._device(serial)
        return self.handover_packets.setdefault(
            serial, {"docs": {}, "training": None, "responsibility": None}
        )

    @staticmethod
    def _item_primary_state(item: StockItem) -> str:
        if item.quarantined:
            return "quarantined"
        if item.issued:
            return "issued"
        if item.available:
            return "available"
        return "dispositioned"

    @staticmethod
    def _require_available(item: StockItem, qty: int, action: str) -> None:
        if qty <= 0:
            raise ConservationViolation(f"{action}数量必须为正数")
        if item.available < qty:
            raise ConservationViolation(
                f"{item.item_id} 可用 {item.available}{item.unit}，"
                f"不足以{action} {qty}{item.unit}"
            )

    def _quarantine_contents(self, box: Box, reason: str) -> None:
        for item_id in box.contents:
            item = self.items[item_id]
            if item.on_hand:
                self._quarantine_item(item, item.on_hand, reason)
        for serial in box.device_serials:
            self._quarantine_device(self.devices[serial], reason)
        box.seal_intact = False

    @staticmethod
    def _quarantine_item(item: StockItem, qty: int, reason: str) -> None:
        movable = item.available + item.issued
        if qty > movable:
            raise ConservationViolation(
                f"{item.item_id} 在库/发出仅 {movable}{item.unit}，不能隔离 {qty}{item.unit}"
            )
        from_avail = min(item.available, qty)
        from_issued = qty - from_avail
        item.available -= from_avail
        item.issued -= from_issued
        item.quarantined += qty
        item.quarantine_reasons.append(reason)

    def _dispose_item(self, item: StockItem, qty: int, bucket: str) -> None:
        if qty <= 0:
            raise ConservationViolation("处置数量必须为正数")
        # 销毁/报损通常发生在隔离鉴定之后，优先出隔离量；返运优先出可用量。
        if bucket in ("destroyed", "written_off"):
            order = ("quarantined", "available", "issued")
        else:
            order = ("available", "issued", "quarantined")
        remaining = qty
        for source in order:
            take = min(remaining, getattr(item, source))
            if take:
                setattr(item, source, getattr(item, source) - take)
                remaining -= take
            if remaining == 0:
                break
        if remaining:
            raise ConservationViolation(
                f"{item.item_id} 在途仅 {item.on_hand}{item.unit}，"
                f"无法处置 {qty}{item.unit}"
            )
        setattr(item, bucket, getattr(item, bucket) + qty)
        if bucket in ("shipped_back", "destroyed") and item.on_hand == 0:
            item.holder = None
            item.holder_role = None

    @staticmethod
    def _quarantine_device(device: Device, reason: str) -> None:
        if device.state in ("retained", "shipped_back", "destroyed", "written_off"):
            return
        device.state = "quarantined"
        device.quarantine_reasons.append(reason)

    def _device_usable(self, device: Device, today: date) -> dict[str, Any]:
        reasons: list[str] = []
        if device.state == "quarantined":
            reasons.append("隔离中：" + "; ".join(device.quarantine_reasons))
        elif device.state != "available":
            reasons.append(f"当前状态 {device.state}")
        if device.calibration_due is None:
            reasons.append("无校准记录")
        elif device.calibration_due < today:
            reasons.append(f"校准已过期（应于 {device.calibration_due.isoformat()} 前完成）")
        if device.software_version is None:
            reasons.append("软件版本未核验")
        elif device.software_version != device.required_software:
            reasons.append(
                f"软件版本 {device.software_version} 与要求 {device.required_software} 不符"
            )
        return {"ref": device.serial, "usable": not reasons, "reasons": reasons,
                "calibration_due": device.calibration_due.isoformat()
                if device.calibration_due else None,
                "software_version": device.software_version}
