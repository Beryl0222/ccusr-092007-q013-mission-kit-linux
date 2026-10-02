import tempfile
import unittest
from pathlib import Path

from mission_kit import (
    DuplicateSubmission,
    EventStore,
    PrivacyViolation,
)
from mission_kit.ledger import normalize_occurred_at, notice_fingerprint


class LedgerTest(unittest.TestCase):
    def test_timezone_normalized_to_utc_but_local_kept(self):
        utc_iso, original = normalize_occurred_at("2026-09-20T09:00:00+08:00")
        self.assertEqual(utc_iso, "2026-09-20T01:00:00+00:00")
        self.assertEqual(original, "2026-09-20T09:00:00+08:00")
        # 同一时刻在不同时区表达，归一后完全相同，重放顺序不受时区影响。
        utc_iso2, _ = normalize_occurred_at("2026-09-20T02:00:00+01:00")
        self.assertEqual(utc_iso, utc_iso2)

    def test_naive_timestamp_rejected(self):
        store = EventStore()
        with self.assertRaises(ValueError):
            store.append("x", {}, actor="a", occurred_at="2026-09-20T09:00:00")

    def test_offline_backfill_idempotent(self):
        store = EventStore()
        kw = dict(actor="scanner-7", occurred_at="2026-09-20T09:00:00+08:00")
        e1 = store.append("scan", {"v": 1}, client_event_id="SCAN-0001", **kw)
        # 设备断网恢复后重放缓存：时间戳不同也必须识别为同一事件。
        with self.assertRaises(DuplicateSubmission) as ctx:
            store.append("scan", {"v": 1}, client_event_id="SCAN-0001",
                         actor="scanner-7",
                         occurred_at="2026-09-20T17:30:00+00:00")
        self.assertEqual(ctx.exception.original_event_id, e1.event_id)
        self.assertEqual(len(store.events), 1)

    def test_transport_notice_dedup_ignores_gateway_timezone(self):
        store = EventStore()
        parts = ["leg-2", "BOX-1", "departed"]
        store.append_notice(
            "leg_update", parts, {"location": "DXB"},
            actor="gateway-a", occurred_at="2026-09-20T09:00:00+08:00",
        )
        # 另一网关用本地时区重复推送同一航段事实。
        with self.assertRaises(DuplicateSubmission):
            store.append_notice(
                "leg_update", parts, {"location": "DXB"},
                actor="gateway-b", occurred_at="2026-09-20T05:00:00+04:00",
            )
        self.assertEqual(len(store.events), 1)
        # 不同航段的通知不会被误杀。
        store.append_notice(
            "leg_update", ["leg-3", "BOX-1", "departed"], {"location": "FRA"},
            actor="gateway-a", occurred_at="2026-09-20T13:00:00+08:00",
        )
        self.assertEqual(len(store.events), 2)

    def test_fingerprint_stable(self):
        self.assertEqual(
            notice_fingerprint("leg", ["a", "b"]),
            notice_fingerprint("leg", ["a", "b"]),
        )
        self.assertNotEqual(
            notice_fingerprint("leg", ["a", "b"]),
            notice_fingerprint("leg", ["a", "c"]),
        )

    def test_privacy_fields_rejected_at_ledger(self):
        store = EventStore()
        with self.assertRaises(PrivacyViolation):
            store.append("consumable_used",
                         {"item_id": "X", "patient_name": "Ali"},
                         actor="a", occurred_at="2026-09-20T09:00:00+08:00")

    def test_jsonl_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            store = EventStore(path)
            store.append("scan", {"v": 1}, actor="a",
                         occurred_at="2026-09-20T09:00:00+08:00",
                         client_event_id="SCAN-0009")
            reloaded = EventStore(path)
            self.assertEqual(len(reloaded.events), 1)
            self.assertEqual(reloaded.events[0].payload, {"v": 1})
            self.assertEqual(reloaded.events[0].occurred_at,
                             "2026-09-20T01:00:00+00:00")

    def test_rejected_stage_does_not_consume_seq(self):
        store = EventStore()
        staged = store.stage("scan", {"v": 1}, actor="a",
                             occurred_at="2026-09-20T09:00:00+08:00")
        # 未 commit 的暂存事件不占序号。
        committed = store.append("scan", {"v": 2}, actor="a",
                                 occurred_at="2026-09-20T09:05:00+08:00")
        self.assertEqual(committed.seq, 1)
        with self.assertRaises(Exception):
            store.commit(staged)


if __name__ == "__main__":
    unittest.main()
