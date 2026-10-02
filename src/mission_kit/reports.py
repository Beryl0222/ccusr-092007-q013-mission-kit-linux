"""查询与结存复算报表。

保障人员随时要回答三件事：某件物品在哪里、是否还能使用、由谁接管。
行动结束后 ``closing_balance`` 按分装谱系复算结存并校验数量守恒。

隐私约定：消耗登记里的 ``usage_ref`` 是脱敏用途令牌，只留在事件日志
供审计；本模块的所有输出只含数量与去向，不含任何患者诊疗详情。
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from .devices import calibration_valid, software_allowed
from .events import parse_instant
from .ledger import Ledger

# 可以投入诊疗的批次状态
_USABLE_BATCH_STATES = ("stock", "issued")


def _as_instant(at) -> datetime:
    if isinstance(at, datetime):
        if at.tzinfo is None:
            raise ValueError("时间必须携带时区偏移")
        return at.astimezone(timezone.utc)
    return parse_instant(at)


def _expiry_instant(expiry: str) -> datetime:
    """效期当日结束（UTC）前有效。"""

    day = date.fromisoformat(expiry)
    return datetime.combine(day, time.max, tzinfo=timezone.utc)


def locate(ledger: Ledger, item_id: str) -> dict:
    """某件物品现在在哪里、处于什么状态。"""

    state = ledger.state
    if item_id in state.batches:
        batch = state.batches[item_id]
        return {
            "item_id": item_id,
            "kind": "batch",
            "name": batch.name,
            "lot": batch.lot,
            "expiry": batch.expiry,
            "customs_ref": batch.customs_ref,
            "holdings": [
                {"state": k.state, "location": k.location,
                 "custodian": k.custodian, "quantity": q}
                for k, q in sorted(state.holdings.items(),
                                   key=lambda kv: (kv[0].state, kv[0].location))
                if k.batch_id == item_id
            ],
            "terminal": {t: n for t, n in state.terminal[item_id].items() if n},
        }
    if item_id in state.devices:
        status = state.devices[item_id]
        device = ledger.manifest.devices[item_id]
        return {
            "item_id": item_id,
            "kind": "device",
            "name": device.name,
            "model": device.model,
            "serial": device.serial,
            "state": status.state,
            "location": status.location,
            "custodian": status.custodian,
            "quarantine_reason": status.quarantine_reason,
        }
    raise LookupError(f"未知物品: {item_id}")


def usability(ledger: Ledger, item_id: str, at) -> dict:
    """某件物品此刻是否还能使用；批次给出可用数量，设备给出原因。"""

    state = ledger.state
    instant = _as_instant(at)
    if item_id in state.batches:
        batch = state.batches[item_id]
        reasons = []
        if instant > _expiry_instant(batch.expiry):
            reasons.append("expired")
        usable_qty = sum(
            q for k, q in state.holdings.items()
            if k.batch_id == item_id and k.state in _USABLE_BATCH_STATES
        )
        if usable_qty == 0:
            reasons.append("no_usable_stock")
        quarantined = sum(
            q for k, q in state.holdings.items()
            if k.batch_id == item_id and k.state == "quarantined"
        )
        return {
            "item_id": item_id,
            "kind": "batch",
            "usable": not reasons,
            "usable_quantity": 0 if "expired" in reasons else usable_qty,
            "quarantined_quantity": quarantined,
            "reasons": reasons,
        }
    if item_id in state.devices:
        status = state.devices[item_id]
        device = ledger.manifest.devices[item_id]
        reasons = []
        if status.state == "quarantined":
            reasons.append("device_quarantined")
        elif status.state not in ("warehouse", "room"):
            reasons.append("device_unavailable")
        if not calibration_valid(device, instant):
            reasons.append("calibration_expired")
        if not software_allowed(ledger.manifest.allowed_software, device):
            reasons.append("software_not_allowed")
        return {
            "item_id": item_id,
            "kind": "device",
            "usable": not reasons,
            "reasons": reasons,
        }
    raise LookupError(f"未知物品: {item_id}")


def custodian_of(ledger: Ledger, item_id: str) -> dict:
    """某件物品现在由谁负责；终态物品给出凭证上的责任方。"""

    state = ledger.state
    if item_id in state.batches:
        custodians = sorted({
            k.custodian for k in state.holdings
            if k.batch_id == item_id and k.custodian
        })
        return {
            "item_id": item_id,
            "custodians": custodians,
            "terminal": _terminal_responsibility(ledger, item_id, state.terminal[item_id]),
        }
    if item_id in state.devices:
        status = state.devices[item_id]
        terminal = {}
        if status.state in ("handed_over", "shipped_back", "destroyed"):
            terminal = _certificate_responsibility(ledger, item_id)
        return {
            "item_id": item_id,
            "custodians": [status.custodian] if status.custodian else [],
            "terminal": terminal,
        }
    raise LookupError(f"未知物品: {item_id}")


def _certificate_responsibility(ledger: Ledger, item_id: str) -> dict:
    for cert in ledger.state.certificates.values():
        if item_id in cert.item_ids:
            return {"state": cert.kind, "responsible": cert.issuer,
                    "certificate_id": cert.certificate_id}
    return {}


def _terminal_responsibility(ledger: Ledger, item_id: str, terminal: dict[str, int]) -> dict:
    """终态数量的责任方，取自覆盖该物品的凭证。"""

    out = {}
    for cert in ledger.state.certificates.values():
        if item_id not in cert.item_ids:
            continue
        if cert.kind == "handover" and terminal.get("handed_over"):
            out["handed_over"] = {"responsible": cert.detail,
                                  "certificate_id": cert.certificate_id}
        elif cert.kind == "return_shipment" and terminal.get("shipped_back"):
            out["shipped_back"] = {"responsible": cert.issuer,
                                   "certificate_id": cert.certificate_id}
        elif cert.kind == "destruction" and terminal.get("destroyed"):
            out["destroyed"] = {"responsible": cert.issuer,
                                "certificate_id": cert.certificate_id}
    return out


def environment_log(ledger: Ledger, subject_id: str | None = None) -> list[dict]:
    """温湿度记录；可按箱件或批次过滤。"""

    return [r for r in ledger.state.env_log
            if subject_id is None or r["subject_id"] == subject_id]


def closing_balance(ledger: Ledger) -> dict:
    """行动结束后的结存复算。

    按分装谱系校验数量守恒（variance 恒为 0），列出每批在账数量与
    终态去向、每台设备状态与全部凭证。输出只含数量与去向，不含
    ``usage_ref`` 等任何患者诊疗详情。
    """

    state = ledger.state
    lineages = ledger.conservation()
    batches = []
    for batch_id, batch in sorted(state.batches.items()):
        live = [
            {"state": k.state, "location": k.location,
             "custodian": k.custodian, "quantity": q}
            for k, q in sorted(state.holdings.items(),
                               key=lambda kv: (kv[0].state, kv[0].location))
            if k.batch_id == batch_id
        ]
        terminal = {t: n for t, n in state.terminal[batch_id].items() if n}
        batches.append({
            "batch_id": batch_id,
            "name": batch.name,
            "lot": batch.lot,
            "expiry": batch.expiry,
            "customs_ref": batch.customs_ref,
            "lineage_root": batch.lineage_root,
            "live": live,
            "terminal": terminal,
            "on_book": sum(h["quantity"] for h in live) + sum(terminal.values()),
        })
    devices = [
        {
            "device_id": device_id,
            "name": ledger.manifest.devices[device_id].name,
            "state": state.devices[device_id].state,
            "location": state.devices[device_id].location,
            "custodian": state.devices[device_id].custodian,
        }
        for device_id in sorted(state.devices)
    ]
    certificates = [
        {
            "certificate_id": c.certificate_id,
            "kind": c.kind,
            "item_ids": list(c.item_ids),
            "issuer": c.issuer,
            "issued_at": c.issued_at,
            "digest": c.digest,
        }
        for c in sorted(state.certificates.values(), key=lambda c: c.certificate_id)
    ]
    discrepancies = [r for r in lineages if r["variance"] != 0]
    return {
        "mission_id": ledger.manifest.mission_id,
        "event_count": ledger.event_count,
        "batches": batches,
        "devices": devices,
        "lineages": lineages,
        "certificates": certificates,
        "rejected_events": dict(ledger.rejected),
        "conservation_ok": not discrepancies,
        "discrepancies": discrepancies,
    }
