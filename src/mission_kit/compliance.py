"""设备启用核对：校准、软件版本、操作者资质三项全过才允许开机。

核对失败时不写"已启用"事件（诊疗未发生，不能污染台账），只写一条隔离
事件，影响范围严格限定为该设备及其登记附件；同诊室其他设备和耗材继续
正常轮转。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .errors import ComplianceHold, UnknownReference
from .inventory import Inventory
from .ledger import EventStore, validate_encounter_ref


def check_device_ready(
    inventory: Inventory,
    serial: str,
    operator_id: str,
    *,
    as_of: date | str | None = None,
) -> list[str]:
    """纯核对：返回失败原因列表；为空表示可以启用，不修改任何状态。"""
    today = date.fromisoformat(as_of) if isinstance(as_of, str) else (as_of or date.today())
    if serial not in inventory.devices:
        raise UnknownReference(f"设备 {serial} 未登记")
    device = inventory.devices[serial]
    operator = inventory.operators.get(operator_id)

    failures: list[str] = []
    if device.state == "quarantined":
        failures.append("设备处于隔离状态：" + "; ".join(device.quarantine_reasons))
    elif device.state != "available":
        failures.append(f"设备当前状态为 {device.state}，不可启用")

    if device.calibration_due is None:
        failures.append("缺少校准记录")
    elif device.calibration_due < today:
        failures.append(f"校准已过期（{device.calibration_due.isoformat()} 到期）")

    if device.software_version is None:
        failures.append("软件版本尚未核验")
    elif device.software_version != device.required_software:
        failures.append(
            f"软件版本不符（装机 {device.software_version}，要求 {device.required_software}）"
        )

    if operator is None:
        failures.append(f"操作者 {operator_id} 未登记资质")
    else:
        for cert_id in device.required_certs:
            expiry = operator.certifications.get(cert_id)
            if expiry is None:
                failures.append(f"操作者 {operator.name} 缺少资质 {cert_id}")
            elif expiry < today:
                failures.append(f"操作者 {operator.name} 的资质 {cert_id} 已于 {expiry.isoformat()} 到期")
    return failures


class MissionService:
    """应用门面：把"扫码动作"翻译成台账事件，封装合规与隐私规则。"""

    def __init__(self, store: EventStore, inventory: Inventory | None = None):
        self.store = store
        self.inventory = inventory if inventory is not None else store.replay_into(Inventory())

    # 重新投影（例如从磁盘重建后）。
    @classmethod
    def rebuild(cls, store: EventStore) -> "MissionService":
        return cls(store, store.replay_into(Inventory()))

    # ---------------------------------------------------------------- #
    # 入库
    # ---------------------------------------------------------------- #
    def register_manifest(
        self,
        *,
        container_id: str,
        seal_id: str,
        customs_ref: str,
        contents: list[dict[str, Any]],
        devices: list[dict[str, Any]] | None = None,
        location: str = "inbound_custody",
        actor: str = "logistics",
        occurred_at: str | datetime | None = None,
        client_event_id: str | None = None,
    ):
        return self._emit(
            "manifest_registered",
            {
                "container_id": container_id,
                "seal_id": seal_id,
                "customs_ref": customs_ref,
                "location": location,
                "contents": contents,
                "devices": devices or [],
            },
            actor=actor,
            occurred_at=occurred_at,
            client_event_id=client_event_id,
        )

    def verify_seal(self, *, container_id: str, seal_id: str, intact: bool,
                    actor: str, occurred_at: str | datetime, note: str = ""):
        return self._emit(
            "seal_verified",
            {"container_id": container_id, "seal_id": seal_id, "intact": intact, "note": note},
            actor=actor, occurred_at=occurred_at,
        )

    def open_box(self, *, container_id: str, seal_id: str, room: str,
                 actor: str, occurred_at: str | datetime):
        return self._emit(
            "box_opened",
            {"container_id": container_id, "seal_id": seal_id, "room": room},
            actor=actor, occurred_at=occurred_at,
        )

    # ---------------------------------------------------------------- #
    # 轮转
    # ---------------------------------------------------------------- #
    def subpack(self, *, item_id: str, parent_item_id: str, qty: int,
                location: str, actor: str, occurred_at: str | datetime,
                client_event_id: str | None = None):
        return self._emit(
            "subpack_created",
            {"item_id": item_id, "parent_item_id": parent_item_id, "qty": qty,
             "location": location},
            actor=actor, occurred_at=occurred_at, client_event_id=client_event_id,
        )

    def issue(self, *, items: list[dict[str, Any]], recipient: str, role: str | None,
              room: str, actor: str, occurred_at: str | datetime,
              client_event_id: str | None = None):
        return self._emit(
            "items_issued",
            {"items": items, "recipient": recipient, "role": role, "room": room},
            actor=actor, occurred_at=occurred_at, client_event_id=client_event_id,
        )

    def record_use(self, *, item_id: str, qty: int, actor: str,
                   occurred_at: str | datetime, encounter_ref: str | None = None,
                   purpose_code: str | None = None, client_event_id: str | None = None):
        validate_encounter_ref(encounter_ref)
        return self._emit(
            "consumable_used",
            {"item_id": item_id, "qty": qty, "encounter_ref": encounter_ref,
             "purpose_code": purpose_code},
            actor=actor, occurred_at=occurred_at, client_event_id=client_event_id,
        )

    def return_items(self, *, item_id: str, qty: int, location: str,
                     actor: str, occurred_at: str | datetime):
        return self._emit(
            "items_returned",
            {"item_id": item_id, "qty": qty, "location": location},
            actor=actor, occurred_at=occurred_at,
        )

    def checkout_equipment(self, *, serial: str, recipient: str, room: str,
                           actor: str, occurred_at: str | datetime):
        return self._emit(
            "equipment_checked_out",
            {"serial": serial, "recipient": recipient, "room": room},
            actor=actor, occurred_at=occurred_at,
        )

    def return_equipment(self, *, serial: str, location: str,
                         actor: str, occurred_at: str | datetime):
        return self._emit(
            "equipment_returned",
            {"serial": serial, "location": location},
            actor=actor, occurred_at=occurred_at,
        )

    # ---------------------------------------------------------------- #
    # 环境与隔离
    # ---------------------------------------------------------------- #
    def env_reading(self, *, location: str, temperature_c: float | None = None,
                    humidity_pct: float | None = None, limits: dict[str, float] | None = None,
                    actor: str, occurred_at: str | datetime,
                    client_event_id: str | None = None):
        return self._emit(
            "env_reading",
            {"location": location, "temperature_c": temperature_c,
             "humidity_pct": humidity_pct, "limits": limits or {}},
            actor=actor, occurred_at=occurred_at, client_event_id=client_event_id,
        )

    def quarantine(self, *, reason: str, actor: str, occurred_at: str | datetime,
                   item_id: str | None = None, qty: int | None = None,
                   device_serials: list[str] | None = None,
                   client_event_id: str | None = None):
        payload: dict[str, Any] = {"reason": reason}
        if item_id:
            payload["item_id"] = item_id
        if qty is not None:
            payload["qty"] = qty
        if device_serials:
            payload["device_serials"] = device_serials
        return self._emit("item_quarantined", payload, actor=actor,
                          occurred_at=occurred_at, client_event_id=client_event_id)

    def release_hold(self, *, actor: str, occurred_at: str | datetime,
                     item_id: str | None = None, qty: int | None = None,
                     device_serials: list[str] | None = None):
        payload: dict[str, Any] = {}
        if item_id:
            payload["item_id"] = item_id
        if qty is not None:
            payload["qty"] = qty
        if device_serials:
            payload["device_serials"] = device_serials
        return self._emit("item_released", payload, actor=actor, occurred_at=occurred_at)

    # ---------------------------------------------------------------- #
    # 设备合规
    # ---------------------------------------------------------------- #
    def register_operator(self, *, operator_id: str, name: str, role: str,
                          certifications: list[dict[str, Any]],
                          actor: str, occurred_at: str | datetime):
        return self._emit(
            "operator_registered",
            {"operator_id": operator_id, "name": name, "role": role,
             "certifications": certifications},
            actor=actor, occurred_at=occurred_at,
        )

    def record_calibration(self, *, serial: str, calibration_due: str,
                           certificate_ref: str, actor: str,
                           occurred_at: str | datetime):
        return self._emit(
            "calibration_recorded",
            {"serial": serial, "calibration_due": calibration_due,
             "certificate_ref": certificate_ref},
            actor=actor, occurred_at=occurred_at,
        )

    def verify_software(self, *, serial: str, version: str,
                        actor: str, occurred_at: str | datetime):
        return self._emit(
            "software_verified",
            {"serial": serial, "version": version},
            actor=actor, occurred_at=occurred_at,
        )

    def enable_device(self, *, serial: str, operator_id: str, room: str,
                      actor: str, occurred_at: str | datetime) -> tuple[Any, Any | None]:
        """尝试启用设备。

        通过则写入 ``device_session_started``；失败则只隔离该设备及其登记
        附件（一条 ``item_quarantined``），并抛出 :class:`ComplianceHold`。
        其他诊疗活动不受影响。
        """
        failures = check_device_ready(self.inventory, serial, operator_id)
        if failures:
            self.quarantine(
                reason="启用核对未通过：" + "; ".join(failures),
                actor=actor,
                occurred_at=occurred_at,
                device_serials=[serial],
            )
            raise ComplianceHold(failures)
        event = self._emit(
            "device_session_started",
            {"serial": serial, "operator_id": operator_id, "room": room},
            actor=actor, occurred_at=occurred_at,
        )
        return event, None
    def end_session(self, *, serial: str, actor: str, occurred_at: str | datetime):
        return self._emit(
            "device_session_ended", {"serial": serial},
            actor=actor, occurred_at=occurred_at,
        )

    # ---------------------------------------------------------------- #
    # 留置 / 返运 / 销毁 / 报损
    # ---------------------------------------------------------------- #
    def record_handover_docs(self, *, serial: str, documents: dict[str, str],
                             actor: str, occurred_at: str | datetime):
        return self._emit(
            "handover_docs_recorded",
            {"serial": serial, "documents": documents},
            actor=actor, occurred_at=occurred_at,
        )

    def complete_training(self, *, serial: str, trainer: str, trainees: list[str],
                          session_ref: str | None, actor: str,
                          occurred_at: str | datetime):
        return self._emit(
            "training_completed",
            {"serial": serial, "trainer": trainer, "trainees": trainees,
             "session_ref": session_ref},
            actor=actor, occurred_at=occurred_at,
        )

    def accept_responsibility(self, *, serial: str, owner_name: str, owner_org: str,
                              signoff_ref: str, actor: str, occurred_at: str | datetime):
        return self._emit(
            "responsibility_accepted",
            {"serial": serial, "owner_name": owner_name, "owner_org": owner_org,
             "signoff_ref": signoff_ref},
            actor=actor, occurred_at=occurred_at,
        )

    def retain_asset(self, *, serial: str, location: str, actor: str,
                     occurred_at: str | datetime):
        return self._emit(
            "asset_retained", {"serial": serial, "location": location},
            actor=actor, occurred_at=occurred_at,
        )

    def write_off(self, *, item_id: str, qty: int, reason: str, witness: str,
                  actor: str, occurred_at: str | datetime):
        return self._emit(
            "item_written_off",
            {"item_id": item_id, "qty": qty, "reason": reason, "witness": witness},
            actor=actor, occurred_at=occurred_at,
        )

    def destroy(self, *, items: list[dict[str, Any]], device_serials: list[str] | None,
                certificate_ref: str, witness: str, actor: str,
                occurred_at: str | datetime):
        return self._emit(
            "items_destroyed",
            {"items": items, "device_serials": device_serials or [],
             "certificate_ref": certificate_ref, "witness": witness},
            actor=actor, occurred_at=occurred_at,
        )

    def ship_back(self, *, waybill: str, customs_declaration: str,
                  items: list[dict[str, Any]], device_serials: list[str] | None,
                  actor: str, occurred_at: str | datetime,
                  client_event_id: str | None = None):
        return self._emit(
            "return_shipment",
            {"waybill": waybill, "customs_declaration": customs_declaration,
             "items": items, "device_serials": device_serials or []},
            actor=actor, occurred_at=occurred_at, client_event_id=client_event_id,
        )

    # ---------------------------------------------------------------- #
    # 航段通知
    # ---------------------------------------------------------------- #
    def transport_notice(self, *, kind: str, fingerprint_parts: list[Any],
                         payload: dict[str, Any], actor: str,
                         occurred_at: str | datetime):
        from .ledger import notice_fingerprint
        event = self.store.stage(
            "transport_notice",
            {**payload, "notice_kind": kind},
            actor=actor,
            occurred_at=occurred_at,
            notice_fp=notice_fingerprint(kind, fingerprint_parts),
        )
        self._validate_on_scratch(event)
        self.inventory.apply(event)
        return self.store.commit(event)

    # ---------------------------------------------------------------- #
    def _validate_on_scratch(self, event) -> None:
        """在草稿投影上完整重放历史并试算事件。

        正式投影只有在草稿成功后才接收事件，因此业务校验失败时既不落盘，
        也不会留下半变更状态，外部持有的物品/设备引用始终有效。
        """
        scratch = Inventory()
        for committed in self.store.events:
            scratch.apply(committed)
        scratch.apply(event)

    def _emit(self, event_type: str, payload: dict[str, Any], *, actor: str,
              occurred_at: str | datetime | None, client_event_id: str | None = None):
        from .ledger import utc_now
        if occurred_at is None:
            occurred_at = utc_now()
        # 两阶段：事件先暂存（完成隐私/幂等检查），草稿投影校验通过后才
        # 推进正式投影并落盘，保证被业务规则拒绝的事实不产生任何副作用。
        event = self.store.stage(
            event_type, payload, actor=actor, occurred_at=occurred_at,
            client_event_id=client_event_id,
        )
        self._validate_on_scratch(event)
        self.inventory.apply(event)
        return self.store.commit(event)
