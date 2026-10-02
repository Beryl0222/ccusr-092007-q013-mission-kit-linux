"""不可变事件台账：离线补传幂等、航段通知去重、时区归一与 JSONL 持久化。

所有库存状态都由事件重放得到（见 :mod:`mission_kit.inventory`），台账本身
只负责接收事实，不做业务数量判断。每条事件只记录"谁在何时对哪件器材做了
什么"，不记录患者诊疗详情。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import DuplicateSubmission, LedgerStateError, PrivacyViolation

SCHEMA_VERSION = 2

# 载荷中禁止出现的患者标识字段——行动结存只回答器材问题，不承载诊疗信息。
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "patient_name",
        "patient_id",
        "patient_mrn",
        "mrn",
        "national_id",
        "passport_no",
        "diagnosis",
        "diagnosis_code",
        "procedure_note",
        "clinical_note",
        "patient_dob",
        "patient_phone",
    }
)
_ENCOUNTER_TOKEN = re.compile(r"^ENC-[A-Z0-9]{4,}$")
_CLIENT_EVENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_occurred_at(value: str | datetime) -> tuple[str, str]:
    """把任意带偏移的本地时间归一为 UTC，同时保留原始表达。

    返回 ``(utc_iso, original_iso)``。缺失时区信息时拒绝——时间戳必须能
    无歧义地排序，避免跨时区补传打乱重放顺序。
    """
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        parsed = value
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError("occurred_at 必须携带时区偏移，例如 2026-09-20T09:00:00+08:00")
    original = parsed.isoformat()
    as_utc = parsed.astimezone(timezone.utc)
    return as_utc.isoformat(), original


def notice_fingerprint(kind: str, parts: Iterable[Any]) -> str:
    """航段/运输重复通知的语义指纹。

    同一条"某箱件在某航段发生某事"的通知可能由不同网关在不同时区重复
    推送，指纹只取业务身份，不含送达时间，使重复通知天然幂等。
    """
    material = kind + "|" + "|".join(str(p) for p in parts)
    return "fp-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _scan_privacy(obj: Any, path: str = "") -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in _FORBIDDEN_PAYLOAD_KEYS:
                raise PrivacyViolation(f"载荷禁止携带患者诊疗字段：{path}{key}")
            _scan_privacy(value, f"{path}{key}.")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            _scan_privacy(value, f"{path}[{index}].")


def validate_encounter_ref(encounter_ref: str | None) -> None:
    """消耗去向使用不透明就诊代号（ENC-XXXX），台账不接触患者身份。"""
    if encounter_ref is not None and not _ENCOUNTER_TOKEN.match(encounter_ref):
        raise PrivacyViolation(
            "消耗用途须使用脱敏就诊代号（形如 ENC-A1B2），不得记录患者身份或诊疗内容"
        )


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    seq: int
    type: str
    payload: dict[str, Any]
    occurred_at: str          # 始终为 UTC ISO-8601
    recorded_at: str          # 台账接收时刻（UTC）
    actor: str
    client_event_id: str | None = None
    local_occurred_at: str | None = None  # 扫码设备本地时间，保留以备核对
    notice_fingerprint: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "event_id": self.event_id,
                "seq": self.seq,
                "type": self.type,
                "payload": self.payload,
                "occurred_at": self.occurred_at,
                "recorded_at": self.recorded_at,
                "actor": self.actor,
                "client_event_id": self.client_event_id,
                "local_occurred_at": self.local_occurred_at,
                "notice_fingerprint": self.notice_fingerprint,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, line: str) -> "LedgerEvent":
        data = json.loads(line)
        return cls(
            event_id=data["event_id"],
            seq=data["seq"],
            type=data["type"],
            payload=data["payload"],
            occurred_at=data["occurred_at"],
            recorded_at=data["recorded_at"],
            actor=data["actor"],
            client_event_id=data.get("client_event_id"),
            local_occurred_at=data.get("local_occurred_at"),
            notice_fingerprint=data.get("notice_fingerprint"),
        )


class EventStore:
    """append-only 事件台账。

    去重有两道独立的键：

    * ``client_event_id``——扫码离线补传：同一台设备重放同一批缓存事件；
    * ``notice_fingerprint``——航段重复通知：不同网关对同一事实的重复推送。

    二者都可能以不同时区时间戳到达，因此判重绝不依赖时间。
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else None
        self._events: list[LedgerEvent] = []
        self._client_index: dict[str, str] = {}
        self._notice_index: dict[str, str] = {}
        if self.path is not None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._ingest(LedgerEvent.from_json(line), persist=False)

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._events)

    # ------------------------------------------------------------------ #
    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: str,
        occurred_at: str | datetime,
        client_event_id: str | None = None,
        notice_fp: str | None = None,
    ) -> LedgerEvent:
        """生成并提交事件（先做业务校验的调用方请用 :meth:`stage`/:meth:`commit`）。"""
        event = self.stage(
            event_type, payload, actor=actor, occurred_at=occurred_at,
            client_event_id=client_event_id, notice_fp=notice_fp,
        )
        self.commit(event)
        return event

    def stage(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: str,
        occurred_at: str | datetime,
        client_event_id: str | None = None,
        notice_fp: str | None = None,
    ) -> LedgerEvent:
        """构造事件并完成隐私与幂等检查，但不写入台账。

        调用方随后应把事件交给投影做业务校验，成功后再 :meth:`commit`；
        校验失败时事件既未落盘也未占用序号。
        """
        if client_event_id is not None and not _CLIENT_EVENT_RE.match(client_event_id):
            raise ValueError("client_event_id 须为 ≥4 位字母数字及 ._:- 组成的稳定标识")
        _scan_privacy(payload)

        if client_event_id is not None and client_event_id in self._client_index:
            raise DuplicateSubmission("离线补传", self._client_index[client_event_id])
        if notice_fp is not None and notice_fp in self._notice_index:
            raise DuplicateSubmission("航段重复通知", self._notice_index[notice_fp])

        utc_iso, original_iso = normalize_occurred_at(occurred_at)
        seq = len(self._events) + 1
        return LedgerEvent(
            event_id=f"EVT-{seq:06d}",
            seq=seq,
            type=event_type,
            payload=payload,
            occurred_at=utc_iso,
            recorded_at=utc_now().isoformat(),
            actor=actor,
            client_event_id=client_event_id,
            local_occurred_at=original_iso if original_iso != utc_iso else None,
            notice_fingerprint=notice_fp,
        )

    def commit(self, event: LedgerEvent) -> LedgerEvent:
        """提交由 :meth:`stage` 构造的事件。"""
        if event.seq != len(self._events) + 1:
            raise LedgerStateError("事件序号已过期或已提交，请重新 stage")
        self._ingest(event, persist=True)
        return event

    def append_notice(
        self,
        kind: str,
        fingerprint_parts: Iterable[Any],
        payload: dict[str, Any],
        *,
        actor: str,
        occurred_at: str | datetime,
    ) -> LedgerEvent:
        """记录运输/航段通知，重复通知幂等丢弃。"""
        fp = notice_fingerprint(kind, fingerprint_parts)
        return self.append(
            "transport_notice",
            {**payload, "notice_kind": kind},
            actor=actor,
            occurred_at=occurred_at,
            notice_fp=fp,
        )

    # ------------------------------------------------------------------ #
    def _ingest(self, event: LedgerEvent, *, persist: bool) -> None:
        self._events.append(event)
        if event.client_event_id is not None:
            self._client_index.setdefault(event.client_event_id, event.event_id)
        if event.notice_fingerprint is not None:
            self._notice_index.setdefault(event.notice_fingerprint, event.event_id)
        if persist:
            self._persist(event)

    def _persist(self, event: LedgerEvent) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.to_json() + "\n")

    def replay_into(self, projector: Any) -> Any:
        """把全部事件按顺序喂给投影；返回该投影以支持链式使用。"""
        for event in self._events:
            projector.apply(event)
        return projector
