import unittest

from _helpers import build_service, register_standard_box, register_second_box, ts
from mission_kit import (
    ConservationViolation,
    HandoffIncomplete,
)


class HandoverAndDispositionTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        register_standard_box(self.svc)
        register_second_box(self.svc)
        for cid, seal in (("BOX-1", "SEAL-A001"), ("BOX-2", "SEAL-B002")):
            self.svc.verify_seal(container_id=cid, seal_id=seal, intact=True,
                                 actor="logistics", occurred_at=ts(1))
        self.svc.open_box(container_id="BOX-1", seal_id="SEAL-A001", room="诊室A",
                          actor="nurse-li", occurred_at=ts(2))

    def _complete_handover(self, minute=10):
        s = self.svc
        s.record_handover_docs(
            serial="DEV-SCAN-01",
            documents={"zh": "DOC-ZH-22", "en": "DOC-EN-22"},
            actor="mission-lead", occurred_at=ts(minute),
        )
        s.complete_training(
            serial="DEV-SCAN-01", trainer="biomed-zhou",
            trainees=["dr-ahmed", "nurse-fatima"], session_ref="TRN-07",
            actor="biomed-zhou", occurred_at=ts(minute + 1),
        )
        s.accept_responsibility(
            serial="DEV-SCAN-01", owner_name="Dr. Ahmed",
            owner_org="Local Eye Hospital", signoff_ref="SIGN-44",
            actor="mission-lead", occurred_at=ts(minute + 2),
        )

    def test_retain_requires_bilingual_training_and_signoff(self):
        with self.assertRaises(HandoffIncomplete) as ctx:
            self.svc.retain_asset(serial="DEV-SCAN-01", location="当地医院设备科",
                                  actor="mission-lead", occurred_at=ts(9))
        self.assertEqual(
            set(ctx.exception.missing),
            {"中英双语交接文档", "培训验收记录", "接管责任确认"},
        )

    def test_chinese_only_docs_still_incomplete(self):
        self.svc.record_handover_docs(
            serial="DEV-SCAN-01", documents={"zh": "DOC-ZH-22"},
            actor="mission-lead", occurred_at=ts(10),
        )
        with self.assertRaises(HandoffIncomplete) as ctx:
            self.svc.retain_asset(serial="DEV-SCAN-01", location="设备科",
                                  actor="mission-lead", occurred_at=ts(12))
        self.assertIn("中英双语交接文档", ctx.exception.missing)

    def test_full_retention_identifies_custodian(self):
        self._complete_handover()
        self.svc.retain_asset(serial="DEV-SCAN-01", location="当地医院设备科",
                              actor="mission-lead", occurred_at=ts(13))
        d = self.svc.inventory.devices["DEV-SCAN-01"]
        self.assertEqual(d.state, "retained")
        self.assertEqual(d.holder, "Dr. Ahmed")
        info = self.svc.inventory.custodian("DEV-SCAN-01")
        self.assertEqual(info["responsibility"]["signoff_ref"], "SIGN-44")
        where = self.svc.inventory.where_is("DEV-SCAN-01")
        self.assertEqual(where["location"], "当地医院设备科")

    def test_failed_retention_does_not_change_ownership(self):
        with self.assertRaises(HandoffIncomplete):
            self.svc.retain_asset(serial="DEV-SCAN-01", location="设备科",
                                  actor="mission-lead", occurred_at=ts(9))
        d = self.svc.inventory.devices["DEV-SCAN-01"]
        self.assertEqual(d.state, "available")
        self.assertIsNone(d.holder)

    def test_return_shipment_requires_waybill_and_customs(self):
        with self.assertRaises(ConservationViolation):
            self.svc.ship_back(waybill="", customs_declaration="CUS-OUT-1",
                               items=[{"item_id": "ITM-PROBE", "qty": 20}],
                               device_serials=[], actor="logistics",
                               occurred_at=ts(20))
        self.svc.ship_back(
            waybill="AWB-5566", customs_declaration="CUS-OUT-1",
            items=[{"item_id": "ITM-PROBE", "qty": 20}],
            device_serials=[], actor="logistics", occurred_at=ts(21),
        )
        item = self.svc.inventory.items["ITM-PROBE"]
        self.assertEqual(item.shipped_back, 20)
        self.assertEqual(item.on_hand, 0)
        self.assertTrue(self.svc.inventory.reconcile()["balanced"])

    def test_destroy_requires_certificate(self):
        with self.assertRaises(ConservationViolation):
            self.svc.destroy(items=[{"item_id": "ITM-GAUZE", "qty": 4}],
                             device_serials=[], certificate_ref="", witness="x",
                             actor="logistics", occurred_at=ts(22))
        self.svc.destroy(items=[{"item_id": "ITM-GAUZE", "qty": 4}],
                         device_serials=[], certificate_ref="DES-09",
                         witness="hospital-officer", actor="logistics",
                         occurred_at=ts(23))
        self.assertEqual(self.svc.inventory.items["ITM-GAUZE"].destroyed, 4)

    def test_where_is_usable_custodian_queries(self):
        inv = self.svc.inventory
        # 效期查询：过期物品不可用。
        status = inv.usable("ITM-GLOVE", as_of="2029-01-01")
        self.assertFalse(status["usable"])
        self.assertTrue(any("过期" in r for r in status["reasons"]))
        # 正常状态下可用。
        self.assertTrue(inv.usable("ITM-GLOVE", as_of="2026-09-20")["usable"])
        # 领用后持有人可查。
        self.svc.issue(items=[{"item_id": "ITM-GAUZE", "qty": 3}],
                       recipient="nurse-fatima", role="nurse", room="诊室B",
                       actor="nurse-li", occurred_at=ts(5))
        where = inv.where_is("ITM-GAUZE")
        self.assertEqual(where["holder"], "nurse-fatima")
        self.assertEqual(where["location"], "诊室B")
        self.assertEqual(inv.custodian("ITM-GAUZE")["holder_role"], "nurse")


if __name__ == "__main__":
    unittest.main()
