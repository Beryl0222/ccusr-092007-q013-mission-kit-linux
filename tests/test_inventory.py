import unittest

from _helpers import build_service, register_second_box, register_standard_box, ts
from mission_kit import (
    ConservationViolation,
    DuplicateSubmission,
    MissionService,
    PrivacyViolation,
)
from mission_kit.errors import SealedContainerError


class InventoryFlowTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        register_standard_box(self.svc)
        register_second_box(self.svc)

    def test_full_conservation_chain(self):
        svc = self.svc
        svc.verify_seal(container_id="BOX-1", seal_id="SEAL-A001", intact=True,
                        actor="logistics", occurred_at=ts(1))
        svc.open_box(container_id="BOX-1", seal_id="SEAL-A001", room="诊室A",
                     actor="nurse-li", occurred_at=ts(2))
        # 分装 30 副手套到小包装：总数不变。
        svc.subpack(item_id="ITM-GLOVE-A", parent_item_id="ITM-GLOVE", qty=30,
                    location="诊室A", actor="nurse-li", occurred_at=ts(3))
        g = svc.inventory.items["ITM-GLOVE"]
        ga = svc.inventory.items["ITM-GLOVE-A"]
        self.assertEqual((g.available, ga.available), (70, 30))

        # 领用 20 副（含分装）。
        svc.issue(items=[{"item_id": "ITM-GLOVE-A", "qty": 20}],
                  recipient="dr-wang", role="ophthalmologist", room="诊室A",
                  actor="nurse-li", occurred_at=ts(4))
        # 未领用不能消耗（防"开封未登记用途"）。
        with self.assertRaises(ConservationViolation):
            svc.record_use(item_id="ITM-GLOVE", qty=5, actor="dr-wang",
                           encounter_ref="ENC-9F3K", occurred_at=ts(5))
        # 消耗必须有脱敏就诊代号或非临床用途。
        with self.assertRaises(ConservationViolation):
            svc.record_use(item_id="ITM-GLOVE-A", qty=20, actor="dr-wang",
                           occurred_at=ts(5))
        svc.record_use(item_id="ITM-GLOVE-A", qty=12, actor="dr-wang",
                       encounter_ref="ENC-9F3K", occurred_at=ts(6))
        svc.record_use(item_id="ITM-GLOVE-A", qty=3, actor="dr-wang",
                       purpose_code="training", occurred_at=ts(7))
        # 剩余 5 副未用，归还入库。
        svc.return_items(item_id="ITM-GLOVE-A", qty=5, location="诊室A",
                         actor="nurse-li", occurred_at=ts(8))
        self.assertEqual(ga.consumed, 15)
        # 分装 30：发出 20、消耗 15、归还 5 → 可用 10+5=15。
        self.assertEqual(ga.available, 15)

        # 报损 2 副，需原因+见证。
        with self.assertRaises(ConservationViolation):
            svc.write_off(item_id="ITM-GLOVE-A", qty=2, reason="包装破损",
                          witness="", actor="nurse-li", occurred_at=ts(9))
        svc.write_off(item_id="ITM-GLOVE-A", qty=2, reason="包装破损污染",
                      witness="nurse-chen", actor="nurse-li", occurred_at=ts(10))

        report = svc.inventory.reconcile()
        self.assertTrue(report["balanced"])
        glove_lines = [l for l in report["items"] if l["lot"] == "L2026-09"]
        self.assertEqual(sum(l["registered"] for l in glove_lines), 130)
        # 手套子树去重分装后，在册总量仍是原始 100 副。
        self.assertEqual(
            sum(l["registered"] for l in glove_lines)
            - sum(l["subdivided"] for l in glove_lines),
            100,
        )
        self.assertEqual(
            sum(l["available"] + l["issued"] + l["quarantined"]
                + l["consumed"] + l["written_off"] + l["retained"]
                + l["shipped_back"] + l["destroyed"] + l["subdivided"]
                for l in glove_lines),
            130,
        )

    def test_reconcile_report_has_no_patient_data(self):
        svc = self.svc
        svc.verify_seal(container_id="BOX-1", seal_id="SEAL-A001", intact=True,
                        actor="logistics", occurred_at=ts(1))
        svc.open_box(container_id="BOX-1", seal_id="SEAL-A001", room="诊室A",
                     actor="nurse-li", occurred_at=ts(2))
        svc.issue(items=[{"item_id": "ITM-GAUZE", "qty": 10}],
                  recipient="dr-wang", role="doctor", room="诊室A",
                  actor="nurse-li", occurred_at=ts(3))
        svc.record_use(item_id="ITM-GAUZE", qty=10, actor="dr-wang",
                       encounter_ref="ENC-77QQ", occurred_at=ts(4))
        import json
        report = json.dumps(svc.inventory.reconcile(), ensure_ascii=False)
        self.assertNotIn("ENC-77QQ", report)
        self.assertNotIn("patient", report.lower())

    def test_encounter_token_format(self):
        with self.assertRaises(PrivacyViolation):
            self.svc.record_use(item_id="ITM-GAUZE", qty=1, actor="a",
                                encounter_ref="Ali Hassan",
                                occurred_at=ts(5))

    def test_over_issue_rejected(self):
        svc = self.svc
        svc.verify_seal(container_id="BOX-1", seal_id="SEAL-A001", intact=True,
                        actor="logistics", occurred_at=ts(1))
        svc.open_box(container_id="BOX-1", seal_id="SEAL-A001", room="诊室A",
                     actor="nurse-li", occurred_at=ts(2))
        with self.assertRaises(ConservationViolation):
            svc.issue(items=[{"item_id": "ITM-GLOVE", "qty": 101}],
                      recipient="x", role=None, room="诊室A",
                      actor="nurse-li", occurred_at=ts(3))

    def test_offline_issue_backfill_no_extra_stock(self):
        svc = self.svc
        svc.verify_seal(container_id="BOX-1", seal_id="SEAL-A001", intact=True,
                        actor="logistics", occurred_at=ts(1))
        svc.open_box(container_id="BOX-1", seal_id="SEAL-A001", room="诊室A",
                     actor="nurse-li", occurred_at=ts(2))
        kw = dict(items=[{"item_id": "ITM-GAUZE", "qty": 10}],
                  recipient="dr-wang", role="doctor", room="诊室A",
                  actor="scanner-3")
        svc.issue(**kw, occurred_at=ts(3), client_event_id="ISSUE-0100")
        # 同一条离线扫码补传第二次到达，不能再扣库存。
        with self.assertRaises(DuplicateSubmission):
            svc.issue(**kw, occurred_at=ts(90), client_event_id="ISSUE-0100")
        self.assertEqual(svc.inventory.items["ITM-GAUZE"].issued, 10)
        self.assertEqual(svc.inventory.items["ITM-GAUZE"].available, 40)

    def test_rejected_event_not_persisted(self):
        svc = self.svc
        before = len(svc.store.events)
        with self.assertRaises(SealedContainerError):
            # 未核验封签即拆箱，应被拒绝。
            svc.open_box(container_id="BOX-1", seal_id="SEAL-A001", room="诊室A",
                         actor="x", occurred_at=ts(2))
        self.assertEqual(len(svc.store.events), before)

    def test_broken_seal_quarantines_only_that_box(self):
        svc = self.svc
        svc.verify_seal(container_id="BOX-1", seal_id="SEAL-A001", intact=False,
                        actor="logistics", occurred_at=ts(1), note="封签断裂")
        # BOX-1 内物品与设备全部隔离。
        self.assertEqual(svc.inventory.items["ITM-GLOVE"].quarantined, 100)
        self.assertEqual(svc.inventory.devices["DEV-SCAN-01"].state, "quarantined")
        # BOX-2 与 BOX-1 同在机场库房，却完全不受影响。
        self.assertEqual(svc.inventory.items["ITM-PROBE"].quarantined, 0)
        self.assertTrue(svc.inventory.reconcile()["balanced"])
        # 异常箱禁止常规拆箱。
        with self.assertRaises(SealedContainerError):
            svc.open_box(container_id="BOX-1", seal_id="SEAL-A001", room="诊室A",
                         actor="x", occurred_at=ts(2))

    def test_wrong_seal_id_rejected(self):
        svc = self.svc
        with self.assertRaises(SealedContainerError):
            svc.verify_seal(container_id="BOX-1", seal_id="SEAL-FAKE",
                            intact=True, actor="logistics", occurred_at=ts(1))

    def test_temperature_breach_isolates_cold_chain_only(self):
        svc = self.svc
        svc.verify_seal(container_id="BOX-1", seal_id="SEAL-A001", intact=True,
                        actor="logistics", occurred_at=ts(1))
        svc.open_box(container_id="BOX-1", seal_id="SEAL-A001", room="诊室A",
                     actor="nurse-li", occurred_at=ts(2))
        svc.env_reading(location="诊室A", temperature_c=31.5,
                        limits={"temp_min": 2, "temp_max": 25},
                        actor="sensor-2", occurred_at=ts(3))
        # 冷链手套隔离，普通纱布不动，设备在场也被隔离待检。
        self.assertEqual(svc.inventory.items["ITM-GLOVE"].quarantined, 100)
        self.assertEqual(svc.inventory.items["ITM-GAUZE"].quarantined, 0)
        self.assertEqual(svc.inventory.devices["DEV-SCAN-01"].state, "quarantined")

    def test_release_after_inspection_restores_stock(self):
        svc = self.svc
        svc.verify_seal(container_id="BOX-1", seal_id="SEAL-A001", intact=False,
                        actor="logistics", occurred_at=ts(1), note="疑似开封")
        svc.quarantine(item_id="ITM-PROBE", reason="同托盘待查", actor="qa",
                       occurred_at=ts(2))
        # 复查 BOX-2 物品无虞，单独解除；BOX-1 仍隔离，诊疗不受牵连。
        svc.release_hold(item_id="ITM-PROBE", actor="qa", occurred_at=ts(3))
        self.assertEqual(svc.inventory.items["ITM-PROBE"].available, 20)
        self.assertEqual(svc.inventory.items["ITM-GLOVE"].quarantined, 100)
        self.assertTrue(svc.inventory.reconcile()["balanced"])

    def test_transport_notice_moves_but_never_creates_stock(self):
        svc = self.svc
        before = svc.inventory.reconcile()
        svc.transport_notice(
            kind="leg_arrived",
            fingerprint_parts=["leg-2", "BOX-1", "arrived"],
            payload={"location": "DXB-transit", "container_ids": ["BOX-1"]},
            actor="gateway-a", occurred_at=ts(20),
        )
        with self.assertRaises(DuplicateSubmission):
            svc.transport_notice(
                kind="leg_arrived",
                fingerprint_parts=["leg-2", "BOX-1", "arrived"],
                payload={"location": "DXB-transit", "container_ids": ["BOX-1"]},
                actor="gateway-b", occurred_at="2026-09-20T04:00:00+04:00",
            )
        after = svc.inventory.reconcile()
        self.assertEqual(before["balanced"], after["balanced"])
        self.assertEqual(svc.inventory.boxes["BOX-1"].location, "DXB-transit")
        for line in after["items"]:
            b = next(x for x in before["items"] if x["item_id"] == line["item_id"])
            self.assertEqual(b["registered"], line["registered"])

    def test_replay_from_events_matches_state(self):
        svc = self.svc
        svc.verify_seal(container_id="BOX-1", seal_id="SEAL-A001", intact=True,
                        actor="logistics", occurred_at=ts(1))
        svc.open_box(container_id="BOX-1", seal_id="SEAL-A001", room="诊室A",
                     actor="nurse-li", occurred_at=ts(2))
        svc.issue(items=[{"item_id": "ITM-GAUZE", "qty": 7}],
                  recipient="dr-wang", role="doctor", room="诊室A",
                  actor="nurse-li", occurred_at=ts(3))
        rebuilt = MissionService.rebuild(svc.store).inventory
        self.assertEqual(rebuilt.items["ITM-GAUZE"].issued, 7)
        self.assertEqual(rebuilt.boxes["BOX-1"].status, "open")


if __name__ == "__main__":
    unittest.main()
