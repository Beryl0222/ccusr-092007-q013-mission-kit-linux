"""台账：数量守恒、幂等、离线补传、时区与按物品隔离。"""

import tempfile
import unittest
from pathlib import Path

from helpers import T, arrive_and_unpack, make_ledger
from mission_kit import Event, JsonlEventStore, Ledger, load_manifest
from mission_kit.events import CONSUME, parse_instant


class ConservationTest(unittest.TestCase):
    def setUp(self):
        self.ledger = arrive_and_unpack(make_ledger())

    def test_quantities_conserved_across_operations(self):
        le = self.ledger
        le.split_batch("BATCH-IOL-2605", "BATCH-IOL-2605-A", 30, T("21T11:00:00"))
        le.issue("BATCH-IOL-2605", 40, "OP-01", "诊室A", T("21T13:00:00"))
        le.consume("BATCH-IOL-2605", 5, "OP-01", "诊室A", "u#7f3a", T("21T14:00:00"))
        le.borrow("BATCH-IOL-2605-A", 10, "OP-02", T("21T15:00:00"))
        le.return_borrowed("BATCH-IOL-2605-A", 6, "OP-02", T("22T09:00:00"))
        le.report_damage("BATCH-IOL-2605-A", 2, "包装破损", T("22T10:00:00"))

        for row in le.conservation():
            self.assertEqual(row["variance"], 0, row)

        root = next(r for r in le.conservation() if r["lineage_root"] == "BATCH-IOL-2605")
        self.assertEqual(root["initial"], 100)
        self.assertEqual(root["live"], 95)  # 100 - 5消耗；报损 2 仍在账（damaged）
        self.assertEqual(root["terminal"], 5)

        holdings = {(k.batch_id, k.state, k.location, k.custodian): q
                    for k, q in le.state.holdings.items()}
        self.assertEqual(holdings[("BATCH-IOL-2605", "stock", "warehouse", None)], 30)
        self.assertEqual(holdings[("BATCH-IOL-2605", "issued", "room:诊室A", "OP-01")], 35)
        self.assertEqual(holdings[("BATCH-IOL-2605-A", "borrowed", "loan", "OP-02")], 4)
        self.assertEqual(holdings[("BATCH-IOL-2605-A", "damaged", "warehouse", None)], 2)
        self.assertEqual(le.state.terminal["BATCH-IOL-2605"]["consumed"], 5)

    def test_consume_requires_usage_ref(self):
        le = self.ledger
        le.issue("BATCH-SWAB-0422", 10, "OP-01", "诊室A", T("21T13:00:00"))
        result = le.consume("BATCH-SWAB-0422", 3, "OP-01", "诊室A", "", T("21T14:00:00"))
        self.assertTrue(result.rejected)
        self.assertEqual(result.reason, "missing_usage_ref")
        self.assertEqual(le.state.terminal["BATCH-SWAB-0422"]["consumed"], 0)

    def test_cannot_overdraw(self):
        le = self.ledger
        result = le.issue("BATCH-VISCO-0311", 999, "OP-01", "诊室A", T("21T13:00:00"))
        self.assertTrue(result.rejected)
        self.assertEqual(result.reason, "insufficient_quantity")


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.ledger = arrive_and_unpack(make_ledger())

    def test_duplicate_event_id_is_noop(self):
        le = self.ledger
        le.issue("BATCH-IOL-2605", 10, "OP-01", "诊室A", T("21T13:00:00"))
        first = le.consume("BATCH-IOL-2605", 5, "OP-01", "诊室A", "u#7f3a",
                           T("21T14:00:00"), event_id="scan-0001", source="scanner-offline")
        self.assertTrue(first.applied)
        # 离线扫码重传：同一 event_id 再次上传
        second = le.consume("BATCH-IOL-2605", 5, "OP-01", "诊室A", "u#7f3a",
                            T("21T14:00:00"), event_id="scan-0001", source="scanner-offline")
        self.assertFalse(second.applied)
        self.assertEqual(le.state.terminal["BATCH-IOL-2605"]["consumed"], 5)

    def test_conflicting_duplicate_keeps_first(self):
        le = self.ledger
        le.issue("BATCH-IOL-2605", 10, "OP-01", "诊室A", T("21T13:00:00"),
                 event_id="scan-0002")
        again = le.issue("BATCH-IOL-2605", 99, "OP-01", "诊室A", T("21T13:00:00"),
                         event_id="scan-0002")
        self.assertFalse(again.applied)
        self.assertEqual(again.reason, "conflicting_duplicate")
        issued = sum(q for k, q in le.state.holdings.items() if k.state == "issued")
        self.assertEqual(issued, 10)

    def test_duplicate_leg_notification_creates_no_inventory(self):
        le = make_ledger()
        first = le.notify_leg_arrival("LEG-2", "CASE-01", T("21T08:00:00"))
        # 航司重复推送同一航段同一箱件，时间戳略有差异
        dup = le.notify_leg_arrival("LEG-2", "CASE-01", T("21T08:05:00"))
        self.assertTrue(first.applied)
        self.assertFalse(dup.applied)
        le.unpack("CASE-01", T("21T10:00:00"))
        total = sum(q for k, q in le.state.holdings.items()
                    if k.batch_id == "BATCH-IOL-2605")
        self.assertEqual(total, 100)


class TimeAndBackfillTest(unittest.TestCase):
    def test_naive_timestamp_rejected(self):
        le = make_ledger()
        with self.assertRaises(ValueError):
            le.record(Event("e1", CONSUME, "2026-09-21T10:00:00", {}))

    def test_offsets_normalize_to_same_instant(self):
        a = parse_instant("2026-09-21T09:00:00+08:00")
        b = parse_instant("2026-09-21T03:00:00+02:00")
        self.assertEqual(a, b)

    def test_offline_backfill_reorders_and_retries(self):
        le = arrive_and_unpack(make_ledger())
        # 离线扫码先补传消耗（发生时间 11:00 +08:00），此时领用尚未入帐
        consume = le.consume("BATCH-IOL-2605", 5, "OP-01", "诊室A", "u#7f3a",
                             T("21T11:00:00"), event_id="scan-consume-1",
                             source="scanner-offline")
        self.assertTrue(consume.rejected)
        self.assertEqual(consume.reason, "insufficient_quantity")
        # 另一台设备在另一个时区记录的领用（10:30 +08:00 = 前一天 21:30 -05:00）晚到
        issue = le.issue("BATCH-IOL-2605", 40, "OP-01", "诊室A",
                         "2026-09-20T21:30:00-05:00", event_id="scan-issue-1",
                         source="scanner-offline")
        self.assertFalse(issue.rejected)
        # 重放后消耗落在领用之后，暂缓自动解除
        self.assertNotIn("scan-consume-1", le.rejected)
        self.assertEqual(le.state.terminal["BATCH-IOL-2605"]["consumed"], 5)
        for row in le.conservation():
            self.assertEqual(row["variance"], 0, row)


class ScopedIsolationTest(unittest.TestCase):
    def test_env_excursion_quarantines_only_affected_batch(self):
        le = make_ledger()
        for case_id in ("CASE-01", "CASE-02"):
            le.notify_leg_arrival("LEG-1", case_id, T("20T10:00:00"))
            le.notify_leg_arrival("LEG-2", case_id, T("21T08:00:00"))
        # 运输途中 CASE-01 温度记录仪报 12°C，超出人工晶状体 2-8°C 范围
        le.record_environment("case", "CASE-01", T("21T07:30:00"), temp_c=12.0)
        iol = {(k.state, k.location) for k in le.state.holdings
               if k.batch_id == "BATCH-IOL-2605"}
        self.assertEqual(iol, {("quarantined", "case:CASE-01")})
        # 同机其他箱件不受影响
        swab = {(k.state, k.location) for k in le.state.holdings
                if k.batch_id == "BATCH-SWAB-0422"}
        self.assertEqual(swab, {("sealed", "case:CASE-02")})

        le.unpack("CASE-01", T("21T10:00:00"))
        le.unpack("CASE-02", T("21T10:05:00"))
        # 隔离状态随拆箱保留；其他批次照常领用，不停诊疗
        result = le.issue("BATCH-SWAB-0422", 20, "OP-01", "诊室A", T("21T11:00:00"))
        self.assertFalse(result.rejected)
        # 质检复核后解除隔离，回到入库状态
        le.release(["BATCH-IOL-2605"], T("21T12:00:00"), reason="质检复核合格")
        states = {k.state for k in le.state.holdings if k.batch_id == "BATCH-IOL-2605"}
        self.assertEqual(states, {"stock"})

    def test_broken_seal_quarantines_case_contents_only(self):
        le = make_ledger()
        for case_id in ("CASE-01", "CASE-02"):
            le.notify_leg_arrival("LEG-1", case_id, T("20T10:00:00"))
        le.notify_leg_arrival("LEG-2", "CASE-02", T("21T08:00:00"), seal_intact=False)
        self.assertEqual(le.state.devices["DEV-SLIT-01"].state, "quarantined")
        visco = {k.state for k in le.state.holdings if k.batch_id == "BATCH-VISCO-0311"}
        self.assertEqual(visco, {"quarantined"})
        # CASE-01 完好
        self.assertEqual(le.state.devices["DEV-TONO-01"].state, "in_case")
        iol = {k.state for k in le.state.holdings if k.batch_id == "BATCH-IOL-2605"}
        self.assertEqual(iol, {"sealed"})


class StoreTest(unittest.TestCase):
    def test_jsonl_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlEventStore(Path(tmp) / "events.jsonl")
            le = arrive_and_unpack(make_ledger(store=store))
            le.issue("BATCH-IOL-2605", 10, "OP-01", "诊室A", T("21T13:00:00"))
            le.consume("BATCH-IOL-2605", 2, "OP-01", "诊室A", "u#7f3a", T("21T14:00:00"))

            from helpers import FIXTURE
            reloaded = Ledger(load_manifest(FIXTURE), store=store)
            self.assertEqual(reloaded.event_count, le.event_count)
            self.assertEqual(reloaded.state.holdings, le.state.holdings)
            self.assertEqual(reloaded.state.terminal, le.state.terminal)


if __name__ == "__main__":
    unittest.main()
