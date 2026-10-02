"""事件溯源台账：数量守恒、幂等去重、离线补传与按物品隔离。

设计约定：

- 一切库存变化都是事件，状态由事件重放得出，可随时复算结存；
- ``event_id`` 全局唯一，重复登记（离线扫码重传、航段重复通知）
  只生效一次，不会制造额外库存；
- 事件按 ``occurred_at`` 的 UTC 瞬间排序，晚到的补传事件会落到
  正确位置，此前因缺少前因而被暂缓（rejected）的事件会自动重试；
- 被暂缓的事件保留在日志中供审计，但不改变状态；
- 隔离（quarantine）只作用于指定物品，不影响其他批次、设备与诊室。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from . import events as ev
from .devices import INTEGRITY_REASONS, activation_blockers
from .handover import REQUIRED_PARTIES, make_certificate, missing_languages
from .manifest import MissionManifest
from .model import (
    BATCH_TERMINAL_STATES,
    DEVICE_TERMINAL_STATES,
    Batch,
    Certificate,
)


class Reject(Exception):
    """事件未通过校验：保留在日志中，不改变状态，重算时自动重试。"""


@dataclass(frozen=True)
class HoldingKey:
    """一笔在账库存的坐标：批次 + 状态 + 位置 + 领用人。"""

    batch_id: str
    state: str
    location: str
    custodian: str | None = None


@dataclass
class CaseStatus:
    location: str = "in_transit"
    at_destination: bool = False
    seal_intact: bool = True
    last_leg: str | None = None


@dataclass
class DeviceStatus:
    state: str
    location: str
    custodian: str | None = None
    prev_state: str | None = None
    prev_location: str | None = None
    quarantine_reason: str | None = None


@dataclass(frozen=True)
class RecordResult:
    event_id: str
    applied: bool  # False 表示重复事件被幂等去重
    rejected: bool
    reason: str | None = None


@dataclass
class State:
    cases: dict[str, CaseStatus] = field(default_factory=dict)
    devices: dict[str, DeviceStatus] = field(default_factory=dict)
    batches: dict[str, Batch] = field(default_factory=dict)
    holdings: dict[HoldingKey, int] = field(default_factory=dict)
    terminal: dict[str, dict[str, int]] = field(default_factory=dict)
    quarantine_origin: dict[tuple[str, str, str | None], str] = field(default_factory=dict)
    env_log: list[dict] = field(default_factory=list)
    consumptions: list[dict] = field(default_factory=list)
    activations: dict[str, dict] = field(default_factory=dict)
    handovers: dict[str, dict] = field(default_factory=dict)
    certificates: dict[str, Certificate] = field(default_factory=dict)
    audits: list[dict] = field(default_factory=list)


def _new_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


class Ledger:
    """一台行动一份台账；可选挂接 JSONL 存储实现持久化。"""

    def __init__(self, manifest: MissionManifest, store=None):
        self.manifest = manifest
        self.store = store
        self._events: list[ev.Event] = []
        self._by_id: dict[str, ev.Event] = {}
        self.rejected: dict[str, str] = {}
        self.state = State()
        if store is not None:
            for event in store.read_all():
                if event.event_id not in self._by_id:
                    self._by_id[event.event_id] = event
                    self._events.append(event)
        self.rebuild()

    # ------------------------------------------------------------------
    # 事件登记
    # ------------------------------------------------------------------
    def record(self, event: ev.Event) -> RecordResult:
        ev.parse_instant(event.occurred_at)  # 朴素时间在入帐前拒绝
        existing = self._by_id.get(event.event_id)
        if existing is not None:
            reason = None
            if ev.event_to_dict(existing) != ev.event_to_dict(event):
                reason = "conflicting_duplicate"  # 同号不同内容：先到的生效
            return RecordResult(event.event_id, False, event.event_id in self.rejected,
                                reason or self.rejected.get(event.event_id))
        self._by_id[event.event_id] = event
        self._events.append(event)
        if self.store is not None:
            self.store.append(event)
        self.rebuild()
        return RecordResult(event.event_id, True, event.event_id in self.rejected,
                            self.rejected.get(event.event_id))

    def record_many(self, events) -> list[RecordResult]:
        """离线补传批量登记：全部入列后一次重放。"""

        fresh = []
        results = []
        for event in events:
            ev.parse_instant(event.occurred_at)
            if event.event_id in self._by_id:
                results.append(RecordResult(event.event_id, False,
                                            event.event_id in self.rejected,
                                            self.rejected.get(event.event_id)))
                continue
            self._by_id[event.event_id] = event
            self._events.append(event)
            fresh.append(event)
            results.append(RecordResult(event.event_id, True, False, None))
        if fresh and self.store is not None:
            for event in fresh:
                self.store.append(event)
        if fresh:
            self.rebuild()
        return [
            RecordResult(r.event_id, r.applied, r.event_id in self.rejected,
                         self.rejected.get(r.event_id))
            for r in results
        ]

    def rebuild(self) -> None:
        """按发生时间重放全部事件；被暂缓的事件每次都会重试。"""

        self.state = self._initial_state()
        self.rejected = {}
        ordered = sorted(
            enumerate(self._events),
            key=lambda pair: (ev.parse_instant(pair[1].occurred_at), pair[0]),
        )
        for _, event in ordered:
            try:
                self._apply(event)
            except Reject as exc:
                self.rejected[event.event_id] = str(exc)

    @property
    def event_count(self) -> int:
        return len(self._events)

    # ------------------------------------------------------------------
    # 初始状态与守恒
    # ------------------------------------------------------------------
    def _initial_state(self) -> State:
        m = self.manifest
        state = State()
        state.batches = dict(m.batches)
        state.terminal = {b: {t: 0 for t in BATCH_TERMINAL_STATES} for b in m.batches}
        for case_id, case in m.cases.items():
            state.cases[case_id] = CaseStatus()
            for batch_id in case.batch_refs:
                state.holdings[HoldingKey(batch_id, "sealed", f"case:{case_id}")] = (
                    m.batches[batch_id].quantity
                )
            for device_id in case.device_refs:
                state.devices[device_id] = DeviceStatus("in_case", f"case:{case_id}")
        for device_id in m.devices:
            state.devices.setdefault(device_id, DeviceStatus("warehouse", "warehouse"))
        return state

    def conservation(self) -> list[dict]:
        """按分装谱系复算数量守恒；variance 必须恒为 0。"""

        rows = []
        for root_id, root in self.state.batches.items():
            if root.lineage_root != root_id:
                continue
            members = {b for b in self.state.batches.values() if b.lineage_root == root_id}
            member_ids = {b.batch_id for b in members}
            live = sum(q for k, q in self.state.holdings.items() if k.batch_id in member_ids)
            terminal = sum(
                self.state.terminal[b][t] for b in member_ids for t in BATCH_TERMINAL_STATES
            )
            rows.append({
                "lineage_root": root_id,
                "initial": root.quantity,
                "live": live,
                "terminal": terminal,
                "variance": live + terminal - root.quantity,
            })
        return rows

    # ------------------------------------------------------------------
    # 事件应用
    # ------------------------------------------------------------------
    def _apply(self, event: ev.Event) -> None:
        handler = getattr(self, f"_on_{event.kind}", None)
        if handler is None:
            raise Reject(f"unknown_kind:{event.kind}")
        handler(event)

    def _batch(self, batch_id: str) -> Batch:
        batch = self.state.batches.get(batch_id)
        if batch is None:
            raise Reject("unknown_batch")
        return batch

    def _case(self, case_id: str) -> CaseStatus:
        case = self.state.cases.get(case_id)
        if case is None:
            raise Reject("unknown_case")
        return case

    def _device(self, device_id: str) -> DeviceStatus:
        device = self.state.devices.get(device_id)
        if device is None:
            raise Reject("unknown_device")
        return device

    @staticmethod
    def _qty(payload: dict) -> int:
        qty = payload.get("qty")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise Reject("invalid_qty")
        return qty

    def _add(self, key: HoldingKey, qty: int) -> None:
        if qty:
            self.state.holdings[key] = self.state.holdings.get(key, 0) + qty

    def _dec(self, key: HoldingKey, qty: int) -> None:
        have = self.state.holdings.get(key, 0)
        if have < qty:
            raise Reject("insufficient_quantity")
        if have == qty:
            del self.state.holdings[key]
        else:
            self.state.holdings[key] = have - qty

    def _audit(self, kind: str, item: str, detail: str, at: str, event_id: str) -> None:
        self.state.audits.append(
            {"at": at, "kind": kind, "item": item, "detail": detail, "event_id": event_id}
        )

    # -- 运输与拆箱 ------------------------------------------------------
    def _on_leg_arrival(self, event: ev.Event) -> None:
        p = event.payload
        case = self._case(p["case_id"])
        leg = self.manifest.legs.get(p["leg_id"])
        if leg is None:
            raise Reject("unknown_leg")
        case.location = leg.arrived_port
        case.last_leg = leg.leg_id
        if leg.arrived_port == self.manifest.destination_port:
            case.at_destination = True
        if not p.get("seal_intact", True):
            case.seal_intact = False
            self._quarantine_case_contents(p["case_id"], "seal_broken", event)

    def _on_unpack(self, event: ev.Event) -> None:
        case_id = event.payload["case_id"]
        case = self._case(case_id)
        if not case.at_destination:
            raise Reject("case_not_arrived")
        moved = False
        for key in list(self.state.holdings):
            if key.location == f"case:{case_id}" and key.state in ("sealed", "quarantined"):
                qty = self.state.holdings.pop(key)
                dst_state = "stock" if key.state == "sealed" else "quarantined"
                self._add(HoldingKey(key.batch_id, dst_state, "warehouse"), qty)
                if dst_state == "quarantined":
                    origin = self.state.quarantine_origin.pop(
                        (key.batch_id, key.location, key.custodian), "sealed"
                    )
                    self.state.quarantine_origin[(key.batch_id, "warehouse", None)] = (
                        "stock" if origin == "sealed" else origin
                    )
                moved = True
        for device_id, status in self.state.devices.items():
            if status.location == f"case:{case_id}" and status.state in ("in_case", "quarantined"):
                if status.state == "in_case":
                    status.state = "warehouse"
                elif status.prev_state == "in_case":
                    status.prev_state = "warehouse"
                status.location = "warehouse"
                if status.prev_location == f"case:{case_id}":
                    status.prev_location = "warehouse"
                moved = True
        if not moved:
            raise Reject("nothing_to_unpack")
        self._audit("unpack", case_id, "to_warehouse", event.occurred_at, event.event_id)

    # -- 耗材流转 --------------------------------------------------------
    def _on_split(self, event: ev.Event) -> None:
        p = event.payload
        parent = self._batch(p["parent_batch"])
        child_id = p["child_batch"]
        if child_id in self.state.batches:
            raise Reject("duplicate_batch")
        qty = self._qty(p)
        src = HoldingKey(parent.batch_id, "stock", p.get("location", "warehouse"),
                         p.get("custodian"))
        if self.state.holdings.get(src, 0) < qty:
            raise Reject("insufficient_quantity")
        child = Batch(
            batch_id=child_id,
            name=parent.name,
            lot=parent.lot,
            expiry=parent.expiry,
            quantity=qty,
            unit=parent.unit,
            customs_ref=parent.customs_ref,
            storage=parent.storage,
            lineage_root=parent.lineage_root,
            parent_batch=parent.batch_id,
        )
        self._dec(src, qty)
        self.state.batches[child_id] = child
        self.state.terminal[child_id] = {t: 0 for t in BATCH_TERMINAL_STATES}
        self._add(HoldingKey(child_id, "stock", src.location, src.custodian), qty)
        self._audit("split", child_id, f"from:{parent.batch_id}", event.occurred_at,
                    event.event_id)

    def _on_issue(self, event: ev.Event) -> None:
        p = event.payload
        self._batch(p["batch_id"])
        qty = self._qty(p)
        src = HoldingKey(p["batch_id"], "stock", p.get("from_location", "warehouse"))
        self._dec(src, qty)
        self._add(HoldingKey(p["batch_id"], "issued", f"room:{p['room']}", p["custodian"]), qty)

    def _on_consume(self, event: ev.Event) -> None:
        p = event.payload
        self._batch(p["batch_id"])
        qty = self._qty(p)
        usage_ref = str(p.get("usage_ref") or "").strip()
        if not usage_ref:
            raise Reject("missing_usage_ref")  # 开封必须登记脱敏用途凭证
        src = HoldingKey(p["batch_id"], "issued", f"room:{p['room']}", p["custodian"])
        self._dec(src, qty)
        self.state.terminal[p["batch_id"]]["consumed"] += qty
        self.state.consumptions.append({
            "batch_id": p["batch_id"], "qty": qty, "room": p["room"],
            "custodian": p["custodian"], "at": event.occurred_at,
            "usage_ref": usage_ref,  # 脱敏令牌，仅供审计，不进任何报表
        })

    def _on_borrow(self, event: ev.Event) -> None:
        p = event.payload
        self._batch(p["batch_id"])
        qty = self._qty(p)
        src = HoldingKey(p["batch_id"], "stock", p.get("from_location", "warehouse"))
        self._dec(src, qty)
        self._add(HoldingKey(p["batch_id"], "borrowed", "loan", p["borrower"]), qty)
        self._audit("borrow", p["batch_id"],
                    f"to:{p['borrower']} due:{p.get('due_at', '-')}",
                    event.occurred_at, event.event_id)

    def _on_return(self, event: ev.Event) -> None:
        p = event.payload
        self._batch(p["batch_id"])
        qty = self._qty(p)
        src = HoldingKey(p["batch_id"], "borrowed", "loan", p["borrower"])
        self._dec(src, qty)
        self._add(HoldingKey(p["batch_id"], "stock", "warehouse"), qty)

    def _on_damage(self, event: ev.Event) -> None:
        p = event.payload
        self._batch(p["batch_id"])
        qty = self._qty(p)
        scope = p.get("scope") or {}
        src = HoldingKey(
            p["batch_id"],
            scope.get("state", "stock"),
            scope.get("location", "warehouse"),
            scope.get("custodian"),
        )
        disposition = p.get("disposition", "held")
        if disposition == "held":
            self._dec(src, qty)
            self._add(HoldingKey(p["batch_id"], "damaged", src.location, src.custodian), qty)
        elif disposition == "lost":
            self._dec(src, qty)
            self.state.terminal[p["batch_id"]]["written_off"] += qty
        else:
            raise Reject("unknown_disposition")
        self._audit("damage", p["batch_id"], str(p.get("reason", "")),
                    event.occurred_at, event.event_id)

    # -- 环境与隔离 ------------------------------------------------------
    def _on_env_reading(self, event: ev.Event) -> None:
        p = event.payload
        subject_type = p.get("subject_type")
        subject_id = p.get("subject_id")
        temp_c = p.get("temp_c")
        humidity_pct = p.get("humidity_pct")
        affected: set[str] = set()
        if subject_type == "case":
            self._case(subject_id)
            for key in self.state.holdings:
                if key.location == f"case:{subject_id}":
                    affected.add(key.batch_id)
        elif subject_type == "batch":
            self._batch(subject_id)
            affected.add(subject_id)
        else:
            raise Reject("unknown_subject")
        self.state.env_log.append({
            "subject_type": subject_type, "subject_id": subject_id,
            "temp_c": temp_c, "humidity_pct": humidity_pct, "at": event.occurred_at,
        })
        for batch_id in sorted(affected):
            batch = self.state.batches[batch_id]
            if not batch.storage.allows(temp_c, humidity_pct):
                # 温湿度超标只隔离受影响的批次，其余照常诊疗
                self._quarantine_batch(batch_id, "env_excursion", event)

    def _quarantine_batch(self, batch_id: str, reason: str, event: ev.Event) -> None:
        for key in list(self.state.holdings):
            if key.batch_id == batch_id and key.state != "quarantined":
                qty = self.state.holdings.pop(key)
                self._add(HoldingKey(batch_id, "quarantined", key.location, key.custodian), qty)
                self.state.quarantine_origin[(batch_id, key.location, key.custodian)] = key.state
        self._audit("quarantine", batch_id, reason, event.occurred_at, event.event_id)

    def _quarantine_device(self, device_id: str, reason: str, event: ev.Event) -> None:
        status = self.state.devices[device_id]
        if status.state in ("quarantined",) + DEVICE_TERMINAL_STATES:
            return
        status.prev_state, status.prev_location = status.state, status.location
        status.state = "quarantined"
        status.quarantine_reason = reason
        self._audit("quarantine", device_id, reason, event.occurred_at, event.event_id)

    def _quarantine_case_contents(self, case_id: str, reason: str, event: ev.Event) -> None:
        batch_ids = {k.batch_id for k in self.state.holdings
                     if k.location == f"case:{case_id}"}
        for batch_id in sorted(batch_ids):
            self._quarantine_batch(batch_id, reason, event)
        for device_id, status in self.state.devices.items():
            if status.location == f"case:{case_id}":
                self._quarantine_device(device_id, reason, event)

    def _on_quarantine(self, event: ev.Event) -> None:
        items = event.payload.get("items") or []
        if not items:
            raise Reject("empty_items")
        for item in items:  # 先校验再变更，避免部分生效
            if item not in self.state.batches and item not in self.state.devices:
                raise Reject("unknown_item")
        reason = str(event.payload.get("reason", "manual"))
        for item in items:
            if item in self.state.batches:
                self._quarantine_batch(item, reason, event)
            else:
                self._quarantine_device(item, reason, event)

    def _on_release(self, event: ev.Event) -> None:
        items = event.payload.get("items") or []
        if not items:
            raise Reject("empty_items")
        for item in items:
            if item not in self.state.batches and item not in self.state.devices:
                raise Reject("unknown_item")
        for item in items:
            if item in self.state.batches:
                for key in list(self.state.holdings):
                    if key.batch_id == item and key.state == "quarantined":
                        qty = self.state.holdings.pop(key)
                        origin = self.state.quarantine_origin.pop(
                            (item, key.location, key.custodian), "stock"
                        )
                        self._add(HoldingKey(item, origin, key.location, key.custodian), qty)
            else:
                status = self.state.devices[item]
                if status.state == "quarantined":
                    status.state = status.prev_state or "warehouse"
                    if status.prev_location:
                        status.location = status.prev_location
                    status.prev_state = status.prev_location = None
                    status.quarantine_reason = None
            self._audit("release", item, str(event.payload.get("reason", "")),
                        event.occurred_at, event.event_id)

    # -- 设备 ------------------------------------------------------------
    def _on_device_assign(self, event: ev.Event) -> None:
        p = event.payload
        status = self._device(p["device_id"])
        if status.state not in ("warehouse", "room"):
            raise Reject("device_unavailable")
        status.state = "room"
        status.location = f"room:{p['room']}"
        status.custodian = p["custodian"]

    def _on_device_return(self, event: ev.Event) -> None:
        status = self._device(event.payload["device_id"])
        if status.state != "room":
            raise Reject("device_not_in_room")
        status.state = "warehouse"
        status.location = "warehouse"
        status.custodian = None

    def _on_device_activation(self, event: ev.Event) -> None:
        p = event.payload
        device_id = p["device_id"]
        if device_id not in self.manifest.devices:
            raise Reject("unknown_device")
        device = self.manifest.devices[device_id]
        status = self.state.devices[device_id]
        operator = self.manifest.operators.get(p.get("operator_id", ""))
        at = ev.parse_instant(event.occurred_at)
        reasons = activation_blockers(
            device_state=status.state,
            device=device,
            operator=operator,
            allowed_software=self.manifest.allowed_software,
            at=at,
        )
        allowed = not reasons
        self.state.activations[event.event_id] = {
            "device_id": device_id,
            "operator_id": p.get("operator_id"),
            "occurred_at": event.occurred_at,
            "allowed": allowed,
            "reasons": reasons,
        }
        if not allowed and INTEGRITY_REASONS.intersection(reasons):
            # 校准失效或软件未放行：只隔离这台设备，不影响其他物品与诊室
            self._quarantine_device(device_id, "activation_integrity", event)
        self._audit("device_activation", device_id,
                    "allowed" if allowed else "denied:" + ",".join(reasons),
                    event.occurred_at, event.event_id)

    # -- 留置交接 --------------------------------------------------------
    def _on_handover_announced(self, event: ev.Event) -> None:
        p = event.payload
        handover_id = p["handover_id"]
        if handover_id in self.state.handovers:
            raise Reject("duplicate_handover")
        missing = missing_languages(p.get("terms") or {}, self.manifest.handover_languages)
        if missing:
            raise Reject("missing_languages:" + "+".join(missing))
        items = list(p.get("item_ids") or [])
        if not items:
            raise Reject("empty_items")
        for item in items:
            if item not in self.state.batches and item not in self.state.devices:
                raise Reject("unknown_item")
        if not str(p.get("to_org", "")).strip():
            raise Reject("missing_to_org")
        self.state.handovers[handover_id] = {
            "status": "open",
            "item_ids": items,
            "to_org": p["to_org"],
            "terms": dict(p["terms"]),
            "trainings": [],
            "confirmations": {},
            "announced_at": event.occurred_at,
        }

    def _handover(self, handover_id: str) -> dict:
        handover = self.state.handovers.get(handover_id)
        if handover is None:
            raise Reject("unknown_handover")
        return handover

    def _on_training_logged(self, event: ev.Event) -> None:
        p = event.payload
        handover = self._handover(p["handover_id"])
        if handover["status"] != "open":
            raise Reject("handover_not_open")
        handover["trainings"].append({
            "trainee": p["trainee"], "topic": p["topic"], "at": event.occurred_at,
        })

    def _on_handover_confirmed(self, event: ev.Event) -> None:
        p = event.payload
        handover = self._handover(p["handover_id"])
        if handover["status"] != "open":
            raise Reject("handover_not_open")
        party = p.get("party")
        if party not in REQUIRED_PARTIES:
            raise Reject("unknown_party")
        handover["confirmations"][party] = {
            "name": p["name"], "role": p["role"], "at": event.occurred_at,
        }

    def _on_handover_completed(self, event: ev.Event) -> None:
        p = event.payload
        handover = self._handover(p["handover_id"])
        if handover["status"] != "open":
            raise Reject("handover_not_open")
        if not handover["trainings"]:
            raise Reject("training_missing")
        missing = [x for x in REQUIRED_PARTIES if x not in handover["confirmations"]]
        if missing:
            raise Reject("confirmation_missing:" + "+".join(missing))
        certificate_id = p["certificate_id"]
        if certificate_id in self.state.certificates:
            raise Reject("duplicate_certificate")
        # 仍有领用/借用/待处置数量的物品须先清账，才能交接
        for item in handover["item_ids"]:
            self._check_handoverable(item)
        for item in handover["item_ids"]:
            if item in self.state.batches:
                for key in list(self.state.holdings):
                    if key.batch_id == item:
                        qty = self.state.holdings.pop(key)
                        self.state.terminal[item]["handed_over"] += qty
            else:
                self.state.devices[item].state = "handed_over"
                self.state.devices[item].custodian = None
        handover["status"] = "completed"
        handover["completed_at"] = event.occurred_at
        self.state.certificates[certificate_id] = make_certificate(
            "handover", certificate_id, handover["item_ids"], p["issuer"],
            event.occurred_at, detail=handover["to_org"],
        )
        self._audit("handover_completed", certificate_id, handover["to_org"],
                    event.occurred_at, event.event_id)

    def _check_handoverable(self, item: str) -> None:
        if item in self.state.batches:
            for key in self.state.holdings:
                if key.batch_id == item and key.state in (
                        "issued", "borrowed", "damaged", "quarantined"):
                    raise Reject("items_outstanding")
        else:
            if self.state.devices[item].state not in ("warehouse", "room"):
                raise Reject("items_outstanding")

    # -- 终态：返运与销毁 --------------------------------------------------
    def _on_ship_back(self, event: ev.Event) -> None:
        p = event.payload
        items = list(p.get("item_ids") or [])
        if not items:
            raise Reject("empty_items")
        if p["leg_id"] not in self.manifest.legs:
            raise Reject("unknown_leg")
        certificate_id = p["certificate_id"]
        if certificate_id in self.state.certificates:
            raise Reject("duplicate_certificate")
        for item in items:
            if item in self.state.batches:
                for key in self.state.holdings:
                    if key.batch_id == item and key.state in ("issued", "borrowed"):
                        raise Reject("items_outstanding")
            elif item in self.state.devices:
                if self.state.devices[item].state in DEVICE_TERMINAL_STATES:
                    raise Reject("items_outstanding")
            else:
                raise Reject("unknown_item")
        for item in items:
            if item in self.state.batches:
                for key in list(self.state.holdings):
                    if key.batch_id == item:
                        qty = self.state.holdings.pop(key)
                        self.state.terminal[item]["shipped_back"] += qty
            else:
                status = self.state.devices[item]
                status.state = "shipped_back"
                status.custodian = None
        self.state.certificates[certificate_id] = make_certificate(
            "return_shipment", certificate_id, items, p["issuer"], event.occurred_at,
            detail=f"leg:{p['leg_id']}",
        )
        self._audit("ship_back", certificate_id, f"leg:{p['leg_id']}",
                    event.occurred_at, event.event_id)

    def _on_destroy(self, event: ev.Event) -> None:
        p = event.payload
        items = list(p.get("item_ids") or [])
        if not items:
            raise Reject("empty_items")
        certificate_id = p["certificate_id"]
        if certificate_id in self.state.certificates:
            raise Reject("duplicate_certificate")
        for item in items:
            if item in self.state.batches:
                for key in self.state.holdings:
                    if key.batch_id == item and key.state in ("issued", "borrowed"):
                        raise Reject("items_outstanding")
            elif item in self.state.devices:
                if self.state.devices[item].state in DEVICE_TERMINAL_STATES:
                    raise Reject("items_outstanding")
            else:
                raise Reject("unknown_item")
        for item in items:
            if item in self.state.batches:
                for key in list(self.state.holdings):
                    if key.batch_id == item:
                        qty = self.state.holdings.pop(key)
                        self.state.terminal[item]["destroyed"] += qty
            else:
                status = self.state.devices[item]
                status.state = "destroyed"
                status.custodian = None
        self.state.certificates[certificate_id] = make_certificate(
            "destruction", certificate_id, items, p["issuer"], event.occurred_at,
            detail=str(p.get("detail", "")),
        )
        self._audit("destroy", certificate_id, str(p.get("detail", "")),
                    event.occurred_at, event.event_id)

    # ------------------------------------------------------------------
    # 业务操作（构造事件并登记）
    # ------------------------------------------------------------------
    def notify_leg_arrival(self, leg_id, case_id, occurred_at, *, seal_intact=True,
                           actor="system", source="airway-bill", event_id=None):
        """航段到达通知；同一航段同一箱件的重复通知按自然键去重。"""

        return self.record(ev.Event(
            event_id or f"leg-arrival:{leg_id}:{case_id}", ev.LEG_ARRIVAL, occurred_at,
            {"leg_id": leg_id, "case_id": case_id, "seal_intact": seal_intact},
            actor=actor, source=source,
        ))

    def unpack(self, case_id, occurred_at, *, actor="system", event_id=None):
        # 重复拆箱由 nothing_to_unpack 兜底，因此每次尝试用新事件号
        return self.record(ev.Event(
            event_id or _new_id("unpack"), ev.UNPACK, occurred_at,
            {"case_id": case_id}, actor=actor,
        ))

    def split_batch(self, parent_batch, child_batch, qty, occurred_at, *,
                    location="warehouse", actor="system", event_id=None):
        return self.record(ev.Event(
            event_id or _new_id("split"), ev.SPLIT, occurred_at,
            {"parent_batch": parent_batch, "child_batch": child_batch, "qty": qty,
             "location": location}, actor=actor,
        ))

    def issue(self, batch_id, qty, custodian, room, occurred_at, *,
              actor="system", source="manual", event_id=None):
        return self.record(ev.Event(
            event_id or _new_id("issue"), ev.ISSUE, occurred_at,
            {"batch_id": batch_id, "qty": qty, "custodian": custodian, "room": room},
            actor=actor, source=source,
        ))

    def consume(self, batch_id, qty, custodian, room, usage_ref, occurred_at, *,
                actor="system", source="manual", event_id=None):
        return self.record(ev.Event(
            event_id or _new_id("consume"), ev.CONSUME, occurred_at,
            {"batch_id": batch_id, "qty": qty, "custodian": custodian, "room": room,
             "usage_ref": usage_ref}, actor=actor, source=source,
        ))

    def borrow(self, batch_id, qty, borrower, occurred_at, *, due_at=None,
               actor="system", event_id=None):
        return self.record(ev.Event(
            event_id or _new_id("borrow"), ev.BORROW, occurred_at,
            {"batch_id": batch_id, "qty": qty, "borrower": borrower, "due_at": due_at},
            actor=actor,
        ))

    def return_borrowed(self, batch_id, qty, borrower, occurred_at, *,
                        actor="system", event_id=None):
        return self.record(ev.Event(
            event_id or _new_id("return"), ev.RETURN, occurred_at,
            {"batch_id": batch_id, "qty": qty, "borrower": borrower}, actor=actor,
        ))

    def report_damage(self, batch_id, qty, reason, occurred_at, *, disposition="held",
                      scope=None, actor="system", event_id=None):
        return self.record(ev.Event(
            event_id or _new_id("damage"), ev.DAMAGE, occurred_at,
            {"batch_id": batch_id, "qty": qty, "reason": reason,
             "disposition": disposition, "scope": scope or {}}, actor=actor,
        ))

    def record_environment(self, subject_type, subject_id, occurred_at, *, temp_c=None,
                           humidity_pct=None, actor="system", source="logger",
                           event_id=None):
        return self.record(ev.Event(
            event_id or _new_id("env"), ev.ENV_READING, occurred_at,
            {"subject_type": subject_type, "subject_id": subject_id,
             "temp_c": temp_c, "humidity_pct": humidity_pct},
            actor=actor, source=source,
        ))

    def quarantine(self, items, reason, occurred_at, *, actor="system", event_id=None):
        return self.record(ev.Event(
            event_id or _new_id("quarantine"), ev.QUARANTINE, occurred_at,
            {"items": list(items), "reason": reason}, actor=actor,
        ))

    def release(self, items, occurred_at, *, reason="", actor="system", event_id=None):
        return self.record(ev.Event(
            event_id or _new_id("release"), ev.RELEASE, occurred_at,
            {"items": list(items), "reason": reason}, actor=actor,
        ))

    def assign_device(self, device_id, room, custodian, occurred_at, *,
                      actor="system", event_id=None):
        return self.record(ev.Event(
            event_id or _new_id("device-assign"), ev.DEVICE_ASSIGN, occurred_at,
            {"device_id": device_id, "room": room, "custodian": custodian}, actor=actor,
        ))

    def return_device(self, device_id, occurred_at, *, actor="system", event_id=None):
        return self.record(ev.Event(
            event_id or _new_id("device-return"), ev.DEVICE_RETURN, occurred_at,
            {"device_id": device_id}, actor=actor,
        ))

    def activate_device(self, device_id, operator_id, occurred_at, *,
                        actor="system", event_id=None):
        """启用核查；返回 (RecordResult, outcome)，outcome 含允许与否及原因。"""

        eid = event_id or _new_id("activation")
        result = self.record(ev.Event(
            eid, ev.DEVICE_ACTIVATION, occurred_at,
            {"device_id": device_id, "operator_id": operator_id}, actor=actor,
        ))
        return result, self.state.activations.get(eid)

    def announce_handover(self, handover_id, item_ids, to_org, terms, occurred_at, *,
                          actor="system", event_id=None):
        return self.record(ev.Event(
            event_id or f"handover:{handover_id}:announced", ev.HANDOVER_ANNOUNCED,
            occurred_at,
            {"handover_id": handover_id, "item_ids": list(item_ids), "to_org": to_org,
             "terms": dict(terms)}, actor=actor,
        ))

    def log_training(self, handover_id, trainee, topic, occurred_at, *,
                     actor="system", event_id=None):
        return self.record(ev.Event(
            event_id or _new_id("training"), ev.TRAINING_LOGGED, occurred_at,
            {"handover_id": handover_id, "trainee": trainee, "topic": topic},
            actor=actor,
        ))

    def confirm_handover(self, handover_id, party, name, role, occurred_at, *,
                         actor="system", event_id=None):
        return self.record(ev.Event(
            event_id or f"handover:{handover_id}:confirm:{party}",
            ev.HANDOVER_CONFIRMED, occurred_at,
            {"handover_id": handover_id, "party": party, "name": name, "role": role},
            actor=actor,
        ))

    def complete_handover(self, handover_id, certificate_id, issuer, occurred_at, *,
                          actor="system", event_id=None):
        # 完成条件可能逐步补齐，每次尝试都是独立事件；
        # 重复完成由 handover_not_open / duplicate_certificate 兜底
        return self.record(ev.Event(
            event_id or _new_id("handover-complete"), ev.HANDOVER_COMPLETED,
            occurred_at,
            {"handover_id": handover_id, "certificate_id": certificate_id,
             "issuer": issuer}, actor=actor,
        ))

    def ship_back(self, item_ids, leg_id, certificate_id, issuer, occurred_at, *,
                  actor="system", event_id=None):
        return self.record(ev.Event(
            event_id or _new_id("ship-back"), ev.SHIP_BACK, occurred_at,
            {"item_ids": list(item_ids), "leg_id": leg_id,
             "certificate_id": certificate_id, "issuer": issuer}, actor=actor,
        ))

    def destroy(self, item_ids, certificate_id, issuer, occurred_at, *, detail="",
                actor="system", event_id=None):
        return self.record(ev.Event(
            event_id or _new_id("destroy"), ev.DESTROY, occurred_at,
            {"item_ids": list(item_ids), "certificate_id": certificate_id,
             "issuer": issuer, "detail": detail}, actor=actor,
        ))
